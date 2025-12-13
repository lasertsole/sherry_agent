def escape_xml(s: str) -> str:
    """
    XML 转义

    Args:
        s: 原始字符串

    Returns:
        转义后的字符串
    """
    return (
        s.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
