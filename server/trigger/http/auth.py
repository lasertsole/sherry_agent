"""Login HTTP routes (``/auth/*``).

Cookie policy: the access token travels in an HttpOnly
``sherry_session`` cookie scoped to ``/``, the refresh token in an HttpOnly
``sherry_refresh`` cookie scoped to ``/auth/refresh`` so it is not attached to
every request, and the client never sees either string — the login/refresh
bodies carry only ``expires_in`` and the public user.

Account management is a trusted-operator action: a caller that is NOT gated by
the login middleware (loopback, or auth switched off) may manage the single
account without a session — it must still prove the current password before any
change, so that rule holds even for the local operator.
"""

from __future__ import annotations

from robyn import Request, Response

from config.features import AUTH
from server.service import auth_service
from server.service.auth_service import AuthError, TokenPair
from server.trigger import auth_user
from server.trigger.core import app
from server.trigger.http.helpers import ok, read_body, to_text_response


def _same_site() -> str:
    """Robyn serializes the canonical spellings, so normalize the config value."""
    configured = str(AUTH["cookie_samesite"] or "strict").strip().lower()
    return {"lax": "Lax", "none": "None"}.get(configured, "Strict")


def _set_session_cookies(response: Response, pair: TokenPair) -> Response:
    """Attach the two HttpOnly cookies to a response."""
    response.set_cookie(
        key=AUTH["cookie_name"],
        value=pair.access_token,
        path=AUTH["cookie_path"],
        max_age=int(AUTH["session_ttl_seconds"]),
        secure=bool(AUTH["cookie_secure"]),
        http_only=True,
        same_site=_same_site(),
    )
    response.set_cookie(
        key=AUTH["refresh_cookie_name"],
        value=pair.refresh_token,
        path=AUTH["refresh_cookie_path"],
        max_age=int(AUTH["refresh_ttl_seconds"]),
        secure=bool(AUTH["cookie_secure"]),
        http_only=True,
        same_site=_same_site(),
    )
    return response


def _clear_session_cookies(response: Response) -> Response:
    """Expire both cookies (same paths, so the browser replaces them)."""
    response.set_cookie(
        key=AUTH["cookie_name"],
        value="",
        path=AUTH["cookie_path"],
        max_age=0,
        secure=bool(AUTH["cookie_secure"]),
        http_only=True,
        same_site=_same_site(),
    )
    response.set_cookie(
        key=AUTH["refresh_cookie_name"],
        value="",
        path=AUTH["refresh_cookie_path"],
        max_age=0,
        secure=bool(AUTH["cookie_secure"]),
        http_only=True,
        same_site=_same_site(),
    )
    return response


def _auth_error(exc: AuthError) -> Response:
    return to_text_response(
        exc.status, {"success": False, "error": exc.code, "message": exc.message}
    )


def _access_token(request: Request) -> str:
    return auth_user.cookie_value(request.headers.get("Cookie"), AUTH["cookie_name"]) or ""


def _refresh_token(request: Request) -> str:
    return auth_user.cookie_value(request.headers.get("Cookie"), AUTH["refresh_cookie_name"]) or ""


async def _acting_user(request: Request) -> dict | None:
    """The account these account-management routes operate on.

    A signed-in caller acts on its own account. A caller the login middleware
    does not gate (loopback / auth off) acts on the single configured account —
    the local operator pattern the first-run flow needs.
    """
    session_user = await auth_service.current_user(_access_token(request))
    if session_user is not None:
        return session_user
    loopback = auth_user.is_loopback(getattr(request, "ip_addr", None))
    if loopback or not await auth_service.enforcement_active():
        return await auth_service.sole_account()
    return None


@app.get("/auth/status")
async def auth_status_handler(request: Request):
    """Whether this client must log in (the client's pre-navigation probe)."""
    info = await auth_service.status(
        remote_is_loopback=auth_user.is_loopback(getattr(request, "ip_addr", None))
    )
    return ok({"success": True, **info})


