"""Task 7b - auto_turn pre-start gate must not trip on its own inflight signal.

Regression guard for the 7b diagnosis: ``maybe_trigger_auto_turn`` registers the
runner task in ``_INFLIGHT`` (auto_turn.py) BEFORE the task body runs, so
``_run_auto_turn``'s pre-start gate ("await asyncio.sleep(0)"; ``detect_state``)
saw the runner's OWN live entry the moment Task 2 (55d4457) added the
``auto_turn_inflight`` signal - every auto turn self-abandoned before start and
the idle auto-turn feature died silently.

Unlike tests/unit/test_auto_turn.py and test_takeover_removal.py (which stub
``at.detect_state`` to idle - the exact stub that blinded the unit layer to
this bug), these tests drive the REAL ``detect_state`` and control its other
signals through the session_state seams (``_get_active_tasks`` /
``_get_state_register``). ``_get_auto_turn_module`` stays REAL, so the gate
reads the genuine ``_INFLIGHT`` dict the runner registered itself into.

Guarded here:
1. ``test_*_self_inflight_*`` - the only busy reason at the pre-start gate is
   the runner's own registration: the turn STARTS (no abandon, no steering
   requeue, injection consumed by the turn, ``_INFLIGHT`` cleaned).
2. ``test_*_ws_task_*`` / ``test_*_answering_*`` - a REAL user signal live at
   the gate still abandons (spy requeue, generate never started): the fix must
   not over-broaden. Deterministic: the fake signal is set synchronously after
   ``maybe_trigger_auto_turn`` returns and the created task has not started yet
   (its first action is a yield), and ws_task/answering outrank
   auto_turn_inflight in the frozen precedence (ws_task > answering >
   hitl_pending > auto_turn_inflight > idle).
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


def _injection(run_id="run-7b"):
    return HumanMessage(
        content="[subagent:run-7b] child finished: done",
        metadata={"internal": True, "provenance": "subagent_completion", "run_id": run_id, "status": "completed"},
    )


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


async def _wait_finished(task, timeout=10):
    await asyncio.wait_for(task, timeout=timeout)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    at = _mod()
    sq = _sq()
    ss = _state_mod()
    saved_holder = dict(sq._QUEUE_HOLDER)
    saved_hitl = set(ss._HITL_PENDING)
    at._INFLIGHT.clear()
    sq._QUEUE_HOLDER.clear()
    ss._HITL_PENDING.clear()

    # Real detect_state, controlled signals: the seams session_state's readers
    # call at check time. _get_auto_turn_module is NOT patched - the gate must
    # read the real _INFLIGHT the runner registers itself into.
    tasks: dict[str, object] = {}
    register = _FakeStateRegister()
    monkeypatch.setattr(ss, "_get_active_tasks", lambda: tasks)
    monkeypatch.setattr(ss, "_get_state_register", lambda: register)

    yield {"tasks": tasks, "register": register}

    for task in list(at._INFLIGHT.values()):
        task.cancel()
    at._INFLIGHT.clear()
    ss._HITL_PENDING.clear()
    ss._HITL_PENDING.update(saved_hitl)
    sq._QUEUE_HOLDER.clear()
    sq._QUEUE_HOLDER.update(saved_holder)


@pytest.mark.asyncio
async def test_prestart_gate_ignores_own_inflight_turn_starts(monkeypatch):
    """Only busy reason at the gate is the runner's own _INFLIGHT entry:
    the turn STARTS - no abandon, no steering requeue, _INFLIGHT cleaned."""
    at = _mod()
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    spy = _make_spy()
    monkeypatch.setattr(at, "enqueue_steering", spy)

    class _Socket:
        def __init__(self):
            self.sent = []

        async def send_text(self, payload):
            self.sent.append(payload)

    ws = _Socket()
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: ws)
    calls, started, finished, cancelled = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, None, finished, cancelled))

    result = await at.maybe_trigger_auto_turn("sess-7b-self", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED

    # Handle captured BEFORE the first await: the runner is registered but has
    # NOT started (its first action is a yield). Re-reading _INFLIGHT after
    # _wait_started races the runner's own cleanup - the unblocked fake turn
    # can run to full completion inside one 10ms poll window.
    task = at._INFLIGHT["sess-7b-self"]
    assert not task.done(), "runner task finished before its first yield"

    # RED (pre-fix): the runner self-abandons here and generate never starts.
    await _wait_started(started)
    assert not spy.calls, "injection was re-queued as PENDING steering on self-inflight"

    await _wait_finished(task)
    assert not task.cancelled(), "runner task was cancelled during the turn"
    assert finished.is_set(), "turn did not run to completion"
    assert not cancelled.is_set()
    assert calls and calls[0][0] == "sess-7b-self", "injection never consumed by the turn"
    events = [json.loads(p).get("event") for p in ws.sent]
    assert events and events[-1] == "done"
    assert "stopped" not in events
    assert "sess-7b-self" not in at._INFLIGHT


@pytest.mark.asyncio
async def test_prestart_gate_still_abandons_on_ws_task(monkeypatch):
    """A live ws task at the pre-start gate STILL abandons (real user race):
    spy requeue called, generate never started, _INFLIGHT cleaned."""
    at = _mod()
    ss = _state_mod()
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    spy = _make_spy()
    monkeypatch.setattr(at, "enqueue_steering", spy)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)
    calls, started, finished, cancelled = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, None, finished, cancelled))

    result = await at.maybe_trigger_auto_turn("sess-7b-ws", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    task = at._INFLIGHT["sess-7b-ws"]

    # User frame registered BEFORE the runner's first yield: the created task
    # has not started yet (its first action is await asyncio.sleep(0)).
    live_user_task = asyncio.create_task(asyncio.sleep(60))
    ss._get_active_tasks()["sess-7b-ws"] = live_user_task
    try:
        await _wait_finished(task)
        assert not started.is_set(), "generate started despite a live ws task at the gate"
        assert not calls, "turn input reached the model despite a live ws task at the gate"
        assert len(spy.calls) == 1 and spy.calls[0][0] == "sess-7b-ws", (
            "gate did not re-persist the injection as PENDING steering"
        )
    finally:
        live_user_task.cancel()
    assert "sess-7b-ws" not in at._INFLIGHT


@pytest.mark.asyncio
async def test_prestart_gate_still_abandons_on_answering(monkeypatch):
    """A live answering flag at the pre-start gate STILL abandons: spy requeue
    called, generate never started."""
    at = _mod()
    ss = _state_mod()
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    spy = _make_spy()
    monkeypatch.setattr(at, "enqueue_steering", spy)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)
    calls, started, finished, cancelled = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(at, "async_generate", _fake_generate(calls, started, None, finished, cancelled))

    result = await at.maybe_trigger_auto_turn("sess-7b-ans", _injection())
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    task = at._INFLIGHT["sess-7b-ans"]

    ss._get_state_register().answering["sess-7b-ans"] = True
    await _wait_finished(task)
    assert not started.is_set(), "generate started despite a live answering flag at the gate"
    assert not calls, "turn input reached the model despite a live answering flag at the gate"
    assert len(spy.calls) == 1 and spy.calls[0][0] == "sess-7b-ans", (
        "gate did not re-persist the injection as PENDING steering"
    )
    assert "sess-7b-ans" not in at._INFLIGHT
