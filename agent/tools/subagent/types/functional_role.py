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
