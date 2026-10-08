"""The one HTTP GET that every non-media fetch path should use.

``urllib.request.urlopen`` is the wrong primitive for attacker-influenced URLs,
in three ways this module fixes at once:

* **DNS rebinding** — the guard resolves the host, then ``urlopen`` resolves it
  again when it connects. An attacker who answers the first query with a public
  address and the second with ``169.254.169.254`` wins the race. Here the
  address is resolved once, verified, and the socket is opened to *that*
  address; the hostname still travels in the ``Host`` header (HTTP) and the TLS
  SNI + certificate check (HTTPS), so virtual hosting and verification keep
  working.
* **Redirects** — ``urlopen`` follows a 30x to a private target before the
  caller can inspect anything. Here every hop is re-verified and re-pinned, so a
  public URL cannot bounce the request inward.
* **Unbounded bodies** — the size cap is enforced while reading, so a hostile
  endpoint cannot stream a caller into memory exhaustion.

Deliberate limits: GET only (that is what the media and document paths need),
no proxy support (a proxy would be the one making the connection, which defeats
pinning), and no HTTP/2 or connection reuse.

``SHERRY_ALLOW_PRIVATE_MEDIA_URLS=1`` keeps its existing meaning from
``public_url``: the guard is bypassed *and* pinning is skipped (the hostname is
dialed normally), which is the documented escape hatch for local development
against a loopback server — and for fake-ip proxy hosts, where every public name
resolves into the RFC 2544 range (see ``public_url``).
"""

from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from loguru import logger

from pub.func.validator.public_url import _allow_private, resolve_public_addresses

__all__ = ["FetchResult", "safe_fetch"]

_DEFAULT_TIMEOUT_S = 30.0
_MAX_REDIRECTS = 5
_READ_CHUNK = 65536
_USER_AGENT = "Mozilla/5.0 (EMA_AI_agent)"
_ACCEPTED_SCHEMES = frozenset({"http", "https"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one :func:`safe_fetch` call.

    ``reason`` is empty on success and otherwise one of ``scheme``, ``ssrf``,
    ``network``, ``redirects`` or ``oversize`` — the media pipeline maps them to
    different model-visible notices, so the distinction is part of the contract.
    """

    ok: bool
    body: bytes | None = None
    declared_length: int | None = None
    observed_bytes: int = 0
    reason: str = ""
    final_url: str = ""
    status: int = 0


def _port_for(parsed) -> int:
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _connect(parsed, timeout: float, pinned: str | None) -> http.client.HTTPConnection:
    """Open a connection to ``pinned`` (a verified address) or to the hostname.

    ``pinned`` is the address ``_resolve_for_hop`` already verified; dialing it
    — not the hostname — is what closes the rebinding window between the check
    and the connect. ``None`` means the escape hatch is on and the hostname is
    resolved by the OS as usual.
    """
    host = parsed.hostname or ""
    port = _port_for(parsed)

    if pinned is None:
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        conn = connection_type(host, port, timeout=timeout)
        conn.connect()
        return conn

    sock = socket.create_connection((pinned, port), timeout=timeout)
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
        # server_hostname keeps SNI and hostname verification on the *hostname*,
        # so pinning the address does not weaken the certificate check.
        conn.sock = context.wrap_socket(sock, server_hostname=host)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
    return conn


def _read_body(response, max_bytes: int | None) -> tuple[bytes | None, int, str]:
    """Read the body up to the cap. Returns ``(body, observed, reason)``."""
    chunks: list[bytes] = []
    observed = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        observed += len(chunk)
        if max_bytes is not None and observed > max_bytes:
            return None, observed, "oversize"
        chunks.append(chunk)
    return b"".join(chunks), observed, ""


def _resolve_for_hop(host: str) -> tuple[str | None, bool]:
    """One verified resolution per hop.

    Returns ``(pinned_address, ok)``: the address to dial, and whether the hop
    may proceed at all. An empty address with ``ok`` True means the escape hatch
    is active (dial the hostname); ``ok`` False means the hop is refused —
    either a non-global answer (which includes a rebinding answer, because every
    address must be global) or a host that does not resolve.
    """
    if _allow_private():
        return None, True
    addresses = resolve_public_addresses(host)
    if not addresses:
        return None, False
    return addresses[0], True


def safe_fetch(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_S,
    max_bytes: int | None = None,
    headers: dict[str, str] | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> FetchResult:
    """GET ``url`` with the SSRF guard, DNS pinning and per-hop redirect checks."""
    if not isinstance(url, str) or not url.strip():
        return FetchResult(ok=False, reason="scheme")
    current = url.strip()

    request_headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)

    for _hop in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme not in _ACCEPTED_SCHEMES or not parsed.hostname:
            logger.warning("safe_fetch: refused scheme/host url={}", current)
            return FetchResult(ok=False, reason="scheme", final_url=current)
        pinned, allowed = _resolve_for_hop(parsed.hostname or "")
        if not allowed:
            logger.warning("safe_fetch: refused by the SSRF guard url={}", current)
            return FetchResult(ok=False, reason="ssrf", final_url=current)

        try:
            conn = _connect(parsed, timeout, pinned)
        except (OSError, ssl.SSLError) as exc:
            logger.warning("safe_fetch: connect failed url={} exc={}", current, exc)
            return FetchResult(ok=False, reason="network", final_url=current)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            conn.request("GET", path, headers=request_headers)
            response = conn.getresponse()
            status = response.status
            if status in _REDIRECT_CODES:
                location = response.getheader("Location")
                declared = response.getheader("Content-Length")
                conn.close()
                if not location:
                    logger.warning("safe_fetch: redirect without Location url={}", current)
                    return FetchResult(ok=False, reason="network", final_url=current, status=status)
                nxt = urljoin(current, location)
                logger.debug("safe_fetch: following redirect {} -> {}", current, nxt)
                current = nxt
                continue  # next hop is re-verified and re-pinned at the top

            declared = response.getheader("Content-Length")
            declared_length = int(declared) if declared and declared.isdigit() else None
            if (
                max_bytes is not None
                and declared_length is not None
                and declared_length > max_bytes
            ):
                conn.close()
                return FetchResult(
                    ok=False,
                    declared_length=declared_length,
                    reason="oversize",
                    final_url=current,
                    status=status,
                )

            body, observed, reason = _read_body(response, max_bytes)
            conn.close()
            if reason:
                return FetchResult(
                    ok=False,
                    observed_bytes=observed,
                    declared_length=declared_length,
                    reason=reason,
                    final_url=current,
                    status=status,
                )
            return FetchResult(
                ok=True,
                body=body,
                observed_bytes=observed,
                declared_length=declared_length,
                final_url=current,
                status=status,
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            conn.close()
            logger.warning("safe_fetch: request failed url={} exc={}", current, exc)
            return FetchResult(ok=False, reason="network", final_url=current)

    logger.warning("safe_fetch: exceeded {} redirects url={}", max_redirects, url)
    return FetchResult(ok=False, reason="redirects", final_url=current)
