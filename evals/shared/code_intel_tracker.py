"""Code-intel usage tracking for eval reports (the plan's §7.4).

The tool taxonomy (``CODE_INTEL_TOOLS`` / precise / greedy sets) and the
degraded-call detector now come from the product analyzer — one source of
truth. What stays here is the eval-only data shape: the harness feeds per-name
counters via ``record_names``/``task_id`` (the product tracker reads the
transcript instead), and the eval report pins the ``code_intel_usage.json``
keys (``tool_calls``, ``degraded_calls`` as a count, ``code_intel_available``).
"""

from __future__ import annotations

from typing import Any

from pub.func.session_report.code_intel_tracker import (
    CODE_INTEL_TOOLS,
    _GREEDY_TOOLS,
    _PRECISE_TOOLS,
    _is_degraded,
)

__all__ = ["CODE_INTEL_TOOLS", "CodeIntelTracker", "analyze_code_intel"]


class CodeIntelTracker:
    """One eval task's code-intel usage (harness counters + transcript)."""

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
        """The eval report's code_intel_usage.json shape."""
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
