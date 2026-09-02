"""E2E integration suite (plan task 10) - subagent completion-notification chain.

Drives the REAL chain end-to-end. The only fakes are the LLM (canned text or
an Event-blocked stream) and the websocket adapters - everything else runs the
production code path:

    register_run (real registry + SQLite)
      -> complete_subagent_run (real lifecycle; the announce flow is awaited)
         -> run_subagent_announce_flow -> deliver_subagent_announcement
            -> dual-path EventBus dispatch (real bus)
            -> _maybe_route_third_path (real task 9 third path)
               busy: SteeringQueue + PendingInjectionStore SQLite (real)
               idle: maybe_trigger_auto_turn -> async_generate turn (task 8)
         -> SubagentCompletionDrainMiddleware (task 7) injects the carrier
            into the parent's next model call via a REAL create_agent graph

Crash recovery, user-race takeover, yield-wake dedupe and the three legacy
paths (settle-batch wake, sub->sub internal injection, WS notification bridge)
are exercised against the same real components.
"""

import asyncio
import itertools
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from agent.middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware
from agent.tools.subagent.announce import delivery as dl
from agent.tools.subagent.announce import steering_queue as sq
from agent.tools.subagent.announce.completion_message import build_completion_message
from agent.tools.subagent.announce.dispatch import AnnounceDeliveryResult
from agent.tools.subagent.announce.steering_queue import SteeringQueue
from agent.tools.subagent.events import bridge as bus_bridge
from agent.tools.subagent.events import get_event_bus
from agent.tools.subagent.registry import memory as registry_memory
from agent.tools.subagent.registry import session_state as session_state_mod
from agent.tools.subagent.registry import yield_events
from agent.tools.subagent.registry.lifecycle import complete_subagent_run
from agent.tools.subagent.registry.pending_injections import (
    PendingInjectionStatus,
    PendingInjectionStore,
)
from agent.tools.subagent.registry.run_manager import register_run
from agent.tools.subagent.registry.settle_wake import SettleWakeState, get_settle_wake_batch
from agent.tools.subagent.types.delivery import DeliveryContext
from agent.tools.subagent.types.registry import (
    CompletionState,
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)
from pub_func.build_agent_config import build_agent_config
from runtime.relation_register import relation_register
from runtime.state_register import state_register_mem
from server.queue import UserInputQueue
from type.message import MultiModalMessage

from server.service import auto_turn as at
from server.service import input_queue_service as iqs
from server.service import messages as messages_mod
from server.service import turn_runner as tr

# pytest-asyncio STRICT mode: every async test in this module is marked.
pytestmark = pytest.mark.asyncio

_TIMEOUT = 30.0  # hard bound for every in-test wait: regressions fail, never hang


# ---------------------------------------------------------------------------
# Fakes - ONLY the model and the websocket adapter are fake.
# ---------------------------------------------------------------------------


class RecordingFakeChatModel(BaseChatModel):
    """Fake LLM: records every model-call input messages, replies canned text."""

    response_text: str = "fake reply"
    received: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """Streaming path - emit the canned reply as one token chunk.

        langgraph's stream_mode="messages" only surfaces text that flows
        through ``run_manager.on_llm_new_token``; a ``_generate``-only model
        emits no token events at all, so the ws seam would never see a chunk
        frame. Both sync and async APIs are overridden so neither path hops
        through a fallback.
        """
        self.received.append(list(messages))
        chunk = ChatGenerationChunk(message=AIMessageChunk(content=self.response_text))
        if run_manager is not None:
            run_manager.on_llm_new_token(self.response_text, chunk=chunk)
        yield chunk

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        # Streaming path: the production stream filter (messages.py) only
        # forwards AIMessageChunk items — a _generate-only fake arrives as a
        # plain AIMessage and its text is silently dropped. Yield one real
        # chunk so the reply surfaces as a text frame like a live provider.
        self.received.append(list(messages))
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.response_text))


