"""The ``file_changes_updated`` frame: what this session can still revert.

Same shape and contract as the taskflow progress push: build the payload from
persisted state, send it to the session's websocket when one is bound, swallow
every failure (a dead socket must never break a tool call). A reconnecting
client asks for a ``file_changes_refresh`` and gets the identical payload, so
there is one shape to render.
"""

from __future__ import annotations

import json

from loguru import logger

from runtime.session.relation_register import relation_register

from .snapshot import file_changes_payload

__all__ = ["FILE_CHANGES_UPDATED_EVENT", "push_file_changes"]

#: The frame event name (client-side ``ws:file_changes_updated`` mitt event).
FILE_CHANGES_UPDATED_EVENT = "file_changes_updated"


async def push_file_changes(session_id: str) -> None:
    """Best-effort ``file_changes_updated`` push to the session's websocket."""
    session_id = (session_id or "").strip()
    if not session_id:
        return
    try:
        websocket = relation_register.get_websocket_by_session_id(session_id)
        if websocket is None:
            return
        payload = {
            "event": FILE_CHANGES_UPDATED_EVENT,
            "session_id": session_id,
            "content": file_changes_payload(session_id),
        }
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — a push failure must never surface
        logger.warning("Failed to push file_changes_updated for session {}: {}", session_id, e)
