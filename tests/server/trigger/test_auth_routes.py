"""The ``/auth/*`` routes: cookie shape, refusal codes, operator trust.

Contract under test: login hands out two HttpOnly cookies (scoped per the plan)
and never a token in the body, refresh rotates them, logout clears them and
revokes, account management follows the setup/update split with the password
checks the plan requires (R4), and the WS-ticket route makes the loopback
decision that WebSockets cannot make for themselves.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from config.features import AUTH
from server.DAO import auth_store
from server.service import auth_service
from server.trigger.http import auth as routes

pytestmark = [pytest.mark.unit]

LOCAL = "127.0.0.1"
REMOTE = "203.0.113.9"


class _FakeRequest:
    def __init__(self, *, ip: str | None = REMOTE, cookie: str | None = None, body=None) -> None:
        self.method = "POST"
        self.url = type("_Url", (), {"path": "/auth/whatever"})()
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        self.headers = headers
        self.ip_addr = ip
        self._body = body or {}

    def json(self):
        return self._body


def _call(handler, request):
    return asyncio.run(handler(request))


def _payload(response) -> dict:
    return json.loads(response.description)


def _cookie(response, name: str):
    return response.cookies.get(name)


async def _account(username: str = "admin", password: str = "hunter2-long") -> None:
    await auth_service.setup_account(username, password)


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reports_the_three_flags_for_a_remote_caller(isolated_auth_store) -> None:
    payload = _payload(_call(routes.auth_status_handler, _FakeRequest(ip=REMOTE)))

    assert payload == {
        "success": True,
        "auth_enabled": False,
        "auth_required": False,
        "has_account": False,
    }


def test_status_never_requires_login_on_loopback(isolated_auth_store) -> None:
    asyncio.run(_account())

    payload = _payload(_call(routes.auth_status_handler, _FakeRequest(ip=LOCAL)))

    assert payload["auth_enabled"] is True
    assert payload["has_account"] is True
    assert payload["auth_required"] is False


def test_status_requires_login_for_remote_after_setup(isolated_auth_store) -> None:
    asyncio.run(_account())

    assert (
        _payload(_call(routes.auth_status_handler, _FakeRequest(ip=REMOTE)))["auth_required"]
        is True
    )


# ── login / refresh / logout ─────────────────────────────────────────────────


def test_login_sets_the_two_httponly_cookies_and_no_token_in_the_body(
    isolated_auth_store,
) -> None:
    asyncio.run(_account())

    response = _call(
        routes.auth_login_handler,
        _FakeRequest(body={"username": "admin", "password": "hunter2-long"}),
    )

    assert response.status_code == 200
    payload = _payload(response)
    assert payload["success"] is True
    assert payload["user"]["username"] == "admin"
    assert payload["expires_in"] == AUTH["session_ttl_seconds"]
    assert "token" not in json.dumps(payload).lower().replace('"access', "")

    access = _cookie(response, AUTH["cookie_name"])
    refresh = _cookie(response, AUTH["refresh_cookie_name"])
    assert access is not None and access.value
    assert refresh is not None and refresh.value
    assert access.http_only is True
    assert refresh.http_only is True
    assert access.path == "/"
    assert refresh.path == AUTH["refresh_cookie_path"]
    assert access.max_age == AUTH["session_ttl_seconds"]
    assert refresh.max_age == AUTH["refresh_ttl_seconds"]


def test_login_refusal_is_401_with_a_code_and_no_cookies(isolated_auth_store) -> None:
    asyncio.run(_account())

    response = _call(
        routes.auth_login_handler,
        _FakeRequest(body={"username": "admin", "password": "wrong-password"}),
    )

    assert response.status_code == 401
    assert _payload(response)["error"] == "invalid_credentials"
    assert _cookie(response, AUTH["cookie_name"]) is None


def test_refresh_rotates_the_cookies(isolated_auth_store) -> None:
    async def _login() -> str:
        await _account()
        pair = await auth_service.login("admin", "hunter2-long")
        return f"{AUTH['refresh_cookie_name']}={pair.refresh_token}"

    cookie = asyncio.run(_login())
    response = _call(routes.auth_refresh_handler, _FakeRequest(cookie=cookie))

    assert response.status_code == 200
    rotated = _cookie(response, AUTH["refresh_cookie_name"])
    assert rotated is not None and rotated.value
    assert rotated.value != cookie.split("=", 1)[1]


def test_refresh_without_a_cookie_is_401(isolated_auth_store) -> None:
    response = _call(routes.auth_refresh_handler, _FakeRequest())

    assert response.status_code == 401
    assert _payload(response)["error"] == "invalid_refresh_token"


def test_logout_clears_both_cookies_and_revokes(isolated_auth_store) -> None:
    async def _login() -> tuple[str, str]:
        await _account()
        pair = await auth_service.login("admin", "hunter2-long")
        return pair.access_token, pair.refresh_token

    access_token, refresh_token = asyncio.run(_login())
    cookie = f"{AUTH['cookie_name']}={access_token}; {AUTH['refresh_cookie_name']}={refresh_token}"

    response = _call(routes.auth_logout_handler, _FakeRequest(cookie=cookie))

    assert response.status_code == 200
    cleared_access = _cookie(response, AUTH["cookie_name"])
    cleared_refresh = _cookie(response, AUTH["refresh_cookie_name"])
    assert cleared_access is not None and cleared_access.max_age == 0
    assert cleared_refresh is not None and cleared_refresh.max_age == 0
    assert asyncio.run(auth_service.current_user(access_token)) is None


# ── account management ───────────────────────────────────────────────────────


def test_first_time_setup_over_loopback_needs_no_session(isolated_auth_store) -> None:
    response = _call(
        routes.auth_account_handler,
        _FakeRequest(ip=LOCAL, body={"username": "admin", "password": "hunter2-long"}),
    )

    assert response.status_code == 200
    assert _payload(response)["user"]["username"] == "admin"
    assert asyncio.run(auth_service.enforcement_active()) is True


def test_setup_refuses_short_passwords(isolated_auth_store) -> None:
    response = _call(
        routes.auth_account_handler,
        _FakeRequest(ip=LOCAL, body={"username": "admin", "password": "short"}),
    )

    assert response.status_code == 400
    assert _payload(response)["error"] == "password_policy"


def test_local_operator_can_change_the_password_with_the_current_one(
    isolated_auth_store,
) -> None:
    asyncio.run(_account())
    response = _call(
        routes.auth_account_handler,
        _FakeRequest(ip=LOCAL, body={"password": "hunter2-long", "new_password": "brand-new-pass"}),
    )

    assert response.status_code == 200
    assert asyncio.run(auth_service.login("admin", "brand-new-pass")).user["username"] == "admin"


def test_change_with_a_wrong_current_password_is_401(isolated_auth_store) -> None:
    asyncio.run(_account())
    response = _call(
        routes.auth_account_handler,
        _FakeRequest(ip=LOCAL, body={"password": "nope", "new_password": "brand-new-pass"}),
    )

    assert response.status_code == 401
    assert _payload(response)["error"] == "invalid_password"


def test_a_remote_caller_without_a_session_cannot_manage_the_account(
    isolated_auth_store,
) -> None:
    asyncio.run(_account())
    response = _call(
        routes.auth_account_handler,
        _FakeRequest(
            ip=REMOTE, body={"password": "hunter2-long", "new_password": "brand-new-pass"}
        ),
    )

    assert response.status_code == 401
    assert _payload(response)["error"] == "not_authenticated"


def test_disable_requires_the_password_and_turns_the_switch_off(isolated_auth_store) -> None:
    asyncio.run(_account())

    wrong = _call(routes.auth_disable_handler, _FakeRequest(ip=LOCAL, body={"password": "wrong"}))
    assert wrong.status_code == 401
    assert asyncio.run(auth_service.enforcement_active()) is True

    ok = _call(
        routes.auth_disable_handler, _FakeRequest(ip=LOCAL, body={"password": "hunter2-long"})
    )
    assert ok.status_code == 200
    assert asyncio.run(auth_service.enforcement_active()) is False
    # The account survives: logging in still works while protection is off.
    assert asyncio.run(auth_store.count_users()) == 1


def test_me_reports_the_session_user(isolated_auth_store) -> None:
    async def _login() -> str:
        await _account()
        pair = await auth_service.login("admin", "hunter2-long")
        return f"{AUTH['cookie_name']}={pair.access_token}"

    cookie = asyncio.run(_login())

    payload = _payload(_call(routes.auth_me_handler, _FakeRequest(cookie=cookie)))
    assert payload["user"]["username"] == "admin"

    assert _call(routes.auth_me_handler, _FakeRequest()).status_code == 401


# ── WS tickets ───────────────────────────────────────────────────────────────


def test_loopback_mints_a_ticket_without_a_session(isolated_auth_store) -> None:
    asyncio.run(_account())

    payload = _payload(_call(routes.auth_ws_ticket_handler, _FakeRequest(ip=LOCAL)))

    assert payload["success"] is True
    assert payload["ticket"]
    assert payload["expires_in"] == AUTH["ws_ticket_ttl_seconds"]


def test_remote_minting_requires_a_session(isolated_auth_store) -> None:
    asyncio.run(_account())

    assert _call(routes.auth_ws_ticket_handler, _FakeRequest(ip=REMOTE)).status_code == 401

    async def _login() -> str:
        pair = await auth_service.login("admin", "hunter2-long")
        return f"{AUTH['cookie_name']}={pair.access_token}"

    cookie = asyncio.run(_login())
    response = _call(routes.auth_ws_ticket_handler, _FakeRequest(ip=REMOTE, cookie=cookie))
    assert response.status_code == 200
    assert _payload(response)["ticket"]
