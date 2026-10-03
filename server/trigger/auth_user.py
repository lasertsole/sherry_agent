"""Per-user login gate — the SECOND auth layer, for non-loopback clients.

Order of the gates on every request (see ``server/trigger/core.py``)::

    gateway_auth_middleware   Origin allowlist + boot token   (existing)
    csrf_guard_middleware     mutating-method guard            (existing)
    user_auth_middleware      JWT session                       (this module)

The two layers answer different questions: the gateway token stops hostile web
pages and token-less scripts; this one stops a *remote* client that never logged
in. Loopback clients are exempt (``AUTH["require_auth_non_loopback"]``), which is
what keeps the desktop app and a local browser friction-free — the plan's R1.

Enforcement is **fail-closed**: any path that is not explicitly public is gated,
including paths that match no route at all (a 401 is as good as a 404 for an
unauthenticated caller, and a new route can never leak by omission). Static
assets are public so the login page itself can load.

WebSockets carry no client address in Robyn, so their gate is the ticket:
``GET /auth/ws-ticket`` makes the loopback decision (over HTTP, where
``request.ip_addr`` is trustworthy) and the socket only spends the ticket.
"""

from __future__ import annotations

import json
from typing import Any
from ipaddress import ip_address

from loguru import logger
from robyn import Request, Response

from config.features import AUTH
from server.service import auth_service
from server.trigger.security_headers import security_headers

__all__ = [
    "PUBLIC_PATHS",
    "STATIC_PREFIXES",
    "cookie_value",
    "is_loopback",
    "should_gate",
    "user_auth_middleware",
    "ws_user_check",
]

#: Reachable without a session: the login flow itself, the client's status probe
#: and the gateway-token bootstrap that predates this feature.
PUBLIC_PATHS = frozenset({"/auth/status", "/auth/login", "/auth/refresh", "/auth/token"})

#: Served from disk, no data behind them — the login page must be able to load.
STATIC_PREFIXES = ("/static", "/favicon.ico")

#: Methods that never carry a session worth checking (preflights are answered by
#: the CORS middleware before this one runs; this is belt-and-braces).
_SKIP_METHODS = frozenset({"OPTIONS"})


def is_loopback(remote_addr: str | None) -> bool:
    """True for the local machine (``127.0.0.0/8``, ``::1``, IPv4-mapped ``::ffff:``).

    An unknown address is NOT loopback: a refusal is the safe answer.
    """
    if not remote_addr:
        return False
    candidate = remote_addr.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.lower().startswith("::ffff:"):
        candidate = candidate[7:]
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    # A v4-mapped address parses as IPv6; unwrap it so 127.x still counts.
    mapped = getattr(parsed, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def cookie_value(cookie_header: str | None, name: str) -> str | None:
    """Extract one cookie from a raw ``Cookie:`` header (no parser in Robyn)."""
    if not cookie_header or not name:
        return None
    for chunk in cookie_header.split(";"):
        key, _, value = chunk.strip().partition("=")
        if key == name:
            return value.strip() or None
    return None


def should_gate(path: str) -> bool:
    """Whether this path needs a session (everything except the public set)."""
    normalized = path.rstrip("/") or "/"
    if normalized in PUBLIC_PATHS:
        return False
    return not any(
        normalized == prefix or normalized.startswith(prefix + "/") for prefix in STATIC_PREFIXES
    )


def _refusal(message: str) -> Response:
    return Response(
        status_code=401,
        headers={"Content-Type": "application/json", **security_headers()},
        description=json.dumps(
            {"success": False, "error": "not_authenticated", "message": message},
            ensure_ascii=False,
        ),
    )


async def user_auth_middleware(request: Request):
    """Pass the request through, or answer 401 when a session is required.

    Returns the request object to continue (Robyn's middleware contract), or a
    ``Response`` to short-circuit.
    """
    if request.method in _SKIP_METHODS:
        return request
    path = request.url.path or "/"
    if not should_gate(path):
        return request
    if AUTH["require_auth_non_loopback"] and is_loopback(getattr(request, "ip_addr", None)):
        return request
    if not await auth_service.enforcement_active():
        return request
    token = cookie_value(request.headers.get("Cookie"), AUTH["cookie_name"])
    user = await auth_service.current_user(token or "")
    if user is None:
        logger.info("user auth: refused {} {} (no valid session)", request.method, path)
        return _refusal("login required")
    return request


async def ws_user_check(query_params: Any) -> str | None:
    # ``query_params`` is Robyn's Rust ``QueryParams`` (only ``.get``), not a Mapping.
    """WebSocket gate: ``None`` = allowed, otherwise a reason for the log.

    Mirrors the HTTP gate without needing the peer address: while enforcement is
    active a single-use ``ticket`` (minted at ``/auth/ws-ticket``, which *does*
    see the address) is required. When nothing enforces a login, sockets stay as
    permissive as they were.
    """
    if not await auth_service.enforcement_active():
        return None
    ticket = str(query_params.get("ticket", "") or "")
    if not ticket:
        return "missing ws ticket"
    if not await auth_service.consume_ws_ticket(ticket):
        return "invalid or expired ws ticket"
    return None