@app.post("/auth/login")
async def auth_login_handler(request: Request):
    """Verify credentials, then hand out the two HttpOnly cookies."""
    body = read_body(request) or {}
    try:
        pair = await auth_service.login(
            str(body.get("username") or ""), str(body.get("password") or "")
        )
    except AuthError as exc:
        return _auth_error(exc)
    return _set_session_cookies(
        ok({"success": True, "expires_in": pair.expires_in, "user": pair.user}), pair
    )


@app.post("/auth/refresh")
async def auth_refresh_handler(request: Request):
    """Rotate the session: a fresh pair for a valid refresh cookie."""
    try:
        pair = await auth_service.refresh(_refresh_token(request))
    except AuthError as exc:
        return _auth_error(exc)
    return _set_session_cookies(
        ok({"success": True, "expires_in": pair.expires_in, "user": pair.user}), pair
    )


@app.post("/auth/logout")
async def auth_logout_handler(request: Request):
    """Revoke the presented tokens and clear the cookies."""
    await auth_service.logout(_access_token(request), _refresh_token(request) or None)
    return _clear_session_cookies(ok({"success": True}))


@app.get("/auth/me")
async def auth_me_handler(request: Request):
    """The signed-in user (401 when there is no valid session)."""
    user = await auth_service.current_user(_access_token(request))
    if user is None:
        return to_text_response(
            401, {"success": False, "error": "not_authenticated", "message": "not signed in"}
        )
    return ok({"success": True, "user": user})


@app.put("/auth/account")
async def auth_account_handler(request: Request):
    """First-time setup, or change the username / password of the account."""
    body = read_body(request) or {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    new_username = body.get("new_username")
    new_password = body.get("new_password")
    try:
        if username:
            # Setup path: only reachable while no account exists.
            user = await auth_service.setup_account(username, password)
            return ok({"success": True, "user": user})
        acting = await _acting_user(request)
        if acting is None:
            return to_text_response(
                401, {"success": False, "error": "not_authenticated", "message": "not signed in"}
            )
        if body.get("enabled") is True:
            # Re-enable a disabled switch; the account itself is unchanged.
            await auth_service.enable_auth(acting, password)
            return ok({"success": True, "user": acting})
        user = await auth_service.update_account(
            acting,
            password=password,
            new_username=str(new_username) if new_username is not None else None,
            new_password=str(new_password) if new_password is not None else None,
        )
    except AuthError as exc:
        return _auth_error(exc)
    return ok({"success": True, "user": user})


@app.delete("/auth/account")
async def auth_disable_handler(request: Request):
    """Turn login protection off — the current password is required."""
    body = read_body(request) or {}
    acting = await _acting_user(request)
    if acting is None:
        return to_text_response(
            401, {"success": False, "error": "not_authenticated", "message": "not signed in"}
        )
    try:
        await auth_service.disable_auth(acting, str(body.get("password") or ""))
    except AuthError as exc:
        return _auth_error(exc)
    return _clear_session_cookies(ok({"success": True}))


@app.get("/auth/ws-ticket")
async def auth_ws_ticket_handler(request: Request):
    """Mint a single-use WebSocket handshake ticket.

    The loopback decision lives HERE because only HTTP requests carry the peer
    address: a gated (remote) caller must present a session, a local one mints
    one exactly the way it may talk to the API without logging in.
    """
    loopback = auth_user.is_loopback(getattr(request, "ip_addr", None))
    if await auth_service.enforcement_active() and not loopback:
        if await auth_service.current_user(_access_token(request)) is None:
            return to_text_response(
                401, {"success": False, "error": "not_authenticated", "message": "not signed in"}
            )
    ticket, ttl = await auth_service.mint_ws_ticket()
    return ok({"success": True, "ticket": ticket, "expires_in": ttl})
