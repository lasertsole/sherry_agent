"""The safety filter's in-process event log + (fire-and-forget) WS notices.

A bounded per-session ring: the settings tab lists the newest entries for the
session it is showing, and nothing is persisted (a filter decision is not
history worth keeping — the replaced/blocked text is already visible in the
transcript). Both calls are fail-open: logging and notifying must never cost a
model call or a streamed chunk.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from loguru import logger

from config.features import SAFETY_FILTER

__all__ = ["clear_log", "notify_safety", "recent_events", "record_event"]

_logs: dict[str, deque[dict[str, Any]]] = {}
_MAX_KEYS = 500


def record_event(
    session_id: str,
    *,
    layer: str,
    action: str,
    rule_count: int,
    message: str,
    snippet: str = "",
) -> dict[str, Any]:
    """Append one filter event to the session's ring and return it."""
    event = {
        "at": round(time.time(), 3),
        "layer": layer,
        "action": action,
        "rule_count": rule_count,
        "message": message,
        "snippet": snippet[:50],
    }
    if not session_id:
        return event
    try:
        log = _logs.get(session_id)
        if log is None:
            log = deque(maxlen=max(1, int(SAFETY_FILTER["log_max_entries"])))
            _logs[session_id] = log
            if len(_logs) > _MAX_KEYS:
                # Process-lifetime growth guard: drop the oldest session's ring.
                _logs.pop(next(iter(_logs)), None)
        log.append(event)
    except Exception:  # noqa: BLE001 - logging is best-effort
        logger.debug("safety filter: log append failed", exc_info=True)
    return event


def recent_events(session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first slice of one session's ring (or every session's when None)."""
    capped = max(1, min(int(limit or 50), 200))
    if session_id:
        rows = list(_logs.get(session_id, ()))
    else:
        rows = [event for ring in _logs.values() for event in ring]
    rows.sort(key=lambda event: float(event.get("at") or 0.0), reverse=True)
    return rows[:capped]


def clear_log(session_id: str | None = None) -> None:
    """Drop one session's ring (or all of them) — tests and session deletion."""
    if session_id is None:
        _logs.clear()
    else:
        _logs.pop(session_id, None)


def notify_safety(
    session_id: str,
    *,
    layer: str,
    action: str,
    rule_count: int,
    message: str,
    snippet: str = "",
) -> None:
    """Record the event and push a ``safety_filter`` WS frame when a loop runs.

    Called from sync code inside a running loop (the stream frame builder) and
    from async hooks; scheduling keeps one call shape for both. No running loop
    (tests, a maintenance script) simply logs the event.
    """
    event = record_event(
        session_id,
        layer=layer,
        action=action,
        rule_count=rule_count,
        message=message,
        snippet=snippet,
    )
    if not session_id:
        return
    payload = {
        "event": "safety_filter",
        "session_id": session_id,
        "content": {
            key: event[key] for key in ("layer", "action", "rule_count", "message", "snippet")
        },
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from runtime.session.ws_push import broadcast_to_session

        loop.create_task(broadcast_to_session(session_id, payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - a push failure must never matter
        logger.debug("safety filter: notify failed", exc_info=True)
