"""System notices — in-chat transient cards that never touch persistence.

The third message kind between the two existing ones (plan
`TODO/计划_临时系统信息协议_system_notice_20261112.md`):

| kind | persisted | in chat flow | seen by |
|---|---|---|---|
| system-reminder (DynamicReminderMiddleware) | yes | yes (folded card) | model + user |
| dedicated WS panel frames (todo/taskflow/terminal badge) | no | no (side panels) | user |
| **system.notice (this module)** | **no** | **yes (transient card)** | user |

Producers: agent tools / middlewares (importing this module is free — it lives
in ``runtime``) and the HTTP push endpoint
(``POST /sessions/notice``) for external tools that hold the gateway token but
no websocket. The frame rides the session push channel:
``{"event": "system_notice", "session_id": …, "content": {title, content, level, source}}``.

Nothing here writes MesMemory, the checkpoint or the transcript — a page
refresh loses every notice by design.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from .ws_push import broadcast_to_session

__all__ = ["SYSTEM_NOTICE_EVENT", "NOTICE_LEVELS", "push_system_notice"]

#: The session-push frame event name (client-side `ws:system_notice`).
SYSTEM_NOTICE_EVENT = "system_notice"

#: Visual severity the client card renders (color + icon).
NOTICE_LEVELS = ("info", "success", "warning", "error", "progress")

#: Bounds for the two free-text fields: a notice is a headline + a peek, not a
#: transcript. Longer payloads belong in the notification center or a file.
_TITLE_MAX_CHARS = 120
_CONTENT_MAX_CHARS = 2000
_SOURCE_MAX_CHARS = 64


def _clean(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


async def push_system_notice(
    session_id: str,
    *,
    title: str,
    content: str = "",
    level: str = "info",
    source: str = "external",
) -> dict[str, Any]:
    """Broadcast one transient in-chat notice; returns the frame content.

    Best-effort like every other session push: a dead socket is pruned by the
    broadcast layer and the failure is logged, never raised — a notice must not
    break whatever produced it. An unknown ``level`` degrades to ``info``.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return {}
    if level not in NOTICE_LEVELS:
        level = "info"
    content_payload = {
        "title": _clean(title, _TITLE_MAX_CHARS),
        "content": _clean(content, _CONTENT_MAX_CHARS),
        "level": level,
        "source": _clean(source, _SOURCE_MAX_CHARS) or "external",
    }
    payload = {
        "event": SYSTEM_NOTICE_EVENT,
        "session_id": session_id,
        "content": content_payload,
    }
    try:
        delivered = await broadcast_to_session(
            session_id, payload, ensure_ascii=False, warn_prefix="system.notice push failed"
        )
        logger.debug(
            "system.notice pushed: session={} level={} title={!r} delivered={}",
            session_id,
            level,
            content_payload["title"],
            delivered,
        )
    except Exception as exc:  # noqa: BLE001 — a notice must never break its producer
        logger.warning("system.notice push failed for session {}: {}", session_id, exc)
    return content_payload
