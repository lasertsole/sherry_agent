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


from loguru import logger

from runtime.session.ws_push import broadcast_to_session

__all__ = ["SEARCH_FALLBACK_EVENT", "push_search_fallback", "push_searxng_cooldown"]

#: The frame event name (client-side `ws:search_backend_fallback` event).
SEARCH_FALLBACK_EVENT = "search_backend_fallback"

#: The two breaker reason codes (client-side localized under
#: ``searchFallback.reason.*``; unknown codes render verbatim).
COOLDOWN_OPENED_REASON = "searxng_cooldown_opened"
COOLDOWN_RECOVERED_REASON = "searxng_cooldown_recovered"


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
        await broadcast_to_session(session_id, payload, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — a push failure must never surface
        logger.warning("Failed to push search_backend_fallback for session {}: {}", session_id, e)


async def push_searxng_cooldown(
    session_id: str, *, opened: bool, cooldown_seconds: float = 0.0
) -> None:
    """Best-effort breaker notice on the same ``search_backend_fallback`` frame.

    Fired on the OPEN (the threshold was just reached) and on a CONFIRMED
    recovery (the window expired AND the probe attempt succeeded) — the caller
    decides which, from transitions it observed around a real attempt.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return
    if opened:
        reason = COOLDOWN_OPENED_REASON
        message = f"SearXNG 连续失败，已暂停使用（约 {int(cooldown_seconds)} 秒后自动恢复）"
    else:
        reason = COOLDOWN_RECOVERED_REASON
        message = "SearXNG 冷却结束，已恢复使用"
    try:
        payload = {
            "event": SEARCH_FALLBACK_EVENT,
            "session_id": session_id,
            "content": {
                "from": "searxng",
                "backend": "searxng",
                "reason": reason,
                "message": message,
                "cooldown_seconds": int(cooldown_seconds),
            },
        }
        await broadcast_to_session(session_id, payload, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — a push failure must never surface
        logger.warning("Failed to push the SearXNG cooldown notice for {}: {}", session_id, e)
