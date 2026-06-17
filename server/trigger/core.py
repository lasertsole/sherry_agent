import json
from loguru import logger
from robyn import Response
from robyn import Robyn, ALLOW_CORS
from server.service import session_end
from typing import Any, Dict, Callable
from robyn import WebSocketDisconnect, WebSocketAdapter
from robyn.status_codes import HTTP_500_INTERNAL_SERVER_ERROR
from context_engine import rectification_and_standardization
from runtime import relation_register, clear_all_register_sessions, count_register, state_register, timer_register

# Create the app
app = Robyn(__file__)

# Enable CORS for all origins (development)
ALLOW_CORS(app, origins=["*"])

@app.exception
def handle_exception(error: Exception):
    """
    Global exception interceptor
    Called when any uncaught exception is raised inside route handlers
    """
    # Log the error for debugging
    logger.exception(error)

    # Return a uniform JSON error response
    return Response(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        headers={"Content-Type": "application/json"},
        description=json.dumps({
            "success": False,
            "message": "Internal Server Error",
            "error": str(error),
        }, ensure_ascii=False),
    )

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
async def handle_connect(websocket: WebSocketAdapter):
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

    state_register.set_state(session_id, "should_skill_memory_maintenance", False)

    async def skill_memory_maintenance(session_id: str)->None:
        if state_register.get_state(session_id, "skill_memory_maintenance", False) == True:
            state_register.set_state(session_id, "skill_memory_maintenance", False)
            await rectification_and_standardization(session_id)

    # Register skill memory maintenance(threshold = 20, minutes = 15)
    count_register.register(session_id, "skill_memory_maintenance", skill_memory_maintenance, args={"session_id": session_id},
                            threshold=20)
    timer_register.register(session_id, "skill_memory_maintenance", skill_memory_maintenance, args={"session_id": session_id},
                            minutes = 10)

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

        clear_all_register_sessions(session_id=session_id)
    else:
        logger.debug(f"WebSocket disconnect without session: websocket_id={websocket.id}")
