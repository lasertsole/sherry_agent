"""WS notice when ``web_search`` had to change backends mid-call.

Same best-effort contract as the taskflow progress push
(``agent/tools/taskflow/progress_push.py``): the frame goes to the session's live
websocket when one is bound, and every failure is logged and swallowed — a
notification must never break the search it describes.

    {"event": "search_backend_fallback", "session_id": …,
     "content": {"from": "tavily", "backend": "searxng",
                 "reason": "tavily_auth_failed", "message": …}}

The client shows it as a non-blocking banner (the turn it belongs to keeps
streaming); the search result itself already names the backend it came from, so
a client that misses the frame still shows the truth.
"""

from __future__ import annotations

import json

from loguru import logger

from runtime.session.relation_register import relation_register

__all__ = ["SEARCH_FALLBACK_EVENT", "push_search_fallback"]

#: The frame event name (client-side `ws:search_backend_fallback` event).
SEARCH_FALLBACK_EVENT = "search_backend_fallback"


def fallback_message(backend: str, reason: str) -> str:
    """The one-line user-facing notice for a backend swap."""
    label = "SearXNG" if backend == "searxng" else "Tavily"
    return f"Tavily 无法使用（{reason}），已切换到 {label}"


async def push_search_fallback(session_id: str, backend: str, reason: str) -> None:
    """Best-effort ``search_backend_fallback`` push to the session's websocket."""
    session_id = (session_id or "").strip()
    if not session_id:
        return
    try:
        websocket = relation_register.get_websocket_by_session_id(session_id)
        if websocket is None:
            return
        payload = {
            "event": SEARCH_FALLBACK_EVENT,
            "session_id": session_id,
            "content": {
                "from": "tavily",
                "backend": backend,
                "reason": reason,
                "message": fallback_message(backend, reason),
            },
        }
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — a push failure must never surface
        logger.warning("Failed to push search_backend_fallback for session {}: {}", session_id, e)
