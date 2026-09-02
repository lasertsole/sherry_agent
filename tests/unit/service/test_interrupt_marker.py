"""Task 6 — interrupted marker writer (``server/service/interrupt_marker.py``).

Covers the plan spec (input-queueing-reply-binding Task 6) against the
production module:

1. **AC-1 binding sequence** — seed state with Q1 via ``aupdate_state``,
   run ``write_interrupted_marker`` (cancelled), then ``ainvoke`` Q2:
   the model receives EXACTLY ``[human(Q1), ai(interrupted), human(Q2)]``
   with ``metadata={"interrupted": True, "reason": ...}`` intact.
2. **Deterministic id** — ``interrupted-{thread_id}-{turn_seq}`` where
   ``turn_seq`` is the HumanMessage count (stable across retries).
3. **Trailing-span heal** — a trailing ``AIMessage(tool_calls)`` without
   results gets error ToolMessage placeholders (deterministic ids
   ``{marker_id}-heal-{call_id}``) committed BEFORE the marker in the SAME
   ``aupdate_state`` (Task 3 verdict: WRITE-TIME heal).
4. **Idempotent rewrite** — second call with the same state skips the
   checkpointer write AND the MesMemory insert (one marker, one row), but
   still voids CLAIMED queue rows.
5. **MesMemory dual-write** — one ``role=ai`` row prefixed
   ``[interrupted:{reason}] `` via the existing store writer.
6. **CLAIMED cleanup** — CLAIMED rows → VOIDED; QUEUED rows untouched.
7. **Best-effort** — internal failures never raise into the caller.

Hermetic: real ``create_agent`` graph + ``InMemorySaver`` +
``RecordingFakeChatModel`` (mirrors the Task 3 harness in
``tests/integration/test_interrupt_marker_approach.py``), MesMemory on a tmp
sqlite3 connection monkeypatched over ``context_engine.store.core._db``, and a
real ``UserInputQueue`` on a tmp SQLite file.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field

import context_engine.store.core as mes_store_core
from context_engine.store.db import _migrate as mes_migrate
from server.queue import UserInputQueue, UserInputQueueStatus
from server.service.interrupt_marker import write_interrupted_marker

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.timeout(60)]

THREAD_ID = "unit-marker-thread"
SESSION_ID = "sess-unit-marker"

MARKER_ID_1 = f"interrupted-{THREAD_ID}-1"
DANGLING_CALL_ID = "call_1"
DANGLING_AI_ID = "ai-dangling-superstep"

# Mirrors pub_func/transcript_repair.make_missing_tool_result content.
HEAL_CONTENT = "tool result missing after context trim."


# ---------------------------------------------------------------------------
# Harness — mirrors tests/integration/test_interrupt_marker_approach.py (T3)
# ---------------------------------------------------------------------------


class RecordingFakeChatModel(BaseChatModel):
    """Fake LLM: records every model-call input, replies canned text."""

    response_text: str = "fake reply"
    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response_text))])


class _TestState(AgentState):
    """Graph state matching agent/core.py StateSchema (session_id carrier)."""

    session_id: str


def _build_graph(model: BaseChatModel) -> CompiledStateGraph[Any, Any, Any, Any]:
    return create_agent(
        model=model,
        tools=[],
        middleware=[],
        state_schema=_TestState,
        checkpointer=InMemorySaver(),
    )


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": THREAD_ID}}


def _input(messages: list[HumanMessage]) -> dict[str, Any]:
    return {"messages": messages, "session_id": SESSION_ID}


def _dangling_ai() -> AIMessage:
    """AIMessage left by a cancel that landed right after a model super-step."""
    return AIMessage(
        content="",
        id=DANGLING_AI_ID,
        tool_calls=[
            {"name": "echo", "args": {"x": 1}, "id": DANGLING_CALL_ID, "type": "tool_call"}
        ],
    )


async def _state_messages(
    graph: CompiledStateGraph[Any, Any, Any, Any], config: RunnableConfig
) -> list[BaseMessage]:
    snap = await graph.aget_state(config=config)
    return list(snap.values["messages"])


def _markers(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [m for m in messages if getattr(m, "id", None) == MARKER_ID_1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mes_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """MesMemory on a hermetic tmp SQLite file.

    Patches ``context_engine.store.core._db`` (the module-global connection
    every store operation reads at call time) so the dual-write lands in an
    isolated database.
    """
    conn = sqlite3.connect(
        tmp_path / "mes_memory.db", check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    mes_migrate(conn)
    monkeypatch.setattr(mes_store_core, "_db", conn)
    yield conn
    conn.close()


@pytest.fixture
def queue(tmp_path: Path) -> UserInputQueue:
    """Real store on a hermetic tmp SQLite file (same DB layout as production)."""
    return UserInputQueue(db_path=tmp_path / "user_input_queue.db")


def _queue_statuses(db_path: Path) -> list[str]:
    """All queue row statuses (created_at ASC), including terminal rows."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status FROM user_input_queue ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()


