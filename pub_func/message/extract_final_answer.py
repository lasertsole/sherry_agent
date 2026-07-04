def extract_final_answer(result: dict) -> str:
    """从 invoke 结果中提取 LLM 最终回答内容。"""
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content and not getattr(msg, "tool_calls", None):
            return msg.content
        if isinstance(msg, dict) and msg.get("content") and not msg.get("tool_calls"):
            return msg["content"]
    return ""