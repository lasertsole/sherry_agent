"""``GET /health``: the process self-check (design concept #22).

The endpoint is the client's HTTP fallback when the socket has no verdict, so
its contract is: always 200, always ``ok: true``, every collector individually
optional (``null`` on failure), and public at BOTH gate layers — the login
middleware's ``PUBLIC_PATHS`` and the gateway's strict-mode token exemption.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config.features import GATEWAY
from server.trigger import auth
from server.trigger import auth_user
from server.trigger.http import health as health_route

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, path: str = "/health", token: str | None = None):
        self.headers: dict[str, str] = {}
        if token is not None:
            self.headers["token"] = token
        self.url = SimpleNamespace(path=path)


def test_health_payload_shape() -> None:
    payload = asyncio.run(health_route.health_payload())

    assert payload["ok"] is True
    assert isinstance(payload["version"], str) and payload["version"]
    assert isinstance(payload["uptime_s"], int) and payload["uptime_s"] >= 0
    assert isinstance(payload["models_configured"], bool)
    # The gauges are process-state dependent; their CONTRACT is "int or None".
    assert payload["live_sessions"] is None or isinstance(payload["live_sessions"], int)
    assert payload["queue_depth"] is None or isinstance(payload["queue_depth"], int)


def test_a_failing_gauge_degrades_to_null_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> int:
        raise RuntimeError("register exploded")

    monkeypatch.setattr(health_route, "_live_session_count", _boom)

    payload = asyncio.run(health_route.health_payload())

    assert payload["ok"] is True
    assert payload["live_sessions"] is None


def test_health_is_public_at_the_login_layer() -> None:
    assert auth_user.should_gate("/health") is False
    assert auth_user.should_gate("/health/") is False


def test_health_is_exempt_from_the_strict_mode_token_but_not_from_an_invalid_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(GATEWAY, "require_token", True)

    assert auth.check_http(origin=None, presented_token=None, path="/health") is None
    # An INVALID token is still refused — "public" means no bootstrap needed.
    verdict = auth.check_http(origin="https://evil.example", presented_token=None, path="/health")
    assert verdict is not None and verdict[0] == 403


def test_the_route_answers_200_ok() -> None:
    response = asyncio.run(health_route.health_handler(_FakeRequest()))

    assert response.status_code == 200
    body = str(response.description)
    assert '"ok"' in body and "true" in body
