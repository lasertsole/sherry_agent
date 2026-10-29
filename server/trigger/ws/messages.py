import json
import uuid
import asyncio
from typing import Any
from collections.abc import AsyncGenerator
from loguru import logger
from server.trigger.core import app
from runtime import state_register_mem
from runtime.session.relation_register import relation_register
from runtime.session.ws_push import broadcast_to_session
from agent.tools.subagent.registry.session_state import set_hitl_pending
from server.service import get_pending_interrupt, resume_agent
from server.service import input_queue_service as iqs
from server.service import turn_runner
from server.service.stream_driver import StreamDriver
from server.service.stream_diag import public_error_text
from server.service.stream_dispatch import _clear_pending_args
from server.service.trajectory_store import turn_scope
from server.utils.ws_helpers import send_ws_json
from server.trigger import auth
from server.trigger.auth_user import ws_user_check
from pub.types.message import MultiModalMessage
from runtime.session.state_keys import StateKey
from robyn import WebSocketDisconnect, WebSocketAdapter

# Tracks the running stream task per session. A generation/HITL-resume request
# is submitted as a background asyncio task so the receiver loop never blocks
# waiting for a stream to finish. A "stop" frame can then cancel that task via
# asyncio.Task.cancel — which propagates into `async_generate`/`resume_agent`
# (both already handle `asyncio.CancelledError` and reset `answering` in their
# `finally` block), giving an immediate interrupt regardless of whether the
# agent is mid-token-stream, waiting on model TTFB, or stuck in a tool call.
#
# generation turns go through the user-input queue (submit_user_input
# → dispatched WsTurnExecutor); this registry now also covers resume turns and
# the executor-driven child tasks so `stop` and `detect_state` see them all.
_active_tasks: dict[str, asyncio.Task] = {}

# register the ws TurnExecutor on the default queue registry so the
# drain orchestrator can execute ws-routed rows (idempotent).
turn_runner.register_default_ws_executor()

# Push the live task registry DOWN into the service layer (dependency
# inversion): turn_runner adopts/registers these tasks without importing this
# trigger module, keeping the server layer contract intact.
turn_runner.register_active_tasks_provider(lambda: _active_tasks)


async def _send_ws(websocket: WebSocketAdapter, payload: dict[str, Any]) -> None:
    """Best-effort send; swallows send failures (socket may be closing).

    Delegates to the shared :func:`server.utils.ws_helpers.send_ws_json`
    (original log wording preserved via ``warn_prefix``).
    """
    await send_ws_json(websocket, payload, warn_prefix="Agent WS send failed")


async def _run_stream(
    websocket: WebSocketAdapter,
    session_id: str,
    source: AsyncGenerator[dict[str, str]],
    stream_kind: str,
    claim_row_id: str | None = None,
    turn_id: str = "",
) -> None:
    """Drive a stream generator to completion, forwarding chunks to ``websocket``.

    Handles the post-stream HITL interrupt / done detection that the old inline
    loop performed, so cancellation and cleanup are uniform whether the stream
    finished, errored, or was cancelled via ``task.cancel()``.

    ``claim_row_id`` (when set) marks this turn's queue row DELIVERED
    in the finally block; the TurnRunner then drains any rows queued while the
    turn was running. Resume turns pass nothing — they own no queue row.

    The loop itself is the shared :class:`StreamDriver` template;
    ``_AgentWsStreamDriver`` carries this site's knobs.
    """
    # Resume turns never pass through the queue executor, so this is their one
    # contextualize scope (a fresh turn_id: a resume is its own turn).
    with turn_scope(turn_id or uuid.uuid4().hex, session_id):
        await _AgentWsStreamDriver(session_id, websocket, claim_row_id, stream_kind, turn_id).drive(
            source
        )


