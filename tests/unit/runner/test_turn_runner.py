"""Task 7 unit tests for the TurnRunner drain orchestrator.

Contract under test (server/service/turn_runner.py, does not exist yet — RED):

- ``on_turn_finished(session_id, claim_row_id=None)``:
  1. marks the given CLAIMED row DELIVERED (best effort, never raises),
  2. then starts a single-flight per-session drain of remaining rows.
- Drain loop: claim_next → empty→exit → route by reply_target (ws/channel)
  → execute → mark_terminal → loop. One failing row = FAILED + error frame +
  continue (never crashes the drain). Channel rows need an executor registered
  under the "channel" route, otherwise FAILED + warning + continue.
- A drain must defer while a foreign CLAIMED row (a live, not-yet-finished
  turn) exists, and pick the row up when that turn finishes.
- Single-flight: concurrent on_turn_finished calls must produce exactly ONE
  drain task per session.
- WsTurnExecutor: adopts the live inline turn's task when present, otherwise
  drives async_generate itself; forwards chunk/done frames to the session
  socket; sets hitl_pending on hitl_request; a cancelled child keeps the row
  CLAIMED and lets the drain continue FIFO.
- Seams (patch points): ``iqs.get_default_queue``, ``turn_runner.get_registry``,
  ``turn_runner.get_websocket_by_session_id``, ``turn_runner._get_active_tasks``,
  ``turn_runner.async_generate``, ``turn_runner.get_pending_interrupt``,
  ``turn_runner.set_hitl_pending``, ``turn_runner._DRAIN_TASKS``,
  ``turn_runner._OUTBOUND_ROUTERS`` / ``register_outbound_router``.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from server.queue.user_input_queue import (
    UserInputQueue,
    UserInputQueueStatus,
)
from server.service import input_queue_service as iqs

pytestmark = pytest.mark.unit


def _tr():
    import server.service.turn_runner as turn_runner  # noqa: PLC0415

    return turn_runner


class FakeSocket:
    """WS double — send_text MUST be a real coroutine (T8 lesson)."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


class RecordingExecutor:
    """TurnExecutor double recording (session_id, text, source, reply_target)."""

    def __init__(self, *, fail_texts: set[str] | None = None, gate: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[str, str, str, str | None]] = []
        self.fail_texts = fail_texts or set()
        self.gate = gate

    async def execute(self, session_id: str, message: str, source: str, reply_target: str | None) -> None:
        self.calls.append((session_id, message, source, reply_target))
        if message in self.fail_texts:
            raise RuntimeError(f"boom: {message}")
        if self.gate is not None:
            await self.gate.wait()


def _payload(text: str) -> str:
    return json.dumps({"text": text, "image_base64_list": []}, ensure_ascii=False)


async def _enqueue(store: UserInputQueue, session_id: str, text: str, *, reply_target: str | None = None):
    """enqueue() returns (row, position); tests only need the row."""
    row, _position = await store.enqueue(session_id, _payload(text), "user", reply_target=reply_target)
    return row


