"""Conversation rewind routes (``GET`` state, ``POST /sessions/rewind``).

Same per-session settings pattern as the other route modules: ``session_id``
via query on GET and body on POST, validation answered with ``bad_request``
instead of raising, blocking work on a worker thread. A rewind is refused while
a turn is in flight — the service enforces it, and ``GET`` reports the same
verdict so the client can grey the control out.
"""

from loguru import logger

from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body
from server.service.rewind_service import apply_session_rewind, rewind_state


@app.get("/sessions/rewind")
async def get_rewind_handler(request):
    """Report the session's branch state and whether a rewind is allowed now."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await rewind_state(session_id)
    except Exception as exc:  # noqa: BLE001 — a read must not 500 the control
        logger.warning("rewind state failed for {}: {}", session_id, exc)
        return bad_request("invalid session_id")
    return ok({"success": True, **state})


@app.post("/sessions/rewind")
async def post_rewind_handler(request):
    """Cut the conversation back to ``cut_after_message_id`` (that message stays).

    The messages are not deleted: reads filter through the branch record, and the
    generation bump fences work created before the cut.
    """
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    cut = body.get("cut_after_message_id")
    if not isinstance(cut, int) or isinstance(cut, bool) or cut < 0:
        return bad_request("'cut_after_message_id' must be a non-negative integer")
    try:
        payload = await apply_session_rewind(session_id, cut)
    except ValueError as exc:
        return bad_request(str(exc) or "rewind refused")
    except Exception as exc:  # noqa: BLE001 — report, never 500 the button
        logger.warning("rewind failed for {}: {}", session_id, exc)
        return bad_request("rewind failed")
    return ok({"success": True, **payload})
