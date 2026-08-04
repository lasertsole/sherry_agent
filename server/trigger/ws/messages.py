import time
import json
from typing import Any
from loguru import logger
from server.trigger.core import app
from runtime import state_register_mem
from server.service import async_generate
from type.message import MultiModalMessage
from robyn import WebSocketDisconnect, WebSocketAdapter


@app.post("/sessions/agent/sse/stop")
def stream_async_stop_handler(request):
    request_json = request.json()
    session_id = request_json.get("session_id", None)
    state_register_mem.set_state(session_id, "answering", False)

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

                multi_modal_message_data: dict[str, Any] | None = obj.get("multi_modal_message", None)
                if not multi_modal_message_data:
                    await websocket.send_text(json.dumps({"event": "error", "session_id": session_id, "content": "Missing multi_modal_message"}))
                    continue

                multi_modal_message = MultiModalMessage(**multi_modal_message_data)

                text_preview = multi_modal_message.text[:50] if multi_modal_message.text else ""
                image_count = len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0
                logger.info(
                    f"Agent WS request started: session_id={session_id}, "
                    f"text_preview='{text_preview}', image_count={image_count}"
                )

                start_time = time.time()
                try:
                    async for chunk in async_generate(session_id, multi_modal_message):
                        await websocket.send_text(json.dumps({"event": "chunk", "session_id": session_id, "content": chunk}))

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