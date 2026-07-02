import json
from typing import List, TypedDict
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens
from langchain_core.messages import BaseMessage, ToolMessage, HumanMessage


class SliceLastNTurn(TypedDict):
    messages: List[BaseMessage]
    tokens: int
    dropped: int


TOKEN_MAX = 6000
def _truncate_msg(msg: BaseMessage)-> BaseMessage:
    if not isinstance(msg, ToolMessage):
        return msg

    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        text:str = json.dumps(content) if content is not None else ""
    else:
        text:str = content

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
def slice_last_turn(messages: List[BaseMessage]) -> SliceLastNTurn:
    """
        From the last role=user to the end, kept intact.
        tool_use/tool_result pairs are naturally preserved.
        Oversized tool_result is truncated (head + tail, middle dropped).
    """
    return slice_last_n_turn(messages, 1)

def slice_last_n_turn(messages: List[BaseMessage], n: int)-> SliceLastNTurn:
    if messages is None or len(messages)==0:
        return { "messages": [], "tokens": 0, "dropped": 0 }

    turn_count = 0
    last_user_idx = -1

    for i, msg in enumerate(reversed(messages)):
        if turn_count >= n:
            break

        if isinstance(msg, HumanMessage):
            last_user_idx = len(messages) - 1 - i
            turn_count+=1

    if last_user_idx < 0:
        last_user_idx = 0

    kept: List[BaseMessage] = messages[last_user_idx:]
    dropped = last_user_idx

    kept = [_truncate_msg(msg) for msg in kept]

    tokens = 0
    for msg in kept:
        tokens += estimate_msg_tokens(msg)

    return { "messages": kept, "tokens": tokens, "dropped": dropped }