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
from server.trigger import auth
from server.trigger.auth_user import user_auth_middleware, ws_user_check
from server.trigger.cors import cors_origin_echo
from server.trigger.csrf import csrf_guard_middleware
from server.trigger.security_headers import security_headers

# Create the app
app = Robyn(__file__)

# CORS: the Origin allowlist replaces the previous wildcard. Requests from an
# unlisted Origin are answered 403 by this middleware (the same check runs
# again in ``gateway_auth_middleware`` for the JSON error body). The custom
# ``token`` header must be listed or the browser's preflight would refuse it.
ALLOW_CORS(app, origins=auth.allowed_origins(), headers=[auth.TOKEN_HEADER])

# Issue the gateway token on every response and expose it to the app's
# cross-origin fetch (without Expose-Headers the browser hides the value). The
# desktop client caches it in memory and echoes it as the ``token`` request
# header / WS ``?token=`` parameter — see server/trigger/auth.py for the policy.
app.set_response_header("Access-Control-Expose-Headers", auth.TOKEN_HEADER)
app.set_response_header(auth.TOKEN_HEADER, auth.gateway_token())


@app.get(auth.BOOTSTRAP_PATH)
async def gateway_token_handler(request):
    """Hand the gateway token to a token-less caller (client bootstrap).

    Reachable without a token in strict mode too — it is how the desktop client
    learns the per-boot secret. The Origin allowlist still applies.
    """
    return {"token": auth.gateway_token()}


def gateway_auth_middleware(request):
    """Origin + token gate for every HTTP route (see server/trigger/auth.py)."""
    verdict = auth.check_http(
        origin=request.headers.get("Origin"),
        presented_token=request.headers.get(auth.TOKEN_HEADER),
        path=request.url.path,
    )
    if verdict is None:
        return request
    status, message = verdict
    # A middleware-produced refusal does not pass through the global response
    # headers, so the security headers are attached here. The token header is
    # deliberately NOT attached: a refused request must not learn it.
    return Response(
        status_code=status,
        headers={"Content-Type": "application/json", **security_headers()},
        description=json.dumps({"success": False, "message": message}, ensure_ascii=False),
    )


# Static security headers on every response (CSP + nosniff + framing). Global
# response headers, so static media routes inherit them too.
for _header_name, _header_value in security_headers().items():
    app.set_response_header(_header_name, _header_value)

# Register BEFORE_REQUEST explicitly: the decorator form rebinds the name to
# None (Robyn's ``add_middleware`` returns no handle), and the function must
# stay importable for tests.
app.before_request()(gateway_auth_middleware)
# CSRF guard runs AFTER the Origin gate above: the hostile page is refused
# before this layer looks at anything, and a same-origin mutation (the gap this
# closes) gets its Sec-Fetch-Site / Origin check here.
app.before_request()(csrf_guard_middleware)
# The login gate runs LAST of the three: an unlisted Origin or a hostile page is
# refused before the session is even looked at, and the CSRF guard keeps its say
# on mutations. Loopback clients and a switched-off login pass straight through
# (see server/trigger/auth_user.py).
app.before_request()(user_auth_middleware)
# Credentialed cross-origin fetches need the exact Origin echoed back, not the
# multi-origin allowlist's ``*`` (see server/trigger/cors.py).
app.after_request()(cors_origin_echo)


def handle_exception(error: Exception):
    """
    Global exception interceptor
    Called when any uncaught exception is raised inside route handlers
    """
    # Log the error for debugging
    logger.exception(error)

    # Client-contract errors keep their message: handlers raise ValueError to
    # report bad input and the client surfaces that text. Anything else is
    # reported as its class name only — the response must not leak internal
    # paths/state, while the log above keeps the full detail (same policy as
    # ``server/trigger/http/helpers.py::failure_detail``).
    detail = str(error) if isinstance(error, ValueError) else type(error).__name__

    # Return a uniform JSON error response
    return Response(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        headers={"Content-Type": "application/json"},
        description=json.dumps(
            {
                "success": False,
                "message": "Internal Server Error",
                "error": detail,
            },
            ensure_ascii=False,
        ),
    )


# Registered explicitly (not via the decorator) so the function stays importable
# for tests — the decorator form rebinds the module name to None.
app.exception(handle_exception)


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


async def taskflow_refresh_processor(
    session_id: str, content: str | dict[str, Any]
) -> dict[str, Any] | None:
    """Reply to a client `taskflow_refresh` frame with the session's flow progress.

    The progress panel's recovery path after a reconnect (and its first load, when
    no push has arrived yet): the reply carries the same payload the push does, so
    the client has exactly one shape to render. Fail-open like the todo refresh —
    an unknown session or any store error logs and returns ``None``.
    """
    try:
        from agent.tools.taskflow.progress_push import progress_payload

        return {
            "event": "taskflow_updated",
            "session_id": session_id,
            "content": progress_payload(session_id),
        }
    except Exception as e:
        logger.warning(f"taskflow_refresh failed: {e}, session_id={session_id}")
        return None


ws_event_processor_dict["taskflow_refresh"] = taskflow_refresh_processor


async def file_changes_refresh_processor(
    session_id: str, content: str | dict[str, Any]
) -> dict[str, Any] | None:
    """Reply to a client ``file_changes_refresh`` frame with the revert state.

    The revert chip's recovery path after a reconnect (and its first load): the
    reply carries the same payload the push does, so the client renders one
    shape. Fail-open like the other refreshes.
    """
    try:
        from agent.tools.file_tools.snapshot import file_changes_payload

        return {
            "event": "file_changes_updated",
            "session_id": session_id,
            "content": file_changes_payload(session_id),
        }
    except Exception as e:
        logger.warning(f"file_changes_refresh failed: {e}, session_id={session_id}")
        return None


ws_event_processor_dict["file_changes_refresh"] = file_changes_refresh_processor


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
    refusal = auth.check_ws(websocket.query_params.get(auth.TOKEN_QUERY_PARAM, None))
    if refusal is not None:
        logger.warning(f"WebSocket connection rejected: {refusal}, websocket_id={websocket.id}")
        await websocket.close()
        return
    user_refusal = await ws_user_check(websocket.query_params)
    if user_refusal is not None:
        logger.warning(
            f"WebSocket connection rejected: {user_refusal}, websocket_id={websocket.id}"
        )
        await websocket.close()
        return

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
