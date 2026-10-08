"""Unit tests for the task 9 third delivery path (announce/delivery.py).

Third path (additive, after the existing dual-path dispatch): when a completed
subagent run announces to a main-agent WS session, the completion is ALSO
routed into the requester's turn pipeline — busy -> task 6 steering queue,
idle -> task 8 auto-turn trigger. Guards (task 9 frozen contract):

- channel sessions / non-session requesters short-circuit (Q2 status quo);
- already-consumed injections are never re-enqueued (Q4: no double-inject);
- failed runs announce with the task 4 builder vocabulary (Q3);
- a third-path error never alters the existing dual-path result.
"""

from types import SimpleNamespace

import pytest
from agent.tools.subagent.announce import delivery as dl
from agent.tools.subagent.announce.dispatch import AnnounceDeliveryResult
from agent.tools.subagent.registry.memory import clear as clear_registry_memory
from agent.tools.subagent.registry.pending_injections import (
    PendingInjection,
    PendingInjectionStore,
)
from agent.tools.subagent.types.registry import (
    CompletionState,
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _seams(monkeypatch, tmp_path):
    """Patch every third-path seam with controllable fakes + a real tmp-path store."""
    calls = {"steering": [], "turns": [], "detect": []}
    state = {"busy": False, "ws": True, "turn_outcome": "triggered"}

    async def fake_enqueue_steering(session_key, injection):
        calls["steering"].append((session_key, injection))

    async def fake_trigger_auto_turn(session_key, injection):
        calls["turns"].append((session_key, injection))
        return SimpleNamespace(outcome=state["turn_outcome"])

    def fake_detect_state(session_key):
        calls["detect"].append(session_key)
        reason = "ws_task" if state["busy"] else "idle"
        return SimpleNamespace(session_id=session_key, busy=state["busy"], reason=reason)

    def fake_get_bound_websocket(session_id):
        return SimpleNamespace(id=session_id) if state["ws"] else None

    store = PendingInjectionStore(db_path=tmp_path / "pending.db")

    monkeypatch.setattr(dl, "_enqueue_steering", fake_enqueue_steering)
    monkeypatch.setattr(dl, "_trigger_auto_turn", fake_trigger_auto_turn)
    monkeypatch.setattr(dl, "_detect_session_state", fake_detect_state)
    monkeypatch.setattr(dl, "_get_bound_websocket", fake_get_bound_websocket)
    monkeypatch.setattr(dl, "_get_injection_store", lambda: store)

    clear_registry_memory()
    yield {"calls": calls, "state": state, "store": store}
    clear_registry_memory()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="run-third-1",
        child_session_key="agent:main:subagent:uuid-9",
        requester_session_key="agent:main:session:p1",
        task="test task",
        label="worker-1",
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL,
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
        ),
        completion=CompletionState(required=True, result_text="All done"),
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


@pytest.mark.asyncio
async def test_busy_routes_to_steering(_seams):
    _seams["state"]["busy"] = True
    run = _make_run()

    await dl._maybe_route_third_path(run, AnnounceDeliveryResult(success=True))

    steered = _seams["calls"]["steering"]
    assert len(steered) == 1
    bare, msg = steered[0]
    assert bare == "p1"  # normalized BEFORE routing (prefix stripped)
    assert msg.content.startswith("[subagent:worker-1 completed]")
    assert msg.content.endswith("All done")
    meta = getattr(msg, "metadata", None) or {}
    assert meta["internal"] is True
    assert meta["provenance"] == "subagent_completion"
    assert meta["run_id"] == "run-third-1"
    assert meta["status"] == "completed"
    assert _seams["calls"]["turns"] == []  # busy: auto-turn must not fire


@pytest.mark.asyncio
async def test_consumed_run_not_enqueued(_seams):
    run = _make_run()
    store: PendingInjectionStore = _seams["store"]
    await store.enqueue(
        PendingInjection(
            run_id=run.run_id,
            requester_session_key=run.requester_session_key,
            content="already delivered once",
        )
    )
    assert await store.mark_consumed(run.run_id) is True  # drain already took it

    _seams["state"]["busy"] = True  # would steer if the dedupe gate failed
    await dl._maybe_route_third_path(run, AnnounceDeliveryResult(success=True))

    assert _seams["calls"]["steering"] == []  # Q4: consumed run is never re-enqueued
    assert _seams["calls"]["turns"] == []
    assert _seams["calls"]["detect"] == []  # short-circuits before busy/idle detection


@pytest.mark.asyncio
async def test_channel_session_short_circuits(_seams):
    # Prefixed main-session key but no websocket binding -> channel session:
    # notification-bell status quo (Q2), no turn-input routing at all.
    _seams["state"]["ws"] = False
    run = _make_run(requester_session_key="agent:main:session:qq-session")

    await dl._maybe_route_third_path(run, AnnounceDeliveryResult(success=True))

    assert _seams["calls"]["steering"] == []
    assert _seams["calls"]["turns"] == []
    assert _seams["calls"]["detect"] == []


@pytest.mark.asyncio
async def test_failed_run_announces(_seams):
    run = _make_run(
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL,
            outcome=RunOutcome(status=RunOutcomeStatus.ERROR, error="boom"),
        )
    )

    await dl._maybe_route_third_path(run, AnnounceDeliveryResult(success=True))

    turns = _seams["calls"]["turns"]
    assert len(turns) == 1
    bare, msg = turns[0]
    assert bare == "p1"
    assert msg.content.split("\n", 1)[0] == "[subagent:worker-1 failed]"  # Q3 marker
    assert msg.content.endswith("All done")
    meta = getattr(msg, "metadata", None) or {}
    assert meta["status"] == "failed"
    assert _seams["calls"]["steering"] == []


@pytest.mark.asyncio
async def test_idle_routes_to_auto_turn(_seams):
    run = _make_run()

    await dl._maybe_route_third_path(run, AnnounceDeliveryResult(success=True))

    turns = _seams["calls"]["turns"]
    assert len(turns) == 1
    bare, msg = turns[0]
    assert bare == "p1"
    assert msg.content.startswith("[subagent:worker-1 completed]")
    assert _seams["calls"]["steering"] == []  # triggered: no steering fallback

    # Un-triggerable auto-turn (already pending) falls back to steering: never drop.
    _seams["calls"]["turns"].clear()
    _seams["state"]["turn_outcome"] = "already_pending"
    await dl._maybe_route_third_path(run, AnnounceDeliveryResult(success=True))
    assert len(_seams["calls"]["steering"]) == 1
    assert _seams["calls"]["steering"][0][0] == "p1"
    assert _seams["calls"]["steering"][0][1].content == msg.content


@pytest.mark.asyncio
async def test_third_path_error_does_not_break_existing_path(_seams, monkeypatch):
    def exploding_detect(session_key):
        raise RuntimeError("detector exploded")

    async def fake_dispatch(run, deliver, **kwargs):
        return AnnounceDeliveryResult(success=True)

    monkeypatch.setattr(dl, "_detect_session_state", exploding_detect)
    monkeypatch.setattr(dl, "run_announce_dispatch", fake_dispatch)

    run = _make_run()
    result = await dl.deliver_subagent_announcement(run)

    # The dual-path result is untouched: delivery succeeded despite the third path blowing up.
    assert result.success is True
    assert not result.suspended
    # Best-effort fallback: the injection is still enqueued, never dropped.
    steered = _seams["calls"]["steering"]
    assert len(steered) == 1
    assert steered[0][0] == "p1"
    assert steered[0][1].content.startswith("[subagent:worker-1 completed]")
