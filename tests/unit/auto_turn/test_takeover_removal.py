"""Task 8 — takeover removal + send_text await fix (new queueing semantics).

Under the new queueing semantics user input arriving during an auto turn is
queued; the auto turn is NEVER cancelled by user presence and the injection is
NEVER re-queued as PENDING steering from the auto_turn side (the old
``_watch_user_takeover`` / per-chunk takeover-check branch is dead code).

Guarded here:
1. ``test_takeover_*`` — user input mid-turn (detector flips to ``ws_task``)
   does NOT cancel the turn, does NOT requeue, turn completes normally.
2. ``test_send_*`` — ``send_text`` is awaited (frames actually delivered) and
   a closed socket (ConnectionClosed) is silently tolerated: no unhandled
   exception escapes, turn lifecycle unaffected.
3. ``test_inflight_*`` — while a turn runs, ``detect_state`` /
   ``_is_auto_turn_inflight`` (session_state lazy seam over module-level
   ``_INFLIGHT``) observes the session as ``auto_turn_inflight``.

Stub style follows tests/unit/test_auto_turn.py (fake generate generator +
fake detect_state + monkeypatched seams; no real WS, no LLM).
"""

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


def _state_mod():
    from agent.tools.subagent.registry import session_state

    return session_state


def _injection(run_id="run-t8"):
    return HumanMessage(
        content="[subagent:run-t8] child finished: done",
        metadata={"internal": True, "provenance": "subagent_completion", "run_id": run_id, "status": "completed"},
    )


class _ConnectionClosed(Exception):
    """Local stand-in for websockets.ConnectionClosed (no extra dependency)."""


class _ClosedFirstSendSocket:
    """Stub WS: send_text is a coroutine (robyn shape); first call simulates a
    socket that died mid-turn, later calls record the delivered frames."""

    def __init__(self):
        self.sent = []
        self.closed_seen = False

    async def send_text(self, payload):
        if not self.closed_seen:
            self.closed_seen = True
            raise _ConnectionClosed()
        self.sent.append(payload)


class _RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, payload):
        self.sent.append(payload)


class _FakeStateRegister:
    """Minimal stand-in for runtime.state_register.state_register_mem."""

    def __init__(self) -> None:
        self.answering: dict[str, object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.answering.get(session_id, default)


def _fake_generate(calls, started, block, finished, cancelled):
    async def fake(session_id, multi_modal_message, is_stream=True, origin=None):
        calls.append((session_id, multi_modal_message.text, origin))
        started.set()
        if block is not None:
            try:
                await asyncio.wait_for(block.wait(), timeout=10)
            except asyncio.CancelledError:
                cancelled.set()
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


def _make_spy():
    class _Spy:
        def __init__(self):
            self.calls = []

        async def __call__(self, session_key, message):
            self.calls.append((session_key, message))
            return None

    return _Spy()


async def _no_interrupt(session_id):
    return None


async def _wait_started(started):
    for _ in range(200):
        if started.is_set():
            return
        await asyncio.sleep(0.01)
    assert started.is_set(), "fake generate never started"


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


@pytest.mark.asyncio
async def test_takeover_user_input_mid_turn_does_not_cancel(monkeypatch):
    """User input arrives during a running auto turn: the turn is NOT cancelled,
    no steering requeue happens, and the turn runs to completion."""
    at = _mod()
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    ws = _RecordingSocket()
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: ws)
    spy = _make_spy()
    monkeypatch.setattr(at, "enqueue_steering", spy)
    # RED/GREEN compat: the old module exposes the takeover poll interval, the
    # new one does not — raising=False keeps the accelerator harmless either way.
    monkeypatch.setattr(at, "_USER_TAKEOVER_POLL_SECONDS", 0.02, raising=False)
    calls, started, block, finished, cancelled = (
        [],
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, block, finished, cancelled))

    result = await at.maybe_trigger_auto_turn("sess-t8", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    await _wait_started(started)

    # User frame arrives mid-turn: the state detector now reports a live ws_task.
    _fake_detect(monkeypatch, at, {"busy": True, "reason": "ws_task"})
    await asyncio.sleep(0.15)  # longer than the old takeover poll interval
    assert not cancelled.is_set(), "auto turn was cancelled by user input"
    assert not spy.calls, "injection was re-queued as PENDING steering on takeover"
    assert at._INFLIGHT.get("sess-t8") is not None and not at._INFLIGHT["sess-t8"].done()

    block.set()  # let the turn finish naturally
    task = at._INFLIGHT["sess-t8"]
    await asyncio.wait_for(task, timeout=10)
    assert finished.is_set(), "turn did not run to completion"
    events = [json.loads(p).get("event") for p in ws.sent]
    assert events and events[-1] == "done"
    assert "stopped" not in events
    assert "sess-t8" not in at._INFLIGHT


@pytest.mark.asyncio
async def test_send_text_closed_socket_tolerated_and_delivered(monkeypatch):
    """send_text is awaited (frames actually reach the socket) and a
    ConnectionClosed from a dead socket is silently tolerated: no unhandled
    exception escapes the send path and the turn lifecycle is unaffected."""
    at = _mod()
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    ws = _ClosedFirstSendSocket()
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: ws)
    monkeypatch.setattr(at, "enqueue_steering", _make_spy())
    calls, started, finished, cancelled = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, None, finished, cancelled))

    result = await at.maybe_trigger_auto_turn("sess-t8s", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    task = at._INFLIGHT["sess-t8s"]
    await asyncio.wait_for(task, timeout=10)

    assert finished.is_set(), "turn lifecycle broken by closed socket"
    assert not cancelled.is_set()
    assert ws.closed_seen, "closed-socket send_text path never exercised"
    assert ws.sent, "no frames delivered — send_text coroutine was never awaited"
    events = [json.loads(p).get("event") for p in ws.sent]
    assert events[-1] == "done"
    assert "sess-t8s" not in at._INFLIGHT


@pytest.mark.asyncio
async def test_inflight_observable_by_detect_state_during_turn(monkeypatch):
    """While a fake auto turn runs, the session_state lazy seam observes the
    session as auto_turn_inflight (module-level _INFLIGHT contract, Task 2)."""
    at = _mod()
    ss = _state_mod()
    tasks: dict[str, object] = {}
    monkeypatch.setattr(ss, "_get_active_tasks", lambda: tasks)
    register = _FakeStateRegister()
    monkeypatch.setattr(ss, "_get_state_register", lambda: register)
    # auto_turn's own pre-start gate (its detect_state binding) reports idle.
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)
    monkeypatch.setattr(at, "enqueue_steering", _make_spy())
    calls, started, block, finished, cancelled = (
        [],
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, block, finished, cancelled))

    result = await at.maybe_trigger_auto_turn("sess-t8i", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    await _wait_started(started)

    # Mid-turn: the real detect_state (announce-form key) + the direct
    # _is_auto_turn_inflight consumer both see the session as auto-turn-busy.
    st = ss.detect_state("agent:main:session:sess-t8i")
    assert st.busy and st.reason == "auto_turn_inflight"
    assert ss._is_auto_turn_inflight("sess-t8i")
    assert not ss._is_auto_turn_inflight("sess-other")

    block.set()
    await asyncio.wait_for(at._INFLIGHT["sess-t8i"], timeout=10)

    assert not ss._is_auto_turn_inflight("sess-t8i"), "stale _INFLIGHT entry after completion"
    assert ss.detect_state("sess-t8i").reason == "idle"
