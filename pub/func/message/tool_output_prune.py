"""Tool-output pruning with one-line summaries (P0-4).

When `prune_tool_outputs` clears an oversized historical tool result it
replaces the content with a tool-specific one-line summary instead of the
opaque `_PRUNE_MARKER`, so the model keeps a trace of what ran and how large
the output was.
"""

import re
from collections.abc import Callable

from config.features import SUMMARIZATION
from langchain_core.messages import (
    BaseMessage,
    ToolMessage,
    AIMessage,
)

PRUNE_PROTECT_TOKENS = SUMMARIZATION["prune_protect_tokens"]
PRUNE_MIN_REDUCTION_TOKENS = SUMMARIZATION["prune_min_reduction_tokens"]

_PRUNE_MARKER = "[Old tool result content cleared]"
_SUMMARY_LC_SOURCE = "summarization"
_SUMMARY_MAX_CHARS = 200


# === Tool-output summary templates (P0-4) ===


def _extract_exit_code(result_text: str) -> str:
    """Extract the exit code from common shell-output formats, else 'unknown'."""
    match = re.search(r"[Ee]xit\s*[Cc]ode[:\s]+(\d+)", result_text)
    if match:
        return match.group(1)
    return "unknown"


def _count_lines(text: str) -> int:
    """Count non-empty lines (0 for blank or whitespace-only text)."""
    return text.count("\n") + 1 if text.strip() else 0


_TOOL_SUMMARY_TEMPLATES: dict[str, Callable[[str], str]] = {
    # File operations
    "read": lambda r: f"[read] read file, {len(r)} chars, {_count_lines(r)} lines",
    "read_file": lambda r: f"[read_file] read file, {len(r)} chars, {_count_lines(r)} lines",
    "write": lambda r: f"[write] wrote file, {len(r)} chars",
    "write_file": lambda r: f"[write_file] wrote file, {len(r)} chars",
    "edit": lambda r: f"[edit] edited file, {len(r)} chars",
    "edit_file": lambda r: f"[edit_file] edited file, {len(r)} chars",
    # Search
    "grep": lambda r: f"[grep] search done, {_count_lines(r)} matches",
    "glob": lambda r: f"[glob] matched {len(r.strip().splitlines())} files",
    # Code execution
    "bash": lambda r: f"[bash] exit_code={_extract_exit_code(r)}, output {len(r)} chars",
    "shell": lambda r: f"[shell] exit_code={_extract_exit_code(r)}, output {len(r)} chars",
    # Task flow
    "taskflow_summary": lambda r: f"[taskflow_summary] {len(r)} chars",
    "taskflow_run_task": lambda r: f"[taskflow_run_task] dispatched, {len(r)} chars",
    "taskflow_resume": lambda r: f"[taskflow_resume] injected result, {len(r)} chars",
    # Memory
    "memory": lambda r: f"[memory] {r[:80]}...",
}

_DEFAULT_TEMPLATE: Callable[[str], str] = (  # noqa: E731 -- dispatch-table default callable
    lambda r: f"[tool] output {len(r)} chars, first 100: {r[:100]}..."
)


def _summarize_tool_result(tool_name: str, result_text: str) -> str:
    """Return a one-line summary of a tool result for the pruned replacement.

    Dispatches on tool name (unknown tools use `_DEFAULT_TEMPLATE`), caps the
    result at `_SUMMARY_MAX_CHARS`, and degrades to a generic length-only line
    if a template raises.
    """
    template = _TOOL_SUMMARY_TEMPLATES.get(tool_name, _DEFAULT_TEMPLATE)
    try:
        summary = template(result_text)
        if len(summary) > _SUMMARY_MAX_CHARS:
            summary = summary[: _SUMMARY_MAX_CHARS - 3] + "..."
        return summary
    except Exception:
        return f"[{tool_name}] output {len(result_text)} chars"


def _is_summary_message(msg: BaseMessage) -> bool:
    return getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE


def _find_tool_name(messages: list[BaseMessage], target_idx: int, tc_id: str) -> str:
    if not tc_id:
        return ""
    for i in range(target_idx - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("id") == tc_id:
                    return tc.get("name", "")
    return ""


def prune_tool_outputs(
    messages: list[BaseMessage],
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    min_reduction_tokens: int = PRUNE_MIN_REDUCTION_TOKENS,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()
    if estimator is None:

        def _default_estimator(msgs):
            return sum(len(str(getattr(m, "content", ""))) // 4 for m in msgs)

        estimator = _default_estimator

    total_tool_tokens = 0
    pruned_tokens = 0
    to_prune: list[tuple[int, str]] = []

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if _is_summary_message(msg):
            break
        if not isinstance(msg, ToolMessage):
            continue

        tc_id = getattr(msg, "tool_call_id", "")
        tool_name = _find_tool_name(messages, i, tc_id)
        if tool_name in protected:
            continue
        if getattr(msg, "status", "") == "compacted":
            continue

        content_len = len(str(getattr(msg, "content", "")))
        token_est = content_len // 4
        total_tool_tokens += token_est

        if total_tool_tokens <= protect_tokens:
            continue

        to_prune.append((i, tool_name))
        pruned_tokens += token_est

    if pruned_tokens < min_reduction_tokens or not to_prune:
        return messages, 0

    result = list(messages)
    for idx, tool_name in to_prune:
        msg = result[idx]
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        try:
            summary = _summarize_tool_result(tool_name, content)
        except Exception:
            # `_summarize_tool_result` absorbs template failures itself; this
            # guard is the last-resort fallback if summarization dies outright.
            summary = _PRUNE_MARKER
        result[idx] = msg.model_copy(
            update={
                "content": summary,
                "additional_kwargs": {
                    **getattr(msg, "additional_kwargs", {}),
                    "status": "compacted",
                    "original_length": len(content),
                },
            }
        )

    return result, pruned_tokens