class SyncRecordingWebSocket:
    """async send_text - the contract consumed by auto_turn._send_ws.

    Task 8 made production ``_send_ws`` await ``websocket.send_text`` (robyn's
    send_text is a coroutine); a sync double gets every frame silently dropped
    by the tolerance except (TypeError: NoneType not awaitable).

    ``id`` mirrors the robyn WebSocketAdapter attribute that
    RelationManager.register_websocket keys its bidirectional maps on.
    """

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.id = f"fake-ws-{next(self._ids)}"
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


class AsyncRecordingWebSocket:
    """async send_text - the contract consumed by the EventBus bridge."""

    _ids = itertools.count(1_000_000)

    def __init__(self) -> None:
        self.id = f"fake-ws-{next(self._ids)}"
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


class _BlockingStreamAgent:
    """Stands in for the compiled graph while a parent turn must stay busy.

    async_generate sets the REAL answering state flag at turn start, so task 5
    detect_state reports busy through the production signal. The stream stays
    parked on the release Event until the test lets the turn finish.
    """

    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.reached_block = asyncio.Event()

    async def astream(self, *args: Any, **kwargs: Any):
        self.reached_block.set()
        await asyncio.wait_for(self.release.wait(), timeout=_TIMEOUT)
        chunk = AIMessageChunk(content="busy turn output")
        yield ("messages", (chunk, {"langgraph_node": "model"}))

    async def aget_state(self, config: Any, **kwargs: Any):
        """Production-shaped state probe for get_pending_interrupt().

        The real checkpointer suspends inside aget_state, and that suspension
        is where a pending task.cancel() (the per-chunk takeover check in
        _drive_turn) finally lands as CancelledError -> "stopped" frame. With
        no suspension the turn tail runs to completion synchronously and
        emits "done" instead - which never happens against a real agent.
        """
        await asyncio.sleep(0)
        return SimpleNamespace(tasks=[], values={})


class _ParentAgentState(AgentState):
    """Graph state carrying session_id (read by the drain middleware)."""

    session_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_drain_graph(model: RecordingFakeChatModel):
    """Real create_agent graph: drain middleware + in-memory checkpointer."""
    return create_agent(
        model=model,
        tools=[],
        middleware=[SubagentCompletionDrainMiddleware()],
        state_schema=_ParentAgentState,
        checkpointer=InMemorySaver(),
    )


def _make_run(
    run_id: str,
    label: str,
    requester: str,
    status: RunOutcomeStatus = RunOutcomeStatus.OK,
) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key="agent:main:subagent:e2e-uuid",
        requester_session_key=requester,
        task="e2e task",
        label=label,
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL,
            outcome=RunOutcome(status=status),
        ),
        completion=CompletionState(required=True, result_text="All done"),
    )


def _injection(run_id: str, label: str, sid: str, status: str = "completed") -> HumanMessage:
    return build_completion_message(_make_run(run_id, label, f"agent:main:session:{sid}"), "All done", status)


async def _wait_until(predicate, timeout: float = _TIMEOUT, what: str = "condition") -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _consume_generate(session_id: str, text: str) -> list:
    frames: list = []
    async for frame in messages_mod.async_generate(
        session_id, MultiModalMessage(text=text), is_stream=True
    ):
        frames.append(frame)
    return frames


def _patch_built_agent(monkeypatch, agent: Any) -> None:
    async def _fake_built_agent(*args: Any, **kwargs: Any):
        return agent

    monkeypatch.setattr(messages_mod, "built_agent", _fake_built_agent)


def _meta_of(msg: BaseMessage) -> dict:
    return getattr(msg, "metadata", None) or {}


def _messages_text(msg: BaseMessage) -> str:
    """Turn input content is either a str or a [{type,text}] block list."""
    if isinstance(msg.content, str):
        return msg.content
    return "".join(block.get("text", "") for block in msg.content if isinstance(block, dict))


def _assert_completion_carrier(
    msg: BaseMessage, label: str, status: str, run_id: str, check_status: bool = True
) -> None:
    assert isinstance(msg, HumanMessage)
    assert msg.content.startswith(f"[subagent:{label} {status}]")
    meta = _meta_of(msg)
    assert meta.get("internal") is True
    assert meta.get("provenance") == "subagent_completion"
    assert meta.get("run_id") == run_id
    if check_status:
        assert meta.get("status") == status


