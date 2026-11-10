"""Spawn request validation, role resolution, and the child's tool policy.

Phases 1–2 (input + admission gate), the functional-role resolution and its
capability containment, and phases 8–8.6 (the child's ``(allow, deny)`` tool
policy) live here so the spawn orchestration in :mod:`spawn.core` reads as a
pipeline of named phases.
"""

import re
from typing import Any, Literal

from loguru import logger

from ..config import get_config
from ..types.capability import SubagentSessionRole
from ..types.functional_role import FunctionalRole
from ..roles import RoleDefinition
from ..registry.read import count_active_runs_readonly
from ..registry.spawn_budget import total_spawns
from ..capabilities import can_spawn_children
from .depth import (
    get_subagent_depth,
    validate_spawn_depth,
    validate_concurrent_children,
    validate_total_spawns,
)
from .target_policy import validate_target_policy
from .inherited_tool_policy import DEFAULT_SUBAGENT_BLOCKED_TOOLS

# Only alphanumeric, underscore and hyphen allowed — prevents path/injection attacks via agent_id
_VALID_AGENT_ID = re.compile(r"^[a-zA-Z0-9_-]+$")

# Tools that only a spawning node (MAIN / ORCHESTRATOR) may hold. A functional
# role whitelist or a per-spawn extra_tools entry can name them, but the spawn
# pipeline intersects the final allow-list with can_spawn_children(role) so a
# LEAF can never be granted recursive spawning or control over children.
# The prompt's spawn section names exactly this set as usable and calls the
# main-only management tools (sessions_kill / sessions_steer) out as unavailable,
# so a child is never told to call a tool it does not have.
_SPAWN_CONTROL_TOOLS = (
    "sessions_spawn",
    "sessions_yield",
    "subagents_list",
    "sessions_send",
)


def _resolve_functional_role(
    hint: str | None,
    agent_id: str,
) -> FunctionalRole:
    """Resolve the functional role from an explicit hint or an agent_id name match.

    Priority: explicit hint → agent_id name match → GENERAL default.
    Unknown hints and non-role agent_ids fall back to GENERAL, which leaves the
    pre-migration behavior untouched.
    """
    if hint:
        try:
            return FunctionalRole(hint)
        except ValueError:
            logger.warning("Unknown functional_role hint '{}', falling back to GENERAL", hint)
    try:
        return FunctionalRole(agent_id)
    except ValueError:
        pass
    try:
        return FunctionalRole(get_config().default_functional_role)
    except ValueError:
        return FunctionalRole.GENERAL


def _check_role_containment(
    requester_session_key: str,
    child_functional_role: FunctionalRole,
) -> str | None:
    """Reject escalation-capable spawning from a specialized parent.

    MAIN (no run record) and general (``tools: inherit`` = every tool) impose no
    restriction: every specialized role's whitelist is a subset of theirs, so no
    escalation is possible. A specialized parent may spawn its OWN role plus any
    roles listed in ``SPAWN_ALLOWED_CHILD_ROLES`` — an executor may therefore
    spawn read-only researcher / librarian children before it writes, and the
    two research roles may spawn each other (or their own role). The map only
    ever permits READ-ONLY extras, so no write-capable escalation path opens,
    and a role ABSENT from the map keeps the legacy same-role-only rule (a
    reviewer still cannot hire a researcher).

    Returns the refusal text, or ``None`` when the spawn is allowed.
    """
    from ..registry import get_run_by_child_session_key
    from ..types.functional_role import SPAWN_ALLOWED_CHILD_ROLES

    parent_run = get_run_by_child_session_key(requester_session_key)
    if parent_run is None:
        return None  # MAIN — the orchestrating root is not contained
    parent_role = getattr(parent_run, "functional_role", FunctionalRole.GENERAL)
    if parent_role == FunctionalRole.GENERAL:
        return None  # inherit mode: every specialized whitelist is a subset
    allowed = SPAWN_ALLOWED_CHILD_ROLES.get(parent_role, frozenset({parent_role}))
    if child_functional_role in allowed:
        return None
    allowed_names = " / ".join(role.value for role in sorted(allowed, key=str))
    return (
        f"Capability containment: a {parent_role} subagent can only spawn "
        f"these roles: {allowed_names} (requested {child_functional_role}). "
        'Pass functional_role="<one of the allowed roles>"; use a general '
        "subagent or the main agent to spawn a different role."
    )


