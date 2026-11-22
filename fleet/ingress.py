"""HTTP/WS Ingress for the bare-metal multi-tenant deployment.

Authentication lives HERE, never in a cell: the browser gets an Ingress cookie,
the Ingress resolves its tenant, and the loopback cell receives only the
per-cell gateway token. The Ingress also owns ``/auth/*`` so a cell's public
``/auth/token`` bootstrap can never leak its gateway token through the proxy.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger

from server.utils.password import hash_password, password_policy_error

from .config import IngressConfig, load_config
from .manager import FleetManager
from .security import (
    ACTIVE_TENANT_COOKIE,
    access_claims,
    claims_tenants,
    clear_auth_cookies,
    csrf_ok,
    issue_tokens,
    origin_allowed,
    refresh_claims,
    set_active_tenant_cookie,
    set_auth_cookies,
)
from .store import FleetStore

CONFIG_KEY = web.AppKey("config", IngressConfig)
STORE_KEY = web.AppKey("store", FleetStore)
CLIENT_KEY = web.AppKey("client", aiohttp.ClientSession)
CLAIMS_KEY = web.RequestKey("claims", dict[str, Any])


class LoginLimiter:
    """Bounded in-memory failure window per (client, username)."""

    def __init__(self, *, window_s: float = 300.0, max_failures: int = 5) -> None:
        self.window_s = window_s
        self.max_failures = max_failures
        self._failures: dict[str, list[float]] = {}

    def _prune(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window_s
        values = [stamp for stamp in self._failures.get(key, []) if stamp >= cutoff]
        if values:
            self._failures[key] = values
        else:
            self._failures.pop(key, None)
        return values

    def allow(self, key: str) -> bool:
        """True when this key has not exhausted its failure window."""
        return len(self._prune(key)) < self.max_failures

    def fail(self, key: str) -> None:
        """Record one failed login."""
        values = self._prune(key)
        values.append(time.monotonic())
        self._failures[key] = values

    def reset(self, key: str) -> None:
        """Forget a key after a successful login."""
        self._failures.pop(key, None)


LIMITER_KEY = web.AppKey("login_limiter", LoginLimiter)
MANAGER_KEY = web.AppKey("manager", FleetManager)
SUPERVISOR_KEY = web.AppKey("supervisor", asyncio.Task)


def _client_address(request: web.Request) -> str:
    """The limiter identity; a proxy's X-Forwarded-For is opt-in only."""
    if request.app[CONFIG_KEY].trust_forwarded:
        forwarded = str(request.headers.get("X-Forwarded-For", "") or "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or "unknown"
    return str(request.remote or "unknown")


def _login_key(request: web.Request, username: str) -> str:
    return f"{_client_address(request)}:{username.casefold()}"


_PUBLIC_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/refresh",
        "/auth/status",
        "/auth/logout",
        "/auth/me",
        "/auth/account",
        "/auth/tenant",
        "/auth/tenants",
        "/health",
    }
)
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_DROP_REQUEST_HEADERS = _HOP_BY_HOP | {
    "host",
    "cookie",
    "origin",
    "referer",
    "authorization",
    # The Ingress injects the cell token itself; a browser-supplied value must
    # never race it (header names are case-insensitive on the wire).
    "token",
    "x-refresh-token",
    "content-length",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
}
_DROP_RESPONSE_HEADERS = _HOP_BY_HOP | {"content-length", "set-cookie"}


def _json(status: int, payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload, status=status)


def _public_user(user: dict[str, Any], tenant_id: str | None = None) -> dict[str, Any]:
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
        "tenant_id": str(tenant_id if tenant_id is not None else user["tenant_id"]),
    }


def _tenant_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["tenant_id"]) for row in rows]


async def _memberships(store: FleetStore, user_id: int) -> list[dict[str, Any]]:
    """The account's current tenant memberships, default first."""
    return await store.list_user_tenants(user_id)