def _drain_event_bus() -> None:
    bus = get_event_bus()
    while bus.size:
        bus._buffer.popleft()  # noqa: SLF001 - test isolation


# ---------------------------------------------------------------------------
# Autouse environment: real components on tmp SQLite, real relation manager.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def e2e_env(monkeypatch, tmp_path):
    db_path = tmp_path / "e2e-pending.db"
    store = PendingInjectionStore(db_path=db_path)
    queue = SteeringQueue(store=store)

    saved_holder = dict(sq._QUEUE_HOLDER)
    sq._QUEUE_HOLDER.clear()
    sq._QUEUE_HOLDER["queue"] = queue
    monkeypatch.setattr(dl, "_injection_store", store)  # real store class, tmp db

    tracked: dict[str, Any] = {}

    def bind_ws(session_id: str, ws: Any) -> Any:
        relation_register.register_websocket(session_id, ws)
        tracked[session_id] = ws
        return ws

    # WS trigger's live-task table: faked dict instead of importing the robyn
    # module; detect_state / auto_turn logic itself stays REAL.
    active_tasks: dict[str, asyncio.Task] = {}
    monkeypatch.setattr(session_state_mod, "_get_active_tasks", lambda: active_tasks)
    # Unify the TWO active-task tables: turn_runner's own seam otherwise reads
    # the real server.trigger.ws.messages._active_tasks, while detect_state
    # reads the patched dict above - WsTurnExecutor adoption/registration and
    # busy signals must agree on ONE dict.
    monkeypatch.setattr(tr, "_get_active_tasks", lambda: active_tasks)

    # Hermetic user-input queue: submit_user_input / the TurnRunner drain must
    # NEVER touch the real default db (subagent_registry.db).
    user_queue = UserInputQueue(db_path=tmp_path / "e2e-user-input.db")
    monkeypatch.setattr(iqs, "get_default_queue", lambda: user_queue)

    registry_memory.clear()
    dl._delivered_keys.clear()
    dl._delivery_mirror.clear()
    yield_events._yield_events.clear()
    _drain_event_bus()

    yield SimpleNamespace(
        store=store,
        queue=queue,
        db_path=db_path,
        bind_ws=bind_ws,
        active_tasks=active_tasks,
        user_queue=user_queue,
    )

    for task in list(at._INFLIGHT.values()):
        task.cancel()
    for task in list(tr._DRAIN_TASKS.values()):
        task.cancel()
    for task in list(active_tasks.values()):
        task.cancel()
    at._INFLIGHT.clear()
    tr._DRAIN_TASKS.clear()
    active_tasks.clear()
    iqs._SESSION_LOCKS.clear()
    for sid in tracked:
        relation_register.clear_session(sid)
        try:
            state_register_mem.clear_session(sid)
        except Exception:
            pass
    yield_events._yield_events.clear()
    registry_memory.clear()
    dl._delivered_keys.clear()
    dl._delivery_mirror.clear()
    _drain_event_bus()
    sq._QUEUE_HOLDER.clear()
    sq._QUEUE_HOLDER.update(saved_holder)


# ---------------------------------------------------------------------------
# 1. Busy injection reaches the next model call
# ---------------------------------------------------------------------------