def _status_of(store: UserInputQueue, row_id: str) -> str | None:
    conn = sqlite3.connect(store._db_path)  # noqa: SLF001
    try:
        cur = conn.execute("SELECT status FROM user_input_queue WHERE id = ?", (row_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


async def _wait_until(pred, timeout: float = 5.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    tr = _tr()
    store = UserInputQueue(db_path=tmp_path / "subagent_registry.db")
    registry = iqs.TurnExecutorRegistry()
    sockets: dict[str, FakeSocket] = {}
    active_tasks: dict[str, asyncio.Task] = {}
    hitl_sets: list[tuple[str, bool]] = []

    monkeypatch.setattr(iqs, "get_default_queue", lambda: store)
    monkeypatch.setattr(tr, "get_registry", lambda: registry)
    monkeypatch.setattr(tr, "get_websocket_by_session_id", lambda session_id: sockets.get(session_id))
    monkeypatch.setattr(tr, "_get_active_tasks", lambda: active_tasks)
    monkeypatch.setattr(tr, "get_pending_interrupt", _noop_pending_interrupt)
    monkeypatch.setattr(tr, "set_hitl_pending", lambda session_id, value: hitl_sets.append((session_id, value)))

    return SimpleNamespace(
        tr=tr,
        store=store,
        registry=registry,
        sockets=sockets,
        active_tasks=active_tasks,
        hitl_sets=hitl_sets,
    )


@pytest.fixture(autouse=True)
def _reset_runner_state():
    tr = _tr()
    tr._DRAIN_TASKS.clear()
    tr._OUTBOUND_ROUTERS.clear()
    iqs._SESSION_LOCKS.clear()
    yield
    for task in list(tr._DRAIN_TASKS.values()):
        task.cancel()
    tr._DRAIN_TASKS.clear()
    tr._OUTBOUND_ROUTERS.clear()
    iqs._SESSION_LOCKS.clear()


async def _noop_pending_interrupt(session_id: str) -> dict[str, Any] | None:
    return None


# ---------------------------------------------------------------------------
# Drain loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_turn_finished_marks_claimed_row_and_drains_one_queued(env):
    """on_turn_finished marks the given CLAIMED row DELIVERED, then drains."""
    tr, store, registry = env.tr, env.store, env.registry
    r0 = await store.insert_claimed("s1", _payload("first"), "user")
    r1 = await _enqueue(store, "s1", "second")
    executor = RecordingExecutor()
    registry.register("ws", executor)

    await tr.on_turn_finished("s1", claim_row_id=r0.id)

    await _wait_until(lambda: _status_of(store, r1.id) == "DELIVERED", what="queued row drained")
    assert _status_of(store, r0.id) == "DELIVERED", "given CLAIMED row must be marked DELIVERED"
    assert executor.calls == [("s1", "second", "user", None)], "only the queued row must execute"
    await _wait_until(
        lambda: tr._DRAIN_TASKS == {}, what="drain task self-cleanup"
    )


@pytest.mark.asyncio
async def test_drain_executes_queued_rows_in_fifo_order(env):
    tr, store, registry = env.tr, env.store, env.registry
    rows = [await _enqueue(store, "s1", text) for text in ("a", "b", "c")]
    executor = RecordingExecutor()
    registry.register("ws", executor)

    await tr.on_turn_finished("s1")

    drain = tr._DRAIN_TASKS.get("s1")
    assert drain is not None, "a drain task must exist after on_turn_finished"
    await asyncio.wait_for(drain, timeout=10)
    assert [call[1] for call in executor.calls] == ["a", "b", "c"], "rows must run FIFO"
    assert all(_status_of(store, row.id) == "DELIVERED" for row in rows)


@pytest.mark.asyncio
async def test_drain_executes_with_no_socket_still_marks_delivered(env):
    """WS-routed row with no live socket still executes and marks DELIVERED."""
    tr, store, registry = env.tr, env.store, env.registry
    row = await _enqueue(store, "s1", "no-socket")
    executor = RecordingExecutor()
    registry.register("ws", executor)

    await tr.on_turn_finished("s1")

    await _wait_until(lambda: _status_of(store, row.id) == "DELIVERED", what="row drained without socket")
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_drain_exits_cleanly_when_queue_empty(env):
    tr, store = env.tr, env.store

    await tr.on_turn_finished("s1")
    drain = tr._DRAIN_TASKS.get("s1")
    assert drain is not None, "a drain must be kicked even when the queue is empty"
    await asyncio.wait_for(drain, timeout=10)

    assert tr._DRAIN_TASKS == {}, "empty queue must leave no drain task"
    assert await store.list_active("s1") == []


@pytest.mark.asyncio
async def test_single_failure_marks_failed_sends_error_frame_and_continues(env):
    tr, store, registry, sockets = env.tr, env.store, env.registry, env.sockets
    socket = FakeSocket()
    sockets["s1"] = socket
    ok1 = await _enqueue(store, "s1", "ok-1")
    bad = await _enqueue(store, "s1", "bad")
    ok2 = await _enqueue(store, "s1", "ok-2")
    registry.register("ws", RecordingExecutor(fail_texts={"bad"}))

    await tr.on_turn_finished("s1")

    drain = tr._DRAIN_TASKS.get("s1")
    await asyncio.wait_for(drain, timeout=10)
    assert _status_of(store, ok1.id) == "DELIVERED"
    assert _status_of(store, bad.id) == "FAILED", "failing row must be marked FAILED"
    assert _status_of(store, ok2.id) == "DELIVERED", "drain must continue after a failure"
    errors = [f for f in socket.frames if f.get("event") == "error"]
    assert len(errors) == 1 and "boom: bad" in errors[0].get("content", "")


@pytest.mark.asyncio
async def test_channel_row_without_executor_fails_and_does_not_block_ws_rows(env):
    tr, store, registry = env.tr, env.store, env.registry
    channel_row = await _enqueue(store, "s1", "to-channel", reply_target="channel")
    ws_row = await _enqueue(store, "s1", "to-ws")
    registry.register("ws", RecordingExecutor())

    records: list[Any] = []
    hid = logger.add(records.append, level="WARNING")
    try:
        await tr.on_turn_finished("s1")
        drain = tr._DRAIN_TASKS.get("s1")
        await asyncio.wait_for(drain, timeout=10)
    finally:
        logger.remove(hid)

    assert _status_of(store, channel_row.id) == "FAILED"
    assert _status_of(store, ws_row.id) == "DELIVERED", "missing channel executor must not block ws rows"
    # loguru plain-callable sinks receive the formatted string, not a record dict
    assert any("channel" in rec for rec in records), "a warning must be logged"


@pytest.mark.asyncio
async def test_channel_router_receives_error_frame_on_failure(env):
    tr, store = env.tr, env.store
    sent: list[dict[str, Any]] = []

    class FakeRouter:
        async def send_error(self, session_id: str, content: str) -> None:
            sent.append({"session_id": session_id, "content": content})

    row = await _enqueue(store, "s1", "boom", reply_target="channel")
    env.registry.register("ws", RecordingExecutor())
    tr.register_outbound_router("channel", FakeRouter())

    await tr.on_turn_finished("s1")

    drain = tr._DRAIN_TASKS.get("s1")
    await asyncio.wait_for(drain, timeout=10)
    assert _status_of(store, row.id) == "FAILED", "row has no channel executor → FAILED"
    assert len(sent) == 1 and sent[0]["session_id"] == "s1", (
        "the registered router must receive exactly one error frame"
    )
    assert "No executor registered" in sent[0]["content"], (
        "the error frame must explain the missing executor"
    )


# ---------------------------------------------------------------------------
# Single-flight + foreign CLAIMED defer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_flight_concurrent_on_turn_finished_runs_one_drain(env):
    tr, store, registry = env.tr, env.store, env.registry
    row = await _enqueue(store, "s1", "first")
    gate = asyncio.Event()
    executor = RecordingExecutor(gate=gate)
    registry.register("ws", executor)

    await asyncio.gather(tr.on_turn_finished("s1"), tr.on_turn_finished("s1"))

    drain = tr._DRAIN_TASKS.get("s1")
    assert drain is not None and not drain.done(), "a live drain task must exist"
    await tr.on_turn_finished("s1")  # third call while the drain is live
    assert tr._DRAIN_TASKS.get("s1") is drain, "single-flight: drain task must not be replaced"

    gate.set()
    await asyncio.wait_for(drain, timeout=10)
    assert executor.calls == [("s1", "first", "user", None)], "each row must execute exactly once"
    assert _status_of(store, row.id) == "DELIVERED"


@pytest.mark.asyncio
async def test_drain_defers_when_claimed_row_is_foreign_then_picks_up_after(env):
    """A live turn's CLAIMED row defers the drain; its completion picks up."""
    tr, store, registry = env.tr, env.store, env.registry
    r0 = await store.insert_claimed("s1", _payload("live"), "user")  # foreign CLAIMED
    r1 = await _enqueue(store, "s1", "queued")
    executor = RecordingExecutor()
    registry.register("ws", executor)

    await tr.on_turn_finished("s1")  # no claim_row_id → foreign-CLAIMED defer check
    await asyncio.sleep(0.05)
    assert executor.calls == [], "drain must defer while a CLAIMED row is live"
    assert _status_of(store, r1.id) == "QUEUED", "queued row must stay queued"

    await store.mark_terminal(r0.id, "DELIVERED")
    await tr.on_turn_finished("s1")  # the live turn's completion re-triggers
    await _wait_until(lambda: _status_of(store, r1.id) == "DELIVERED", what="deferred row picked up")
    assert executor.calls == [("s1", "queued", "user", None)]


# ---------------------------------------------------------------------------
# WsTurnExecutor
# ---------------------------------------------------------------------------


def _fake_generate_factory(calls, started=None, *, block=None, on_cancel=None, text_filter=None):
    """Async-generator fake of async_generate; yields one text chunk + meta."""

    async def fake(session_id, message, is_stream=True, origin=None):
        text = getattr(message, "text", str(message))
        calls.append((session_id, text))
        if started is not None and (text_filter is None or text == text_filter):
            started.set()
        if block is not None and (text_filter is None or text == text_filter):
            try:
                await block.wait()
            except asyncio.CancelledError:
                if on_cancel is not None:
                    await on_cancel()
                raise
        yield {"type": "text", "content": f"echo:{text}"}
        yield {"type": "meta", "model_name": "fake", "input_tokens": 3, "output_tokens": 5}

    return fake


@pytest.mark.asyncio
async def test_ws_executor_drives_turn_sends_frames_and_marks_delivered(env, monkeypatch):
    tr, store, registry, sockets = env.tr, env.store, env.registry, env.sockets
    socket = FakeSocket()
    sockets["s1"] = socket
    row = await _enqueue(store, "s1", "hello")
    registry.register("ws", tr.WsTurnExecutor())
    calls: list[tuple[str, str]] = []
    # turn_runner resolves async_generate as a module global at call time
    monkeypatch.setattr(tr, "async_generate", _fake_generate_factory(calls))

    await tr.on_turn_finished("s1")

    await _wait_until(
        lambda: _status_of(store, row.id) == "DELIVERED" and len(socket.frames) >= 2,
        what="ws turn driven + frames sent",
    )
    chunk = next(f for f in socket.frames if f.get("event") == "chunk")
    assert chunk["content"] == "echo:hello" and chunk["session_id"] == "s1"
    done = next(f for f in socket.frames if f.get("event") == "done")
    assert done["model_name"] == "fake" and done["input_tokens"] == 3
    assert env.active_tasks == {}, "driven child task must be unregistered"
    assert calls == [("s1", "hello")]


@pytest.mark.asyncio
async def test_ws_executor_adopts_inline_turn_instead_of_double_running(env, monkeypatch):
    """A live inline turn's registered task is adopted, not executed twice."""
    tr, store, registry = env.tr, env.store, env.registry
    row = await _enqueue(store, "s1", "live")
    registry.register("ws", tr.WsTurnExecutor())

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(tr, "async_generate", _fake_generate_factory(calls))
    gate = asyncio.Event()

    async def inline_task():
        await gate.wait()

    live = asyncio.create_task(inline_task())
    env.active_tasks["s1"] = live

    await tr.on_turn_finished("s1")
    await asyncio.sleep(0.05)
    assert calls == [], "an adopted turn must NOT drive async_generate again"
    assert _status_of(store, row.id) != "DELIVERED", "row is delivered only after the adopted turn finishes"

    gate.set()
    await _wait_until(lambda: _status_of(store, row.id) == "DELIVERED", what="adopted turn delivered")
    assert env.active_tasks == {}
    await asyncio.wait_for(live, timeout=5)


@pytest.mark.asyncio
async def test_ws_executor_hitl_sets_pending_flag_and_sends_hitl_request(env, monkeypatch):
    tr, store, registry, sockets = env.tr, env.store, env.registry, env.sockets
    socket = FakeSocket()
    sockets["s1"] = socket
    row = await _enqueue(store, "s1", "with-tool")
    registry.register("ws", tr.WsTurnExecutor())

    monkeypatch.setattr(tr, "async_generate", _fake_generate_factory([]))
    monkeypatch.setattr(tr, "get_pending_interrupt", _interrupt_present)

    await tr.on_turn_finished("s1")

    await _wait_until(
        lambda: _status_of(store, row.id) == "DELIVERED",
        what="hitl turn delivered",
    )
    hitl_frames = [f for f in socket.frames if f.get("event") == "hitl_request"]
    assert len(hitl_frames) == 1
    assert hitl_frames[0]["content"] == {"tool_name": "write_file"}
    assert env.hitl_sets == [("s1", True)], "hitl_pending must be set when a hitl_request is sent"
    done = next((f for f in socket.frames if f.get("event") == "done"), None)
    assert done is None, "no done frame must follow a hitl_request"


async def _interrupt_present(session_id: str) -> dict[str, Any] | None:
    return {"tool_name": "write_file"}


@pytest.mark.asyncio
async def test_ws_executor_stop_cancels_child_and_drain_continues_fifo(env, monkeypatch):
    """Stop: child cancelled → row VOIDED by marker → 'stopped' frame → drain continues."""
    tr, store, registry, sockets = env.tr, env.store, env.registry, env.sockets
    socket = FakeSocket()
    sockets["s1"] = socket
    r1 = await _enqueue(store, "s1", "first")
    r2 = await _enqueue(store, "s1", "second")
    registry.register("ws", tr.WsTurnExecutor())

    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def on_cancel():
        for row in await store.list_active("s1"):
            if row.status is UserInputQueueStatus.CLAIMED:
                await store.mark_terminal(row.id, "VOIDED")  # T6 marker stand-in
        cancelled.set()

    monkeypatch.setattr(
        tr,
        "async_generate",
        _fake_generate_factory(
            calls, started=started, block=asyncio.Event(), on_cancel=on_cancel, text_filter="first"
        ),
    )

    await tr.on_turn_finished("s1")
    await _wait_until(lambda: "s1" in env.active_tasks, what="child task registered")
    child = env.active_tasks["s1"]
    assert not child.done()
    child.cancel()

    await _wait_until(
        lambda: _status_of(store, r1.id) == "VOIDED" and _status_of(store, r2.id) == "DELIVERED",
        what="interrupted row voided + queued row drained",
    )
    assert cancelled.is_set()
    events = [f.get("event") for f in socket.frames]
    assert "stopped" in events, "a stopped frame must be sent to the socket"
    assert "echo:second" in [f.get("content") for f in socket.frames if f.get("event") == "chunk"]
    assert env.active_tasks == {}, "child task must be unregistered after cancellation"
