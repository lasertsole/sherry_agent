"""Public-URL (SSRF) guard for remote media fetches.

``is_url`` checks the scheme only: it happily accepts loopback, RFC1918 and
link-local targets — including the cloud metadata endpoint
(``169.254.169.254``), which turns a user-supplied media URL into a credential
read whose body the model then summarizes back to the caller.

:func:`is_public_url` resolves the host and accepts it only when DNS yields at
least one address and EVERY resolved address is globally routable. A host with
one private answer among several is rejected, so DNS round-robin cannot smuggle
the guard, and a resolution failure is a rejection (fail-closed) — a fetch we
cannot classify must not proceed.

``SHERRY_ALLOW_PRIVATE_MEDIA_URLS=1`` disables the guard for local development
against a loopback media server; it is read on every call (no import-time
caching), so it can be monkeypatched in tests.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from loguru import logger

__all__ = ["is_public_url"]

_ACCEPTED_SCHEMES = frozenset({"http", "https"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _allow_private() -> bool:
    """Whether ``SHERRY_ALLOW_PRIVATE_MEDIA_URLS`` disables the guard."""
    return os.getenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", "").strip().lower() in _TRUTHY


def _is_global_address(raw: str) -> bool:
    """True when the address is globally routable (not private/reserved/…)."""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            # ::ffff:127.0.0.1 must be judged as its IPv4 self.
            addr = mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def is_public_url(url: str) -> bool:
    """True when *url* is http(s) and every address it resolves to is global."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in _ACCEPTED_SCHEMES:
        return False
    host = parsed.hostname
    if not host:
        return False

    if _allow_private():
        logger.debug("public-url guard disabled by SHERRY_ALLOW_PRIVATE_MEDIA_URLS: host={}", host)
        return True

    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        logger.warning("public-url guard: DNS resolution failed for {}: {}", host, exc)
        return False

    # ``getaddrinfo`` sockaddr hosts are ``str | int`` in typeshed; an int is
    # never a valid textual address, so str() makes the set homogeneous (and
    # ``_is_global_address`` rejects whatever cannot be parsed).
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        return False
    refused = sorted(addr for addr in addresses if not _is_global_address(addr))
    if refused:
        logger.warning(
            "public-url guard: refused host={} — non-global address(es) {}", host, refused
        )
        return False
    return True
