"""Code-intel usage tracking: precise LSP/explore retrieval vs brute-force grep/read.

Consumes the session transcript: a per-tool call-name count plus DEGRADED
detection (a code-intel ToolMessage carrying an error that names a missing
binary/model). Renders the report's ``code_intel`` shape.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CODE_INTEL_TOOLS",
    "CodeIntelTracker",
    "analyze_code_intel",
    # Shared taxonomy/consumer hooks used by the eval-side compatibility adapter.
    "_PRECISE_TOOLS",
    "_GREEDY_TOOLS",
    "_is_degraded",
]

#: Every tool name the code-intel layer registers (agent/tools/code_intel/).
CODE_INTEL_TOOLS: frozenset[str] = frozenset(
    {
        "explore",
        "callers",
        "callees",
        "impact",
        "path",
        "semantic_code_search",
        "lsp_goto_definition",
        "lsp_find_references",
        "lsp_workspace_symbol",
        "lsp_call_hierarchy",
        "lsp_hover",
        "lsp_rename",
        "lsp_diagnostics",
        "lsp_format",
        "ast_grep_search",
        "ast_grep_replace",
    }
)

_LSP_TOOLS = frozenset(name for name in CODE_INTEL_TOOLS if name.startswith("lsp_"))
_PRECISE_TOOLS = _LSP_TOOLS | {"explore", "callers", "callees", "semantic_code_search"}
_GREEDY_TOOLS = frozenset({"search_files", "read_file"})

_DEGRADED_PATTERNS = (
    re.compile(r"not installed|not found|unavailable|missing", re.IGNORECASE),
    re.compile(r"lsp|language server", re.IGNORECASE),
    re.compile(r"semantic|model", re.IGNORECASE),
)


def _is_degraded(tool: str, result_text: str) -> tuple[bool, str]:
    head = (result_text or "")[:300].lower()
    if "error" not in head and "unavailable" not in head and "not" not in head:
        return False, ""
    for pattern in _DEGRADED_PATTERNS:
        match = pattern.search(head)
        if match:
            return True, match.group(0)
    return False, ""


class CodeIntelTracker:
    """One session's code-intel usage."""

    def __init__(self) -> None:
        self.tool_calls: dict[str, int] = {}
        self.degraded_reasons: list[dict[str, str]] = []
        self.greedy_calls = 0

    def record_transcript(self, messages: list[Any]) -> None:
        """Fold tool-name counts in and detect degraded calls from ToolMessages."""
        from langchain_core.messages import ToolMessage  # noqa: PLC0415 — lazy

        for message in messages:
            if isinstance(message, ToolMessage):
                name = str(getattr(message, "name", "") or "")
                content = (
                    message.content if isinstance(message.content, str) else str(message.content)
                )
                if name in _GREEDY_TOOLS:
                    self.greedy_calls += 1
                if name in CODE_INTEL_TOOLS:
                    self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
                    degraded, reason = _is_degraded(name, content)
                    if degraded:
                        self.degraded_reasons.append({"tool": name, "reason": reason})
            else:
                for call in getattr(message, "tool_calls", None) or []:
                    name = str(call.get("name") or "")
                    if name in CODE_INTEL_TOOLS:
                        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
                    elif name in _GREEDY_TOOLS:
                        self.greedy_calls += 1

    def payload(self) -> dict[str, Any]:
        precise = sum(count for name, count in self.tool_calls.items() if name in _PRECISE_TOOLS)
        total = sum(self.tool_calls.values())
        ratio = (
            round(precise / (precise + self.greedy_calls), 4)
            if (precise + self.greedy_calls)
            else None
        )
        return {
            "precise_calls": precise,
            "greedy_calls": self.greedy_calls,
            "total_code_intel_calls": total,
            "lsp_vs_grep_ratio": ratio,
            "degraded_calls": self.degraded_reasons[:10],
            "available": bool(self.tool_calls),
        }


def analyze_code_intel(messages: list[Any]) -> dict[str, Any]:
    tracker = CodeIntelTracker()
    tracker.record_transcript(messages)
    return tracker.payload()