async def test_busy_injection_reaches_next_model_call(monkeypatch, e2e_env):
    sid = "e2e-busy"
    e2e_env.bind_ws(sid, SyncRecordingWebSocket())
    run = register_run(
        child_session_key="agent:main:subagent:e2e-busy-1",
        requester_session_key=f"agent:main:session:{sid}",
        task="busy parent child",
        label="worker-busy",
    )

    # Turn 1: hold the parent busy through the REAL async_generate answering flag.
    release = asyncio.Event()
    blocking = _BlockingStreamAgent(release)
    _patch_built_agent(monkeypatch, blocking)
    turn_task = asyncio.create_task(_consume_generate(sid, "hold the turn"))
    await asyncio.wait_for(blocking.reached_block.wait(), timeout=_TIMEOUT)
    await _wait_until(
        lambda: bool(state_register_mem.get_state(sid, "answering")),
        what="parent answering flag",
    )

    # REAL completion chain -> announce -> third path -> busy -> steering queue.
    updated = await asyncio.wait_for(
        complete_subagent_run(run.run_id, RunOutcome(status=RunOutcomeStatus.OK), "All done"),
        timeout=_TIMEOUT,
    )
    assert updated is not None and updated.execution.status == ExecutionStatus.TERMINAL
    row = await e2e_env.store.get(run.run_id)
    assert row is not None and row.status == PendingInjectionStatus.PENDING

    # Let turn 1 finish; the real finally block resets the answering flag.
    release.set()
    await asyncio.wait_for(turn_task, timeout=_TIMEOUT)

    # Turn 2: REAL create_agent graph + drain middleware -> next model call.
    model = RecordingFakeChatModel(response_text="injected turn reply")
    _patch_built_agent(monkeypatch, _build_drain_graph(model))
    await asyncio.wait_for(_consume_generate(sid, "next turn"), timeout=_TIMEOUT)

    injected = [
        m
        for m in model.received[0]
        if isinstance(m, HumanMessage) and str(m.content).startswith("[subagent:worker-busy completed]")
    ]
    assert injected, f"next model call never received the injection: {model.received!r}"
    _assert_completion_carrier(injected[0], "worker-busy", "completed", run.run_id)
    assert str(model.received[0][-1].content).startswith("[subagent:worker-busy completed]")

    # The injected message persists in the parent session's checkpointer history.
    graph = await messages_mod.built_agent()
    state = await graph.aget_state(config=build_agent_config(sid))
    persisted = [
        m
        for m in state.values.get("messages", [])
        if isinstance(m, HumanMessage) and str(m.content).startswith("[subagent:worker-busy completed]")
    ]
    assert persisted, "injected message missing from the session checkpoint history"
    assert _meta_of(persisted[0]).get("provenance") == "subagent_completion"

    # Exactly once: the drain consumed the SQLite row.
    row = await e2e_env.store.get(run.run_id)
    assert row.status == PendingInjectionStatus.CONSUMED


# ---------------------------------------------------------------------------
# 2. Idle auto-turn: full turn with the injection as input
# ---------------------------------------------------------------------------


async def test_idle_auto_turn_full_turn(monkeypatch, e2e_env):
    sid = "e2e-idle"
    ws = e2e_env.bind_ws(sid, SyncRecordingWebSocket())
    injection = _injection("run-idle-1", "worker-idle", sid)

    model = RecordingFakeChatModel(response_text="auto turn reply")
    _patch_built_agent(monkeypatch, _build_drain_graph(model))

    result = await at.maybe_trigger_auto_turn(f"agent:main:session:{sid}", injection)
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    assert result.session_key == sid

    await _wait_until(lambda: at._INFLIGHT.get(sid) is None, what="auto turn finished")
    assert sid not in at._INFLIGHT

    # The synthetic message was the turn input the fake model received.
    assert model.received, "auto turn never reached the model"
    turn_input = " | ".join(_messages_text(m) for m in model.received[0])
    assert "[subagent:worker-idle completed]" in turn_input

    # Zero-frontend-change proof: chunk/done frames on the WS seam exactly as
    # the production _run_stream contract emits them.
    assert ws.sent, "no ws frames sent"
    assert any('"auto turn reply"' in p for p in ws.sent)
    frames = [json.loads(p) for p in ws.sent]
    assert frames[-1]["event"] == "done"


# ---------------------------------------------------------------------------
# 3. Failed run notifies the parent
# ---------------------------------------------------------------------------


