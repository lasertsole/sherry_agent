"""File-change revert routes (``GET`` state, ``POST /sessions/file-changes/revert``).

Fifth instance of the per-session settings pattern (see ``session_project.py``):
``session_id`` travels as a query parameter on GET and in the JSON body on
POST, validation errors answer with ``bad_request`` instead of raising, and the
blocking work runs in a worker thread. The revert core is the SAME function the
agent-side tool calls — the button and the tool cannot drift apart.
"""

import asyncio

from loguru import logger

from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body
from server.service.file_changes_service import file_changes_state, revert_session_file_changes


@app.get("/sessions/file-changes")
async def get_file_changes_handler(request):
    """Return the session's snapshot state: which writes could still be reverted."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    try:
        state = await asyncio.to_thread(file_changes_state, session_id)
    except Exception as exc:  # noqa: BLE001 — a read must not 500 the panel
        logger.warning("file-changes state failed for {}: {}", session_id, exc)
        return bad_request("invalid session_id")
    return ok({"success": True, **state})


@app.post("/sessions/file-changes/revert")
async def post_file_changes_revert_handler(request):
    """Revert files to their snapshotted content (``dry_run`` plans only).

    Body: ``{"session_id", "paths"?: [...], "to_tool_call_id"?: str,
    "dry_run"?: bool}``. A refusal is reported in the payload (``success:
    false`` with per-file reasons) rather than as an HTTP error — the client
    shows it verbatim, and ``merge3way`` carries the read-only feasibility hint.
    """
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    paths = body.get("paths")
    if paths is not None and not isinstance(paths, list):
        return bad_request("'paths' must be a list of strings")
    to_tool_call_id = body.get("to_tool_call_id") or ""
    if not isinstance(to_tool_call_id, str):
        return bad_request("'to_tool_call_id' must be a string")
    dry_run = bool(body.get("dry_run", False))
    try:
        payload = await revert_session_file_changes(
            session_id,
            paths=[str(p) for p in paths] if paths else None,
            to_tool_call_id=to_tool_call_id,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — report, never 500 the button
        logger.warning("file-changes revert failed for {}: {}", session_id, exc)
        return bad_request("revert failed")
    return ok({"session_id": session_id, **payload})
