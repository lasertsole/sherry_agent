import time
import json
import asyncio
from typing import Any, AsyncGenerator
from loguru import logger
from server.trigger.core import app
from runtime import state_register_mem
from server.service import async_generate, get_pending_interrupt, resume_agent
from type.message import MultiModalMessage
from robyn import WebSocketDisconnect, WebSocketAdapter

# Tracks the running stream task per session. A generation/HITL-resume request
# is submitted as a background asyncio task so the receiver loop never blocks
# waiting for a stream to finish. A "stop" frame can then cancel that task via
# asyncio.Task.cancel() — which propagates into `async_generate`/`resume_agent`
# (both already handle `asyncio.CancelledError` and reset `answering` in their
# `finally` block), giving an immediate interrupt regardless of whether the
# agent is mid-token-stream, waiting on model TTFB, or stuck in a tool call.
_active_tasks: dict[str, asyncio.Task] = {}


async def _send_ws(websocket: WebSocketAdapter, payload: dict[str, Any]) -> None:
    """Best-effort send; swallows send failures (socket may be closing)."""
    try:
        await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.warning(f"Agent WS send failed: {e}")


async def _run_stream(
    websocket: WebSocketAdapter,
    session_id: str,
    source: AsyncGenerator[dict[str, str], None],
    stream_kind: str,
) -> None:
    """Drive a stream generator to completion, forwarding chunks to ``websocket``.

    Handles the post-stream HITL interrupt / done detection that the old inline
    loop performed, so cancellation and cleanup are uniform whether the stream
    finished, errored, or was cancelled via ``task.cancel()``.
    """
    start_time = time.time()
    meta: dict[str, Any] = {}
    try:
        async for chunk in source:
            if chunk.get("type") == "meta":
                # Model metadata travels to the client on the done frame, not
                # as a regular chunk.
                meta = {k: v for k, v in chunk.items() if k != "type"}
                continue
            await _send_ws(
                websocket,
                {
                    "event": "chunk",
                    "session_id": session_id,
                    **chunk,
                },
            )

        # After the stream ends, check if the agent paused for HITL approval.
        interrupt_data = await get_pending_interrupt(session_id)
        if interrupt_data:
            logger.info(
                f"Agent WS HITL interrupt detected: session_id={session_id}, "
                f"tool={interrupt_data.get('tool_name')}"
            )
            await _send_ws(
                websocket,
                {
                    "event": "hitl_request",
                    "session_id": session_id,
                    "content": interrupt_data,
                },
            )
        else:
            await _send_ws(
                websocket,
                {
                    "event": "done",
                    "session_id": session_id,
                    "content": "",
                    "model_name": meta.get("model_name", ""),
                    "input_tokens": meta.get("input_tokens", 0),
                    "output_tokens": meta.get("output_tokens", 0),
                },
            )
    except asyncio.CancelledError:
        # asyncio.Task.cancel() landed; the generator already yields
        # "Request cancelled" (left inside the stream) and resets answering.
        logger.info(f"Agent WS {stream_kind} cancelled: session_id={session_id}")
        await _send_ws(
            websocket,
            {
                "event": "stopped",
                "session_id": session_id,
                "content": "Request cancelled",
            },
        )
        raise
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"Agent WS {stream_kind} failed: session_id={session_id}, "
            f"duration={elapsed:.2f}s, error={str(e)}"
        )
        await _send_ws(
            websocket,
            {
                "event": "error",
                "session_id": session_id,
                "content": str(e),
            },
        )
    finally:
        # Gracefully release the session's task slot if this is the current one.
        current = _active_tasks.get(session_id)
        if current is asyncio.current_task():
            _active_tasks.pop(session_id, None)


async def _cancel_session(session_id: str) -> None:
    """Immediately cancel any running stream task for ``session_id``."""
    task = _active_tasks.get(session_id)
    if task is not None and not task.done():
        task.cancel()
        logger.info(f"Agent WS stop cancelling active task: session_id={session_id}")
        # Give the cancelled task a chance to send its "stopped"/cleanup frame
        # and reset state before we ack. Don't block indefinitely — the callee
        # stream is expected to surface promptly after cancel().
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    else:
        state_register_mem.set_state(session_id, "answering", False)
        logger.info(f"Agent WS stop requested (no active task): session_id={session_id}")


@app.websocket("/sessions/agent/ws")
async def agent_ws_handler(websocket: WebSocketAdapter):
    logger.info(f"Agent WebSocket handler started: websocket_id={websocket.id}")
    try:
        while True:
            try:
                msg: str = await websocket.receive_text()
                obj: dict[str, Any] = json.loads(msg)

                session_id: str | None = obj.get("session_id", None)
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

                # Cancel any existing generation for this session before starting
                # a new one (mirrors the "one answering turn at a time" invariant).
                await _cancel_session(session_id)

                task = asyncio.ensure_future(
                    _run_stream(
                        websocket,
                        session_id,
                        async_generate(session_id, multi_modal_message),
                        "generate",
                    )
                )
                _active_tasks[session_id] = task
            except json.JSONDecodeError as e:
                logger.warning(f"Agent WS JSON decode error: {e}, websocket_id={websocket.id}")
                await _send_ws(
                    websocket, {"event": "error", "session_id": None, "content": "Invalid JSON"}
                )
            except Exception as e:
                logger.warning(f"Error in agent_ws_handler: {e}, websocket_id={websocket.id}")
    except (WebSocketDisconnect, ConnectionResetError) as e:
        logger.warning(f"Agent WS client {websocket.id} disconnected: {e}")
    except Exception as e:
        logger.warning(f"Agent WS client {websocket.id} disconnected: {e}")

    # Clean up any task that was bound to this now-closed socket.
    for sid, task in list(_active_tasks.items()):
        # A task holds `websocket` in its closure; we can't reliably inspect it,
        # so only reap tasks that are already finished.
        if task.done():
            _active_tasks.pop(sid, None)
