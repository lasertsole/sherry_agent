"""CSRF guard for state-changing HTTP methods.

Threat model
------------
``gateway_auth_middleware`` already refuses requests whose ``Origin`` is outside
the allowlist, and a browser always sends ``Origin`` on a cross-site fetch or
form POST — so a hostile *page* cannot reach a mutating route. The residual gap
is same-origin: if a payload lands inside the webview (a DOMPurify 0day, a
compromised dev-server asset), the injected script shares the webview's origin
(``tauri://localhost``), passes the Origin gate, and — in non-strict token mode —
can issue ``POST/PUT/PATCH/DELETE`` without any credential.

This module adds the second layer, in the order the signals deserve:

1. **Sec-Fetch-Site** — a browser-set request header the page cannot forge.
   ``cross-site`` is refused regardless of ``Origin``, which also covers the
   simple-form POST where ``Origin`` may be omitted.
2. **Origin / Referer fallback** — for clients that send neither
   ``Sec-Fetch-Site`` (non-browser) nor ``Origin``: if either header *is*
   present it must be loopback or allowlisted; both absent means a script
   (curl, a test client, the desktop shell) and passes, exactly like the
   existing Origin gate — the guard must not turn routine automation into 403s.

Read-only methods (``GET``/``HEAD``/``OPTIONS``) are never gated: they change no
state, and gating them would break cross-origin asset loading.

The middleware runs *after* ``gateway_auth_middleware`` so the Origin decision
and the token check happen first (a hostile page learns nothing here).
``GATEWAY["csrf_guard_enabled"]`` is the switch, for a headless client that
mutates without any of the three headers *and* runs in strict token mode.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from loguru import logger

from server.trigger import auth

__all__ = ["csrf_guard_middleware", "csrf_verdict"]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _loopback_or_allowed(value: str) -> bool:
    """True when *value* resolves to a loopback host or an allowlisted origin."""
    candidate = value.strip()
    if not candidate or candidate.lower() == "null":
        return False
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127."):
        return True
    allowed = {origin.rstrip("/").lower() for origin in auth.allowed_origins()}
    if candidate.rstrip("/").lower() in allowed:
        return True
    # An allowlisted origin with a different path (a page under the dev server).
    return any(urlparse(origin).hostname == host for origin in allowed)


def csrf_verdict(
    method: str,
    origin: str | None,
    referer: str | None,
    sec_fetch_site: str | None,
) -> tuple[int, str] | None:
    """Return ``(status, message)`` to refuse the request, or ``None`` to allow."""
    if method.upper() not in _MUTATING_METHODS:
        return None

    site = (sec_fetch_site or "").strip().lower()
    if site == "cross-site":
        return 403, "Cross-site request blocked by the CSRF guard"

    if origin and origin.strip():
        if not _loopback_or_allowed(origin):
            return 403, "Origin not allowed for a state-changing request"
        return None

    if referer and referer.strip():
        if not _loopback_or_allowed(referer):
            return 403, "Referer not allowed for a state-changing request"
        return None

    # No browser signals at all: a script client. The token gate (strict mode)
    # and the Origin allowlist still apply upstream.
    return None


def csrf_guard_middleware(request):
    """Robyn ``before_request`` hook: CSRF gate for mutating methods."""
    from config.features import GATEWAY

    if not GATEWAY.get("csrf_guard_enabled", True):
        return request

    verdict = csrf_verdict(
        method=getattr(request, "method", "") or "",
        origin=request.headers.get("Origin"),
        referer=request.headers.get("Referer"),
        sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
    )
    if verdict is None:
        return request

    status, message = verdict
    from robyn import Response

    from server.trigger.security_headers import security_headers

    logger.warning(
        "csrf guard: refused method={} path={} reason={}",
        getattr(request, "method", "?"),
        getattr(getattr(request, "url", None), "path", "?"),
        message,
    )
    return Response(
        status_code=status,
        headers={"Content-Type": "application/json", **security_headers()},
        description=json.dumps({"success": False, "message": message}, ensure_ascii=False),
    )
