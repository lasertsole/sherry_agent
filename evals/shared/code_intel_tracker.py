"""Code-intel usage tracking (the plan's §7.4).

The code-intel tools (tree-sitter explore/callers/callees/impact/path, semantic
search, the lsp_* family, ast-grep) are subagent-only in production; a benchmark
run that spawns CODE_INTEL_ROLES children on a host with the binaries may use
them, and the plan wants that usage counted. The tracker consumes what the
harness already captures — per-task ``tool_names`` counters plus the session
transcript for DEGRADED calls (a code-intel call whose ToolMessage carries an
error mentions the missing binary / model) — and renders the plan's
``code_intel_usage.json`` shape.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["CODE_INTEL_TOOLS", "CodeIntelTracker", "analyze_code_intel"]

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
#: The tool calls the precise-retrieval side of the ratio counts.
_PRECISE_TOOLS = _LSP_TOOLS | {"explore", "callers", "callees", "semantic_code_search"}
#: The brute-force side it is compared against.
_GREEDY_TOOLS = frozenset({"search_files", "read_file"})

_DEGRADED_PATTERNS = (
    re.compile(r"not installed|not found|unavailable|missing", re.IGNORECASE),
    re.compile(r"lsp|language server", re.IGNORECASE),
    re.compile(r"semantic|model", re.IGNORECASE),
)


def _is_degraded(tool: str, result_text: str) -> tuple[bool, str]:
    """A code-intel call that came back with an error naming the missing
    dependency. The tool result of a degraded call says WHY (the tool layer's
    fail-open text), so a bounded prefix check is enough."""
    head = (result_text or "")[:300].lower()
    if "error" not in head and "unavailable" not in head and "not" not in head:
        return False, ""
    for pattern in _DEGRADED_PATTERNS:
        match = pattern.search(head)
        if match:
            return True, match.group(0)
    return False, ""


class CodeIntelTracker:
    """One task's code-intel usage."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.tool_calls: dict[str, int] = {}
        self.degraded_reasons: list[str] = []
        self.greedy_calls = 0

    def record_names(self, tool_names: dict[str, int]) -> None:
        """Fold the harness's per-name call counters in."""
        for name, count in (tool_names or {}).items():
            if name in CODE_INTEL_TOOLS:
                self.tool_calls[name] = self.tool_calls.get(name, 0) + int(count)
            elif name in _GREEDY_TOOLS:
                self.greedy_calls += int(count)

    def record_transcript(self, messages: list[Any]) -> None:
        """Detect degraded calls (fail-open misses) from the transcript."""
        from langchain_core.messages import ToolMessage  # noqa: PLC0415 — lazy

        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            name = str(getattr(message, "name", "") or "")
            if name not in CODE_INTEL_TOOLS:
                continue
            content = message.content if isinstance(message.content, str) else str(message.content)
            degraded, reason = _is_degraded(name, content)
            if degraded:
                self.degraded_reasons.append(f"{name}: {reason}")

    def payload(self) -> dict[str, Any]:
        """The plan's code_intel_usage.json shape."""
        total = sum(self.tool_calls.values())
        precise = sum(count for name, count in self.tool_calls.items() if name in _PRECISE_TOOLS)
        ratio = (
            round(precise / (precise + self.greedy_calls), 4)
            if (precise + self.greedy_calls)
            else None
        )
        return {
            "task_id": self.task_id,
            "code_intel_available": bool(self.tool_calls) or None,
            "tool_calls": dict(sorted(self.tool_calls.items(), key=lambda item: -item[1])),
            "total_code_intel_calls": total,
            "degraded_calls": len(self.degraded_reasons),
            "degraded_reasons": self.degraded_reasons[:10],
            "lsp_vs_grep_ratio": ratio,
        }


def analyze_code_intel(
    task_id: str, tool_names: dict[str, int], messages: list[Any]
) -> dict[str, Any]:
    """One-call helper: counters + transcript -> the usage payload."""
    tracker = CodeIntelTracker(task_id)
    tracker.record_names(tool_names)
    tracker.record_transcript(messages)
    payload = tracker.payload()
    payload["code_intel_available"] = bool(payload["total_code_intel_calls"])
    return payload