async def test_failed_run_notifies_parent(e2e_env):
    sid = "e2e-failed"
    e2e_env.bind_ws(sid, SyncRecordingWebSocket())
    run = register_run(
        child_session_key="agent:main:subagent:e2e-failed-1",
        requester_session_key=f"agent:main:session:{sid}",
        task="failing child",
        label="worker-f",
    )

    # Busy parent via the real answering signal -> third path routes to steering.
    state_register_mem.set_state(sid, "answering", True)
    await asyncio.wait_for(
        complete_subagent_run(
            run.run_id,
            RunOutcome(status=RunOutcomeStatus.ERROR, error="boom"),
            "partial output before crash",
        ),
        timeout=_TIMEOUT,
    )

    items = await sq.drain(sid)
    assert len(items) == 1
    item = items[0]
    assert item.consumed is True
    _assert_completion_carrier(item.message, "worker-f", "failed", run.run_id)
    assert item.message.content.endswith("partial output before crash")

    row = await e2e_env.store.get(run.run_id)
    assert row is not None and row.status == PendingInjectionStatus.CONSUMED
    assert await sq.drain(sid) == []  # exactly once


# ---------------------------------------------------------------------------
# 4. Crash recovery: no loss, no duplicates
# ---------------------------------------------------------------------------


async def test_crash_recovery_no_loss_no_dup(tmp_path):
    sid = "e2e-crash"
    db_path = tmp_path / "crash-e2e.db"
    queue1 = SteeringQueue(store=PendingInjectionStore(db_path=db_path))
    injection = _injection("run-crash-1", "worker-crash", sid)

    item = await queue1.enqueue_steering(f"agent:main:session:{sid}", injection)
    assert item is not None and item.run_id == "run-crash-1"
    row = await queue1.store.get("run-crash-1")
    assert row is not None and row.status == PendingInjectionStatus.PENDING

    # "Restart": ALL in-memory state destroyed; only the SQLite file survives.
    fresh = SteeringQueue(store=PendingInjectionStore(db_path=db_path))
    snapshot = await fresh.rehydrate(f"agent:main:session:{sid}")
    assert len(snapshot) == 1
    rebuilt = snapshot[0].message
    assert isinstance(rebuilt, HumanMessage)
    assert rebuilt.content == "[subagent:worker-crash completed]\nAll done"
    rmeta = _meta_of(rebuilt)
    assert rmeta.get("internal") is True
    assert rmeta.get("provenance") == "subagent_completion"
    assert rmeta.get("run_id") == "run-crash-1"

    items = await fresh.drain(sid)
    assert len(items) == 1 and items[0].consumed is True
    row = await fresh.store.get("run-crash-1")
    assert row.status == PendingInjectionStatus.CONSUMED

    # Exactly once: no second delivery, and consumed rows are never revived.
    assert await fresh.drain(sid) == []
    assert await fresh.enqueue_steering(sid, injection) is None


# ---------------------------------------------------------------------------
# 5. User race under NEW queueing semantics (Task 8): user input mid-turn does
#    NOT cancel the auto turn; it runs to completion and the still-PENDING
#    injection row is consumed by the next draining turn.
# ---------------------------------------------------------------------------


