import json
import asyncio
from typing import Any
from collections.abc import AsyncGenerator
from loguru import logger
from server.trigger.core import app
from runtime import state_register_mem
from runtime.relation_register import relation_register
from agent.tools.subagent.registry.session_state import set_hitl_pending
from server.service import get_pending_interrupt, resume_agent
from server.service import input_queue_service as iqs
from server.service import turn_runner
from server.service.stream_driver import StreamDriver
from server.utils.ws_helpers import send_ws_json
from type.message import MultiModalMessage
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

    Audit 2.1.2: delegates to the shared :func:`server.utils.ws_helpers.send_ws_json`
    (original log wording preserved via ``warn_prefix``).
    """
    await send_ws_json(websocket, payload, warn_prefix="Agent WS send failed")


async def _run_stream(
    websocket: WebSocketAdapter,
    session_id: str,
    source: AsyncGenerator[dict[str, str]],
    stream_kind: str,
    claim_row_id: str | None = None,
) -> None:
    """Drive a stream generator to completion, forwarding chunks to ``websocket``.

    Handles the post-stream HITL interrupt / done detection that the old inline
    loop performed, so cancellation and cleanup are uniform whether the stream
    finished, errored, or was cancelled via ``task.cancel()``.

    ``claim_row_id`` (when set) marks this turn's queue row DELIVERED
    in the finally block; the TurnRunner then drains any rows queued while the
    turn was running. Resume turns pass nothing — they own no queue row.

    Audit 2.1.3: the loop itself is the shared :class:`StreamDriver` template;
    ``_AgentWsStreamDriver`` carries this site's knobs.
    """
    await _AgentWsStreamDriver(session_id, websocket, claim_row_id, stream_kind).drive(source)


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
    ) -> None:
        super().__init__(session_id, websocket)
        self.claim_row_id = claim_row_id
        self.stream_kind = stream_kind

    async def send_frame(self, payload: dict[str, Any]) -> None:
        await _send_ws(self.websocket, payload)

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
        state_register_mem.set_state(session_id, "answering", False)
        logger.info(f"Agent WS stop requested (no active task): session_id={session_id}")


@app.websocket("/sessions/agent/ws")
async def agent_ws_handler(websocket: WebSocketAdapter):
    logger.info(f"Agent WebSocket handler started: websocket_id={websocket.id}")
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

                if obj.get("type") == "stop":
                    await _cancel_session(session_id)
                    # ack on the (possibly separate) stop connection
                    await _send_ws(
                        websocket, {"event": "stopped", "session_id": session_id, "content": ""}
                    )
                    continue

                if obj.get("type") == "hitl_response":
                    decision: str = obj.get("decision", "reject")
                    hitl_message: str = obj.get("message", "")
                    edited_args: dict[str, Any] | None = obj.get("edited_args")
                    logger.info(
                        f"Agent WS HITL resume: session_id={session_id}, decision={decision}"
                    )
                    # Cancel any in-flight generation before resuming.
                    await _cancel_session(session_id)
                    # the HITL wait is over — clear the pending flag as
                    # the resume turn starts.
                    set_hitl_pending(session_id, False)
                    task = asyncio.ensure_future(
                        _run_stream(
                            websocket,
                            session_id,
                            resume_agent(session_id, decision, hitl_message, edited_args),
                            "resume",
                        )
                    )
                    _active_tasks[session_id] = task
                    continue

                multi_modal_message_data: dict[str, Any] | None = obj.get(
                    "multi_modal_message", None
                )
                if not multi_modal_message_data:
                    await _send_ws(
                        websocket,
                        {
                            "event": "error",
                            "session_id": session_id,
                            "content": "Missing multi_modal_message",
                        },
                    )
                    continue

                multi_modal_message = MultiModalMessage(**multi_modal_message_data)

                # the WsTurnExecutor resolves the reply socket through
                # relation_register, so THIS connection must be registered under
                # the session — otherwise every streamed chunk/done frame is
                # silently dropped (_send_ws(None) is a no-op). Only generation
                # frames register: stop/hitl arrive on separate sockets that
                # never read stream frames.
                relation_register.register_websocket(session_id, websocket)

                text_preview = multi_modal_message.text[:50] if multi_modal_message.text else ""
                image_count = (
                    len(multi_modal_message.image_base64_list)
                    if multi_modal_message.image_base64_list
                    else 0
                )
                image_path_count = (
                    len(multi_modal_message.image_path_list)
                    if multi_modal_message.image_path_list
                    else 0
                )
                logger.info(
                    f"Agent WS request started: session_id={session_id}, "
                    f"text_preview='{text_preview}', image_count={image_count}, image_path_count={image_path_count}"
                )

                # queue-then-drain. A busy session never gets its turn
                # cancelled — the message is queued and executed FIFO when the
                # current turn finishes (on_turn_finished → TurnRunner drain).
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
                    continue
                if submit_result.status is iqs.SubmitStatus.DEDUPED:
                    # Duplicate msg_id: silently ignored.
                    continue
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
                    continue
                # STARTED: submit inserted the CLAIMED placeholder row and
                # dispatched the registered WsTurnExecutor — that dispatched
                # executor IS this turn's execution; no inline turn here.
                continue
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
                    {"event": "error", "session_id": session_id, "content": str(e)},
                )
    except (WebSocketDisconnect, ConnectionResetError) as e:
        logger.warning(f"Agent WS client {websocket.id} disconnected: {e}")
    except Exception as e:
        logger.warning(f"Agent WS client {websocket.id} disconnected: {e}")

    # Release the session→socket binding. The unregister is last-writer-wins
    # safe: a newer socket's binding survives this (possibly stale) exit.
    relation_register.unregister_websocket_by_websocket(websocket)

    # Clean up any task that was bound to this now-closed socket.
    for sid, task in list(_active_tasks.items()):
        # A task holds `websocket` in its closure; we can't reliably inspect it,
        # so only reap tasks that are already finished.
        if task.done():
            _active_tasks.pop(sid, None)