class _AgentWsStreamDriver(StreamDriver):
    """Driver for the agent WS handler's turns (resume turns via _run_stream).

    ``on_finish`` releases the session's task slot (when owned by this task)
    and marks the turn's queue row terminal (when it owns one), kicking the
    TurnRunner drain.
    """

    def __init__(
        self,
        session_id: str,
        websocket: WebSocketAdapter,
        claim_row_id: str | None,
        stream_kind: str,
        turn_id: str = "",
    ) -> None:
        super().__init__(session_id, websocket, turn_id=turn_id)
        self.claim_row_id = claim_row_id
        self.stream_kind = stream_kind

    async def send_frame(self, payload: dict[str, Any]) -> None:
        # Fan out to every live socket of the session (reconnected viewers and
        # additional viewers each receive the frame exactly once; nothing is
        # ever re-sent — there is no replay log).
        await broadcast_to_session(self.session_id, payload)

    async def check_interrupt(self) -> dict[str, Any] | None:
        return await get_pending_interrupt(self.session_id)

    def apply_hitl_pending(self) -> None:
        set_hitl_pending(self.session_id, True)

    def log_interrupt(self, interrupt: dict[str, Any]) -> None:
        logger.info(
            f"Agent WS HITL interrupt detected: session_id={self.session_id}, "
            f"tool={interrupt.get('tool_name')}"
        )

    def log_cancelled(self) -> None:
        logger.info(f"Agent WS {self.stream_kind} cancelled: session_id={self.session_id}")

    def log_error(self, exc: Exception, elapsed: float) -> None:
        logger.error(
            f"Agent WS {self.stream_kind} failed: session_id={self.session_id}, "
            f"duration={elapsed:.2f}s, error={str(exc)}"
        )

    async def on_finish(self) -> None:
        # Gracefully release the session's task slot if this is the current one.
        current = _active_tasks.get(self.session_id)
        if current is asyncio.current_task():
            _active_tasks.pop(self.session_id, None)
        # the turn's row is marked terminal (when it owns one) and the
        # TurnRunner drains whatever rows were queued while the turn ran.
        await turn_runner.on_turn_finished(self.session_id, self.claim_row_id)


async def _cancel_session(session_id: str) -> None:
    """Immediately cancel any running stream task for ``session_id``."""
    task = _active_tasks.get(session_id)
    if task is not None and not task.done():
        task.cancel()
        logger.info(f"Agent WS stop cancelling active task: session_id={session_id}")
        # Give the cancelled task a chance to send its "stopped"/cleanup frame
        # and reset state before we ack. Don't block indefinitely — the callee
        # stream is expected to surface promptly after cancel.
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.warning(
                f"Agent WS stop: cancelled task raised: session_id={session_id}, error={e}"
            )
    else:
        state_register_mem.set_state(session_id, StateKey.ANSWERING, False)
        logger.info(f"Agent WS stop requested (no active task): session_id={session_id}")


async def _stop_child_runs(session_id: str) -> int:
    """Cancel this session's running subagents when the USER stops the turn.

    Deliberately NOT inside :func:`_cancel_session`: that helper is also used by
    HITL resume and send-now, where killing the session's children would change
    their semantics. The registry kill path already cancels the child task by
    run_id (`task_refs.cancel_task`) and persists KILLED + reconciliation, so no
    session flag or middleware is needed.
    """
    from agent.tools.subagent.control.kill import (
        kill_subagent_run_with_cascade,
        list_killable_children,
    )
    from agent.tools.subagent.registry.session_keys import denormalize_session_key

    requester_key = denormalize_session_key(session_id)
    try:
        killable = list_killable_children(requester_key)
    except Exception as exc:  # noqa: BLE001 - stop must never be blocked by cleanup
        logger.warning("Agent WS stop: child lookup failed for {}: {}", session_id, exc)
        return 0

    killed = 0
    for run in killable:
        try:
            await kill_subagent_run_with_cascade(
                run.run_id,
                reason="user_stop",
                cascade=True,
                requester_session_key=requester_key,
            )
            killed += 1
        except Exception as exc:  # noqa: BLE001 - fail-open per child
            logger.warning("Agent WS stop: killing child run {} failed: {}", run.run_id, exc)
    if killed:
        logger.info("Agent WS stop propagated to {} child run(s): {}", killed, session_id)
    return killed


# ---------------------------------------------------------------------------
# Frame handlers — one per ``type`` we accept, keyed by _FRAME_HANDLERS below.
# A frame WITHOUT a known type is a generation request (the historical default).
# ---------------------------------------------------------------------------


async def _handle_stop(websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]) -> None:
    """``stop``: cancel the running turn and ack on this (possibly separate) socket."""
    await _cancel_session(session_id)
    await _stop_child_runs(session_id)
    # The cancelled turn's driver already broadcast its own `stopped` (carrying
    # `resumable`); this ack answers the REQUESTING socket. It repeats the flag
    # because frames may arrive in either order on a page that just woke up.
    from server.service.resumable_breakpoint import has_breakpoint

    await _send_ws(
        websocket,
        {
            "event": "stopped",
            "session_id": session_id,
            "content": "",
            "resumable": has_breakpoint(session_id),
            "message_ids": [],
        },
    )


