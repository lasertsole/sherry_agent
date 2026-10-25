"""A core socket disconnect must not wipe a session that still has a socket."""

from __future__ import annotations

import pytest

from runtime import relation_register
from server.trigger import core

pytestmark = pytest.mark.unit


class _Socket:
    def __init__(self, ws_id: str) -> None:
        self.id = ws_id


@pytest.mark.asyncio
async def test_one_socket_closing_keeps_the_session_registers(monkeypatch):
    sid = "disconnect-multi-socket"
    first, second = _Socket("ws-first"), _Socket("ws-second")
    relation_register.register_websocket(sid, first)
    relation_register.register_websocket(sid, second)
    cleared: list[str] = []
    monkeypatch.setattr(
        core, "clear_all_register_sessions", lambda **kw: cleared.append(kw["session_id"])
    )
    try:
        await core.handle_disconnect(first)

        assert cleared == [], "the surviving socket owns the session registers now"
        assert [ws.id for ws in relation_register.get_websockets_by_session_id(sid)] == [
            "ws-second"
        ]

        await core.handle_disconnect(second)
        assert cleared == [sid], "the last socket closing performs the cleanup"
    finally:
        relation_register.clear_session(sid)


@pytest.mark.asyncio
async def test_a_lone_socket_closing_still_clears(monkeypatch):
    sid = "disconnect-single-socket"
    only = _Socket("ws-only")
    relation_register.register_websocket(sid, only)
    cleared: list[str] = []
    monkeypatch.setattr(
        core, "clear_all_register_sessions", lambda **kw: cleared.append(kw["session_id"])
    )
    try:
        await core.handle_disconnect(only)
        assert cleared == [sid]
    finally:
        relation_register.clear_session(sid)
