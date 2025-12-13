import json
from langchain_core.messages import BaseMessage


def estimate_msg_tokens(msg: BaseMessage) -> int:
    content = msg.content

    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content) if content is not None else ""

    return len(text)