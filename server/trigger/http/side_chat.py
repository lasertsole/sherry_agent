"""Side-chat creation, listing and explicit deletion routes.

``POST /sessions/side_chat`` creates a server-side child id, seeds its first
turn from the parent checkpoint and stores the parent link. ``GET`` lists the
children of one parent (the client uses it to clear local caches when the parent
is deleted). ``DELETE`` explicitly purges one child; closing the right-sidebar
tab does NOT call it — a side chat survives its panel and is destroyed only
when the parent session is deleted.
"""

from __future__ import annotations

from loguru import logger

from pub.func.validator import is_safe_session_id
from server.DAO import side_chat_store
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, not_found, ok, read_body
from server.service.side_chat_service import create_side_chat, delete_side_chat


@app.post("/sessions/side_chat")
async def create_side_chat_handler(request):
    """Create the child session + parent link + checkpoint-fork inheritance."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    parent_session_id = str(body.get("parent_session_id") or "").strip()
    if not parent_session_id:
        return bad_request("parent_session_id is required")
    try:
        result = await create_side_chat(parent_session_id)
    except ValueError as exc:
        logger.info("Side chat rejected: parent={} reason={}", parent_session_id, exc)
        return bad_request(str(exc) or "invalid parent_session_id")
    return ok({"success": True, **result})


@app.get("/sessions/side_chat")
async def list_side_chats_handler(request):
    """List the side-chat children of one parent session."""
    query = request.query_params or {}
    parent_session_id = str(query.get("parent_session_id", "") or "").strip()
    if not parent_session_id or not is_safe_session_id(parent_session_id):
        return bad_request("invalid parent_session_id")
    return ok(
        {
            "success": True,
            "parent_session_id": parent_session_id,
            "children": side_chat_store.list_children(parent_session_id),
        }
    )


@app.delete("/sessions/side_chat")
async def delete_side_chat_handler(request):
    """Explicitly purge one side chat and remove its parent link."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    child_session_id = str(body.get("session_id") or "").strip()
    parent_session_id = str(body.get("parent_session_id") or "").strip()
    if not child_session_id:
        return bad_request("session_id is required")
    row = side_chat_store.get_side_chat(child_session_id)
    if row is None:
        return not_found("side chat not found")
    if parent_session_id and str(row["parent_session_id"]) != parent_session_id:
        return bad_request("parent_session_id does not match the side chat")
    try:
        await delete_side_chat(child_session_id)
    except Exception:
        logger.exception("Side chat purge failed: child={}", child_session_id)
        return bad_request("side chat purge failed")
    return ok({"success": True, "session_id": child_session_id})
