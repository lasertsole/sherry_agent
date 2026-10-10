"""Read-only REST face of the per-turn trajectory ledger.

The ledger itself (``server/service/trajectory_store.py``) is written by the
stream's frame projector; this route is how a pane reads a turn's timeline back:
``GET /sessions/{session_id}/trajectory`` with an optional ``turn_id``.
"""

from loguru import logger
from pub.func.validator import is_safe_session_id

from server.service.trajectory_store import get_trajectory_store
from server.trigger.core import app
from server.trigger.http.helpers import bad_request as _bad_request
from server.trigger.http.helpers import ok as _ok


@app.get("/sessions/:session_id/trajectory")
async def get_session_trajectory_handler(request, path_params):
    """One turn's (or the session's newest) trajectory events, ordered by seq.

    Query params: ``turn_id`` (optional), ``limit`` (default 50, capped 500),
    ``offset`` (default 0).
    """
    session_id = path_params["session_id"]
    if not is_safe_session_id(session_id):
        return _bad_request("invalid session_id")
    query = request.query_params
    turn_id = query.get("turn_id") or None
    try:
        limit = max(1, min(int(query.get("limit", 50) or 50), 500))
        offset = max(0, int(query.get("offset", 0) or 0))
    except (TypeError, ValueError):
        return _bad_request("limit/offset must be integers")
    events = await get_trajectory_store().read(
        session_id, turn_id=turn_id, limit=limit, offset=offset
    )
    logger.debug(
        "Trajectory read: session={} turn={} events={}", session_id, turn_id or "-", len(events)
    )
    return _ok({"session_id": session_id, "turn_id": turn_id, "events": events})