async def _active_tenant(request: web.Request, claims: dict[str, Any]) -> str | None:
    """Resolve the cell this request targets, re-checking the live membership.

    The cookie is a preference, not an authorization: a stale selection (admin
    revoked the cell, or another account used this browser) falls back to the
    account's current default. A user with no membership resolves to None.
    """
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None
    rows = await _memberships(request.app[STORE_KEY], user_id)
    tenant_ids = _tenant_ids(rows)
    if not tenant_ids:
        return None
    selected = str(request.cookies.get(ACTIVE_TENANT_COOKIE, "") or "")
    return selected if selected in tenant_ids else tenant_ids[0]


_STATIC_FILES = frozenset(
    {"favicon.ico", "robots.txt", "_payload.json", "200.html", "404.html", "manifest.webmanifest"}
)
_STATIC_PREFIXES = ("_nuxt/", "_ipx/", "assets/")


def _static_path(config: IngressConfig, path: str) -> bool:
    if config.static_dir is None:
        return False
    stripped = path.lstrip("/")
    if stripped in _STATIC_FILES:
        return (config.static_dir / stripped).is_file()
    return path == "/" or any(path.startswith(f"/{prefix}") for prefix in _STATIC_PREFIXES)


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Require a session for API routes; the SPA shell and static assets stay public."""
    path = request.path
    if path == "/auth/token":
        return _json(404, {"success": False, "error": "not_found"})
    if path.startswith("/auth/") and path not in _PUBLIC_PATHS:
        return _json(404, {"success": False, "error": "not_found"})
    if path in _PUBLIC_PATHS or _static_path(request.app[CONFIG_KEY], path):
        return await handler(request)
    if (
        request.method == "GET"
        and "text/html" in request.headers.get("Accept", "")
        and _spa_page(request.app[CONFIG_KEY]) is not None
    ):
        # The SPA shell is public (it holds no data): /login must load before a
        # session exists, and every deep route boots the same router. Every API
        # call that shell makes still goes through the checks below.
        return await handler(request)
    if str(request.headers.get("Upgrade", "")).lower() == "websocket" and not origin_allowed(
        request, request.app[CONFIG_KEY]
    ):
        return _json(403, {"success": False, "error": "origin_rejected"})
    if not csrf_ok(request, request.app[CONFIG_KEY]):
        return _json(403, {"success": False, "error": "csrf_rejected"})
    claims = access_claims(request, request.app[CONFIG_KEY])
    if claims is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    request[CLAIMS_KEY] = claims
    return await handler(request)


async def login(request: web.Request) -> web.Response:
    """Authenticate a user and set the Ingress cookies."""
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    if not csrf_ok(request, config):
        return _json(403, {"success": False, "error": "csrf_rejected"})
    try:
        body = await request.json()
    except Exception:
        return _json(400, {"success": False, "error": "invalid_json"})
    if not isinstance(body, dict):
        return _json(400, {"success": False, "error": "invalid_json"})
    username = str(body.get("username", "") or "").strip()
    password = str(body.get("password", "") or "")
    limiter = request.app[LIMITER_KEY]
    key = _login_key(request, username)
    if not limiter.allow(key):
        return _json(429, {"success": False, "error": "too_many_attempts"})
    user = await store.get_user_by_username(username) if username else None
    stored = str(user["password_hash"]) if user is not None else ""
    # Verify even for an unknown username so a miss is not a timing oracle.
    if not store.verify_password(password, stored) or user is None:
        limiter.fail(key)
        return _json(401, {"success": False, "error": "invalid_credentials"})
    limiter.reset(key)
    rows = await _memberships(store, int(user["id"]))
    if not rows:
        return _json(
            403,
            {
                "success": False,
                "error": "no_tenant",
                "message": "no cell is assigned to this account",
            },
        )
    tenants = _tenant_ids(rows)
    default = tenants[0]
    access, refresh, _access_claims, refresh_claims = issue_tokens(user, config, tenants)
    await store.save_refresh(
        str(refresh_claims["jti"]), int(user["id"]), int(refresh_claims["exp"])
    )
    response = _json(
        200,
        {
            "success": True,
            "user": _public_user(user, default),
            "expires_in": config.access_ttl_s,
        },
    )
    set_auth_cookies(response, access=access, refresh=refresh, config=config)
    set_active_tenant_cookie(response, default, config)
    return response


async def refresh(request: web.Request) -> web.Response:
    """Rotate the refresh token and issue a new access cookie."""
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    if not csrf_ok(request, config):
        return _json(403, {"success": False, "error": "csrf_rejected"})
    claims = refresh_claims(request, config)
    if claims is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        return _json(401, {"success": False, "error": "not_authenticated"})
    if not await store.refresh_active(str(claims["jti"]), user_id):
        return _json(401, {"success": False, "error": "not_authenticated"})
    user = await store.get_user_by_id(user_id)
    if user is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    rows = await _memberships(store, user_id)
    if not rows:
        return _json(
            403,
            {
                "success": False,
                "error": "no_tenant",
                "message": "no cell is assigned to this account",
            },
        )
    tenants = _tenant_ids(rows)
    default = tenants[0]
    access, new_refresh, _access_claims, new_claims = issue_tokens(user, config, tenants)
    await store.revoke_refresh(str(claims["jti"]))
    await store.save_refresh(str(new_claims["jti"]), user_id, int(new_claims["exp"]))
    response = _json(
        200,
        {
            "success": True,
            "user": _public_user(user, default),
            "expires_in": config.access_ttl_s,
        },
    )
    set_auth_cookies(response, access=access, refresh=new_refresh, config=config)
    selected = str(request.cookies.get(ACTIVE_TENANT_COOKIE, "") or "")
    if selected not in tenants:
        set_active_tenant_cookie(response, default, config)
    return response


async def logout(request: web.Request) -> web.Response:
    """Revoke the refresh token and clear both cookies."""
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    if not csrf_ok(request, config):
        return _json(403, {"success": False, "error": "csrf_rejected"})
    claims = refresh_claims(request, config)
    if claims is not None:
        await store.revoke_refresh(str(claims["jti"]))
    response = _json(200, {"success": True})
    clear_auth_cookies(response, config)
    return response


async def status(request: web.Request) -> web.Response:
    """Report the current Ingress session (public status probe).

    The flags mirror the cell's shape so the shared SPA guard works unchanged:
    an Ingress ALWAYS requires a login (it exists to gate tenants), and its
    accounts are administrator-provisioned, so ``has_account`` is true.
    """
    config = request.app[CONFIG_KEY]
    payload: dict[str, Any] = {
        "authenticated": False,
        "auth_required": True,
        "auth_enabled": True,
        "has_account": True,
        "username_required": True,
    }
    claims = access_claims(request, config)
    if claims is not None:
        payload["authenticated"] = True
        tenant = str(claims.get("tenant_id") or "")
        if not tenant:
            available = claims_tenants(claims)
            tenant = available[0] if available else ""
        payload["user"] = {
            "id": int(claims["sub"]),
            "username": claims.get("username", ""),
            "role": claims.get("role", "user"),
            "tenant_id": tenant,
        }
    return _json(200, payload)


async def me(request: web.Request) -> web.Response:
    """The signed-in account; a valid refresh cookie heals an expired access one."""
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    claims = access_claims(request, config)
    if claims is not None:
        try:
            user = await store.get_user_by_id(int(claims["sub"]))
        except (TypeError, ValueError):
            user = None
        if user is not None:
            rows = await _memberships(store, int(user["id"]))
            if not rows:
                return _json(
                    403,
                    {
                        "success": False,
                        "error": "no_tenant",
                        "message": "no cell is assigned to this account",
                    },
                )
            return _json(
                200,
                {
                    "success": True,
                    "user": _public_user(user, _tenant_ids(rows)[0]),
                },
            )
    rotated = refresh_claims(request, config)
    if rotated is not None:
        try:
            user_id = int(rotated["sub"])
        except (TypeError, ValueError):
            user_id = 0
        if user_id and await store.refresh_active(str(rotated["jti"]), user_id):
            user = await store.get_user_by_id(user_id)
            if user is not None:
                rows = await _memberships(store, user_id)
                if not rows:
                    return _json(
                        403,
                        {
                            "success": False,
                            "error": "no_tenant",
                            "message": "no cell is assigned to this account",
                        },
                    )
                tenants = _tenant_ids(rows)
                default = tenants[0]
                access, refresh, _access, new_refresh = issue_tokens(user, config, tenants)
                await store.revoke_refresh(str(rotated["jti"]))
                await store.save_refresh(str(new_refresh["jti"]), user_id, int(new_refresh["exp"]))
                response = _json(
                    200,
                    {
                        "success": True,
                        "user": _public_user(user, default),
                        "expires_in": config.access_ttl_s,
                    },
                )
                set_auth_cookies(response, access=access, refresh=refresh, config=config)
                selected = str(request.cookies.get(ACTIVE_TENANT_COOKIE, "") or "")
                if selected not in tenants:
                    set_active_tenant_cookie(response, default, config)
                return response
    return _json(401, {"success": False, "error": "not_authenticated"})


async def account(request: web.Request) -> web.Response:
    """Change the signed-in account's password.

    Enabling/disabling protection belongs to the Ingress administrator (the
    login is the tenant boundary), so those bodies are refused by name.
    """
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    if not csrf_ok(request, config):
        return _json(403, {"success": False, "error": "csrf_rejected"})
    claims = access_claims(request, config)
    if claims is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    try:
        body = await request.json()
    except Exception:
        return _json(400, {"success": False, "error": "invalid_json"})
    if not isinstance(body, dict):
        return _json(400, {"success": False, "error": "invalid_json"})
    try:
        user = await store.get_user_by_id(int(claims["sub"]))
    except (TypeError, ValueError):
        user = None
    if user is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    if body.get("enabled") is not None or body.get("new_password") is None:
        return _json(
            403,
            {
                "success": False,
                "error": "managed_by_admin",
                "message": "accounts are managed by the Ingress administrator",
            },
        )
    if not store.verify_password(str(body.get("password", "")), str(user["password_hash"])):
        return _json(401, {"success": False, "error": "invalid_credentials"})
    rows = await _memberships(store, int(user["id"]))
    if not rows:
        return _json(
            403,
            {
                "success": False,
                "error": "no_tenant",
                "message": "no cell is assigned to this account",
            },
        )
    tenants = _tenant_ids(rows)
    default = tenants[0]
    new_password = str(body.get("new_password", ""))
    problem = password_policy_error(new_password)
    if problem:
        return _json(
            400,
            {"success": False, "error": "password_policy", "message": problem},
        )
    await store.set_password_hash(int(user["id"]), hash_password(new_password))
    await store.revoke_user_refresh_tokens(int(user["id"]))
    access, refresh, _access, refresh_token = issue_tokens(user, config, tenants)
    await store.save_refresh(str(refresh_token["jti"]), int(user["id"]), int(refresh_token["exp"]))
    response = _json(
        200,
        {
            "success": True,
            "user": _public_user(user, default),
            "expires_in": config.access_ttl_s,
        },
    )
    set_auth_cookies(response, access=access, refresh=refresh, config=config)
    selected = str(request.cookies.get(ACTIVE_TENANT_COOKIE, "") or "")
    if selected not in tenants:
        set_active_tenant_cookie(response, default, config)
    return response


async def account_delete(_request: web.Request) -> web.Response:
    """Refuse self-service disable: the Ingress login is the tenant boundary."""
    return _json(
        403,
        {
            "success": False,
            "error": "managed_by_admin",
            "message": "accounts are managed by the Ingress administrator",
        },
    )


async def list_tenants(request: web.Request) -> web.Response:
    """The signed-in account's member cells, with the currently selected one."""
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    claims = access_claims(request, config)
    if claims is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        return _json(401, {"success": False, "error": "not_authenticated"})
    rows = await _memberships(store, user_id)
    if not rows:
        return _json(
            403,
            {
                "success": False,
                "error": "no_tenant",
                "message": "no cell is assigned to this account",
            },
        )
    tenant_ids = _tenant_ids(rows)
    selected = str(request.cookies.get(ACTIVE_TENANT_COOKIE, "") or "")
    active = selected if selected in tenant_ids else tenant_ids[0]
    tenants: list[dict[str, Any]] = []
    for row in rows:
        tenant_id = str(row["tenant_id"])
        process = await store.get_process(tenant_id)
        tenants.append(
            {
                "tenant_id": tenant_id,
                "role": str(row["role"]),
                "is_default": bool(row["is_default"]),
                "cell_status": str((process or {}).get("status") or "stopped"),
            }
        )
    return _json(200, {"success": True, "active": active, "tenants": tenants})


