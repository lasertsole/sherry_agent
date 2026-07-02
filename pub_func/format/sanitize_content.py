import re

def sanitize_content(content: str) -> str:
    #"""Remove content inside brackets"""
    res = re.sub(r'[（\\(].*?[）\\)]', ' ', content)

    # Replace newlines / carriage returns with spaces to prevent word glueing
    res = re.sub(r'[\r\n]+', ' ', res)

    # Collapse tabs and consecutive spaces into a single space
    res = re.sub(r'[\t ]+', ' ', res)

    # Strip chain-of-thought tags
    res = re.sub(r"<think>.*?</think>", "", res)
    res = re.sub(r"<thinking>.*?</thinking>", "", res)

    return res.strip()