import json

from config.features import MESSAGE_PIPELINE
from typing import TypedDict
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens
from langchain_core.messages import BaseMessage, ToolMessage, HumanMessage


class SliceLastNTurn(TypedDict):
    messages: list[BaseMessage]
    tokens: int
    dropped: int


TOKEN_MAX = MESSAGE_PIPELINE["slice_last_turn_token_max"]


def _truncate_msg(msg: BaseMessage) -> BaseMessage:
    if not isinstance(msg, ToolMessage):
        return msg

    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        text: str = json.dumps(content) if content is not None else ""
    else:
        text = content

    if len(text) <= TOKEN_MAX:
        return msg

    head_len = int(TOKEN_MAX * 0.6)
    tail_len = int(TOKEN_MAX * 0.3)

    truncated_text = (
        f"{text[:head_len]}\n"
        f"...[truncated {len(text) - head_len - tail_len} chars]...\n"
        f"{text[-tail_len:]}"
    )

    return msg.model_copy(deep=True, update={"content": truncated_text})


# ─── Take the last complete user turn ────────────────────────
def slice_last_turn(messages: list[BaseMessage]) -> SliceLastNTurn:
    """
    From the last role=user to the end, kept intact.
    tool_use/tool_result pairs are naturally preserved.
    Oversized tool_result is truncated (head + tail, middle dropped).
    """
    return slice_last_n_turn(messages, 1)


def slice_last_n_turn(messages: list[BaseMessage], n: int) -> SliceLastNTurn:
    if messages is None or len(messages) == 0:
        return {"messages": [], "tokens": 0, "dropped": 0}

    # A turn starts at the first HumanMessage of a run of consecutive
    # HumanMessages. Batch-drained turns feed N HumanMessages in one agent
    # turn, so the boundary must be the FIRST of the run — otherwise the
    # earlier batch messages would be dropped from persistence.
    turn_starts: list[int] = [
        i
        for i, msg in enumerate(messages)
        if isinstance(msg, HumanMessage)
        and (i == 0 or not isinstance(messages[i - 1], HumanMessage))
    ]

    if not turn_starts or n <= 0:
        # No HumanMessage (or non-positive n): keep everything.
        start_idx = 0
    else:
        start_idx = turn_starts[max(len(turn_starts) - n, 0)]

    kept: list[BaseMessage] = messages[start_idx:]
    dropped = start_idx

    kept = [_truncate_msg(msg) for msg in kept]

    tokens = 0
    for msg in kept:
        tokens += estimate_msg_tokens(msg)

    return {"messages": kept, "tokens": tokens, "dropped": dropped}
