import inspect
import json
from loguru import logger
from robyn import Response
from typing import Any
from collections.abc import Callable
from robyn import Robyn, ALLOW_CORS
from robyn import WebSocketDisconnect, WebSocketAdapter
from robyn.status_codes import HTTP_500_INTERNAL_SERVER_ERROR
from runtime import relation_register, clear_all_register_sessions

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
        description=json.dumps(
            {
                "success": False,
                "message": "Internal Server Error",
                "error": str(error),
            },
            ensure_ascii=False,
        ),
    )


ws_event_processor_dict: dict[str, Callable[[str, str | dict[str, Any]], Any]] = {}


async def ws_processor(session_id: str, event: str, content: str | dict[str, Any]) -> Any:
    try:
        processor: Callable[[str, str | dict[str, Any]], Any] | None = ws_event_processor_dict.get(
            event
        )
        if processor is None:
            logger.debug(f"No processor registered for event: {event}, session_id={session_id}")
            return None

        logger.debug(f"Processing WS event: event={event}, session_id={session_id}")
        result = processor(session_id, content)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as e:
        logger.warning(f"ws_processor error happened: {e}, session_id={session_id}, event={event}")
        return None


def ping_processor(session_id: str, content: str | dict[str, Any]) -> str | dict[str, Any]:
    """
    Heartbeat keep-alive processor.

    The client performs application-level liveness detection over the
    /sessions/ws connection: it periodically sends a heartbeat frame with
    event="ping", and this handler replies verbatim with {"event": "pong"}
    (returning a dict instead of a str so the client receives a JSON object
    frame). This replaces the old HTTP-polling health check.

    A plain function is enough: ws_processor awaits awaitable results, so a
    handler may be sync (returning the reply frame directly, as here) or async.
    """
    return {"event": "pong"}


# Register the ping -> pong heartbeat event handler
ws_event_processor_dict["ping"] = ping_processor


async def todo_refresh_processor(
    session_id: str, content: str | dict[str, Any]
) -> dict[str, Any] | None:
    """Reply to a client `todo_refresh` frame with the persisted session plan.

    Sent by the frontend after a `/sessions/ws` reconnect to recover the todo
    list; the returned `todo_updated` frame is written back to the requesting
    socket by ``ws_handler``. Fail-open: an unknown session, a removed package,
    or any store error logs and returns ``None`` (the client ignores the null
    reply), never raising into the receive loop.
    """
    try:
        from agent.tools.todolist.service import TodoService

        todos = await TodoService.get_todos(session_id)
        return {
            "event": "todo_updated",
            "session_id": session_id,
            "content": {"todos": todos},
        }
    except Exception as e:
        logger.warning(f"todo_refresh failed: {e}, session_id={session_id}")
        return None


ws_event_processor_dict["todo_refresh"] = todo_refresh_processor


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
                    logger.debug(
                        f"WebSocket message missing session_id: websocket_id={websocket.id}"
                    )
                    continue

                event: str | None = obj.get("event", None)
                if event is None:
                    logger.debug(f"WebSocket message missing event: session_id={session_id}")
                    continue

                content: str | dict[str, Any] | None = obj.get("content", None)
                if content is None:
                    logger.debug(
                        f"WebSocket message missing content: session_id={session_id}, event={event}"
                    )
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


@getattr(ws_handler, "on_connect")
async def handle_connect(websocket: WebSocketAdapter):
    logger.info(f"Client {websocket.id} connected")

    query_params = websocket.query_params
    session_id: str | None = query_params.get("session_id", None)
    if session_id is None:
        logger.warning(
            f"WebSocket connection rejected: missing session_id, websocket_id={websocket.id}"
        )
        await websocket.close()
        relation_register.unregister_websocket_by_websocket_id(websocket_id=websocket.id)
        return

    logger.info(
        f"WebSocket connection established: session_id={session_id}, websocket_id={websocket.id}"
    )
    res = {"content": "websocket connected successfully"}
    await websocket.send_text(json.dumps(res))

    relation_register.register_websocket(session_id=session_id, websocket=websocket)


@getattr(ws_handler, "on_close")
async def handle_disconnect(websocket: WebSocketAdapter):
    logger.info(f"Client {websocket.id} disconnected")

    # Pop and clear session_id when user disconnects
    session_id: str | None = relation_register.get_session_id_by_websocket_id(
        websocket_id=websocket.id
    )

    if session_id:
        logger.info(
            f"WebSocket session cleanup: session_id={session_id}, websocket_id={websocket.id}"
        )
        relation_register.unregister_websocket_by_websocket_id(websocket_id=websocket.id)

        clear_all_register_sessions(session_id=session_id)
    else:
        logger.debug(f"WebSocket disconnect without session: websocket_id={websocket.id}")
