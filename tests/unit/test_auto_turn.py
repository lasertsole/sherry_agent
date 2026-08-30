"""Unit tests for server/service/auto_turn.py (Task 8: idle auto-turn trigger)."""

import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

pytestmark = pytest.mark.unit


def _mod():
    from server.service import auto_turn

    return auto_turn


def _sq():
    from agent.tools.subagent.announce import steering_queue

    return steering_queue


def _injection(run_id="run-1"):
    return HumanMessage(
        content="[subagent:run-1] child finished: done",
        metadata={"internal": True, "provenance": "subagent_completion", "run_id": run_id, "status": "completed"},
    )


def _fake_generate(calls, started, block, finished):
    async def fake(session_id, multi_modal_message, is_stream=True):
        calls.append((session_id, multi_modal_message.text))
        started.set()
        if block is not None:
            try:
                await asyncio.wait_for(block.wait(), timeout=10)
            except asyncio.CancelledError:
                raise
        yield {"type": "text", "content": "hello"}
        yield {"type": "meta", "model_name": "fake", "input_tokens": 3, "output_tokens": 5}
        finished.set()

    return fake


def _fake_detect(monkeypatch, at, state):
    from agent.tools.subagent.registry.session_state import SessionState

    def fake(session_key):
        return SessionState(session_id=session_key, busy=state["busy"], reason=state["reason"])

    monkeypatch.setattr(at, "detect_state", fake)


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    def send_text(self, payload):
        self.sent.append(payload)


async def _no_interrupt(session_id):
    return None


@pytest.fixture(autouse=True)
def _clean_state():
    at = _mod()
    sq = _sq()
    saved = dict(sq._QUEUE_HOLDER)
    at._INFLIGHT.clear()
    sq._QUEUE_HOLDER.clear()
    yield
    for task in list(at._INFLIGHT.values()):
        task.cancel()
    at._INFLIGHT.clear()
    sq._QUEUE_HOLDER.clear()
    sq._QUEUE_HOLDER.update(saved)


async def _cancel_inflight(at):
    tasks = [t for t in at._INFLIGHT.values() if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_started(started):
    for _ in range(200):
        if started.is_set():
            return
        await asyncio.sleep(0.01)
    assert started.is_set(), "fake generate never started"


@pytest.mark.asyncio
async def test_idle_triggers_new_turn(monkeypatch):
    at = _mod()
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)
    monkeypatch.setattr(at, "enqueue_steering", _spy := _make_spy())
    monkeypatch.setattr(at, "_USER_TAKEOVER_POLL_SECONDS", 0.02)
    calls, started, finished = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, asyncio.Event(), finished))

    result = await at.maybe_trigger_auto_turn("sess-1", _injection())

    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    assert result.session_key == "sess-1"
    await _wait_started(started)
    assert len(calls) == 1 and calls[0][0] == "sess-1" and calls[0][1].startswith("[subagent:")
    assert not finished.is_set()  # fire-and-forget: caller returned before turn finished
    assert not _spy.calls
    await _cancel_inflight(at)


def _make_spy():
    class _Spy:
        def __init__(self):
            self.calls = []

        async def __call__(self, session_key, message):
            self.calls.append((session_key, message))
            return None

    return _Spy()


@pytest.mark.asyncio
async def test_double_trigger_idempotent(monkeypatch):
    at = _mod()
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)
    spy = _make_spy()
    monkeypatch.setattr(at, "enqueue_steering", spy)
    monkeypatch.setattr(at, "_USER_TAKEOVER_POLL_SECONDS", 0.02)
    calls, started, finished = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, asyncio.Event(), finished))

    r1 = await at.maybe_trigger_auto_turn("sess-1", _injection())
    r2 = await at.maybe_trigger_auto_turn("sess-1", _injection())

    assert r1.outcome == at.AutoTurnOutcome.TRIGGERED
    assert r2.outcome == at.AutoTurnOutcome.ALREADY_PENDING
    await _wait_started(started)
    await asyncio.sleep(0.05)
    assert len(calls) == 1  # fake generate started exactly once
    assert not spy.calls
    inflight = at._INFLIGHT.get("sess-1")
    assert inflight is not None and not inflight.done()
    await _cancel_inflight(at)


@pytest.mark.asyncio
async def test_busy_not_triggered(monkeypatch):
    at = _mod()
    calls, started, finished = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, None, finished))
    spy = _make_spy()
    monkeypatch.setattr(at, "enqueue_steering", spy)
    for reason in ("ws_task", "answering"):
        _fake_detect(monkeypatch, at, {"busy": True, "reason": reason})
        result = await at.maybe_trigger_auto_turn("sess-1", _injection())
        assert result.outcome == at.AutoTurnOutcome.BUSY
        assert result.reason == reason
    assert not at._INFLIGHT
    assert not calls and not spy.calls


@pytest.mark.asyncio
async def test_user_message_wins_race(monkeypatch, tmp_path):
    from agent.tools.subagent.announce.steering_queue import SteeringQueue
    from agent.tools.subagent.registry.pending_injections import PendingInjectionStatus, PendingInjectionStore

    at = _mod()
    sq = _sq()
    store = PendingInjectionStore(db_path=tmp_path / "t.db")
    sq._QUEUE_HOLDER["queue"] = SteeringQueue(store=store)  # real seam, tmp DB
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    ws = _FakeWebSocket()
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: ws)
    monkeypatch.setattr(at, "_USER_TAKEOVER_POLL_SECONDS", 0.02)
    calls, started, finished = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, asyncio.Event(), finished))
    injection = _injection("run-u1")

    result = await at.maybe_trigger_auto_turn("sess-u", injection)
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    await _wait_started(started)
    state_busy = True  # user/HITL takeover: flip detector to ws_task
    _fake_detect(monkeypatch, at, {"busy": state_busy, "reason": "ws_task"})
    await asyncio.wait_for(at._INFLIGHT["sess-u"], timeout=10)

    events = [json.loads(p).get("event") for p in ws.sent]
    assert "stopped" in events  # generate task was cancelled, not completed
    assert not finished.is_set()
    item = await store.get("run-u1")
    assert item is not None and item.status == PendingInjectionStatus.PENDING
    assert at._INFLIGHT.get("sess-u") is None


@pytest.mark.asyncio
async def test_unknown_session_returns_busy():
    at = _mod()
    result = await at.maybe_trigger_auto_turn("   ", _injection())
    assert result.outcome == at.AutoTurnOutcome.BUSY
    assert result.reason == "unknown_session"
    assert not at._INFLIGHT


@pytest.mark.asyncio
async def test_normal_turn_sends_chunk_and_done(monkeypatch):
    at = _mod()
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    ws = _FakeWebSocket()
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: ws)
    monkeypatch.setattr(at, "_USER_TAKEOVER_POLL_SECONDS", 0.02)
    calls, started, finished = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, None, finished))

    result = await at.maybe_trigger_auto_turn("sess-2", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    await asyncio.wait_for(at._INFLIGHT["sess-2"], timeout=10)

    frames = [json.loads(p) for p in ws.sent]
    assert frames[0] == {"event": "chunk", "session_id": "sess-2", "type": "text", "content": "hello"}
    assert frames[-1]["event"] == "done" and frames[-1]["model_name"] == "fake"
    assert finished.is_set() and "sess-2" not in at._INFLIGHT
