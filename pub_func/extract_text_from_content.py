from typing import Any

def extract_text_from_content(content: Any)->str:
    text: str = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and len(content) > 0:
        for item in content:
            if getattr(item, "type", "") == "text":
                text = getattr(item, "text", "")
                break

    return text