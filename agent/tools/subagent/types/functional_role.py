"""Functional role enum for sub-agents — orthogonal to depth-based SubagentSessionRole.

Functional roles drive LLM selection, tool policy, and system prompt content.
Depth roles (MAIN/ORCHESTRATOR/LEAF) drive spawn permissions and scopes.
"""

from enum import StrEnum


class FunctionalRole(StrEnum):
    """Functional specialization of a sub-agent worker.

    GENERAL: full tool access, can modify files (default fallback).
    RESEARCHER: read-only, codebase/web search, cheaper model.
    EXECUTOR: write-capable, code execution, no subagent spawn.
    REVIEWER: read-only review worker for diff audit.
    LIBRARIAN: read-only external codebase retrieval (clone/index/search).
    """

    GENERAL = "general"
    RESEARCHER = "researcher"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    LIBRARIAN = "librarian"


#: Roles that get the tree-sitter code-intel suite and the eight LSP tools.
#: Both spawn/core.py injection and spawn/system_prompt.py guidance read this
#: single set, so the tool face and its prompt section cannot drift apart.
CODE_INTEL_ROLES: frozenset[FunctionalRole] = frozenset(
    {FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN}
)

#: Roles that get the Programmatic Tool Calling ``execute_code`` tool. EXECUTOR
#: only — it is the sole write-capable code-execution role. Both the tool
#: injection (spawn/core.py) and the prompt guidance (spawn/system_prompt.py)
#: read this single set, so the tool face and its prompt section cannot drift.
PTC_ROLES: frozenset[FunctionalRole] = frozenset({FunctionalRole.EXECUTOR})

#: The roles a specialized parent may spawn, per parent role.
#:
#: An ABSENT key means "same role only" — the legacy rule, unchanged for
#: researcher / reviewer / librarian. MAIN (no run record) and GENERAL
#: (``tools: inherit`` = every tool) never read this map: they short-circuit
#: in ``spawn/core.py::_check_role_containment`` because every specialized
#: whitelist is already a subset of theirs.
#:
#: Two invariants hold for every entry, both pinned by tests:
#:
#: 1. the value CONTAINS the parent role (same-role fan-out stays explicit);
#: 2. every permitted non-parent child is a READ-ONLY role — never a
#:    write-capable one. That single-hop rule is what makes multi-hop safe:
#:    a write role can only ever reach write roles, and a read-only role can
#:    only spawn its own kind, so no path returns to a write capability.
#:    It is also why adding a ``RESEARCHER: {…, EXECUTOR}`` entry (a cycle)
#:    fails the invariant instead of opening an escalation path.
#:
#: The one deliberate deviation from "child tools ⊆ parent tools": an
#: executor's researcher/librarian children DO carry read-only extras the
#: executor lacks (``search_files``, ``web_search``, code-intel, LSP). That
#: is the point — read-only research before writing — and containment exists
#: to stop WRITE escalation, not read-only information access.
SPAWN_ALLOWED_CHILD_ROLES: dict[FunctionalRole, frozenset[FunctionalRole]] = {
    FunctionalRole.EXECUTOR: frozenset(
        {
            FunctionalRole.EXECUTOR,  # same-role fan-out (explicit)
            FunctionalRole.RESEARCHER,  # read-only codebase/web research before writing
            FunctionalRole.LIBRARIAN,  # read-only external-codebase lookup
        }
    ),
}