def _interrupted_rows(session_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """MesMemory rows of the session (newest turn first), decoded."""
    return [
        dict(r) for r in mes_store_core.get_messages_by_lastest_n_turns(session_id, last_n=limit)
    ]


# ---------------------------------------------------------------------------
# AC-1 — reply binding sequence through the production module
# ---------------------------------------------------------------------------


async def test_ac1_reply_binding_sequence_via_production_module(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    """Q1 mid-stream cancel → write_interrupted_marker → next ainvoke: the
    model receives EXACTLY [human(Q1), ai(interrupted), human(Q2)], marker
    metadata intact (plan AC-1, evidence -k binding)."""
    model = RecordingFakeChatModel(response_text="R2")
    graph = _build_graph(model)

    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})
    await write_interrupted_marker(
        SESSION_ID, _config(), "部分回答", "cancelled", graph=graph, queue=queue
    )
    await graph.ainvoke(_input([HumanMessage("Q2")]), _config())

    assert model.received, "model was never called"
    last_call = model.received[-1]
    assert len(last_call) == 3, f"expected [human, ai(interrupted), human], got {last_call!r}"
    assert isinstance(last_call[0], HumanMessage) and last_call[0].content == "Q1"
    assert isinstance(last_call[1], AIMessage) and last_call[1].content == "[interrupted] 部分回答"
    assert last_call[1].id == MARKER_ID_1
    assert getattr(last_call[1], "metadata").get("interrupted") is True
    assert getattr(last_call[1], "metadata").get("reason") == "cancelled"
    assert isinstance(last_call[2], HumanMessage) and last_call[2].content == "Q2"


# ---------------------------------------------------------------------------
# Clean cancel: single deterministic marker
# ---------------------------------------------------------------------------


async def test_clean_cancel_writes_single_deterministic_marker(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)

    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})
    await write_interrupted_marker(
        SESSION_ID, _config(), "partial answer", "cancelled", graph=graph, queue=queue
    )

    msgs = await _state_messages(graph, _config())
    assert len(msgs) == 2, "history preserved + exactly one marker appended"
    assert isinstance(msgs[0], HumanMessage) and msgs[0].content == "Q1"
    assert isinstance(msgs[-1], AIMessage)
    assert msgs[-1].id == MARKER_ID_1, "id = interrupted-{thread_id}-{turn_seq}"
    assert msgs[-1].content == "[interrupted] partial answer"
    assert getattr(msgs[-1], "metadata") == {"interrupted": True, "reason": "cancelled"}


# ---------------------------------------------------------------------------
# Trailing dangling super-step: WRITE-TIME heal before the marker
# ---------------------------------------------------------------------------


