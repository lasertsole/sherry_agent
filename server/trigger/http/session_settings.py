"""Per-session model thinking control endpoints.

The client's thinking control (multimedia toolbar, right side) calls:

    GET /sessions/thinking?session_id=<sid>
        -> {"success": true, "session_id": ..., "mode": "on_off"|"levels",
            "enabled": true|false|null, "level": "low"|"high"|"max"|null}
           null fields = the user never made an explicit choice; the
           MAIN_LLM_ENABLE_THINKING env default applies. "levels" marks an
           always-think model whose control is a 低/高/最高 selector.
    PUT /sessions/thinking  {"session_id": <sid>, "value": <bool|"low"|"high"|"max">}
        -> {"success": true, ...}
           400 for values contradicting the model's control mode;
           409 while the session has a turn in progress (the model variant
           must never change mid-flight).

The flag lands in the session state registers (mem + durable SQLite); the
agent-side ThinkingControlMiddleware reads mem on every main-chain model call
and swaps in the matching thinking client variant.
"""

import asyncio

from loguru import logger

from server.service.session_settings_service import (
    SessionBusyError,
    get_thinking_state,
    set_thinking_value_guarded,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body, to_text_response


@app.get("/sessions/thinking")
async def get_thinking_handler(request):
    """Return the session's explicit thinking choice and the model's mode."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await asyncio.to_thread(get_thinking_state, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, "session_id": session_id, **state})


@app.put("/sessions/thinking")
async def put_thinking_handler(request):
    """Persist the session's explicit thinking choice (both registers)."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    value = body.get("value")
    if not session_id:
        return bad_request("session_id is required")
    if value is None:
        return bad_request("'value' is required")
    try:
        await set_thinking_value_guarded(session_id, value)
    except SessionBusyError as exc:
        return to_text_response(409, {"success": False, "message": str(exc)})
    except ValueError:
        return bad_request("invalid session_id or value")
    logger.info("Thinking toggle: session={} value={}", session_id, value)
    return ok({"success": True, "session_id": session_id, "value": value})
