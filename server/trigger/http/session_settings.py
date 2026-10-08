"""Per-session main-model and thinking control endpoints.

The client's chat toolbar calls:

    GET /sessions/thinking?session_id=<sid>
        -> {"success": true, "session_id": ..., "mode": "on_off"|"levels",
            "enabled": true|false|null, "level": "low"|"high"|"max"|null,
           "default_enabled": true|false, "default_level": "low"|"high"|"max",
            "pending": bool}
           null fields = the user never made an explicit choice; the
           MAIN_LLM_ENABLE_THINKING env default applies. "levels" marks an
           always-think model whose control is a 低/高/最高 selector, and it
           follows the session's model override when one is set. The
           enabled/level fields describe the EFFECTIVE next-turn choice;
           ``pending`` reports that a mid-turn selection is parked and only
           lands when the running turn ends.
    PUT /sessions/thinking  {"session_id": <sid>, "value": <bool|"low"|"high"|"max"|null>}
        -> {"success": true, "value": ..., "pending": bool}
           400 for values contradicting the model's control mode. ``null``
           clears the explicit choice (back to the MAIN_LLM_ENABLE_THINKING
           env default). Switching is allowed at any moment: while a turn is in
           flight the choice is parked (``pending: true``) and applies from the
           next turn, so the running turn never swaps model variants
           mid-flight.

    GET /sessions/model?session_id=<sid>
        -> {"success": true, "session_id": ..., "override": <masked profile>|null,
            "env_model": {"provider": ..., "model": ...}, "pending": bool}
           The override is the env-config profile the session runs on instead
           of the env-configured main LLM (api_key never echoes back —
           ``has_api_key`` reports whether one was stored); ``pending`` as
           above.
    GET /sessions/turn_state?session_id=<sid>
        -> {"success": true, "session_id": ..., "active": bool}
           ``active`` = a turn is in flight (the in-memory busy signal OR a
           QUEUED/CLAIMED input-queue row — a normal chat stream only shows in
           the latter). The composer polls this while it believes it is
           streaming, so a terminal frame lost to a socket swap cannot leave
           the stop button showing for ever.
    PUT /sessions/model  {"session_id": <sid>, "profile": <profile>|null}
        -> {"success": true, ..., "pending": bool}
           null clears the override ("follow the env config"); 400 for
           malformed profiles. Same parking semantics as the thinking twin, so
           the change takes effect from the next turn.

Both values land in the session state registers (mem + durable SQLite); the
agent-side ThinkingControlMiddleware reads the LIVE keys on every main-chain
model call and swaps in the matching model/thinking variant, while
``turn_runner.on_turn_finished`` promotes a parked choice once the turn ends.
"""

import asyncio

from loguru import logger

from server.service.session_settings_service import (
    apply_main_model_choice,
    apply_thinking_choice,
    get_main_model_state,
    get_thinking_state,
    session_turn_active,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body


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
    if not session_id:
        return bad_request("session_id is required")
    if "value" not in body:
        return bad_request("'value' is required (a boolean, a level, or null to clear)")
    value = body.get("value")
    try:
        pending = await apply_thinking_choice(session_id, value)
    except ValueError:
        return bad_request("invalid session_id or value")
    logger.info("Thinking toggle: session={} value={} pending={}", session_id, value, pending)
    return ok({"success": True, "session_id": session_id, "value": value, "pending": pending})


@app.get("/sessions/turn_state")
async def get_turn_state_handler(request):
    """Return whether the session has a turn in flight (watchdog for the client)."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        active = await session_turn_active(session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, "session_id": session_id, "active": active})


@app.get("/sessions/model")
async def get_session_model_handler(request):
    """Return the session's main-model override and the env-configured identity."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await asyncio.to_thread(get_main_model_state, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, "session_id": session_id, **state})


@app.put("/sessions/model")
async def put_session_model_handler(request):
    """Persist (or clear, with ``profile: null``) the session's model override."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "profile" not in body:
        return bad_request("'profile' is required (an object, or null to follow the env config)")
    profile = body.get("profile")
    try:
        pending = await apply_main_model_choice(session_id, profile)
    except ValueError as exc:
        # The sanitizer's messages name the offending field and are safe to
        # surface (they carry no internals).
        return bad_request(str(exc) or "invalid session_id or profile")
    state = await asyncio.to_thread(get_main_model_state, session_id)
    override = state.get("override") or {}
    logger.info(
        "Session model override: session={} profile={} model={} pending={}",
        session_id,
        override.get("id"),
        override.get("model"),
        pending,
    )
    return ok({"success": True, "session_id": session_id, **state})
