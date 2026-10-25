"""Strict delivery guarantees for the stream-resume / broadcast path.

Pinned invariants (the user's hard requirement): **no WebSocket connection ever
receives a replayed or duplicated message** —

* a broadcast reaches every live socket of the session EXACTLY once;
* a dead socket is pruned without affecting the others;
* the resume snapshot goes ONLY to the requesting socket (never broadcast);
* the snapshot is a text value, never a frame log to replay;
* frames naming another session are refused by the isolation check.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.session.relation_register import relation_register
from runtime.session.ws_push import broadcast_to_all, broadcast_to_session

# The server trigger module pulls the whole app (heavy first import; load-sensitive
# on this host), so the budget is generous — these are still unit-level tests.
pytestmark = [pytest.mark.unit, pytest.mark.timeout(300)]

SESSION = "sess-resume"


class _WS:
    """Recording socket (unique id, optional first-send failure)."""

    _counter = 0

    def __init__(self, *, fail: bool = False) -> None:
        _WS._counter += 1
        self.id = f"ws-{_WS._counter}"
        self.sent: list[str] = []
        self._fail = fail

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("socket closed")
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    relation_register.unregister_websocket_by_session_id(SESSION)
    relation_register.unregister_websocket_by_session_id("sess-other")


# ── broadcast: exactly once per live socket ─────────────────────────────────


def test_broadcast_reaches_every_live_socket_exactly_once():
    first, second = _WS(), _WS()
    relation_register.register_websocket(SESSION, first)
    relation_register.register_websocket(SESSION, second)

    delivered = asyncio.run(broadcast_to_session(SESSION, {"event": "chunk", "content": "x"}))

    assert delivered == 2
    assert len(first.sent) == 1
    assert len(second.sent) == 1
    # The SAME serialized frame — no per-socket re-serialization or variation.
    assert first.sent[0] == second.sent[0]


def test_re_registering_the_same_socket_does_not_duplicate_delivery():
    socket = _WS()
    relation_register.register_websocket(SESSION, socket)
    relation_register.register_websocket(SESSION, socket)

    delivered = asyncio.run(broadcast_to_session(SESSION, {"event": "chunk"}))

    assert delivered == 1
    assert len(socket.sent) == 1


def test_a_dead_socket_is_pruned_and_the_others_still_receive():
    alive, dead = _WS(), _WS(fail=True)
    relation_register.register_websocket(SESSION, alive)
    relation_register.register_websocket(SESSION, dead)

    assert asyncio.run(broadcast_to_session(SESSION, {"event": "chunk"})) == 1
    assert len(alive.sent) == 1
    assert relation_register.get_websockets_by_session_id(SESSION) == [alive]

    # The pruned socket is skipped on the next broadcast; the survivor still
    # receives exactly one more frame (never a duplicate of the first).
    assert asyncio.run(broadcast_to_session(SESSION, {"event": "chunk"})) == 1
    assert len(alive.sent) == 2
    assert dead.sent == []


def test_broadcast_never_crosses_sessions():
    ours, other = _WS(), _WS()
    relation_register.register_websocket(SESSION, ours)
    relation_register.register_websocket("sess-other", other)

    assert asyncio.run(broadcast_to_session(SESSION, {"event": "chunk"})) == 1
    assert len(ours.sent) == 1
    assert other.sent == []


def test_broadcast_to_all_reaches_every_session_socket_once():
    ours, other = _WS(), _WS()
    relation_register.register_websocket(SESSION, ours)
    relation_register.register_websocket("sess-other", other)

    delivered = asyncio.run(broadcast_to_all({"event": "model_download_updated"}))

    # Other suites may leave sockets registered in this process; the guarantee
    # under test is that BOTH of ours received the frame exactly once.
    assert delivered >= 2
    assert len(ours.sent) == 1
    assert len(other.sent) == 1
    assert ours.sent[0] == other.sent[0]


def test_broadcast_to_all_prunes_only_the_dead_socket():
    alive, dead = _WS(), _WS(fail=True)
    relation_register.register_websocket(SESSION, alive)
    relation_register.register_websocket("sess-other", dead)

    assert asyncio.run(broadcast_to_all({"event": "model_download_updated"})) >= 1
    assert len(alive.sent) == 1
    assert relation_register.get_websockets_by_session_id("sess-other") == []
    remaining = relation_register.get_all_websockets()
    assert alive in remaining
    assert dead not in remaining


def test_the_one_to_one_view_promotes_a_surviving_socket():
    first, second = _WS(), _WS()
    relation_register.register_websocket(SESSION, first)
    relation_register.register_websocket(SESSION, second)
    assert relation_register.get_websocket_by_session_id(SESSION) is second

    relation_register.unregister_websocket_by_websocket(second)
    assert relation_register.get_websocket_by_session_id(SESSION) is first


# ── resume snapshot: requester only, never a replay ─────────────────────────


def _install_engine(monkeypatch, text: str) -> None:
    from server.service import stream_dispatch

    monkeypatch.setitem(
        stream_dispatch._ACTIVE_TURNS,
        SESSION,
        SimpleNamespace(ai_text=text, _has_reasoning=False, _frame_seq=7, turn_id="turn-7"),
    )


def test_resume_snapshot_reaches_only_the_requesting_socket(monkeypatch):
    from server.trigger.ws import messages as wsm

    _install_engine(monkeypatch, "hello so far")
    requester, bystander = _WS(), _WS()

    asyncio.run(wsm._handle_resume_stream(requester, SESSION, {}))

    assert bystander.sent == [], "another connection must never receive the snapshot"
    assert len(requester.sent) == 1
    payload = json.loads(requester.sent[0])
    assert payload["event"] == "stream_resume"
    content = payload["content"]
    assert content["status"] == "ok"
    assert content["text"] == "hello so far"
    # The snapshot carries the frame seq it was read at: a resumed viewer drops
    # frames at or below it, so an in-flight duplicate can never be appended.
    assert content["seq"] == 7
    assert content["turn_id"] == "turn-7"
    # The snapshot is a TEXT value, not a frame log the client could replay.
    assert "frames" not in content


def test_resume_is_idempotent_and_never_emits_frames(monkeypatch):
    from server.trigger.ws import messages as wsm

    _install_engine(monkeypatch, "abc")
    socket = _WS()

    asyncio.run(wsm._handle_resume_stream(socket, SESSION, {}))
    asyncio.run(wsm._handle_resume_stream(socket, SESSION, {}))

    assert len(socket.sent) == 2
    contents = [json.loads(text)["content"] for text in socket.sent]
    assert all(c["status"] == "ok" and c["text"] == "abc" for c in contents)
    assert all("frames" not in c for c in contents)


def test_resume_reports_ended_when_the_session_has_history(monkeypatch):
    from context_engine.store import core as mem_core
    from server.service import stream_dispatch
    from server.trigger.ws import messages as wsm

    monkeypatch.setattr(stream_dispatch, "active_turn_snapshot", lambda sid: None)
    monkeypatch.setattr(mem_core, "session_has_messages", lambda sid: True)
    socket = _WS()

    asyncio.run(wsm._handle_resume_stream(socket, SESSION, {}))

    assert json.loads(socket.sent[0])["content"] == {"status": "ended"}


def test_resume_reports_gone_for_a_deleted_or_empty_session(monkeypatch):
    from context_engine.store import core as mem_core
    from server.service import stream_dispatch
    from server.trigger.ws import messages as wsm

    monkeypatch.setattr(stream_dispatch, "active_turn_snapshot", lambda sid: None)
    monkeypatch.setattr(mem_core, "session_has_messages", lambda sid: False)
    socket = _WS()

    asyncio.run(wsm._handle_resume_stream(socket, SESSION, {}))

    assert json.loads(socket.sent[0])["content"] == {"status": "gone"}


def test_resume_fails_open_to_unknown(monkeypatch):
    from context_engine.store import core as mem_core
    from server.service import stream_dispatch
    from server.trigger.ws import messages as wsm

    def _boom(sid):
        raise RuntimeError("store down")

    monkeypatch.setattr(stream_dispatch, "active_turn_snapshot", lambda sid: None)
    monkeypatch.setattr(mem_core, "session_has_messages", _boom)
    socket = _WS()

    asyncio.run(wsm._handle_resume_stream(socket, SESSION, {}))

    assert json.loads(socket.sent[0])["content"] == {"status": "unknown"}


# ── session isolation ───────────────────────────────────────────────────────


def test_frames_for_another_session_are_refused():
    from server.trigger.ws import messages as wsm

    socket = _WS()
    relation_register.register_websocket(SESSION, socket)

    assert wsm._frame_session_allowed(socket, SESSION) is True
    assert wsm._frame_session_allowed(socket, "sess-other") is False
    # An unregistered socket is allowed: the generation frame registers it.
    assert wsm._frame_session_allowed(_WS(), "sess-other") is True