async def select_tenant(request: web.Request) -> web.Response:
    """Switch the browser's active cell with a cookie (no token re-issue)."""
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]
    if not csrf_ok(request, config):
        return _json(403, {"success": False, "error": "csrf_rejected"})
    claims = access_claims(request, config)
    if claims is None:
        return _json(401, {"success": False, "error": "not_authenticated"})
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        return _json(401, {"success": False, "error": "not_authenticated"})
    try:
        body = await request.json()
    except Exception:
        return _json(400, {"success": False, "error": "invalid_json"})
    if not isinstance(body, dict):
        return _json(400, {"success": False, "error": "invalid_json"})
    tenant_id = str(body.get("tenant_id", "") or "").strip()
    if not tenant_id or not await store.user_has_tenant(user_id, tenant_id):
        return _json(403, {"success": False, "error": "tenant_not_authorized"})
    response = _json(200, {"success": True, "tenant_id": tenant_id})
    set_active_tenant_cookie(response, tenant_id, config)
    return response


async def health(request: web.Request) -> web.Response:
    """Liveness probe for the Ingress."""
    cells = await request.app[STORE_KEY].list_cells()
    return _json(200, {"ok": True, "cells": len(cells)})


def _request_headers(request: web.Request, gateway_token: str) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    headers["token"] = gateway_token
    return headers