async def _handle_hitl_response(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``hitl_response``: cancel any in-flight generation, then resume the graph."""
    decision: str = obj.get("decision", "reject")
    hitl_message: str = obj.get("message", "")
    edited_args: dict[str, Any] | None = obj.get("edited_args")
    logger.info(f"Agent WS HITL resume: session_id={session_id}, decision={decision}")
    # Cancel any in-flight generation before resuming.
    await _cancel_session(session_id)
    # the HITL wait is over — clear the pending flag as
    # the resume turn starts.
    set_hitl_pending(session_id, False)
    resume_turn_id = uuid.uuid4().hex
    task = asyncio.ensure_future(
        _run_stream(
            websocket,
            session_id,
            resume_agent(session_id, decision, hitl_message, edited_args),
            "resume",
            turn_id=resume_turn_id,
        )
    )
    _active_tasks[session_id] = task


async def _handle_cancel_queued(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``cancel_queued``: void a QUEUED row (a CLAIMED one is refused)."""
    msg_id = str(obj.get("msg_id", ""))
    ok = await iqs.cancel_queued_message(session_id, msg_id)
    await _send_ws(
        websocket,
        {
            "event": "queued_cancelled",
            "session_id": session_id,
            "msg_id": msg_id,
            "ok": ok,
        },
    )


async def _handle_edit_queued(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``edit_queued``: rewrite a QUEUED row's payload."""
    msg_id = str(obj.get("msg_id", ""))
    new_text = str(obj.get("message", ""))
    ok = await iqs.update_queued_message(session_id, msg_id, new_text)
    await _send_ws(
        websocket,
        {
            "event": "queued_updated",
            "session_id": session_id,
            "msg_id": msg_id,
            "ok": ok,
        },
    )


async def _handle_send_now(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``send_now``: flag the row to claim first (priority=1), then cancel."""
    msg_id = str(obj.get("msg_id", ""))
    # Order matters: prioritize BEFORE cancelling. `_cancel_session`
    # awaits the task's cancellation and its finally drains the
    # queue, so a late prioritize would let the drain claim
    # whatever was first.
    ok = await iqs.prioritize_queued_message(session_id, msg_id)
    if ok:
        await _cancel_session(session_id)
    await _send_ws(
        websocket,
        {
            "event": "send_now_ack",
            "session_id": session_id,
            "msg_id": msg_id,
            "ok": ok,
        },
    )


async def _handle_generation(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """A user turn: register the reply socket, then queue-then-drain.

    The ``WsTurnExecutor`` resolves the reply socket through
    ``relation_register``, so THIS connection must be registered under the
    session — otherwise every streamed chunk/done frame is silently dropped
    (``_send_ws(None)`` is a no-op). Only generation frames register: stop/hitl
    arrive on separate sockets that never read stream frames.
    """
    multi_modal_message_data: dict[str, Any] | None = obj.get("multi_modal_message", None)
    if not multi_modal_message_data:
        await _send_ws(
            websocket,
            {
                "event": "error",
                "session_id": session_id,
                "content": "Missing multi_modal_message",
            },
        )
        return

    multi_modal_message = MultiModalMessage(**multi_modal_message_data)
    relation_register.register_websocket(session_id, websocket)

    text_preview = multi_modal_message.text[:50] if multi_modal_message.text else ""
    image_count = (
        len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0
    )
    image_path_count = (
        len(multi_modal_message.image_path_list) if multi_modal_message.image_path_list else 0
    )
    logger.info(
        f"Agent WS request started: session_id={session_id}, "
        f"text_preview='{text_preview}', image_count={image_count}, image_path_count={image_path_count}"
    )

    # queue-then-drain. A busy session never gets its turn
    # cancelled — the message is queued and executed FIFO when the
    # current turn finishes (on_turn_finished → TurnRunner drain).
    # The client may declare origin="user"; anything else is
    # rejected (a client can never self-declare an internal
    # origin) and the entry stamps the authoritative source.
    client_origin = obj.get("origin")
    if client_origin is not None and client_origin != "user":
        logger.warning(
            f"Agent WS ignoring unsupported origin={client_origin!r}: session_id={session_id}"
        )
    submit_result = await iqs.submit_user_input(
        session_id,
        multi_modal_message.text,
        "user",
        client_msg_id=obj.get("msg_id"),
    )
    if submit_result.status is iqs.SubmitStatus.QUEUE_FULL:
        await _send_ws(
            websocket,
            {
                "event": "error",
                "session_id": session_id,
                "content": "Input queue full; please try again later",
            },
        )
        return
    if submit_result.status is iqs.SubmitStatus.DEDUPED:
        # Duplicate msg_id: silently ignored.
        return
    if submit_result.status is iqs.SubmitStatus.QUEUED:
        queue_size = await iqs.get_default_queue().count_active(session_id)
        await _send_ws(
            websocket,
            {
                "event": "queued",
                "session_id": session_id,
                "position": submit_result.position,
                "queue_size": queue_size,
                "message_id": obj.get("msg_id"),
            },
        )
        return
    # STARTED: submit inserted the CLAIMED placeholder row and
    # dispatched the registered WsTurnExecutor — that dispatched
    # executor IS this turn's execution; no inline turn here.


async def _handle_resume_stream(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``resume_stream``: reply with a snapshot of the running turn (THIS socket only).

    The snapshot is a targeted response on the requesting socket — never a
    broadcast (other viewers see live frames only) and never a replay of past
    frames: live frames are broadcast exactly once when produced, so the client
    replaces its accumulated text with the snapshot and keeps consuming.
    """
    content: dict[str, Any]
    try:
        from server.service.stream_dispatch import active_turn_snapshot

        snapshot = active_turn_snapshot(session_id)
        if snapshot is not None:
            content = {"status": "ok", **snapshot}
        else:
            from context_engine.store.core import session_has_messages

            content = {"status": "ended" if session_has_messages(session_id) else "gone"}
    except Exception as e:  # noqa: BLE001 — fail-open: the client refetches
        logger.warning(f"resume_stream failed: {e}, session_id={session_id}")
        content = {"status": "unknown"}
    await _send_ws(
        websocket,
        {"event": "stream_resume", "session_id": session_id, "content": content},
    )


async def _handle_edit_message(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``edit_message``: truncate at a user message, then re-run it.

    Three steps, in this order:

    1. locate the edited user row (``row_id`` when the client has it) and count
       the user turns before it — that ordinal is how the checkpoint counts
       turns, while ``turn_num`` is the persisted batch number;
    2. truncate the live checkpoint (``RemoveMessage``) and hard-delete the
       persisted rows from that turn on, so the re-run reuses its turn number;
    3. re-submit the edited text through the ordinary queue path (a FRESH
       ``msg_id``: the old one is tombstoned by the dedupe) — the normal
       ``turn_started`` / ``chunk`` / ``done`` frames follow.

    Only failures ack (``edit_message_ack``): success is visible as the new
    turn's frames, and the client has already updated its rows optimistically.
    """
    turn_num_raw = obj.get("turn_num")
    row_id_raw = obj.get("row_id")
    turn_num = int(turn_num_raw) if isinstance(turn_num_raw, (int, str)) else 0
    row_id = int(row_id_raw) if isinstance(row_id_raw, int) and row_id_raw > 0 else None
    new_text = str(obj.get("new_text") or "").strip()
    msg_id = str(obj.get("msg_id") or "")

    async def _reject(reason: str) -> None:
        await _send_ws(
            websocket,
            {
                "event": "edit_message_ack",
                "session_id": session_id,
                "content": {"ok": False, "reason": reason},
            },
        )

    if turn_num <= 0 or not new_text or not msg_id:
        await _reject("invalid_payload")
        return

    # A live turn (or a pending approval) owns the transcript: truncating under
    # it would interleave two answers in one checkpoint.
    from agent.tools.subagent.registry.session_state import detect_state

    try:
        state = detect_state(session_id)
    except Exception as exc:  # noqa: BLE001 - detection failure reads as busy
        logger.warning(f"edit_message: state detection failed for {session_id}: {exc}")
        await _reject("busy")
        return
    if state.busy:
        await _reject("busy")
        return

    from context_engine.store.core import delete_messages_from_turn, find_user_turn_cut

    try:
        cut = await asyncio.to_thread(find_user_turn_cut, session_id, turn_num, row_id)
    except Exception as exc:  # noqa: BLE001 - a store failure is reported, not raised
        logger.warning(f"edit_message: row lookup failed for {session_id}: {exc}")
        await _reject("failed")
        return
    if cut is None:
        await _reject("not_found")
        return
    ordinal, cut_turn_num = cut

    from agent.state_port import truncate_messages_after_turn

    removed = await truncate_messages_after_turn(session_id, ordinal)
    if removed < 0:
        # The turn is no longer in the live checkpoint: its history was compacted.
        await _reject("compacted")
        return
    try:
        await asyncio.to_thread(delete_messages_from_turn, session_id, cut_turn_num)
    except Exception as exc:  # noqa: BLE001 - history truncation is best-effort
        logger.exception(f"edit_message: message truncation failed for {session_id}: {exc}")

    # The edited turn replaces the interrupted tail (if any): its breakpoint is
    # stale by construction.
    from server.service.resumable_breakpoint import clear_breakpoint

    clear_breakpoint(session_id)

    logger.info(
        f"Agent edit re-run: session_id={session_id}, turn={cut_turn_num}, ordinal={ordinal}"
    )
    try:
        result = await iqs.submit_user_input(session_id, new_text, "user", client_msg_id=msg_id)
    except Exception as exc:  # noqa: BLE001 - surface a refusal instead of a silent no-op
        logger.exception(f"edit_message: re-run submit failed for {session_id}: {exc}")
        await _reject("failed")
        return
    if result.status is iqs.SubmitStatus.QUEUE_FULL:
        await _reject("queue_full")
    elif result.status is iqs.SubmitStatus.DEDUPED:
        await _reject("duplicate")


async def _handle_continue_generation(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``continue_generation``: resume an interrupted pure-text answer.

    Validation happens HERE, before any stream starts, so a stale or missing
    breakpoint is a single ack the client can act on (hide the button, toast,
    refresh history) instead of a half-started turn. ``busy`` protects the
    transcript: truncating/continuing while another turn writes would interleave
    two answers.
    """
    from server.service import continue_generation
    from server.service.resumable_breakpoint import (
        clear_breakpoint,
        read_breakpoint,
        validate_breakpoint,
    )

    active = _active_tasks.get(session_id)
    reason: str | None = None
    if active is not None and not active.done():
        reason = "busy"
    else:
        breakpoint = read_breakpoint(session_id)
        if breakpoint is None:
            reason = "stale"
        elif not await validate_breakpoint(session_id, breakpoint):
            # The transcript moved past the marker: drop the dead breakpoint so
            # the flag does not resurrect on the next history load.
            clear_breakpoint(session_id)
            reason = "stale"
    await _send_ws(
        websocket,
        {
            "event": "continue_generation_ack",
            "session_id": session_id,
            "content": {"ok": reason is None, "reason": reason},
        },
    )
    if reason is not None:
        return

    logger.info(f"Agent continue-generation started: session_id={session_id}")
    relation_register.register_websocket(session_id, websocket)
    continue_turn_id = uuid.uuid4().hex
    task = asyncio.ensure_future(
        _run_stream(
            websocket,
            session_id,
            continue_generation(session_id),
            "continue",
            turn_id=continue_turn_id,
        )
    )
    _active_tasks[session_id] = task


async def _handle_terminal_bg_kill(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """``terminal_bg_kill``: stop ONE background command, leaving the turn alone.

    Command-level cancel: the process tree of that task dies, the conversation
    keeps running, and the session's other background tasks are untouched. The
    acknowledgement says whether the signal landed; the registry's watcher still
    sends ``terminal_bg_exit`` with the final status (the panel's authority).
    """
    task_id = str(obj.get("task_id") or "").strip()
    ok = False
    reason: str | None = "missing task_id"
    if task_id:
        from agent.tools.terminal_bg_registry import background_registry

        ok = background_registry.kill_task(task_id, session_id)
        reason = None if ok else "not_found_or_not_running"
    await _send_ws(
        websocket,
        {
            "event": "terminal_bg_kill_ack",
            "session_id": session_id,
            "content": {"ok": ok, "task_id": task_id, "reason": reason},
        },
    )


#: ``type`` → handler. A generation frame carries no type (the default).
_FRAME_HANDLERS: dict[str, Any] = {
    "stop": _handle_stop,
    "terminal_bg_kill": _handle_terminal_bg_kill,
    "resume_stream": _handle_resume_stream,
    "hitl_response": _handle_hitl_response,
    "cancel_queued": _handle_cancel_queued,
    "edit_queued": _handle_edit_queued,
    "send_now": _handle_send_now,
    "continue_generation": _handle_continue_generation,
    "edit_message": _handle_edit_message,
}


async def _dispatch_frame(
    websocket: WebSocketAdapter, session_id: str, obj: dict[str, Any]
) -> None:
    """Route one received frame: a known ``type``, else a generation request."""
    frame_type = obj.get("type")
    handler = _FRAME_HANDLERS.get(frame_type) if isinstance(frame_type, str) else None
    if handler is None:
        await _handle_generation(websocket, session_id, obj)
        return
    await handler(websocket, session_id, obj)


def _release_socket(websocket: WebSocketAdapter) -> None:
    """Drop this socket's bindings and cancel its turn when it was the last one.

    The unregister is last-writer-wins safe: a newer socket's binding survives
    this exit. A still-running turn is cancelled only when this socket was the
    session's last binding — a newer socket re-binding the same session keeps
    its turn alive.
    """
    disconnected_session_id = relation_register.get_session_id_by_websocket(websocket)
    relation_register.unregister_websocket_by_websocket(websocket)

    if (
        disconnected_session_id is not None
        and relation_register.get_websocket_id_by_session_id(disconnected_session_id) is None
    ):
        # Stream-resume contract: a disconnected viewer must NOT abort the
        # turn — the client reconnects and asks for a snapshot (resume_stream).
        # The turn's task stays in _active_tasks until it finishes, and its
        # on_finish pops it there.
        _clear_pending_args(disconnected_session_id)

    for sid, task in list(_active_tasks.items()):
        if task.done():
            _active_tasks.pop(sid, None)


def _frame_session_allowed(websocket: WebSocketAdapter, session_id: str) -> bool:
    """Session isolation for one inbound frame.

    Once this socket is registered for a session, it must never process frames
    naming another one (a resume request could otherwise read another session's
    data). An unregistered socket is allowed — the generation frame registers it.
    """
    socket_session = relation_register.get_session_id_by_websocket(websocket)
    if socket_session is not None and socket_session != session_id:
        logger.warning(
            "Agent WS frame for another session dropped: "
            f"socket_session={socket_session}, frame_session={session_id}"
        )
        return False
    return True


@app.websocket("/sessions/agent/ws")
async def agent_ws_handler(websocket: WebSocketAdapter):
    """Receive loop: authenticate, route each frame, frame errors for the client."""
    logger.info(f"Agent WebSocket handler started: websocket_id={websocket.id}")
    # This socket drives generation, approves HITL tool calls and cancels runs,
    # so the handshake must carry the gateway token (see server/trigger/auth.py).
    query_params = getattr(websocket, "query_params", {}) or {}
    refusal = auth.check_ws(query_params.get(auth.TOKEN_QUERY_PARAM, None))
    if refusal is not None:
        logger.warning(f"Agent WS connection rejected: {refusal}")
        await websocket.close()
        return
    # Second layer (login gate): a single-use ticket while enforcement is active
    # — this socket can drive generation, so it is worth the extra check.
    user_refusal = await ws_user_check(query_params)
    if user_refusal is not None:
        logger.warning(f"Agent WS connection rejected: {user_refusal}")
        await websocket.close()
        return

    # Bound before the loop so the receive-loop catch-all can always reference
    # it when composing an error frame (never unbound there).
    session_id: str | None = None
    try:
        while True:
            try:
                msg: str = await websocket.receive_text()
                obj: dict[str, Any] = json.loads(msg)

                session_id = obj.get("session_id", None)
                if session_id is None:
                    await _send_ws(
                        websocket,
                        {"event": "error", "session_id": None, "content": "Missing session_id"},
                    )
                    continue

                if not _frame_session_allowed(websocket, session_id):
                    continue

                await _dispatch_frame(websocket, session_id, obj)
            except (WebSocketDisconnect, ConnectionResetError):
                # The socket is gone — propagate to the outer handler so the
                # receive loop exits. Swallowing these here would hot-loop on
                # a closed socket (receive_text raises without awaiting).
                raise
            except json.JSONDecodeError as e:
                logger.warning(f"Agent WS JSON decode error: {e}, websocket_id={websocket.id}")
                await _send_ws(
                    websocket, {"event": "error", "session_id": None, "content": "Invalid JSON"}
                )
            except Exception as e:
                logger.warning(f"Error in agent_ws_handler: {e}, websocket_id={websocket.id}")
                # The client socket is parked waiting for stream frames; without
                # this frame it would hang forever with no terminal state
                # (chunk/done/error). Best-effort: _send_ws swallows send failures.
                await _send_ws(
                    websocket,
                    {
                        "event": "error",
                        "session_id": session_id,
                        "content": public_error_text(e),
                    },
                )
    except (WebSocketDisconnect, ConnectionResetError) as e:
        logger.info(f"Agent WS client {websocket.id} disconnected: {e}")
    except Exception as e:
        logger.warning(f"Agent WS client {websocket.id} disconnected: {e}")

    _release_socket(websocket)
