"""Agent-side meeting event emission (phase 5, plan §6).

Resolves the ``MEETING_EVENT_PUSH`` hook so the meeting loop never imports a
server module (dependency direction). Missing hook is a no-op — a bare unit test
or a process without the WS server still runs meetings normally.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from runtime import hooks

__all__ = ["emit_meeting_event"]


def emit_meeting_event(meeting_id: str, event: dict[str, Any]) -> None:
    """Fail-open emission of one meeting WS frame."""
    fn = hooks.resolve(hooks.MEETING_EVENT_PUSH)
    if fn is None:
        return
    try:
        fn(meeting_id, event)
    except Exception as exc:  # a broken WS sink must never break the meeting
        logger.warning("meeting event emission failed for {}: {}", meeting_id, exc)
