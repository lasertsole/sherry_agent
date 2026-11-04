"""Trajectory WS channel: hook wiring, per-session routing and session validation."""

from __future__ import annotations

import asyncio
import json

import pytest

from runtime import hooks
from server.trigger.ws import trajectory_ws

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeSocket:
    def __init__(self, query_params: dict | None = None) -> None:
        self.id = f"ws-{id(self)}"
        self.query_params = query_params or {}
        self.closed = False
        self.sent: list[str] = []

    async def close(self) -> None:
        self.closed = True

    async def send_text(self, frame: str) -> None:
        self.sent.append(frame)


def test_the_module_registers_the_push_hook():
    # The module registers at import AND every handler calls this before serving;
    # another suite in the same process may have cleared the registry since, so
    # the idempotent registration is what the test drives.
    trajectory_ws._ensure_hook_registered()
    assert hooks.resolve(hooks.TRAJECTORY_EVENT_PUSH) is trajectory_ws._push


def test_push_routes_one_event_to_that_sessions_channel(monkeypatch):
    enqueued: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        trajectory_ws._channel,
        "enqueue_frame",
        lambda frame, key=None: enqueued.append((frame, key)),
    )
    event = {
        "event_id": "s1:t1:tool:c1",
        "turn_id": "t1",
        "seq": 2,
        "ts": "2026-11-04T23:00:00.000",
        "kind": "tool_start",
        "is_error": False,
        "summary": "read_file",
        "payload": {"args": {"path": "a.py"}},
    }

    trajectory_ws._push("child-session", event)

    assert len(enqueued) == 1
    frame, key = enqueued[0]
    assert key == "child-session"
    parsed = json.loads(frame)
    assert parsed["event"] == "trajectory_event"
    assert parsed["session_id"] == "child-session"
    assert parsed["data"] == event


def test_the_ready_snapshot_reads_the_newest_page(monkeypatch):
    captured: list[dict] = []

    async def _read(session_id: str, *, limit: int = 50, **_kwargs):
        captured.append({"session_id": session_id, "limit": limit})
        return [{"event_id": "e1"}]

    monkeypatch.setattr(
        trajectory_ws,
        "get_trajectory_store",
        lambda: type("S", (), {"read": staticmethod(_read)})(),
    )

    snapshot = asyncio.run(trajectory_ws._snapshot("child-session"))

    assert captured == [{"session_id": "child-session", "limit": 100}]
    assert snapshot == {"session_id": "child-session", "events": [{"event_id": "e1"}]}


def test_an_unsafe_session_id_is_refused_before_any_subscription():
    socket = _FakeSocket({"session_id": "../etc"})

    asyncio.run(trajectory_ws.trajectory_ws_handler(socket))

    assert socket.closed is True
    assert socket.sent == []


def test_a_missing_session_id_is_refused():
    socket = _FakeSocket({})

    asyncio.run(trajectory_ws.trajectory_ws_handler(socket))

    assert socket.closed is True
