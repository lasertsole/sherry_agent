"""Task 7 integration tests: agent_ws_handler queue-then-drain wiring.

Contract under test (server/trigger/ws/messages.py rewiring + turn_runner):

- generate frame → ``submit_user_input``:
  - idle (STARTED): the dispatched WsTurnExecutor drives the turn — chunk/done
    frames stream to the socket and NO "queued" frame is sent. The handler
    itself must NOT drive an inline turn (no double streaming).
  - busy: exact queued frame ``{"event": "queued", "session_id", "position",
    "queue_size", "message_id"}``; no turn starts; row stays QUEUED.
  - queue full: exact error frame; no new row.
  - duplicate msg_id: silent — no frames, no new row.
- stop frame: cancels ONLY the current turn (its row VOIDED via marker), sends
  "stopped", then the drain continues FIFO with the queued row.
- hitl_response frame: clears hitl_pending, drives resume_agent, and the drain
  picks up rows queued during the HITL wait afterwards.
- hitl interrupt: hitl_request frame + REAL session_state.set_hitl_pending.
"""

import asyncio
import json
import sqlite3
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from robyn import WebSocketDisconnect

import server.service.turn_runner as turn_runner
from agent.tools.subagent.registry import session_state
from server.queue.user_input_queue import (
    MAX_ACTIVE_PER_SESSION,
    UserInputQueue,
    UserInputQueueStatus,
)
from server.service import input_queue_service as iqs

pytestmark = pytest.mark.unit


def _wsm():
    from server.trigger.ws import messages as wsm  # noqa: PLC0415

    return wsm


class _HandlerSocket:
    """WS double for driving agent_ws_handler directly.

    receive_text parks on a waiter until the next push (the handler must not
    see a spurious disconnect mid-test); close() surfaces a real
    WebSocketDisconnect so the handler exits. send_text is a real coroutine
    (T8 lesson) that records decoded frames.
    """

    def __init__(self) -> None:
        self.id = "test-ws-id"  # messages.py logs websocket.id at handler entry
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


def _msg_frame(session_id: str, msg_id: str, text: str) -> dict[str, Any]:
    return {"session_id": session_id, "msg_id": msg_id, "multi_modal_message": {"text": text}}


def _payload(text: str) -> str:
    return json.dumps({"text": text, "image_base64_list": []}, ensure_ascii=False)


async def _enqueue(store: UserInputQueue, session_id: str, text: str, **kwargs):
    row, _position = await store.enqueue(session_id, _payload(text), "user", **kwargs)
    return row