def _response_headers(upstream: aiohttp.ClientResponse) -> dict[str, str]:
    return {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }


async def _http_proxy(request: web.Request, cell: dict[str, Any]) -> web.StreamResponse:
    target = f"http://{cell['host']}:{int(cell['port'])}{request.rel_url.raw_path_qs}"
    headers = _request_headers(request, str(cell["gateway_token"]))
    data: Any = request.content if request.can_read_body else None
    session = request.app[CLIENT_KEY]
    try:
        async with session.request(
            request.method,
            target,
            headers=headers,
            data=data,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status, headers=_response_headers(upstream)
            )
            await response.prepare(request)
            if request.method != "HEAD" and upstream.status not in (204, 304):
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
            await response.write_eof()
            return response
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.warning("ingress: cell request failed for {}: {}", cell["tenant_id"], exc)
        return _json(502, {"success": False, "error": "cell_unreachable"})


async def _ws_ticket(session: aiohttp.ClientSession, cell: dict[str, Any]) -> str | None:
    url = f"http://{cell['host']}:{int(cell['port'])}/auth/ws-ticket"
    try:
        async with session.get(
            url,
            headers={"token": str(cell["gateway_token"])},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json()
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return None
    ticket = payload.get("ticket") if isinstance(payload, dict) else None
    return str(ticket) if ticket else None


def _ws_url(request: web.Request, cell: dict[str, Any], ticket: str | None) -> str:
    # The Ingress owns both credentials; a browser-supplied token/ticket must
    # never be forwarded ahead of the values minted here.
    query = [
        ("token", str(cell["gateway_token"])),
        *(
            (key, value)
            for key, value in request.rel_url.query.items()
            if key not in {"token", "ticket"}
        ),
    ]
    if ticket:
        query.append(("ticket", ticket))
    path = request.rel_url.raw_path
    return f"ws://{cell['host']}:{int(cell['port'])}{path}?{urllib.parse.urlencode(query)}"


async def _pipe(
    source: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    target: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
) -> None:
    async for message in source:
        if message.type == aiohttp.WSMsgType.TEXT:
            await target.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await target.send_bytes(message.data)
        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
            break
    await target.close()


async def _ws_proxy(request: web.Request, cell: dict[str, Any]) -> web.StreamResponse:
    session = request.app[CLIENT_KEY]
    ticket = await _ws_ticket(session, cell)
    protocols = [
        item.strip()
        for item in str(request.headers.get("Sec-WebSocket-Protocol", "")).split(",")
        if item.strip()
    ]
    client_ws = web.WebSocketResponse(protocols=protocols or ())
    await client_ws.prepare(request)
    try:
        cell_ws = await session.ws_connect(
            _ws_url(request, cell, ticket),
            protocols=protocols or (),
            autoping=True,
            heartbeat=30,
            max_msg_size=0,
        )
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.warning("ingress: cell websocket failed for {}: {}", cell["tenant_id"], exc)
        await client_ws.close(code=aiohttp.WSCloseCode.INTERNAL_ERROR)
        return client_ws
    try:
        await asyncio.gather(
            _pipe(client_ws, cell_ws),
            _pipe(cell_ws, client_ws),
            return_exceptions=True,
        )
    finally:
        await cell_ws.close()
        await client_ws.close()
    return client_ws


async def proxy(request: web.Request) -> web.StreamResponse:
    """Proxy one authenticated request to the tenant cell."""
    claims = request.get(CLAIMS_KEY) or {}
    if (
        request.method == "GET"
        and "text/html" in request.headers.get("Accept", "")
        and (page := _spa_page(request.app[CONFIG_KEY])) is not None
    ):
        # A client-side route such as /home/<sid> is not a cell endpoint: the
        # browser must receive the SPA shell, which then boots the router.
        return web.FileResponse(page)
    if not claims:
        return _json(401, {"success": False, "error": "not_authenticated"})
    tenant = await _active_tenant(request, claims)
    if not tenant:
        return _json(403, {"success": False, "error": "tenant_not_authorized"})
    cell = await request.app[STORE_KEY].get_cell(tenant)
    if cell is None:
        return _json(503, {"success": False, "error": "cell_unavailable"})
    if str(request.headers.get("Upgrade", "")).lower() == "websocket":
        return await _ws_proxy(request, cell)
    return await _http_proxy(request, cell)


def _spa_page(config: IngressConfig) -> Path | None:
    """The SPA fallback document (Nuxt emits ``200.html`` for static routes)."""
    if config.static_dir is None:
        return None
    for name in ("200.html", "index.html"):
        candidate = config.static_dir / name
        if candidate.is_file():
            return candidate
    return None


def _favicon(path: Path):
    """Build the small async favicon handler (aiohttp needs an awaitable)."""

    async def _handler(_request: web.Request) -> web.StreamResponse:
        return web.FileResponse(path)

    return _handler


async def _index(request: web.Request) -> web.StreamResponse:
    static_dir = request.app[CONFIG_KEY].static_dir
    if static_dir is None:
        raise web.HTTPNotFound()
    return web.FileResponse(static_dir / "index.html")


async def _startup(app: web.Application) -> None:
    app[CLIENT_KEY] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None),
        cookie_jar=aiohttp.DummyCookieJar(),
    )
    manager = app[MANAGER_KEY]
    if app[CONFIG_KEY].supervise:
        app[SUPERVISOR_KEY] = asyncio.create_task(manager.supervise_forever())


