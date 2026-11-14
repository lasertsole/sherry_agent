"""Global + per-session extraction settings (预设角色 → 高级).

GET /sessions/extraction?session_id=<sid>
    -> {"success": true, "global": {...}, "session": {...}, "effective": {...},
        "pending": bool, "defaults": {...}, "bounds": {...}}
       ``session`` is the override payload the NEXT turn uses (a parked mid-turn
       choice wins); ``effective`` is that payload merged over the global
       values; ``defaults``/``bounds`` feed the UI's reset action and input
       validation without a second hardcoded copy.

PUT /sessions/extraction  {"session_id": <sid>, "overrides": {...}|null}
    -> {"success": true, "session": {...}, "effective": {...}, "pending": bool}
       The payload REPLACES the session's override set ({} / null = inherit
       every field). It accepts experience learning and the two iteration
       ceilings; ``flow_extraction_enabled`` is rejected by name because flow
       replay compilation is a global-only decision. While a turn is in flight
       the choice is parked (``pending: true``) and lands on the next turn.

GET /extraction/global
    -> {"success": true, "global": {...}, "defaults": {...}, "bounds": {...}}

PUT /extraction/global  {"values": {...}}
    -> {"success": true, "global": {...}}
       Only the fields present are written; the values land in
       ``sherry.jsonc``'s ``extraction`` group through the typed config writer.
"""

from __future__ import annotations

from loguru import logger

from server.service.extraction_settings_service import (
    ExtractionSettingsError,
    apply_session_extraction_choice,
    get_extraction_state,
    set_global_extraction,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body, to_text_response


@app.get("/sessions/extraction")
async def get_session_extraction_handler(request):
    """Return global + session + effective extraction settings for one session."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    try:
        state = get_extraction_state(session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok({"success": True, **state})


@app.put("/sessions/extraction")
async def put_session_extraction_handler(request):
    """Replace the session's extraction overrides (live, or parked mid-turn)."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "overrides" not in body:
        return bad_request("'overrides' is required (an object; {} / null = inherit)")
    overrides = body.get("overrides") or {}
    try:
        cleaned, pending = await apply_session_extraction_choice(session_id, overrides)
    except ExtractionSettingsError as exc:
        return bad_request(str(exc) or "invalid extraction settings")
    except ValueError:
        return bad_request("invalid session_id")
    state = get_extraction_state(session_id)
    return ok(
        {
            "success": True,
            "session": cleaned,
            "effective": state["effective"],
            "pending": pending,
        }
    )


@app.get("/extraction/global")
async def get_global_extraction_handler(request):  # noqa: ARG001 - no query params
    """Return the app-wide extraction settings and their bounds."""
    state = get_extraction_state("")
    return ok(
        {
            "success": True,
            "global": state["global"],
            "defaults": state["defaults"],
            "bounds": state["bounds"],
        }
    )


@app.put("/extraction/global")
async def put_global_extraction_handler(request):
    """Write the app-wide extraction settings (only the fields present)."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    if "values" not in body:
        return bad_request("'values' is required (an object)")
    try:
        values = set_global_extraction(body.get("values") or {})
    except ExtractionSettingsError as exc:
        return bad_request(str(exc) or "invalid extraction settings")
    except (FileNotFoundError, OSError):
        # sherry.jsonc is unwritable (a packaging / permissions problem, not a
        # caller error) — the same structured 503 the config route answers.
        logger.exception("sherry.jsonc is unavailable for writing")
        return to_text_response(
            503, {"success": False, "message": "sherry.jsonc is unavailable for writing"}
        )
    return ok({"success": True, "global": values})