async def test_dangling_tool_calls_healed_before_marker(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    """Trailing AIMessage(tool_calls) without results → error ToolMessage
    placeholder(s) with deterministic ids committed BEFORE the marker in the
    same aupdate_state commit (Task 3 verdict: heal at WRITE time)."""
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)

    await graph.ainvoke(_input([HumanMessage("Q1")]), _config())
    await graph.aupdate_state(_config(), {"messages": [_dangling_ai()]})

    await write_interrupted_marker(
        SESSION_ID, _config(), "partial answer", "cancelled", graph=graph, queue=queue
    )

    msgs = await _state_messages(graph, _config())
    assert len(_markers(msgs)) == 1, "exactly one marker"

    heal_id = f"{MARKER_ID_1}-heal-{DANGLING_CALL_ID}"
    heal = [m for m in msgs if getattr(m, "id", None) == heal_id]
    assert len(heal) == 1, f"expected one deterministic heal placeholder, got {msgs!r}"
    assert isinstance(heal[0], ToolMessage)
    assert heal[0].tool_call_id == DANGLING_CALL_ID
    assert heal[0].status == "error"
    assert heal[0].content == HEAL_CONTENT

    idx_heal = [getattr(m, "id", None) for m in msgs].index(heal_id)
    idx_marker = [getattr(m, "id", None) for m in msgs].index(MARKER_ID_1)
    assert idx_heal < idx_marker, "placeholder must be committed before the marker"
    assert idx_marker == len(msgs) - 1, "marker appended last"


# ---------------------------------------------------------------------------
# Idempotent rewrite: skip writes, still run CLAIMED cleanup
# ---------------------------------------------------------------------------


async def test_idempotent_rewrite_skips_writes_but_still_voids_claimed(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    """Second write with the same state: one marker (no duplicate), one
    MesMemory row (no append), CLAIMED rows still VOIDED (evidence -k
    idempotent)."""
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)
    await queue.insert_claimed(SESSION_ID, '{"text": "in flight"}', "user")

    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})
    await write_interrupted_marker(
        SESSION_ID, _config(), "partial answer", "cancelled", graph=graph, queue=queue
    )
    msgs_after_first = await _state_messages(graph, _config())
    rows_after_first = _interrupted_rows(SESSION_ID)

    await write_interrupted_marker(
        SESSION_ID, _config(), "partial answer", "cancelled", graph=graph, queue=queue
    )

    msgs = await _state_messages(graph, _config())
    assert len(msgs) == len(msgs_after_first), "no state growth on the rewrite"
    assert len(_markers(msgs)) == 1, "same deterministic id must upsert, not append"
    assert msgs[-1].content == "[interrupted] partial answer"

    rows = _interrupted_rows(SESSION_ID)
    assert len(rows) == len(rows_after_first), "MesMemory insert skipped on rewrite"
    assert sum(
        1 for r in rows if r.get("role") == "ai" and str(r.get("content", "")).startswith("[interrupted:")
    ) == 1

    # The cleanup still ran on the skipped path.
    statuses = _queue_statuses(queue._db_path)
    assert statuses == ["VOIDED"], "CLAIMED row voided even on the idempotent path"
    assert await queue.list_active(SESSION_ID) == []


# ---------------------------------------------------------------------------
# Empty partial text: marker still written
# ---------------------------------------------------------------------------


async def test_empty_partial_text_still_writes_marker(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)
    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})

    await write_interrupted_marker(SESSION_ID, _config(), "", "cancelled", graph=graph, queue=queue)

    msgs = await _state_messages(graph, _config())
    assert len(_markers(msgs)) == 1
    assert msgs[-1].content == "[interrupted]", "empty partial → bare [interrupted] content"

    rows = _interrupted_rows(SESSION_ID)
    ai_rows = [r for r in rows if r.get("role") == "ai"]
    assert any(str(r.get("content")) == "[interrupted:cancelled]" for r in ai_rows)


# ---------------------------------------------------------------------------
# MesMemory dual-write
# ---------------------------------------------------------------------------


