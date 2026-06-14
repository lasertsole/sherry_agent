import json
import time
from .core import app
from loguru import logger
from type import MultiModalMessage
from runtime import relation_register
from typing import Any, Dict, Callable
from server.service import async_generate, session_end, clear_session
from robyn import SSEMessage, SSEResponse, WebSocketDisconnect, WebSocketAdapter

@logger.catch
@app.post("/sessions/agent/sse")
async def stream_async_events_handler(request):
    start_time = time.time()
    request_json = request.json()

    session_id: str = request_json.get("session_id", None)
    if not session_id:
        logger.warning("SSE request missing session_id")
        return SSEMessage("Please provide a session ID")

    multi_modal_message: MultiModalMessage = request_json.get("multi_modal_message", None)
    if not multi_modal_message:
        logger.warning(f"SSE request missing multi_modal_message: session_id={session_id}")
        return SSEMessage("Please provide user input")
    multi_modal_message = MultiModalMessage(**multi_modal_message)

    # Log request summary
    text_preview = multi_modal_message.text[:50] if multi_modal_message.text else ""
    image_count = len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0
    logger.info(
        f"SSE request started: session_id={session_id}, "
        f"text_preview='{text_preview}', image_count={image_count}"
    )

    try:
        response = SSEResponse(async_generate(session_id, multi_modal_message))
        elapsed = time.time() - start_time
        logger.info(
            f"SSE request completed: session_id={session_id}, duration={elapsed:.2f}s"
        )
        return response
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"SSE request failed: session_id={session_id}, duration={elapsed:.2f}s, error={str(e)}"
        )
        raise


ws_event_processor_dict: Dict[str, Callable[[str, str | dict[str, Any]], str]] = {}


async def ws_processor(session_id: str, event: str, content: str | dict[str, Any]) -> Any:
    try:
        processor: Callable[[str, str | dict[str, Any]], str] | None = ws_event_processor_dict.get(event, None)
        if processor is None:
            logger.debug(f"No processor registered for event: {event}, session_id={session_id}")
            return None

        logger.debug(f"Processing WS event: event={event}, session_id={session_id}")
        return processor(session_id, content)
    except Exception as e:
        logger.warning(f"ws_processor error happened: {e}, session_id={session_id}, event={event}")
        return None


@app.websocket("/sessions/ws")
async def ws_handler(websocket: WebSocketAdapter):
    logger.info(f"WebSocket handler started: websocket_id={websocket.id}")
    try:
        while True:
            try:
                msg: str = await websocket.receive_text()
                obj: dict[str, Any] = json.loads(msg)
                session_id: str | None = obj.get("session_id", None)
                if session_id is None:
                    logger.debug(f"WebSocket message missing session_id: websocket_id={websocket.id}")
                    continue

                event: str | None = obj.get("event", None)
                if event is None:
                    logger.debug(f"WebSocket message missing event: session_id={session_id}")
                    continue

                content: str | dict[str, Any] | None = obj.get("content", None)
                if content is None:
                    logger.debug(f"WebSocket message missing content: session_id={session_id}, event={event}")
                    continue

                logger.debug(
                    f"WebSocket message received: session_id={session_id}, event={event}, "
                    f"content_type={type(content).__name__}"
                )

                res: Any = await ws_processor(session_id=session_id, event=event, content=content)

                await websocket.send_text(json.dumps(res))
                logger.debug(f"WebSocket response sent: session_id={session_id}, event={event}")

            except Exception as e:
                logger.warning(f"Error in ws_handler: {e}, websocket_id={websocket.id}")
    except (WebSocketDisconnect, ConnectionResetError, Exception) as e:
        logger.warning(f"Client {websocket.id} disconnected: {e}")


@ws_handler.on_connect
async def on_connect(websocket: WebSocketAdapter):
    logger.info(f"Client {websocket.id} connected")

    query_params = websocket.query_params
    session_id: str | None = query_params.get("session_id", None)
    if session_id is None:
        logger.warning(f"WebSocket connection rejected: missing session_id, websocket_id={websocket.id}")
        await websocket.close()
        relation_register.unregister_websocket_by_websocket_id(websocket_id=websocket.id)
        return

    logger.info(f"WebSocket connection established: session_id={session_id}, websocket_id={websocket.id}")
    res = {"content": "websocket connected successfully"}
    await websocket.send_text(json.dumps(res))

    relation_register.register_websocket(session_id=session_id, websocket=websocket)


@ws_handler.on_close
async def handle_disconnect(websocket: WebSocketAdapter):
    logger.info(f"Client {websocket.id} disconnected")

    # Pop and clear session_id when user disconnects
    session_id: str | None = relation_register.get_session_id_by_websocket_id(websocket_id=websocket.id)

    if session_id:
        logger.info(f"WebSocket session cleanup: session_id={session_id}, websocket_id={websocket.id}")
        relation_register.unregister_websocket_by_websocket_id(websocket_id=websocket.id)

        # Execute session end hook
        await session_end(session_id=session_id)
    else:
        logger.debug(f"WebSocket disconnect without session: websocket_id={websocket.id}")


@app.delete("/sessions")
async def clear_session_handler(request):
    request_json = request.json()

    session_id: str | None = request_json.get("session_id", None)
    logger.info(f"Clearing session: session_id={session_id}")
    await clear_session(session_id=session_id)
    logger.info(f"Session cleared: session_id={session_id}")