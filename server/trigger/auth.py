"""Loopback gateway auth: Origin allowlist + per-boot bootstrap token.

Threat model
------------
The backend listens on ``127.0.0.1``, so any *web page* the user visits can try
to reach it (cross-origin ``fetch``, a ``<form>`` POST, or a WebSocket
handshake). With wildcard CORS and no credentials the whole API was driveable
from a random site — including ``.env`` read/write, the log stream and the
agent-control sockets that can approve tool calls.

Policy
------
1. **Origin gate** — any HTTP request carrying an ``Origin`` outside
   ``GATEWAY["allowed_origins"]`` is refused with 403 before any handler runs
   (wired in ``server/trigger/core.py``; Robyn's ``ALLOW_CORS`` performs the
   same check for the CORS layer). Browsers always send ``Origin`` on
   cross-origin requests, so a hostile page cannot get past this. Requests
   without an ``Origin`` (curl, scripts, the webview's same-origin calls) pass
   — this gate targets web pages, not local tooling.
2. **Token** — a per-boot secret (``SHERRY_GATEWAY_TOKEN`` pins it for tooling
   and tests). Every response carries it in the ``token`` header (exported via
   ``Access-Control-Expose-Headers`` so the app's cross-origin fetch can read
   it); the desktop client keeps it in memory and echoes it as the ``token``
   request header. A WRONG token is refused (401) — the credential is never
   silently replaced. WebSocket handshakes cannot carry custom headers from a
   browser, so they present ``?token=`` and the token is REQUIRED there.
3. **Strict mode** — ``GATEWAY["require_token"]`` (env
   ``SHERRY_GATEWAY_REQUIRE_TOKEN``) refuses token-less HTTP requests as well;
   ``GET /auth/token`` stays reachable so a client can still bootstrap.

Residual risk, stated: a local *process* can always read the token from its own
environment or bootstrap one; defending against local processes was never the
audit's threat model (``任意网站可通过浏览器驱动 API``), and this closes that.
"""

from __future__ import annotations

import os
import secrets

from loguru import logger

from config.features import GATEWAY

__all__ = [
    "BOOTSTRAP_PATH",
    "TOKEN_HEADER",
    "TOKEN_QUERY_PARAM",
    "allowed_origins",
    "check_http",
    "check_ws",
    "gateway_token",
    "token_matches",
]

#: Endpoint that hands the token to a token-less caller (even in strict mode).
BOOTSTRAP_PATH = "/auth/token"
#: Wire names of the credential: response/request header + WS query parameter.
TOKEN_HEADER = "token"
TOKEN_QUERY_PARAM = "token"

#: Per-boot token: ``SHERRY_GATEWAY_TOKEN`` (stable across restarts) or random.
_TOKEN: str = os.getenv("SHERRY_GATEWAY_TOKEN", "").strip() or secrets.token_urlsafe(32)


def gateway_token() -> str:
    """The token this process issues and accepts."""
    return _TOKEN


def allowed_origins() -> list[str]:
    """The Origin allowlist the gateway and its CORS layer enforce."""
    return list(GATEWAY["allowed_origins"])


def token_matches(presented: str | None) -> bool:
    """Constant-time check of a presented token; an empty value never matches."""
    return bool(presented) and secrets.compare_digest(str(presented), _TOKEN)


def _origin_allowed(origin: str | None) -> bool:
    return not origin or origin in GATEWAY["allowed_origins"]


def check_http(
    origin: str | None, presented_token: str | None, path: str
) -> tuple[int, str] | None:
    """Gate one HTTP request.

    Returns ``(status, message)`` to refuse it, or ``None`` to let it through.
    Order matters: the origin gate runs first (a hostile page must not learn
    anything about tokens), then the credential checks.
    """
    if not _origin_allowed(origin):
        logger.warning("gateway auth: refused origin={!r} path={}", origin, path)
        return 403, "Origin not allowed"
    if presented_token and not token_matches(presented_token):
        logger.warning("gateway auth: invalid token for path={}", path)
        return 401, "Invalid token"
    if GATEWAY["require_token"] and not token_matches(presented_token) and path != BOOTSTRAP_PATH:
        logger.warning("gateway auth: token required but missing for path={}", path)
        return 401, "Token required"
    return None


def check_ws(presented_token: str | None) -> str | None:
    """Gate one WebSocket handshake; returns a refusal reason, or ``None``.

    Unlike HTTP, a missing token is never accepted: a browser can only pass it
    as a query parameter, and a token-less socket would otherwise be open to
    any local page that can reach ``ws://127.0.0.1``.
    """
    if token_matches(presented_token):
        return None
    logger.warning(
        "gateway auth: refusing WebSocket handshake ({})",
        "invalid token" if presented_token else "missing token",
    )
    return "missing or invalid token"