async def test_mesmemory_row_written_with_interrupted_prefix(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)
    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})

    await write_interrupted_marker(
        SESSION_ID, _config(), "partial answer", "cancelled", graph=graph, queue=queue
    )

    rows = _interrupted_rows(SESSION_ID)
    assert rows, "MesMemory must hold the interrupted row"
    interrupted = [
        r
        for r in rows
        if r.get("role") == "ai" and str(r.get("content", "")).startswith("[interrupted:cancelled]")
    ]
    assert len(interrupted) == 1
    assert interrupted[0]["content"] == "[interrupted:cancelled] partial answer"
    # Own turn (one add_messages call = one turn); nothing else in this test.
    assert interrupted[0]["turn_num"] == 1
    assert interrupted[0]["tool_call_id"] is None


async def test_heartbeat_timeout_reason_flows_to_metadata_and_mesmemory(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)
    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})

    await write_interrupted_marker(
        SESSION_ID, _config(), "partial answer", "heartbeat_timeout", graph=graph, queue=queue
    )

    msgs = await _state_messages(graph, _config())
    assert getattr(msgs[-1], "metadata") == {"interrupted": True, "reason": "heartbeat_timeout"}

    rows = _interrupted_rows(SESSION_ID)
    assert any(
        r.get("role") == "ai"
        and str(r.get("content", "")) == "[interrupted:heartbeat_timeout] partial answer"
        for r in rows
    ), "MesMemory prefix carries the reason"


# ---------------------------------------------------------------------------
# CLAIMED cleanup: VOIDED, QUEUED untouched
# ---------------------------------------------------------------------------


async def test_claimed_rows_voided_queued_untouched(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)
    await queue.insert_claimed(SESSION_ID, '{"text": "placeholder"}', "user")
    await queue.enqueue(SESSION_ID, '{"text": "waiting"}', "user")

    await write_interrupted_marker(
        SESSION_ID, _config(), "partial", "cancelled", graph=graph, queue=queue
    )

    statuses = _queue_statuses(queue._db_path)
    assert statuses == ["VOIDED", "QUEUED"], "CLAIMED voided, QUEUED preserved for Task 7 drain"
    active = await queue.list_active(SESSION_ID)
    assert [r.status for r in active] == [UserInputQueueStatus.QUEUED]


# ---------------------------------------------------------------------------
# Best-effort: internal failures never raise
# ---------------------------------------------------------------------------


class _ExplodingGraph:
    """aupdate_state/aget_state always crash — the hook must swallow it."""

    async def aget_state(self, config: dict[str, Any]) -> Any:
        raise RuntimeError("state boom")

    async def aupdate_state(self, config: dict[str, Any], values: dict[str, Any]) -> Any:
        raise RuntimeError("update boom")


class _ExplodingQueue:
    """list_active always crash — cleanup failure must not raise."""

    async def list_active(self, session_id: str) -> list[Any]:
        raise RuntimeError("queue boom")


async def test_best_effort_never_raises_on_internal_failures(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    # 1. Graph failure: no raise, no partial state anywhere.
    await write_interrupted_marker(
        SESSION_ID,
        _config(),
        "partial",
        "cancelled",
        graph=_ExplodingGraph(),  # pyright: ignore[reportArgumentType] - duck-typed test double
        queue=queue,
    )
    # 2. Queue-cleanup failure after a successful marker write: no raise,
    #    the marker is still persisted.
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model)
    await graph.aupdate_state(_config(), {"messages": [HumanMessage("Q1")]})
    await write_interrupted_marker(
        SESSION_ID,
        _config(),
        "partial",
        "cancelled",
        graph=graph,
        queue=_ExplodingQueue(),  # pyright: ignore[reportArgumentType] - duck-typed test double
    )
    msgs = await _state_messages(graph, _config())
    assert len(_markers(msgs)) == 1, "marker written before the cleanup failure"