async def test_user_race_user_wins_pending_stays(monkeypatch, e2e_env):
    sid = "e2e-race"
    ws = e2e_env.bind_ws(sid, SyncRecordingWebSocket())
    injection = _injection("run-race-1", "worker-race", sid)

    # Busy-at-completion shape (new queue semantics): the durable PENDING
    # steering row exists BEFORE the idle auto turn is triggered - the
    # completion carrier was persisted while the parent was busy, the session
    # went idle afterwards, and the auto turn consumes the message as its
    # turn input.
    enqueued = await sq.enqueue_steering(f"agent:main:session:{sid}", injection)
    assert enqueued is not None and enqueued.run_id == "run-race-1"

    release = asyncio.Event()
    agent = _BlockingStreamAgent(release)
    _patch_built_agent(monkeypatch, agent)

    # Drain infra: a REAL WsTurnExecutor on a fresh registry (never the
    # process-wide default) - the drain turn below runs through the real
    # TurnRunner path, not a test double.
    registry = iqs.TurnExecutorRegistry()
    registry.register("ws", tr.WsTurnExecutor())
    monkeypatch.setattr(tr, "get_registry", lambda: registry)

    # Idle -> auto turn triggered (REAL detect_state: no ws task, no answering).
    result = await at.maybe_trigger_auto_turn(f"agent:main:session:{sid}", injection)
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    await _wait_until(
        lambda: bool(state_register_mem.get_state(sid, "answering")),
        what="auto turn answering flag",
    )

    # User message arrives mid-turn through the REAL submit seam: the session
    # is busy (answering) so the input QUEUES behind the running turn - the
    # auto turn is never cancelled (new queueing semantics).
    submit = await iqs.submit_user_input(sid, "user follow-up mid-turn", "user")
    assert submit.status is iqs.SubmitStatus.QUEUED and submit.position == 1

    # A live WS task appears (the user's stream task); nothing cancels the
    # auto turn - we let it run to completion.
    user_turn = asyncio.create_task(asyncio.sleep(_TIMEOUT))
    e2e_env.active_tasks[sid] = user_turn
    await _wait_until(agent.reached_block.is_set, what="turn parked mid-stream")

    # Mid-park the carrier row is still PENDING: the running turn never
    # touches it. (Once the auto turn finishes, its _drive_turn finally calls
    # the bare-form on_turn_finished and the TurnRunner drain starts
    # immediately - production behavior - so mid-park is the deterministic
    # observation point for "the row survives the auto turn".)
    row = await e2e_env.store.get("run-race-1")
    assert row is not None and row.status == PendingInjectionStatus.PENDING

    # Before releasing: remove the fake user task so the drain's executor
    # adopts nothing, and swap in the drain graph - the auto-started drain
    # turn must run against the REAL drain middleware (carrier consumption).
    e2e_env.active_tasks.pop(sid, None)
    user_turn.cancel()
    model = RecordingFakeChatModel(response_text="after race")
    _patch_built_agent(monkeypatch, _build_drain_graph(model))

    release.set()
    await _wait_until(lambda: at._INFLIGHT.get(sid) is None, what="auto turn completed")
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="turn runner drain finished")

    # Bare-form on_turn_finished on a drained queue is an idempotent no-op
    # (single-flight guard + empty queue): the explicit re-trigger must not
    # resurrect anything.
    await asyncio.wait_for(tr.on_turn_finished(sid), timeout=_TIMEOUT)
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="re-trigger drain settled")

    # The drain turn's model call consumed the still-PENDING carrier (drain
    # middleware) alongside the queued user input.
    injected = [
        m
        for m in model.received[0]
        if isinstance(m, HumanMessage) and str(m.content).startswith("[subagent:worker-race completed]")
    ]
    assert injected, f"drain turn did not receive the pending injection: {model.received!r}"
    _assert_completion_carrier(injected[0], "worker-race", "completed", "run-race-1")
    row = await e2e_env.store.get("run-race-1")
    assert row.status == PendingInjectionStatus.CONSUMED

    # The queued user input reached a terminal state in the hermetic queue...
    assert await e2e_env.user_queue.list_active(sid) == []
    # ...both turns cleaned up their answering flag...
    assert state_register_mem.get_state(sid, "answering") is not True
    # ...and the frame stream shows: auto turn completed (chunk+done, never
    # stopped) followed by the drain turn completing the same way.
    frames = [json.loads(p) for p in ws.sent]
    events = [f.get("event") for f in frames]
    assert not any(e == "stopped" for e in events), frames
    assert events[:2] == ["chunk", "done"], events
    assert events[-2:] == ["chunk", "done"], events


# ---------------------------------------------------------------------------
# 6. Yield wake + no double injection
# ---------------------------------------------------------------------------


