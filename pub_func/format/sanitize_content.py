import re

def sanitize_content(content: str) -> str:
    #"""去除括号内字符"""
    res = re.sub(r'[（\\(].*?[）\\)]', ' ', content)

    # 清除换行符和回车符，替换为空格，防止单词粘连
    res = re.sub(r'[\r\n]+', ' ', res)

    # 清除缩进符（制表符 \t）和连续的空格
    res = re.sub(r'[\t ]+', ' ', res)

    # 去除思维链标签
    res = re.sub(r"<think>.*?</think>", "", res)
    res = re.sub(r"<thinking>.*?</thinking>", "", res)

    return res.strip()