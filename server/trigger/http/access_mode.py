"""Per-session access-mode endpoint for the chat toolbar's shield control.

Endpoints:
    GET /sessions/access_mode?session_id=<sid>
        -> {"success": true, "session_id": ..., "mode": "confirm_all"|"auto_edit"|"full_access"}
    PUT /sessions/access_mode  {"session_id": <sid>, "mode": <mode>}
        -> {"success": true, "mode": ...}

``auto_edit`` is the default (the normal approval gates); ``confirm_all`` makes
every command and every file change ask; ``full_access`` sets the session's HITL
bypass-all flag, so no approval card is raised for that session. The strict and
bypass flags are mutually exclusive — writing one clears the other. All of them
land in the session state register the middleware reads on every tool call, so
the switch applies from the next tool call on.
"""

from server.service.access_mode_service import get_access_mode, set_access_mode
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body


@app.get("/sessions/access_mode")
async def access_mode_get_handler(request):
    """Return the session's access mode."""
    session_id = request.query_params.get("session_id", "") or ""
    try:
        return ok({"session_id": session_id, "mode": get_access_mode(session_id)})
    except ValueError as error:
        return bad_request(str(error))


@app.put("/sessions/access_mode")
async def access_mode_put_handler(request):
    """Switch the session's access mode (``confirm_all`` / ``auto_edit`` / ``full_access``)."""
    body = read_body(request)
    if not isinstance(body, dict):
        return bad_request("a JSON body is required")
    session_id = body.get("session_id", "")
    mode = body.get("mode", "")
    try:
        return ok({"session_id": session_id, "mode": set_access_mode(session_id, mode)})
    except ValueError as error:
        return bad_request(str(error))
