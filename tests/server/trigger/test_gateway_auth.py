"""Gateway auth tests: Origin allowlist + bootstrap token.

The audit's threat model is a local web page (any site the user visits) driving
the loopback API: wildcard CORS + zero auth let it read `.env`, stream logs and
approve HITL tool calls over the agent WebSocket. Locked here:

- the Origin allowlist is enforced (403 for unlisted origins, wildcard gone);
- a WRONG token is refused (401) and never silently replaced;
- strict mode refuses token-less requests except the ``/auth/token`` bootstrap;
- WebSocket handshakes require the token (browsers cannot set WS headers, so it
  travels as ``?token=``) — all four sockets;
- the global exception handler no longer echoes unexpected exception text.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from config.features import GATEWAY
from server.trigger import auth
from server.trigger import core as trigger_core
from server.trigger.ws import messages as ws_messages
from server.trigger.ws.push_channel import WSPushChannel

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(
        self, path: str = "/sessions", origin: str | None = None, token: str | None = None
    ):
        self.headers: dict[str, str] = {}
        if origin is not None:
            self.headers["Origin"] = origin
        if token is not None:
            self.headers["token"] = token
        self.url = SimpleNamespace(path=path)


class _RejectableWs:
    """WebSocket double: records close(), serves exactly one frame."""

    def __init__(self, params: dict[str, str] | None = None):
        self.query_params = params or {}
        self.id = "ws-test"
        self.closed = False
        self.sent: list[str] = []

    async def close(self) -> None:
        self.closed = True

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        # End the serve loop on the first read (mirrors a client that left).
        from robyn import WebSocketDisconnect

        raise WebSocketDisconnect()


def _body(response) -> dict:
    return json.loads(response.description)


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------


def test_default_allowlist_covers_tauri_and_dev_origins():
    origins = auth.allowed_origins()
    assert "*" not in origins, "the wildcard CORS origin must stay gone"
    assert "tauri://localhost" in origins
    assert "http://tauri.localhost" in origins
    assert "http://localhost:3000" in origins


def test_check_http_matrix():
    token = auth.gateway_token()

    # No Origin (scripts, same-origin webview traffic) passes.
    assert auth.check_http(None, None, "/sessions") is None
    # Allowlisted origin, no token yet: the bootstrap request.
    assert auth.check_http("tauri://localhost", None, "/sessions") is None
    # Unlisted origin: refused regardless of the token.
    assert auth.check_http("https://evil.example", None, "/sessions") == (
        403,
        "Origin not allowed",
    )
    assert auth.check_http("https://evil.example", token, "/sessions") == (
        403,
        "Origin not allowed",
    )
    # Wrong credential is never accepted.
    status, _message = auth.check_http("tauri://localhost", "not-the-token", "/sessions")
    assert status == 401


def test_check_http_strict_mode_requires_the_token(monkeypatch):
    monkeypatch.setitem(GATEWAY, "require_token", True)

    status, message = auth.check_http("tauri://localhost", None, "/sessions")
    assert status == 401
    assert message == "Token required"
    # The bootstrap endpoint stays reachable so a client can still get one.
    assert auth.check_http("tauri://localhost", None, auth.BOOTSTRAP_PATH) is None
    assert auth.check_http(None, auth.gateway_token(), "/sessions") is None


def test_check_ws_always_requires_a_valid_token():
    token = auth.gateway_token()
    assert auth.check_ws(token) is None
    assert auth.check_ws(None) is not None
    assert auth.check_ws("wrong") is not None


def test_token_matches_rejects_empty_and_falsey():
    assert auth.token_matches(auth.gateway_token()) is True
    assert auth.token_matches(None) is False
    assert auth.token_matches("") is False


# ---------------------------------------------------------------------------
# HTTP wiring (before_request middleware + bootstrap endpoint)
# ---------------------------------------------------------------------------


def test_middleware_refuses_an_unlisted_origin_with_403():
    response = trigger_core.gateway_auth_middleware(_FakeRequest(origin="https://evil.example"))

    assert int(response.status_code) == 403
    assert _body(response) == {"success": False, "message": "Origin not allowed"}


def test_middleware_refuses_a_wrong_token_with_401():
    response = trigger_core.gateway_auth_middleware(
        _FakeRequest(origin="tauri://localhost", token="nope")
    )

    assert int(response.status_code) == 401
    assert _body(response)["message"] == "Invalid token"


def test_middleware_passes_allowed_requests_through_unchanged():
    request = _FakeRequest(origin="tauri://localhost")

    assert trigger_core.gateway_auth_middleware(request) is request


def test_bootstrap_endpoint_hands_out_the_same_token():
    # The route decorator wraps the handler: the module attribute returns the
    # same JSON Robyn sends (the payload rides in ``description``).
    response = asyncio.run(trigger_core.gateway_token_handler(_FakeRequest()))

    assert _body(response) == {"token": auth.gateway_token()}


# ---------------------------------------------------------------------------
# WebSocket wiring
# ---------------------------------------------------------------------------


def test_sessions_ws_on_connect_rejects_a_token_less_handshake(monkeypatch):
    registered: list[str] = []
    monkeypatch.setattr(
        trigger_core.relation_register,
        "register_websocket",
        lambda session_id, websocket: registered.append(session_id),
    )
    ws = _RejectableWs({"session_id": "default"})

    asyncio.run(trigger_core.handle_connect(ws))

    assert ws.closed is True
    assert registered == []
    assert ws.sent == []


def test_sessions_ws_on_connect_accepts_the_gateway_token(monkeypatch):
    registered: list[str] = []
    monkeypatch.setattr(
        trigger_core.relation_register,
        "register_websocket",
        lambda session_id, websocket: registered.append(session_id),
    )
    ws = _RejectableWs({"session_id": "default", "token": auth.gateway_token()})

    asyncio.run(trigger_core.handle_connect(ws))

    assert ws.closed is False
    assert registered == ["default"]


def test_push_channel_rejects_a_token_less_handshake():
    channel = WSPushChannel()
    ws = _RejectableWs()

    asyncio.run(
        channel.serve(ws, ensure_registered=lambda: None, handler_label="Test", client_label="Test")
    )

    assert ws.closed is True
    assert ws.sent == [], "a refused socket must not even receive the ready frame"
    assert channel._subscribers == set()
    assert channel._pending == {}


def test_push_channel_serves_a_token_carrying_handshake():
    channel = WSPushChannel()
    registered: list[int] = []
    ws = _RejectableWs({"token": auth.gateway_token()})

    asyncio.run(
        channel.serve(
            ws,
            ensure_registered=lambda: registered.append(1),
            handler_label="Test",
            client_label="Test",
        )
    )

    assert ws.closed is False
    assert registered == [1]
    assert json.loads(ws.sent[0]) == {"event": "ready"}


def test_agent_ws_rejects_a_token_less_handshake():
    ws = _RejectableWs()

    asyncio.run(ws_messages.agent_ws_handler(ws))

    assert ws.closed is True
    assert ws.sent == []


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


def test_exception_handler_keeps_contract_errors_and_hides_internals():
    contract = trigger_core.handle_exception(ValueError("turn_page_size must be <= 200"))
    assert _body(contract)["error"] == "turn_page_size must be <= 200"

    internal = trigger_core.handle_exception(OSError("/srv/secrets/token.txt not readable"))
    body = _body(internal)
    assert body["error"] == "OSError"
    assert "/srv/secrets" not in json.dumps(body)
