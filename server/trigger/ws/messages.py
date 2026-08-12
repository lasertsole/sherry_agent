import time
import json
from typing import Any
from loguru import logger
from server.trigger.core import app
from runtime import state_register_mem
from server.service import async_generate, get_pending_interrupt, resume_agent
from type.message import MultiModalMessage
from robyn import WebSocketDisconnect, WebSocketAdapter


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
                    await websocket.send_text(json.dumps({"event": "error", "session_id": None, "content": "Missing session_id"}))
                    continue

                # Stop an ongoing generation for this session (replaces the old
                # HTTP POST /sessions/agent/sse/stop endpoint).
                if obj.get("type") == "stop":
                    state_register_mem.set_state(session_id, "answering", False)
                    logger.info(f"Agent WS stop requested: session_id={session_id}")
                    await websocket.send_text(json.dumps({"event": "stopped", "session_id": session_id, "content": ""}))
                    continue

                # HITL resume — client sends back a human decision after an interrupt
                if obj.get("type") == "hitl_response":
                    decision: str = obj.get("decision", "reject")
                    hitl_message: str = obj.get("message", "")
                    edited_args: dict[str, Any] | None = obj.get("edited_args")
                    logger.info(
                        f"Agent WS HITL resume: session_id={session_id}, decision={decision}"
                    )
                    try:
                        async for chunk in resume_agent(
                            session_id, decision, hitl_message, edited_args,
                        ):
                            await websocket.send_text(json.dumps({
                                "event": "chunk", "session_id": session_id,
                                "type": chunk["type"], "content": chunk["content"],
                            }))

                        # Check for another interrupt after resume
                        interrupt_data = await get_pending_interrupt(session_id)
                        if interrupt_data:
                            await websocket.send_text(json.dumps({
                                "event": "hitl_request", "session_id": session_id,
                                "content": interrupt_data,
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "event": "done", "session_id": session_id, "content": "",
                            }))
                    except Exception as e:
                        logger.error(
                            f"Agent WS HITL resume failed: session_id={session_id}, error={str(e)}"
                        )
                        await websocket.send_text(json.dumps({
                            "event": "error", "session_id": session_id, "content": str(e),
                        }))
                    continue

                multi_modal_message_data: dict[str, Any] | None = obj.get("multi_modal_message", None)
                if not multi_modal_message_data:
                    await websocket.send_text(json.dumps({"event": "error", "session_id": session_id, "content": "Missing multi_modal_message"}))
                    continue

                multi_modal_message = MultiModalMessage(**multi_modal_message_data)

                text_preview = multi_modal_message.text[:50] if multi_modal_message.text else ""
                image_count = len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0
                image_path_count = len(multi_modal_message.image_path_list) if multi_modal_message.image_path_list else 0
                logger.info(
                    f"Agent WS request started: session_id={session_id}, "
                    f"text_preview='{text_preview}', image_count={image_count}, image_path_count={image_path_count}"
                )

                start_time = time.time()
                try:
                    async for chunk in async_generate(session_id, multi_modal_message):
                        await websocket.send_text(json.dumps({"event": "chunk", "session_id": session_id, "type": chunk["type"], "content": chunk["content"]}))

                    # After stream ends, check if the agent paused for HITL approval
                    interrupt_data = await get_pending_interrupt(session_id)
                    if interrupt_data:
                        logger.info(
                            f"Agent WS HITL interrupt detected: session_id={session_id}, "
                            f"tool={interrupt_data.get('tool_name')}"
                        )
                        await websocket.send_text(json.dumps({
                            "event": "hitl_request", "session_id": session_id,
                            "content": interrupt_data,
                        }))
                    else:
                        await websocket.send_text(json.dumps({"event": "done", "session_id": session_id, "content": ""}))
                    elapsed = time.time() - start_time
                    logger.info(
                        f"Agent WS request completed: session_id={session_id}, duration={elapsed:.2f}s"
                    )
                except Exception as e:
                    elapsed = time.time() - start_time
                    logger.error(
                        f"Agent WS request failed: session_id={session_id}, duration={elapsed:.2f}s, error={str(e)}"
                    )
                    await websocket.send_text(json.dumps({"event": "error", "session_id": session_id, "content": str(e)}))
            except json.JSONDecodeError as e:
                logger.warning(f"Agent WS JSON decode error: {e}, websocket_id={websocket.id}")
                await websocket.send_text(json.dumps({"event": "error", "session_id": None, "content": "Invalid JSON"}))
            except Exception as e:
                logger.warning(f"Error in agent_ws_handler: {e}, websocket_id={websocket.id}")
    except (WebSocketDisconnect, ConnectionResetError, Exception) as e:
        logger.warning(f"Agent WS client {websocket.id} disconnected: {e}")