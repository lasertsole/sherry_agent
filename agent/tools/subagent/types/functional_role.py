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
    """

    GENERAL = "general"
    RESEARCHER = "researcher"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
