"""Per-session project directory routes (``GET`` / ``PUT /sessions/project``).

Fourth instance of the per-session settings pattern (see
``session_settings.py`` for thinking / model): ``session_id`` travels as a query
parameter on GET and in the JSON body on PUT, validation errors are answered
with ``bad_request`` (never raised — the global handler would turn them into a
500 with the raw exception text), and the blocking register reads run in a
worker thread.
"""

import asyncio

from loguru import logger

from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body
from server.service.session_project_service import (
    apply_project_choice_async,
    get_project_state,
)


@app.get("/sessions/project")
async def get_project_directory_handler(request):
    """Return the session's project-directory binding and its effective root.

    ``directory`` is the session's own binding (``null`` = unbound, the UI's
    "unbound" state); ``effective`` is where the tools resolve (the binding, or
    the process default); ``source`` says which of the two is in effect;
    ``pending_directory`` carries a choice parked for the next turn.
    """
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await asyncio.to_thread(get_project_state, session_id)
    except ValueError:
        return bad_request("invalid session_id")
    return ok(
        {
            "success": True,
            "session_id": session_id,
            "directory": state.directory,
            "effective": state.effective,
            "source": state.source,
            "pending_directory": state.pending,
        }
    )


@app.put("/sessions/project")
async def put_project_directory_handler(request):
    """Bind the session's project directory (``{"directory": null}`` clears it).

    The server is the only authority on validity: the value must be an absolute,
    existing directory and is normalised with ``Path.resolve()``. A choice made
    while a turn is running is parked and lands at the next turn boundary
    (``pending: true`` in the response).
    """
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "directory" not in body:
        return bad_request("'directory' is required (an absolute path, or null to clear)")
    requested = body.get("directory")
    if requested is not None and not isinstance(requested, str):
        return bad_request("'directory' must be a string or null")
    try:
        state = await apply_project_choice_async(session_id, requested)
    except ValueError as exc:
        # Validation messages name the user's own input (or the session id);
        # they carry no internals.
        logger.info("Project dir rejected: session={} reason={}", session_id, exc)
        return bad_request(str(exc) or "invalid session_id or directory")
    logger.info(
        "Project dir bound: session={} directory={} effective={} source={} pending={}",
        session_id,
        state.directory,
        state.effective,
        state.source,
        state.pending,
    )
    return ok(
        {
            "success": True,
            "session_id": session_id,
            "ok": True,
            "directory": state.directory,
            "effective": state.effective,
            "source": state.source,
            "pending": state.pending is not None,
        }
    )
