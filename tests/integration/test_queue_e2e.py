"""Task 10 e2e: user-input queueing + reply-binding + interrupt healing.

Drives the REAL composition end-to-end; the only fakes are the LLM
(transcript-recording scripted stream model), the websocket adapter, and the
in-memory checkpointer of the hermetic graph. Everything else is production
code:

    submit_user_input (real, hermetic SQLite queue)
      -> WsTurnExecutor (real, default-route executor)
         -> async_generate (real: answering flag, stream filter, cancel path)
            cancel mid-stream -> interrupt_marker (real):
               checkpointer heal (placeholder ToolMessage + interrupted
               AIMessage marker), MesMemory dual write, CLAIMED row VOIDED,
               swallowed cancel surfaces as a "Request cancelled" text chunk
            -> hermetic create_agent graph (ToolCallNormalize / drain
               middleware / scripted fake model / InMemorySaver)
         -> on_turn_finished (real) -> claim row terminal -> drain loop
            (real FIFO claim_next -> execute -> DELIVERED)
    agent_ws_handler (real, for frame-level scenarios) -> queued / chunk /
    done / stopped frames exactly as the client contract defines them.

Scenario map (plan Task 10):

- AC-1  interrupted marker binds the next turn's model transcript (checkpointer
        + MesMemory dual write, VOIDED claim row)
- AC-2  busy submits queue; exact queued frames; FIFO drain after the turn
- AC-3  concurrent submits race atomically -> exactly one turn + one queue row
- AC-4  hitl_pending queues (real session_state flag); a later turn drains it
- AC-5  cancel parked inside a tool -> dangling tool_calls healed -> no
        provider-400 on the next turn
- AC-6  cron row drains via the queue while the subagent-completion carrier
        rides the drain middleware; neither duplicates into the other's store
- AC-7a restart: orphan CLAIMED row keeps the session busy (queues, never
        silences) and no turn is dispatched
- AC-7b expired orphan + recover() -> VOIDED; fresh submit starts a turn and
        drains the leftovers FIFO (no loss, no duplication)
- AC-8  stop: current turn interrupted (marker + VOIDED), queued rows survive
        and the drain continues FIFO
- G1    duplicate client_msg_id submits are deduped silently

Everything runs against tmp SQLite (user-input queue, PendingInjectionStore,
MesMemory) - the production databases are never touched.
"""

import asyncio
import itertools
import json
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from robyn import WebSocketDisconnect