async def test_yield_wake_no_double_injection(e2e_env):
    sid = "e2e-yield"
    e2e_env.bind_ws(sid, SyncRecordingWebSocket())
    run = register_run(
        child_session_key="agent:main:subagent:e2e-yield-1",
        requester_session_key=f"agent:main:session:{sid}",
        task="yielding parent child",
        label="worker-yield",
    )

    # Parent yield-waits on the prefixed key (the production sessions_yield shape).
    wake_event = yield_events.register_yield_event(f"agent:main:session:{sid}")
    state_register_mem.set_state(sid, "answering", True)  # busy parent

    updated = await asyncio.wait_for(
        complete_subagent_run(run.run_id, RunOutcome(status=RunOutcomeStatus.OK), "All done"),
        timeout=_TIMEOUT,
    )
    assert updated is not None

    # The yield woke normally through the real settle/wake chain.
    assert wake_event.is_set(), "yield event was never set by the completion chain"

    # Third path queued exactly one injection (busy -> steering).
    items = await sq.drain(sid)
    assert len(items) == 1
    _assert_completion_carrier(items[0].message, "worker-yield", "completed", run.run_id)
    assert items[0].consumed is True

    # No additional third-path injection afterwards: the consumed guard wins.
    await dl._maybe_route_third_path(updated, AnnounceDeliveryResult(success=True))
    assert await sq.drain(sid) == []
    row = await e2e_env.store.get(run.run_id)
    assert row.status == PendingInjectionStatus.CONSUMED
    # ...and a duplicate enqueue on a consumed run is never revived.
    assert await sq.enqueue_steering(sid, items[0].message) is None


# ---------------------------------------------------------------------------
# 7. Existing paths regression (settle-batch wake, sub->sub internal
#    injection, WS notification event)
# ---------------------------------------------------------------------------


async def test_existing_paths_regression(e2e_env):
    # (a) Yield wake via the settle batch (invariants from test_settle_wake.py).
    batch = get_settle_wake_batch()
    key = "agent:main:session:e2e-reg"
    batch.register_run_for_settle("run-reg-1", key)
    event = yield_events.register_yield_event(key)
    assert batch.transition_batch(key, "child_completed") == SettleWakeState.COMPLETING
    assert batch.transition_batch(key, "all_settled") == SettleWakeState.SETTLED
    assert await batch.complete_batch(key) is True  # -> wake_yield -> event set
    assert event.is_set()
    batch.retire_after_settle(key)

    # (b) Sub->sub internal injection through the REAL EventBus.
    ctx = DeliveryContext(
        requester_session_key="agent:main:subagent:parent-1",
        child_session_key="agent:main:subagent:child-1",
        child_label="child-1",
        task="regression",
        result_text="child summary",
        outcome=RunOutcome(status=RunOutcomeStatus.OK),
        run_id="run-reg-2",
        is_requester_subagent=True,
    )
    await dl._deliver_internal_injection(ctx)
    bus = get_event_bus()
    internal_msg = await asyncio.wait_for(bus.consume(), timeout=_TIMEOUT)
    assert internal_msg.metadata.get("injected_event") == "subagent_internal_update"
    assert internal_msg.metadata.get("internal") is True
    assert internal_msg.content.startswith("[Subagent Internal] child-1: ok")

    # Internal injections are consumed by the bridge, never user-visible.
    parent_ws = e2e_env.bind_ws("agent:main:subagent:parent-1", AsyncRecordingWebSocket())
    await bus_bridge._route_delivery(internal_msg)
    assert parent_ws.sent == []

    # (c) WS notification event for a user-session completion message.
    user_ctx = DeliveryContext(
        requester_session_key="agent:main:session:e2e-reg",
        child_session_key="agent:main:subagent:child-1",
        child_label="child-1",
        task="regression",
        result_text="user visible result",
        outcome=RunOutcome(status=RunOutcomeStatus.OK),
        run_id="run-reg-3",
        is_requester_subagent=False,
    )
    await dl._deliver_completion_message(user_ctx)
    user_msg = await asyncio.wait_for(bus.consume(), timeout=_TIMEOUT)
    assert user_msg.metadata.get("injected_event") == "subagent_result"

    user_ws = e2e_env.bind_ws("e2e-reg", AsyncRecordingWebSocket())
    await bus_bridge._route_delivery(user_msg)
    assert user_ws.sent, "notification never reached the websocket"
    payload = json.loads(user_ws.sent[-1])
    assert payload["event"] == "notification"
    assert "[child-1]" in payload["content"]
