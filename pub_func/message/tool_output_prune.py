from config.num import PRUNE_PROTECT_TOKENS, PRUNE_MIN_REDUCTION_TOKENS
from langchain_core.messages import (
    BaseMessage,
    ToolMessage,
    HumanMessage,
    AIMessage,
)

_PRUNE_MARKER = "[Old tool result content cleared]"
_SUMMARY_LC_SOURCE = "summarization"


def _is_summary_message(msg: BaseMessage) -> bool:
    return getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE


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


def prune_tool_outputs(
    messages: list[BaseMessage],
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    min_reduction_tokens: int = PRUNE_MIN_REDUCTION_TOKENS,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()
    if estimator is None:
        def estimator(msgs):
            return sum(len(str(getattr(m, "content", ""))) // 4 for m in msgs)

    total_tool_tokens = 0
    pruned_tokens = 0
    to_prune: list[int] = []

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

        to_prune.append(i)
        pruned_tokens += token_est

    if pruned_tokens < min_reduction_tokens or not to_prune:
        return messages, 0

    result = list(messages)
    for idx in to_prune:
        result[idx] = result[idx].model_copy(update={"content": _PRUNE_MARKER})

    return result, pruned_tokens
