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
    apply_custom_thinking_mapping,
    apply_main_model_choice,
    apply_thinking_choice,
    get_custom_thinking_mapping,
    get_main_model_state,
    get_thinking_state,
    session_turn_active,
)
from server.service.session_settings_service import (
    apply_search_backend_choice,
    get_search_backend_state,
)
from server.service.session_settings_service import (
    apply_token_budget_choice,
    apply_turn_token_budget_choice,
    get_token_budget_state,
    reset_token_budget,
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


@app.get("/sessions/thinking/custom")
async def get_thinking_custom_handler(request):
    """Return the session's custom thinking wire mapping (or null)."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        mapping = await asyncio.to_thread(get_custom_thinking_mapping, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, "session_id": session_id, "mapping": mapping})


@app.put("/sessions/thinking/custom")
async def put_thinking_custom_handler(request):
    """Write (or clear, with ``mapping: null``) the custom thinking mapping."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "mapping" not in body:
        return bad_request("'mapping' is required (an object, or null to clear)")
    mapping = body.get("mapping")
    if mapping is not None and not isinstance(mapping, dict):
        return bad_request("'mapping' must be an object or null")
    try:
        pending = await apply_custom_thinking_mapping(session_id, mapping)
    except ValueError as exc:
        return bad_request(str(exc) or "invalid mapping")
    logger.info(
        "Thinking custom mapping: session={} pending={} wire={}",
        session_id,
        pending,
        (mapping or {}).get("wire_format") or "-",
    )
    return ok({"success": True, "session_id": session_id, "pending": pending})


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


@app.get("/sessions/search_backend")
async def get_search_backend_handler(request):
    """Return the session's web-search backend choice and provider availability.

    ``session_id`` may be empty: the 模型联网 panel is a settings tab and opens
    without a session, which still needs the availability + default view.
    """
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    try:
        state = await asyncio.to_thread(get_search_backend_state, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, "session_id": session_id, **state})


@app.put("/sessions/search_backend")
async def put_search_backend_handler(request):
    """Persist the session's web-search backend choice (live or parked)."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "backend" not in body:
        return bad_request("'backend' is required ('tavily', 'searxng', or null to clear)")
    backend = body.get("backend")
    try:
        pending = await apply_search_backend_choice(session_id, backend)
    except ValueError as exc:
        return bad_request(str(exc))
    logger.info(
        "Search backend choice: session={} backend={} pending={}", session_id, backend, pending
    )
    return ok({"success": True, "session_id": session_id, "backend": backend, "pending": pending})


@app.get("/sessions/budget")
async def get_budget_handler(request):
    """Return the session's token cap, accumulated usage/cost and latch."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await asyncio.to_thread(get_token_budget_state, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, "session_id": session_id, **state})


@app.put("/sessions/budget")
async def put_budget_handler(request):
    """Persist the session's cumulative and/or per-turn token caps.

    ``cap`` is the session-wide cap; ``turn_cap`` is the per-turn cap across the
    main agent and all descendants. Either may be omitted; ``null`` clears that
    choice and goes back to inheriting the app-config default.
    """
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "cap" not in body and "turn_cap" not in body:
        return bad_request(
            "'cap' or 'turn_cap' is required (a non-negative integer, or null to clear)"
        )
    cap = body.get("cap")
    turn_cap = body.get("turn_cap")
    try:
        pending = await apply_token_budget_choice(session_id, cap) if "cap" in body else False
        turn_pending = (
            await apply_turn_token_budget_choice(session_id, turn_cap)
            if "turn_cap" in body
            else False
        )
    except ValueError as exc:
        return bad_request(str(exc))
    logger.info(
        "Token budget caps: session={} cap={} pending={} turn_cap={} turn_pending={}",
        session_id,
        cap,
        pending,
        turn_cap,
        turn_pending,
    )
    return ok(
        {
            "success": True,
            "session_id": session_id,
            "cap": cap,
            "pending": pending,
            "turn_cap": turn_cap,
            "turn_pending": turn_pending,
        }
    )


@app.post("/sessions/budget/reset")
async def reset_budget_handler(request):
    """Clear the usage/cost/latch counters, preserving the cap."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await asyncio.to_thread(reset_token_budget, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    logger.info("Token budget reset: session={} cap={}", session_id, state.get("cap"))
    return ok({"success": True, "session_id": session_id, **state})
