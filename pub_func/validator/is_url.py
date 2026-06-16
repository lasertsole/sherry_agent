"""
判断一个字符串是否为 URL。

使用 urllib.parse 做严格 URL 解析 + 常见 scheme 白名单，
避免了正则表达式常见的边界情况问题（过长输入 ReDoS、编码字符误判等）。

Usage:
    from pub_func.validator.is_url import is_url

    is_url("https://example.com/path?a=1#frag")   # -> True
    is_url("not-a-url")                            # -> False
    is_url("")                                     # -> False
"""

from urllib.parse import urlparse

# 视为合法 URL 的 scheme 白名单
_VALID_SCHEMES = frozenset({
    "http",
    "https",
    "ftp",
    "ftps",
    "sftp",
    "ssh",
    "ws",
    "wss",
    "file",
    "data",
    "mailto",
    "tel",
    "sms",
})


def is_url(value: str) -> bool:
    """Check whether *value* is a well-formed, scheme-whitelisted URL.

    Args:
        value: The string to check.

    Returns:
        True if *value* parses as a URL with a recognised scheme and
        has a network location (netloc) or is a ``mailto:`` / ``tel:``
        style URI.
    """
    if not isinstance(value, str) or not value.strip():
        return False

    # urllib.parse 能处理绝大部分畸形输入而不会抛异常，
    # 唯一可能 raise 的是包含 NUL 字节的字符串
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False

    # (1) scheme 必须在白名单内
    if parsed.scheme not in _VALID_SCHEMES:
        return False

    # (2) 对于 http/https/ws/wss/ftp 等，必须有 netloc
    if parsed.scheme in {"http", "https", "ftp", "ftps", "sftp", "ssh", "ws", "wss", "file"}:
        return bool(parsed.netloc)

    # (3) mailto/tel/sms 只需要 scheme + path
    return bool(parsed.path)