class SpawnResult:
    """Outcome of a spawn request — accepted, forbidden, or error.

    Attributes:
        status: "accepted" | "forbidden" | "error"
        child_session_key: Session key of the spawned child (set on accepted)
        run_id: Registry run identifier (set on accepted)
        error: Human-readable error message (set on forbidden/error)
        task_name: Normalised task name
        note: Optional human-readable note (e.g. accepted confirmation text)
    """

    def __init__(
        self,
        status: Literal["accepted", "forbidden", "error"],
        child_session_key: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
        task_name: str | None = None,
        note: str | None = None,
    ):
        self.status = status
        self.child_session_key = child_session_key
        self.run_id = run_id
        self.error = error
        self.task_name = task_name
        self.note = note

    def to_dict(self) -> dict:
        """Serialize the result to a plain dict for API responses."""
        return {
            "status": self.status,
            "child_session_key": self.child_session_key,
            "run_id": self.run_id,
            "error": self.error,
            "task_name": self.task_name,
            "note": self.note,
        }


def _validate_spawn_request(
    task: str, agent_id: str, requester_session_key: str, config: Any
) -> tuple[int, SpawnResult | None]:
    """Phases 1–2: input validation and the depth/concurrency admission gate.

    Returns ``(child_depth, refusal)``: a non-None refusal is what the caller
    hands back. Global concurrency is NOT rejected here — the SUBAGENT lane
    queues over-limit runs (PENDING) instead of refusing them.
    """
    if not task or not task.strip():
        return 0, SpawnResult(status="error", error="Task description is required")

    # Reject agent_id values that could enable path traversal or command injection
    if not _VALID_AGENT_ID.match(agent_id):
        return 0, SpawnResult(
            status="forbidden", error=f"Invalid agent_id: '{agent_id}' (only [a-zA-Z0-9_-] allowed)"
        )

    # When the platform requires explicit agent identification, reject the default "main"
    if config.require_agent_id and agent_id == "main":
        return 0, SpawnResult(
            status="forbidden", error="agent_id is required (cannot use default 'main')"
        )

    allowed, reason = validate_target_policy(agent_id, "main")
    if not allowed:
        return 0, SpawnResult(status="forbidden", error=reason)

    parent_depth = get_subagent_depth(requester_session_key)
    child_depth = parent_depth + 1

    allowed, reason = validate_spawn_depth(parent_depth)
    if not allowed:
        return child_depth, SpawnResult(status="forbidden", error=reason)

    active_count = count_active_runs_readonly(requester_session_key)
    allowed, reason = validate_concurrent_children(active_count)
    if not allowed:
        return child_depth, SpawnResult(status="forbidden", error=reason)

    # Lifetime budget: the concurrent cap bounds children running AT A TIME,
    # this bounds "finish one, start the next" repeating without end.
    allowed, reason = validate_total_spawns(total_spawns(requester_session_key))
    if not allowed:
        return child_depth, SpawnResult(status="forbidden", error=reason)
    return child_depth, None


def _resolve_child_tool_policy(
    role_def: RoleDefinition | None,
    role: SubagentSessionRole,
    extra_tools: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Phases 8–8.6: the child's ``(allow, deny)`` tool policy.

    A functional role with an explicit tool list is a WHITELIST (deny cleared);
    the depth role still controls the spawn/management tools, per-spawn
    ``extra_tools`` join the whitelist, and the final allow-list is intersected
    with ``can_spawn_children(role)`` so a LEAF can never be granted recursion.
    """
    tool_allow: list[str] = []
    tool_deny = list(DEFAULT_SUBAGENT_BLOCKED_TOOLS)

    # Functional role drives tool policy: an explicit tool list becomes a
    # whitelist (deny cleared, since the whitelist already restricts).
    if role_def is not None and role_def.tools is not None:
        tool_allow = list(role_def.tools)
        tool_deny = []

    # Depth role still controls the spawn/management tool set.
    if role == SubagentSessionRole.ORCHESTRATOR:
        if tool_allow:
            # Whitelist mode: deny is already empty, so grant the control tools
            # by adding them to the allow-list instead of removing from deny.
            for t in _SPAWN_CONTROL_TOOLS:
                if t not in tool_allow:
                    tool_allow.append(t)
        else:
            # Inherit mode: unblock the control tools by removing them from the
            # deny list.
            tool_deny = [t for t in tool_deny if t not in _SPAWN_CONTROL_TOOLS]

    # Per-task tool attachment (deepagents TaskTools pattern): extra_tools are
    # attached on top of whatever the role already allows. In whitelist mode
    # they join the allow-list; in inherit mode the base tool set already
    # contains them and only the deny-list applies.
    extra_tool_names: list[str] = list(dict.fromkeys(extra_tools or []))
    for name in extra_tool_names:
        if tool_allow and name not in tool_allow:
            tool_allow.append(name)

    if not can_spawn_children(role):
        tool_allow = [t for t in tool_allow if t not in _SPAWN_CONTROL_TOOLS]
    return tool_allow, tool_deny
