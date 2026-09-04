import json
from langchain_core.messages import BaseMessage
from config.num import CHARS_PER_TOKEN


def estimate_msg_tokens(msg: BaseMessage) -> int:
    total = 0
    content = msg.content

    if isinstance(content, str):
        total += len(content)
    else:
        total += len(json.dumps(content)) if content is not None else 0

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            total += len(str(tc.get("name", "")))
            total += len(str(tc.get("args", "")))

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        total += len(str(tool_call_id))

    return total // CHARS_PER_TOKEN


def estimate_messages_tokens(messages) -> int:
    return sum(estimate_msg_tokens(m) for m in messages)