async def _cleanup(app: web.Application) -> None:
    task = app.get(SUPERVISOR_KEY)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await app[CLIENT_KEY].close()


async def create_app(
    config: IngressConfig | None = None,
    store: FleetStore | None = None,
    manager: FleetManager | None = None,
) -> web.Application:
    """Build the Ingress application (the store is initialised for you)."""
    config = config or load_config()
    store = store or FleetStore(config.db_path)
    await store.init()
    app = web.Application(middlewares=[auth_middleware])
    app[CONFIG_KEY] = config
    app[STORE_KEY] = store
    app[MANAGER_KEY] = manager or FleetManager(store, config)
    app[LIMITER_KEY] = LoginLimiter()
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    app.router.add_post("/auth/login", login)
    app.router.add_post("/auth/refresh", refresh)
    app.router.add_post("/auth/logout", logout)
    app.router.add_get("/auth/status", status)
    app.router.add_get("/auth/me", me)
    app.router.add_get("/auth/tenants", list_tenants)
    app.router.add_post("/auth/tenant", select_tenant)
    app.router.add_put("/auth/account", account)
    app.router.add_delete("/auth/account", account_delete)
    app.router.add_get("/health", health)
    if config.static_dir is not None:
        app.router.add_get("/", _index)
        for name in sorted(_STATIC_FILES):
            path = config.static_dir / name
            if path.is_file():
                app.router.add_get(f"/{name}", _favicon(path))
        for prefix in _STATIC_PREFIXES:
            directory = config.static_dir / prefix.rstrip("/")
            if directory.is_dir():
                app.router.add_static(f"/{prefix}", directory)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app