from agent import core as agent_core
from agent.middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware
from agent.middlewares.tool_call_normalize import ToolCallNormalize
from agent.tools.subagent.announce import steering_queue as sq
from agent.tools.subagent.announce.completion_message import build_completion_message
from agent.tools.subagent.registry import session_state
from agent.tools.subagent.registry.pending_injections import (
    PendingInjectionStatus,
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
from context_engine.store import core as mes_store_core
from context_engine.store.db import _migrate as mes_migrate
from pub_func.build_agent_config import build_agent_config
from runtime.relation_register import relation_register
from runtime.state_register import state_register_mem
from server.queue.user_input_queue import UserInputQueue, UserInputQueueStatus
from server.service import auto_turn as at
from server.service import input_queue_service as iqs
from server.service import messages as messages_mod
from server.service import turn_runner as tr

# pytest-asyncio STRICT mode: every async test in this module is marked; the
# timeout bounds each scenario so a regression fails instead of hanging.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Fakes - ONLY the model, the tool and the websocket adapters are fake.
# ---------------------------------------------------------------------------


class _Provider400(Exception):
    """What a strict provider answers to a dangling tool_use block."""


def _find_dangling_tool_calls(messages: list) -> dict[str, str]:
    """tool_call ids announced by AIMessages but never answered by a ToolMessage."""
    pending: dict[str, str] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                pending[str(tc.get("id"))] = str(tc.get("name"))
        elif isinstance(m, ToolMessage):
            pending.pop(getattr(m, "tool_call_id", None), None)
    return pending


class _ScriptedStreamModel(BaseChatModel):
    """Fake LLM for the real async_generate -> create_agent path.

    - records every model-call input transcript (``received``)
    - streams one canned text chunk per call (``texts[i]``)
    - may park mid-stream after a ``partial-`` chunk (``holds[i]``) - the
      cancellation target for the interrupt scenarios
    - may emit tool_call chunks instead of text (``tool_calls[i]``)
    - STRICT: raises _Provider400 when the input transcript ends with an
      unanswered tool call - the exact provider failure AC-5's healing must
      make impossible
    """

    received: list = Field(default_factory=list)
    texts: list = Field(default_factory=list)
    holds: dict = Field(default_factory=dict)  # call idx -> asyncio.Event
    reached: dict = Field(default_factory=dict)  # call idx -> asyncio.Event
    tool_calls: dict = Field(default_factory=dict)  # call idx -> [(name, args, id)]

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):  # create_agent binds tools when present
        return self.bind(tools=list(tools), **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("non-streaming fallback must never run in these tests")

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        idx = len(self.received)
        self.received.append(list(messages))
        dangling = _find_dangling_tool_calls(messages)
        if dangling:
            raise _Provider400(f"dangling tool_calls in model input: {sorted(dangling)}")

        for name, args, call_id in self.tool_calls.get(idx, ()):
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": name,
                            "args": args,
                            "id": call_id,
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                )
            )
            if run_manager is not None:
                run_manager.on_llm_new_token("", chunk=chunk)
            yield chunk
            # A tool-call turn ends the stream: the super-step commits the
            # AIMessage(tool_calls) and the graph routes into the tools node.
            return

        hold = self.holds.get(idx)
        if hold is not None:
            first = ChatGenerationChunk(message=AIMessageChunk(content="partial-"))
            if run_manager is not None:
                run_manager.on_llm_new_token("partial-", chunk=first)
            yield first
            reached = self.reached.get(idx)
            if reached is not None:
                reached.set()
            await asyncio.wait_for(hold.wait(), timeout=_TIMEOUT)

        text = self.texts[idx] if idx < len(self.texts) else f"reply-{idx}"
        chunk = ChatGenerationChunk(message=AIMessageChunk(content=text))
        if run_manager is not None:
            run_manager.on_llm_new_token(text, chunk=chunk)
        yield chunk


def _make_park_tool(hold: asyncio.Event, reached: asyncio.Event):
    """A real registered tool that parks until released - the cancel target."""

    @tool
    async def park_tool(note: str) -> str:
        """Long-running tool used by the dangling-tool-call e2e scenario."""
        reached.set()
        await asyncio.wait_for(hold.wait(), timeout=_TIMEOUT)
        return "park released"

    return park_tool


class _RecordingSocket:
    """Records frames sent by the real turn runner (async send_text)."""

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.id = f"fake-ws-{next(self._ids)}"
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


