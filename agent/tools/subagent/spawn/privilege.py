"""Call-time spawn-privilege guard for the sessions_spawn tool family.

Defense in depth behind the spawn pipeline's tool-policy intersection
(``spawn/core.py``): even when a ``sessions_spawn`` tool instance reaches a
non-spawning caller — a leaked or restored run record, a per-spawn extra tool —
the tool refuses the call instead of escalating. The check never raises; it
returns a verdict the tool renders through its existing string error contract.
"""

from ..capabilities import (
    can_spawn_children,
    is_subagent_session,
    resolve_subagent_capabilities,
)
from .depth import get_subagent_depth

SPAWN_YIELD_TOOLS = ("sessions_spawn", "sessions_yield")


def resolve_caller_session_key(session_id: str) -> str:
    """Return the canonical session key for a tool caller.

    Child/swarm session keys are already canonical and are used verbatim; any
    other id is a bare main-session id and gets the ``agent:main:session:``
    prefix. Blank input yields ``""``.
    """
    raw = (session_id or "").strip()
    if not raw:
        return ""
    if is_subagent_session(raw) or ":swarm:" in raw:
        return raw
    return f"agent:main:session:{raw}"


def check_spawn_permission(session_id: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a ``sessions_spawn`` call from *session_id*.

    The caller's depth role is resolved from its run record first, then from the
    session-key shape. Only MAIN and ORCHESTRATOR may spawn; LEAF callers are
    refused with a human-readable reason.
    """
    key = resolve_caller_session_key(session_id)
    if not key:
        return True, ""
    depth = get_subagent_depth(key)
    role, _ = resolve_subagent_capabilities(depth)
    if not can_spawn_children(role):
        return False, f"role '{role.value}' at depth {depth} cannot spawn subagents"
    return True, ""