def _status_of(store: UserInputQueue, row_id: str) -> str | None:
    conn = sqlite3.connect(store._db_path)  # noqa: SLF001
    try:
        cur = conn.execute("SELECT status FROM user_input_queue WHERE id = ?", (row_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _events(socket: _HandlerSocket) -> list[str]:
    return [f.get("event") for f in socket.frames]


async def _wait_until(pred, timeout: float = 5.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def _no_interrupt(session_id: str) -> dict[str, Any] | None:
    return None


def _simple_generate_factory(calls, *, on_text=None):
    async def fake(session_id, message, is_stream=True, origin=None):
        text = getattr(message, "text", str(message))
        calls.append((session_id, text))
        if on_text is not None:
            await on_text(text)
        yield {"type": "text", "content": f"echo:{text}"}
        yield {"type": "meta", "model_name": "fake", "input_tokens": 3, "output_tokens": 5}

    return fake


def _resume_generate_factory(calls):
    async def fake(session_id, decision, hitl_message, edited_args):
        calls.append((session_id, decision, hitl_message))
        yield {"type": "text", "content": "echo:resume"}
        yield {"type": "meta", "model_name": "fake", "input_tokens": 7, "output_tokens": 9}

    return fake


@pytest.fixture
def ws_env(monkeypatch, tmp_path: Path):
    store = UserInputQueue(db_path=tmp_path / "subagent_registry.db")
    wsm = _wsm()
    detector = {"busy": False}
    inline_calls: list[tuple[str, str]] = []
    drain_calls: list[tuple[str, str]] = []
    socket_holder: dict[str, _HandlerSocket] = {}

    def fake_detect(session_key: str):
        return SimpleNamespace(
            session_id=session_key,
            busy=detector["busy"],
            reason="busy" if detector["busy"] else "idle",
        )

    monkeypatch.setattr(iqs, "get_default_queue", lambda: store)
    monkeypatch.setattr(iqs, "detect_state", fake_detect)
    # The generate path must go through the queue executor; an inline
    # async_generate call would be the double-turn regression this guards.
    monkeypatch.setattr(wsm, "async_generate", _simple_generate_factory(inline_calls))
    monkeypatch.setattr(turn_runner, "async_generate", _simple_generate_factory(drain_calls))
    monkeypatch.setattr(wsm, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(turn_runner, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(
        turn_runner, "get_websocket_by_session_id", lambda session_id: socket_holder.get("socket")
    )

    return SimpleNamespace(
        store=store,
        wsm=wsm,
        detector=detector,
        inline_calls=inline_calls,
        drain_calls=drain_calls,
        socket_holder=socket_holder,
    )


@pytest.fixture(autouse=True)
def _hermetic_state():
    wsm = _wsm()
    wsm._active_tasks.clear()
    turn_runner._DRAIN_TASKS.clear()
    iqs._SESSION_LOCKS.clear()
    session_state._HITL_PENDING.clear()
    yield
    for task in list(turn_runner._DRAIN_TASKS.values()):
        task.cancel()
    for task in list(wsm._active_tasks.values()):
        task.cancel()
    wsm._active_tasks.clear()
    turn_runner._DRAIN_TASKS.clear()
    iqs._SESSION_LOCKS.clear()
    session_state._HITL_PENDING.clear()


# ---------------------------------------------------------------------------
# Generate frame → submit_user_input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_message_starts_turn_and_completes_without_queued_frame(ws_env):
    store, wsm, drain_calls, inline_calls = (
        ws_env.store,
        ws_env.wsm,
        ws_env.drain_calls,
        ws_env.inline_calls,
    )
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push(_msg_frame("s1", "m1", "hello"))
        await _wait_until(
            lambda: any(f.get("event") == "done" for f in socket.frames),
            what="done frame for idle message",
        )
        await _wait_until(lambda: turn_runner._DRAIN_TASKS == {}, what="drain exited")
        assert "queued" not in _events(socket), "STARTED must not send a queued frame"
        assert inline_calls == [], "handler must NOT drive an inline async_generate turn"
        assert drain_calls == [("s1", "hello")], "the executor drives the turn exactly once"
        assert _events(socket)[-1] == "done"
        assert wsm._active_tasks == {}, "executor task must be unregistered after completion"


@pytest.mark.asyncio
async def test_busy_message_gets_exact_queued_frame_and_no_turn(ws_env):
    store, drain_calls = ws_env.store, ws_env.drain_calls
    ws_env.detector["busy"] = True
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push(_msg_frame("s1", "m1", "while-busy"))
        await _wait_until(lambda: socket.frames, what="queued frame")
        assert socket.frames == [
            {
                "event": "queued",
                "session_id": "s1",
                "position": 1,
                "queue_size": 1,
                "message_id": "m1",
            }
        ], "queued frame payload must match the Task 7 contract exactly"
        assert drain_calls == [], "a busy session must not start a turn"
        assert ws_env.wsm._active_tasks == {}
        row = (await store.list_active("s1"))[0]
        assert row.status is UserInputQueueStatus.QUEUED


@pytest.mark.asyncio
async def test_queue_full_returns_error_frame(ws_env):
    store = ws_env.store
    ws_env.detector["busy"] = True
    for i in range(MAX_ACTIVE_PER_SESSION):
        await _enqueue(store, "s1", f"pre-{i}")
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push(_msg_frame("s1", "m-overflow", "one too many"))
        await _wait_until(lambda: socket.frames, what="queue-full error frame")
        assert socket.frames == [
            {
                "event": "error",
                "session_id": "s1",
                "content": "Input queue full; please try again later",
            }
        ], "QUEUE_FULL must produce the exact error frame"
        assert await store.count_active("s1") == MAX_ACTIVE_PER_SESSION, "no row must be added"


@pytest.mark.asyncio
async def test_duplicate_msg_id_is_silent(ws_env):
    store = ws_env.store
    ws_env.detector["busy"] = True
    await _enqueue(store, "s1", "original", client_msg_id="m1")
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push(_msg_frame("s1", "m1", "duplicate"))
        await asyncio.sleep(0.1)
        assert socket.frames == [], "DEDUPED must be silent"
        assert await store.count_active("s1") == 1


# ---------------------------------------------------------------------------
# Stop: cancel current turn only, drain continues FIFO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_current_turn_and_drain_continues_fifo(ws_env):
    store, wsm, drain_calls = ws_env.store, ws_env.wsm, ws_env.drain_calls
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    started = asyncio.Event()
    cancelled = asyncio.Event()
    block = asyncio.Event()

    async def block_and_void(text: str):
        if text != "first":
            return
        started.set()
        try:
            await block.wait()
        except asyncio.CancelledError:
            for row in await store.list_active("s1"):
                if row.status is UserInputQueueStatus.CLAIMED:
                    await store.mark_terminal(row.id, "VOIDED")  # T6 marker stand-in
            cancelled.set()
            raise

    wsm.async_generate = _simple_generate_factory(ws_env.inline_calls)  # unused here
    turn_runner.async_generate = _simple_generate_factory(drain_calls, on_text=block_and_void)

    async with _handler_session(socket):
        socket.push(_msg_frame("s1", "m1", "first"))
        await _wait_until(lambda: "s1" in wsm._active_tasks, what="executor task registered")
        ws_env.detector["busy"] = True
        socket.push(_msg_frame("s1", "m2", "second"))
        await _wait_until(
            lambda: any(f.get("event") == "queued" for f in socket.frames),
            what="queued frame for m2",
        )
        # The submit behind m2 inserted the queued row — track THAT row
        # (a manual _enqueue here would create a second row the drain must
        # also finish before list_active runs dry).
        r2 = next(
            row
            for row in await store.list_active("s1")
            if row.status is UserInputQueueStatus.QUEUED
        )

        socket.push({"type": "stop", "session_id": "s1"})
        await _wait_until(
            lambda: _status_of(store, r2.id) == "DELIVERED" and cancelled.is_set(),
            what="interrupted turn voided + queued turn drained",
        )

    r1 = (await store.list_active("s1")) + []
    assert r1 == [], "both rows must be terminal after the drain"
    events = _events(socket)
    assert "stopped" in events, "stop must surface a stopped frame"
    assert events.count("stopped") >= 1
    assert "echo:second" in [f.get("content") for f in socket.frames if f.get("event") == "chunk"]
    assert events[-1] == "done", "the drained turn finishes with a done frame"
    assert wsm._active_tasks == {}
    await _wait_until(
        lambda: turn_runner._DRAIN_TASKS == {}, what="drain task self-cleanup"
    )


@pytest.mark.asyncio
async def test_stop_with_no_active_turn_only_acks(ws_env):
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push({"type": "stop", "session_id": "s1"})
        await _wait_until(lambda: socket.frames, what="stop ack")
        assert socket.frames == [{"event": "stopped", "session_id": "s1", "content": ""}]
        assert ws_env.wsm._active_tasks == {}


# ---------------------------------------------------------------------------
# Resume (hitl_response) + hitl interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_clears_hitl_pending_and_drains_queued_after(ws_env, monkeypatch):
    store, wsm = ws_env.store, ws_env.wsm
    session_state.set_hitl_pending("s1", True)
    r1 = await _enqueue(store, "s1", "queued-during-hitl")  # queued during the HITL wait
    resume_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(wsm, "resume_agent", _resume_generate_factory(resume_calls))

    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push(
            {"type": "hitl_response", "session_id": "s1", "decision": "approve", "message": ""}
        )
        await _wait_until(
            lambda: _status_of(store, r1.id) == "DELIVERED",
            what="resume completed and queued row drained",
        )
        await _wait_until(lambda: turn_runner._DRAIN_TASKS == {}, what="drain exited")

    assert session_state._is_hitl_pending("s1") is False, "resume must clear hitl_pending"
    assert resume_calls == [("s1", "approve", "")]
    contents = [f.get("content") for f in socket.frames if f.get("event") == "chunk"]
    assert "echo:resume" in contents
    assert "echo:queued-during-hitl" in contents, "the drain must execute the HITL-wait row"
    assert _events(socket).count("done") == 2, "resume done + drained-turn done"


@pytest.mark.asyncio
async def test_hitl_interrupt_sets_hitl_pending(ws_env, monkeypatch):
    store = ws_env.store

    async def interrupt_present(session_id: str):
        return {"tool_name": "write_file"}

    monkeypatch.setattr(turn_runner, "get_pending_interrupt", interrupt_present)
    socket = _HandlerSocket()
    ws_env.socket_holder["socket"] = socket

    async with _handler_session(socket):
        socket.push(_msg_frame("s1", "m1", "needs-approval"))
        await _wait_until(
            lambda: any(f.get("event") == "hitl_request" for f in socket.frames),
            what="hitl_request frame",
        )

    assert socket.frames[-1] == {
        "event": "hitl_request",
        "session_id": "s1",
        "content": {"tool_name": "write_file"},
    }
    assert session_state._is_hitl_pending("s1") is True, "hitl_pending must be set for real"
    rows = await store.list_active("s1")
    assert rows == [], "the hitl turn's row must be terminal (DELIVERED)"
    assert "done" not in _events(socket), "no done frame may follow a hitl_request"