class _HandlerSocket:
    """WS double for driving agent_ws_handler directly.

    receive_text parks on a waiter until the next push; close() surfaces a
    real WebSocketDisconnect so the handler exits. send_text is a real
    coroutine recording decoded frames.
    """

    def __init__(self) -> None:
        self.id = "test-ws-id"
        self.frames: list[dict[str, Any]] = []
        self._inbound: deque[str] = deque()
        self._waiter: asyncio.Future | None = None
        self._closed = False

    def push(self, frame: dict[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)
            self._waiter = None

    def close(self) -> None:
        self._closed = True
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(WebSocketDisconnect())
            self._waiter = None

    async def receive_text(self) -> str:
        if not self._inbound:
            if self._closed:
                raise WebSocketDisconnect()
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._inbound.popleft()

    async def send_text(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


@asynccontextmanager
async def _handler_session(socket: _HandlerSocket):
    task = asyncio.ensure_future(_wsm().agent_ws_handler(socket))
    await asyncio.sleep(0.05)
    try:
        yield task
    finally:
        socket.close()
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Hermetic environment: real services on tmp SQLite + hermetic graph holder.
# ---------------------------------------------------------------------------


def _text_of(msg: Any) -> str:
    """Human input content may be a str or a [{type,text}] block list."""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


async def _wait_until(predicate, timeout: float = _TIMEOUT, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {what}")


async def _no_active_rows(queue: Any, sid: str) -> bool:
    """Async predicate: no QUEUED/CLAIMED rows left for the session."""
    return await queue.list_active(sid) == []


def _sqlite_user_input_rows(db_path) -> list[tuple[str, str, str]]:
    """(id, status, source) for EVERY row ever written to the user_input_queue.

    ``list_active`` only exposes ACTIVE rows; terminal-state assertions
    (VOIDED / DELIVERED) and cross-store duplication checks read the SQLite
    file directly (the same tmp database the store instance uses).
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, status, source FROM user_input_queue ORDER BY created_at, id"
        )
        return [(row[0], row[1], row[2]) for row in cursor.fetchall()]
    finally:
        conn.close()


class _SessionAgentState(AgentState):
    """Graph state that carries session_id (async_generate passes it through)."""

    session_id: str


def _build_graph(
    model: _ScriptedStreamModel,
    tools: list | None = None,
    middleware: list | None = None,
) -> Any:
    """Hermetic create_agent graph: scripted model + in-memory checkpointer.

    ``tools`` (AC-5: the park tool) and ``middleware`` (AC-6: the completion
    drain middleware) stay opt-in so the default composition stays minimal.
    """
    return create_agent(
        model=model,
        tools=tools or [],
        state_schema=_SessionAgentState,
        checkpointer=InMemorySaver(),
        middleware=middleware or [],
    )


def _thread_of(sid: str) -> str:
    return build_agent_config(sid)["configurable"]["thread_id"]


from types import SimpleNamespace  # noqa: E402


@pytest.fixture(autouse=True)
def e2e_env(monkeypatch, tmp_path):
    """Real services on hermetic SQLite; only LLM/graph/WS are fakes.

    ONE active-task dict shared by both consumers: detect_state's
    session_state seam and turn_runner's own seam must agree.
    """
    active_tasks: dict[str, asyncio.Task] = {}
    monkeypatch.setattr(session_state, "_get_active_tasks", lambda: active_tasks)
    monkeypatch.setattr(tr, "_get_active_tasks", lambda: active_tasks)

    user_queue = UserInputQueue(db_path=tmp_path / "e2e-user-input.db")
    monkeypatch.setattr(iqs, "get_default_queue", lambda: user_queue)

    registry = iqs.TurnExecutorRegistry()
    registry.register("ws", tr.WsTurnExecutor())
    monkeypatch.setattr(iqs, "get_default_registry", lambda: registry)
    monkeypatch.setattr(tr, "get_registry", lambda: registry)

    mes_conn = sqlite3.connect(tmp_path / "e2e-mesmemory.db")
    mes_migrate(mes_conn)
    monkeypatch.setattr(mes_store_core, "_db", mes_conn, raising=False)

    # Subagent-completion steering queue on tmp SQLite (AC-6): both the drain
    # middleware and the announce path reach it through the module-level
    # singleton holder -- swap the holder entry, never the production DB.
    injection_store = PendingInjectionStore(db_path=tmp_path / "e2e-injections.db")
    monkeypatch.setitem(
        sq._QUEUE_HOLDER, "queue", sq.SteeringQueue(store=injection_store)
    )

    holder: dict[str, Any] = {"graph": None}

    async def _fake_built_agent(*args: Any, **kwargs: Any) -> Any:
        return holder["graph"]

    monkeypatch.setattr(messages_mod, "built_agent", _fake_built_agent)
    monkeypatch.setattr(agent_core, "built_agent", _fake_built_agent, raising=False)

    tracked: dict[str, Any] = {}

    def bind_ws(session_id: str, socket: Any) -> Any:
        relation_register.register_websocket(session_id, socket)
        tracked[session_id] = socket
        return socket

    iqs._SESSION_LOCKS.clear()

    yield SimpleNamespace(
        active_tasks=active_tasks,
        user_queue=user_queue,
        registry=registry,
        holder=holder,
        bind_ws=bind_ws,
        injection_store=injection_store,
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
    session_state._HITL_PENDING.clear()
    for sid in list(tracked):
        relation_register.clear_session(sid)
        state_register_mem.clear_session(sid)
    mes_conn.close()


async def test_ac1_reply_binding_message_sequence(e2e_env):
    sid = "e2e-ac1"
    socket = e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        texts=["final"],
        holds={0: asyncio.Event()},
        reached={0: asyncio.Event()},
    )
    e2e_env.holder["graph"] = _build_graph(model)

    first = await iqs.submit_user_input(sid, "first question", "user")
    assert first.status is iqs.SubmitStatus.STARTED
    await _wait_until(model.reached[0].is_set, what="first turn parked mid-stream")

    second = await iqs.submit_user_input(sid, "second question", "user")
    assert second.status is iqs.SubmitStatus.QUEUED
    assert second.position == 1

    # Takeover path: answering flipped off makes the next streamed chunk trip
    # the per-chunk check -> CancelledError absorbed by async_generate ->
    # interrupt marker heals the transcript and VOIDEDs the CLAIMED row, then
    # on_turn_finished drains the queued row.
    state_register_mem.set_state(sid, "answering", False)
    model.holds[0].set()

    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="all queue rows terminal (Q1 VOIDED, Q2 DELIVERED)",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain loop finished")

    assert len(model.received) == 2
    transcript = model.received[1]
    assert len(transcript) == 3
    h1, marker, h2 = transcript
    assert isinstance(h1, HumanMessage) and _text_of(h1) == "first question"
    assert isinstance(marker, AIMessage)
    assert _text_of(marker) == "[interrupted] partial-"
    assert marker.id == f"interrupted-{_thread_of(sid)}-1"
    assert isinstance(h2, HumanMessage) and _text_of(h2) == "second question"


async def test_ac2_queue_fifo_drain(e2e_env):
    sid = "e2e-ac2"
    socket = e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        texts=["final"],
        holds={0: asyncio.Event()},
        reached={0: asyncio.Event()},
    )
    e2e_env.holder["graph"] = _build_graph(model)

    a = await iqs.submit_user_input(sid, "msg-a", "user")
    assert a.status is iqs.SubmitStatus.STARTED
    await _wait_until(model.reached[0].is_set, what="turn A parked")

    b = await iqs.submit_user_input(sid, "msg-b", "user")
    assert b.status is iqs.SubmitStatus.QUEUED and b.position == 1
    c = await iqs.submit_user_input(sid, "msg-c", "user")
    assert c.status is iqs.SubmitStatus.QUEUED and c.position == 2

    # NOTE: submit_user_input sends NO frames itself (OutboundRouter docstring:
    # frame conventions stay with the callers - i.e. the WS handler layer).
    # The queued positions are asserted on the SubmitResult above; only the
    # turn-runner path (chunk/done) frames appear on the bound socket.

    # A completes NORMALLY (no answering flip) -> on_turn_finished drains
    # B then C, FIFO.
    model.holds[0].set()
    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="all rows DELIVERED",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")

    assert len(model.received) == 3
    assert [_text_of(call[-1]) for call in model.received] == [
        "msg-a",
        "msg-b",
        "msg-c",
    ]

    seq: list[str] = []
    for f in socket.frames:
        if f.get("event") == "done":
            seq.append("done")
        elif f.get("event") == "chunk":
            raw = json.dumps(f, ensure_ascii=False)
            for needle in ("partial-", "final", "reply-1", "reply-2"):
                if needle in raw:
                    seq.append(needle)
                    break
    assert seq == [
        "partial-",
        "final",
        "done",
        "reply-1",
        "done",
        "reply-2",
        "done",
    ], f"frame events seen: {[f.get('event') for f in socket.frames]}"


async def test_ac3_concurrent_submit_atomic(e2e_env):
    sid = "e2e-ac3"
    e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        holds={0: asyncio.Event()},
        reached={0: asyncio.Event()},
    )
    e2e_env.holder["graph"] = _build_graph(model)

    results = await asyncio.gather(
        iqs.submit_user_input(sid, "m1", "user"),
        iqs.submit_user_input(sid, "m2", "user"),
    )
    statuses = {r.status for r in results}
    assert statuses == {iqs.SubmitStatus.STARTED, iqs.SubmitStatus.QUEUED}

    # Exactly ONE turn dispatched: the loser saw the CLAIMED placeholder row
    # (deterministic under the per-session lock) and queued behind it.
    await _wait_until(model.reached[0].is_set, what="the single turn parked")
    assert len(model.received) == 1

    model.holds[0].set()
    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="both rows terminal",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")

    assert len(model.received) == 2
    queued_text = "m1" if results[0].status is iqs.SubmitStatus.QUEUED else "m2"
    assert _text_of(model.received[1][-1]) == queued_text


async def test_ac4_hitl_pending_queues(e2e_env):
    sid = "e2e-ac4"
    e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel()
    e2e_env.holder["graph"] = _build_graph(model)

    session_state.set_hitl_pending(sid, True)
    try:
        queued = await iqs.submit_user_input(sid, "queued while hitl", "user")
        assert queued.status is iqs.SubmitStatus.QUEUED and queued.position == 1
        assert len(model.received) == 0  # nothing dispatched while HITL waits
    finally:
        session_state.set_hitl_pending(sid, False)

    started = await iqs.submit_user_input(sid, "wake turn", "user")
    assert started.status is iqs.SubmitStatus.STARTED

    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="both rows terminal",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")

    # FIFO: wake turn first, then the queued row is delivered.
    assert len(model.received) == 2
    assert _text_of(model.received[1][-1]) == "queued while hitl"


async def test_ac5_tool_park_cancel_heals(e2e_env):
    sid = "e2e-ac5"
    e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        tool_calls={0: [("park_tool", '{"note": "x"}', "call-1")]},
    )
    hold = asyncio.Event()
    reached = asyncio.Event()
    e2e_env.holder["graph"] = _build_graph(model, tools=[_make_park_tool(hold, reached)])

    first = await iqs.submit_user_input(sid, "start tool turn", "user")
    assert first.status is iqs.SubmitStatus.STARTED
    await _wait_until(reached.is_set, what="tool parked")

    second = await iqs.submit_user_input(sid, "follow-up", "user")
    assert second.status is iqs.SubmitStatus.QUEUED and second.position == 1

    # Cancel the parked turn the way the WS stop path does. async_generate
    # absorbs the CancelledError, the interrupt marker heals the dangling
    # tool_call with a placeholder ToolMessage, and the CLAIMED row is VOIDED.
    task = e2e_env.active_tasks.get(sid) or next(iter(e2e_env.active_tasks.values()))
    task.cancel()

    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="rows terminal (T1 VOIDED, T2 DELIVERED)",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")
    hold.set()

    # Drain transcript: healed sequence + queued input. The STRICT model
    # raises _Provider400 on dangling tool_calls - reaching the asserts below
    # at all proves the heal worked.
    assert len(model.received) == 2
    transcript = model.received[1]
    assert len(transcript) == 5
    h1, ai_call, tool_msg, marker_msg, h2 = transcript
    assert _text_of(h1) == "start tool turn"
    assert isinstance(ai_call, AIMessage) and ai_call.tool_calls
    assert ai_call.tool_calls[0]["id"] == "call-1"
    assert isinstance(tool_msg, ToolMessage)
    assert getattr(tool_msg, "tool_call_id", None) == "call-1"
    assert isinstance(marker_msg, AIMessage)
    assert _text_of(marker_msg).startswith("[interrupted]")
    assert _text_of(h2) == "follow-up"


async def test_ac6_cron_row_and_completion_carrier_no_cross_duplication(e2e_env):
    """AC-6: the cron row drains via the user-input queue while the
    subagent-completion carrier rides the drain middleware; neither store
    duplicates the other's payload."""
    sid = "e2e-ac6"
    e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        texts=["final"],
        holds={0: asyncio.Event()},
        reached={0: asyncio.Event()},
    )
    e2e_env.holder["graph"] = _build_graph(
        model, middleware=[SubagentCompletionDrainMiddleware()]
    )

    first = await iqs.submit_user_input(sid, "busy-turn", "user")
    assert first.status is iqs.SubmitStatus.STARTED
    await _wait_until(model.reached[0].is_set, what="turn 1 parked")

    # Busy: the cron input queues on the user-input queue (source persisted
    # as a row column, never re-inferred).
    cron = await iqs.submit_user_input(sid, "cron-echo", "cron")
    assert cron.status is iqs.SubmitStatus.QUEUED and cron.position == 1

    # Busy: the announce pipeline's steering path parks the completion
    # carrier (memory + pending_injections SQLite) -- the real enqueue entry.
    carrier = build_completion_message(
        SimpleNamespace(run_id="run-ac6", child_name="finder"),
        "child result text",
        "completed",
    )
    assert await sq.enqueue_steering(sid, carrier) is not None

    model.holds[0].set()
    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="turn 1 + cron row terminal",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")

    # The drained cron row became turn 2's input; the middleware injected the
    # carrier HumanMessage right before that model call -- exactly once.
    assert len(model.received) == 2
    turn2 = model.received[1]
    assert _text_of(turn2[-2]) == "cron-echo"
    injected = turn2[-1]
    assert isinstance(injected, HumanMessage)
    assert "[subagent:finder completed]" in _text_of(injected)
    meta = getattr(injected, "metadata", None) or {}
    assert meta.get("internal") is True
    assert meta.get("provenance") == "subagent_completion"
    assert sum(1 for m in turn2 if "[subagent:finder completed]" in _text_of(m)) == 1

    # No cross-store duplication: the carrier lives exactly once in the
    # pending_injections store (CONSUMED by the middleware drain) and never
    # became a user-input-queue row; the queue holds exactly the two rows.
    assert await e2e_env.injection_store.list_pending() == []
    record = await e2e_env.injection_store.get("run-ac6")
    assert record is not None and record.status is PendingInjectionStatus.CONSUMED
    rows = _sqlite_user_input_rows(e2e_env.user_queue._db_path)
    assert [(status, source) for _id, status, source in rows] == [
        ("DELIVERED", "user"),
        ("DELIVERED", "cron"),
    ]


async def test_ac7a_orphan_claimed_row_keeps_session_busy(e2e_env):
    """AC-7a: restart leftover -- an orphan CLAIMED row keeps the session busy
    (queues, never silences) and no turn is dispatched."""
    sid = "e2e-ac7a"
    model = _ScriptedStreamModel()
    e2e_env.holder["graph"] = _build_graph(model)

    # Crash leftover: a CLAIMED placeholder row with no live turn behind it
    # (exactly what submit_user_input's idle branch writes before dispatch).
    orphan = await e2e_env.user_queue.insert_claimed(
        sid, iqs._payload_json("ghost-turn"), "user"
    )

    # No detect_state signal at all: the CLAIMED row is the ONLY busy fact.
    state = session_state.detect_state(sid)
    assert state.busy is False and state.reason == session_state.REASON_IDLE

    result = await iqs.submit_user_input(sid, "fresh-input", "user")
    assert result.status is iqs.SubmitStatus.QUEUED and result.position == 1

    # Queued, never silenced, and NOT dispatched: no model call, no drain,
    # no active task.
    await asyncio.sleep(0.2)
    assert len(model.received) == 0
    assert tr._DRAIN_TASKS == {}
    assert sid not in e2e_env.active_tasks

    active = await e2e_env.user_queue.list_active(sid)
    assert [(row.status, row.source) for row in active] == [
        (UserInputQueueStatus.CLAIMED, "user"),
        (UserInputQueueStatus.QUEUED, "user"),
    ]
    assert active[0].id == orphan.id


async def test_ac7b_expired_orphan_recovered_then_leftovers_drain_fifo(e2e_env):
    """AC-7b: expired orphan + recover() -> VOIDED; a fresh submit starts a
    turn and drains the leftovers FIFO (no loss, no duplication)."""
    sid = "e2e-ac7b"
    e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel()
    e2e_env.holder["graph"] = _build_graph(model)

    orphan = await e2e_env.user_queue.insert_claimed(
        sid, iqs._payload_json("ghost-turn"), "user"
    )
    leftover_row, leftover_pos = await e2e_env.user_queue.enqueue(
        sid, iqs._payload_json("leftover"), "user"
    )
    assert leftover_pos == 1

    # Age ONLY the orphan past the 24h crash-recovery expiry (time travel via
    # the row column -- the store API has no test hook for expiry).
    conn = sqlite3.connect(e2e_env.user_queue._db_path)
    try:
        conn.execute(
            "UPDATE user_input_queue SET expires_at = ? WHERE id = ?",
            (time.time() - 1.0, orphan.id),
        )
        conn.commit()
    finally:
        conn.close()

    assert await e2e_env.user_queue.recover(sid) == 1

    # The orphan no longer blocks; the fresh QUEUED leftover does not silence.
    result = await iqs.submit_user_input(sid, "fresh", "user")
    assert result.status is iqs.SubmitStatus.STARTED

    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="fresh + leftover DELIVERED",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")

    # No loss, no duplication: exactly two model calls, fresh first, then the
    # leftover; the orphan is VOIDED, never delivered.
    assert [_text_of(call[-1]) for call in model.received] == ["fresh", "leftover"]
    rows = {
        row_id: status
        for row_id, status, _source in _sqlite_user_input_rows(
            e2e_env.user_queue._db_path
        )
    }
    assert rows[orphan.id] == "VOIDED"
    assert rows[leftover_row.id] == "DELIVERED"


async def test_ac8_stop_interrupts_current_keeps_queue(e2e_env):
    sid = "e2e-ac8"
    socket = e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        texts=["final"],
        holds={0: asyncio.Event()},
        reached={0: asyncio.Event()},
    )
    e2e_env.holder["graph"] = _build_graph(model)

    first = await iqs.submit_user_input(sid, "stop target", "user")
    assert first.status is iqs.SubmitStatus.STARTED
    await _wait_until(model.reached[0].is_set, what="turn parked")

    second = await iqs.submit_user_input(sid, "survivor", "user")
    assert second.status is iqs.SubmitStatus.QUEUED and second.position == 1

    # Stop interrupts the current turn only; the queued row SURVIVES and the
    # drain continues FIFO.
    task = e2e_env.active_tasks.get(sid) or next(iter(e2e_env.active_tasks.values()))
    task.cancel()

    await _wait_until(
        lambda: _no_active_rows(e2e_env.user_queue, sid),
        what="T1 terminal + T2 DELIVERED",
    )
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")

    assert len(model.received) == 2
    texts = [_text_of(m) for m in model.received[1]]
    assert "survivor" in texts

    # Absorbed cancel surfaces as a "Request cancelled" text chunk + done
    # frame - NEVER a "stopped" frame.
    raw_frames = "\n".join(json.dumps(f, ensure_ascii=False) for f in socket.frames)
    assert "Request cancelled" in raw_frames
    assert not any(f.get("event") == "stopped" for f in socket.frames)


async def test_g1_duplicate_client_msg_deduped(e2e_env):
    sid = "e2e-g1"
    e2e_env.bind_ws(sid, _RecordingSocket())
    model = _ScriptedStreamModel(
        holds={0: asyncio.Event()},
        reached={0: asyncio.Event()},
    )
    e2e_env.holder["graph"] = _build_graph(model)

    first = await iqs.submit_user_input(sid, "once", "user", client_msg_id="dup-1")
    assert first.status is iqs.SubmitStatus.STARTED
    await _wait_until(model.reached[0].is_set, what="first turn parked mid-stream")

    second = await iqs.submit_user_input(sid, "once", "user", client_msg_id="dup-1")
    assert second.status is iqs.SubmitStatus.DEDUPED

    model.holds[0].set()
    await _wait_until(lambda: _no_active_rows(e2e_env.user_queue, sid), what="turn finished")
    await _wait_until(lambda: tr._DRAIN_TASKS == {}, what="drain finished")
    assert len(model.received) == 1  # only one turn ever ran
