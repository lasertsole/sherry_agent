"""The login gate middleware: who is gated, who is exempt, what a refusal is.

Contract under test — the zero-impact promise plus the security boundary:

* with nothing configured the middleware is a pure pass-through for every path;
* once protection is on, a remote caller without a session gets a 401 *JSON*
  refusal for any non-public path — including paths that match no route, so a
  future route cannot leak by omission — while loopback callers pass;
* the exemption is configurable (``require_auth_non_loopback=False`` gates the
  local machine too);
* an enabled switch with no account still passes (nothing to log in with);
* WebSockets spend a single-use ticket instead of an address they do not have.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from config.features import AUTH
from server.DAO import auth_store
from server.service import auth_service
from server.trigger import auth_user

pytestmark = [pytest.mark.unit]


class _FakeRequest:
    def __init__(
        self,
        path: str,
        *,
        method: str = "GET",
        ip: str | None = "203.0.113.9",
        cookie: str | None = None,
    ) -> None:
        self.method = method
        self.url = type("_Url", (), {"path": path})()
        self.headers = {"Cookie": cookie} if cookie else {}
        self.ip_addr = ip


def _run(request):
    return asyncio.run(auth_user.user_auth_middleware(request))


async def _enable(username: str = "admin", password: str = "hunter2-long") -> str:
    """Configure an account and return a valid access token for it."""
    await auth_service.setup_account(username, password)
    pair = await auth_service.login(username, password)
    return pair.access_token


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.53", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("[::1]", True),
        ("10.0.0.5", False),
        ("192.168.1.20", False),
        ("", False),
        (None, False),
        ("not-an-address", False),
    ],
)
def test_is_loopback(address, expected) -> None:
    assert auth_user.is_loopback(address) is expected


def test_cookie_value_reads_one_name_and_tolerates_noise() -> None:
    header = "a=1; sherry_session=token-1; sherry_refresh=token-2; b=2"

    assert auth_user.cookie_value(header, "sherry_session") == "token-1"
    assert auth_user.cookie_value(header, "sherry_refresh") == "token-2"
    assert auth_user.cookie_value(header, "missing") is None
    assert auth_user.cookie_value(None, "sherry_session") is None
    assert auth_user.cookie_value("sherry_session=", "sherry_session") is None


@pytest.mark.parametrize(
    ("path", "gated"),
    [
        ("/auth/status", False),
        ("/auth/login", False),
        ("/auth/refresh", False),
        ("/auth/token", False),
        ("/static/app.js", False),
        ("/static", False),
        ("/favicon.ico", False),
        ("/sessions", True),
        ("/auth/me", True),
        ("/auth/account", True),
        ("/auth/ws-ticket", True),
        ("/images/a.png", True),
        ("/project/tree", True),
        ("/", True),
        ("/some-future-route", True),
    ],
)
def test_should_gate(path, gated) -> None:
    assert auth_user.should_gate(path) is gated


# ── the gate itself ──────────────────────────────────────────────────────────


def test_nothing_configured_passes_everything_through(isolated_auth_store) -> None:
    request = _FakeRequest("/sessions", cookie=None)

    assert _run(request) is request


def test_remote_without_a_session_is_refused_with_json(isolated_auth_store) -> None:
    asyncio.run(_enable())
    request = _FakeRequest("/sessions")

    response = _run(request)

    assert response.status_code == 401
    payload = json.loads(response.description)
    assert payload["error"] == "not_authenticated"
    assert response.headers["Content-Type"] == "application/json"
    # The refusal still carries the security headers.
    assert (
        "X-Content-Type-Options" in response.headers
        or "Content-Security-Policy" in response.headers
    )


def test_remote_with_a_valid_session_passes(isolated_auth_store) -> None:
    token = asyncio.run(_enable())
    request = _FakeRequest("/sessions", cookie=f"{AUTH['cookie_name']}={token}; other=1")

    assert _run(request) is request


def test_loopback_passes_even_with_protection_on(isolated_auth_store) -> None:
    asyncio.run(_enable())
    request = _FakeRequest("/sessions", ip="127.0.0.1")

    assert _run(request) is request


def test_the_loopback_exemption_can_be_switched_off(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(AUTH, "require_auth_non_loopback", False)
    asyncio.run(_enable())

    response = _run(_FakeRequest("/sessions", ip="127.0.0.1"))

    assert response.status_code == 401


def test_an_enabled_switch_without_an_account_passes(isolated_auth_store) -> None:
    asyncio.run(auth_store.set_auth_enabled(True))
    auth_service.clear_state_cache()

    request = _FakeRequest("/sessions")
    assert _run(request) is request


def test_public_paths_pass_without_a_session(isolated_auth_store) -> None:
    asyncio.run(_enable())

    for path in ("/auth/status", "/auth/login", "/auth/refresh", "/auth/token", "/static/app.js"):
        assert _run(_FakeRequest(path)) is not None


def test_a_revoked_access_token_is_refused(isolated_auth_store) -> None:
    async def _revoke() -> str:
        await auth_service.setup_account("admin", "hunter2-long")
        pair = await auth_service.login("admin", "hunter2-long")
        await auth_service.logout(pair.access_token)
        return pair.access_token

    token = asyncio.run(_revoke())
    response = _run(_FakeRequest("/sessions", cookie=f"{AUTH['cookie_name']}={token}"))

    assert response.status_code == 401


def test_preflight_requests_are_never_gated(isolated_auth_store) -> None:
    asyncio.run(_enable())
    request = _FakeRequest("/sessions", method="OPTIONS")

    assert _run(request) is request


# ── WebSocket tickets ────────────────────────────────────────────────────────


def test_ws_check_passes_when_nothing_enforces(isolated_auth_store) -> None:
    assert asyncio.run(auth_user.ws_user_check({})) is None


def test_ws_check_requires_a_single_use_ticket_once_enforced(isolated_auth_store) -> None:
    async def _scenario() -> tuple[str | None, str | None, str | None]:
        await auth_service.setup_account("admin", "hunter2-long")
        ticket, _ = await auth_service.mint_ws_ticket()
        missing = await auth_user.ws_user_check({})
        first = await auth_user.ws_user_check({"ticket": ticket})
        replay = await auth_user.ws_user_check({"ticket": ticket})
        return missing, first, replay

    missing, first, replay = asyncio.run(_scenario())

    assert missing == "missing ws ticket"
    assert first is None
    assert replay == "invalid or expired ws ticket"
