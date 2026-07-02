"""
Check whether a string is a URL.

Uses ``urllib.parse`` for strict URL parsing with a common scheme
whitelist, avoiding regex edge cases (ReDoS on long input,
misinterpreting encoded characters, etc.).

Usage:
    from pub_func.validator.is_url import is_url

    is_url("https://example.com/path?a=1#frag")   # -> True
    is_url("not-a-url")                            # -> False
    is_url("")                                     # -> False
"""

from urllib.parse import urlparse

# Scheme whitelist for valid URLs
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

    # urllib.parse handles most malformed input without throwing;
    # the only thing that can raise ValueError is a NUL-byte in the string
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False

    # (1) scheme must be in the whitelist
    if parsed.scheme not in _VALID_SCHEMES:
        return False

    # (2) http/https/ws/wss/ftp etc. must have a netloc
    if parsed.scheme in {"http", "https", "ftp", "ftps", "sftp", "ssh", "ws", "wss", "file"}:
        return bool(parsed.netloc)

    # (3) mailto/tel/sms only need a scheme + path
    return bool(parsed.path)
