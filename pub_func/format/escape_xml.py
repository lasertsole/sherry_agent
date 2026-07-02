def escape_xml(s: str) -> str:
    """
    XML escape.

    Args:
        s: Input string.

    Returns:
        Escaped string.
    """
    return (
        s.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
