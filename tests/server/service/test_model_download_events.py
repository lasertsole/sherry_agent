"""Throttled WS emitter for model download progress (no sockets, no network)."""

from __future__ import annotations

import asyncio

import pytest

from server.service import model_download_events as events

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


@pytest.fixture(autouse=True)
def _reset_emitter():
    with events._LOCK:
        events._LOOP = None
        events._LAST_PUSH.clear()
    yield
    with events._LOCK:
        events._LOOP = None
        events._LAST_PUSH.clear()


def test_without_a_loop_publish_is_a_quiet_noop(monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_broadcast(payload, **kwargs):
        sent.append(payload)
        return 1

    monkeypatch.setattr(events, "broadcast_to_all", fake_broadcast)
    events.publish("local", "X", lambda: {"state": "downloading"}, force=True)

    assert sent == []


def test_publish_throttles_progress_and_force_bypasses(monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_broadcast(payload, **kwargs):
        sent.append(payload)
        return 1

    monkeypatch.setattr(events, "broadcast_to_all", fake_broadcast)

    async def scenario() -> None:
        events.capture_loop()
        events.publish("local", "X", lambda: {"state": "downloading", "percent": 1})
        events.publish("local", "X", lambda: {"state": "downloading", "percent": 2})
        await asyncio.sleep(0.01)
        assert [frame["content"]["percent"] for frame in sent] == [1]

        events.publish("local", "X", lambda: {"state": "ready", "percent": 100}, force=True)
        await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert len(sent) == 2
    assert sent[0]["event"] == events.EVENT_NAME
    assert sent[0]["content"] == {"kind": "local", "state": "downloading", "percent": 1}
    assert sent[1]["content"] == {"kind": "local", "state": "ready", "percent": 100}


def test_a_builder_error_does_not_escape(monkeypatch) -> None:
    async def fake_broadcast(payload, **kwargs):
        raise AssertionError("the builder failed, so nothing should be sent")

    monkeypatch.setattr(events, "broadcast_to_all", fake_broadcast)

    def broken() -> dict:
        raise RuntimeError("status unavailable")

    async def scenario() -> None:
        events.capture_loop()
        events.publish("local", "X", broken, force=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
