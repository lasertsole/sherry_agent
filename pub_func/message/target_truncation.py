from config.num import (
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    MAX_TOOL_OUTPUT_CHARS,
    MIN_OUTPUT_CHARS_TO_TRUNCATE,
)
from langchain_core.messages import BaseMessage, ToolMessage, AIMessage

_OMISSION_TEMPLATE = "...[truncated {omitted} chars]..."


def _truncate_content(
    content: str,
    max_chars: int,
    head_ratio: float = CONTENT_HEAD_RATIO,
    tail_ratio: float = CONTENT_TAIL_RATIO,
) -> str:
    if len(content) <= max_chars:
        return content
    head = content[: int(max_chars * head_ratio)]
    tail = content[-int(max_chars * tail_ratio):]
    omitted = len(content) - len(head) - len(tail)
    return f"{head}{_OMISSION_TEMPLATE.format(omitted=omitted)}{tail}"


def _find_tool_name(
    messages: list[BaseMessage], target_idx: int, tc_id: str
) -> str:
    if not tc_id:
        return ""
    for i in range(target_idx - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("id") == tc_id:
                    return tc.get("name", "")
    return ""


def target_truncate_tool_outputs(
    messages: list[BaseMessage],
    target_reduction_tokens: int,
    min_output_chars: int = MIN_OUTPUT_CHARS_TO_TRUNCATE,
    max_output_chars: int = MAX_TOOL_OUTPUT_CHARS,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()

    candidates: list[tuple[int, int]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        content = str(getattr(msg, "content", ""))
        if len(content) < min_output_chars:
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        tool_name = _find_tool_name(messages, i, tc_id)
        if tool_name in protected:
            continue
        candidates.append((i, len(content)))

    candidates.sort(key=lambda x: x[1], reverse=True)

    if not candidates:
        return messages, 0

    result = list(messages)
    total_reduced = 0

    for idx, old_len in candidates:
        if total_reduced >= target_reduction_tokens:
            break
        msg = result[idx]
        content = str(getattr(msg, "content", ""))
        truncated = _truncate_content(content, max_output_chars)
        new_len = len(truncated)
        reduced_tokens = (old_len - new_len) // 4
        total_reduced += max(reduced_tokens, 0)
        result[idx] = msg.model_copy(update={"content": truncated})

    return result, total_reduced
