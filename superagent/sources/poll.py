"""Shared thread polling (prototype ``src/sources/poll.ts``).

Polling keeps a live subscription plus a two-week window after the last
Trigger/Ticket end.  A cursor is moved five seconds back so events that
arrived while the previous poll was in flight are not lost.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, UTC
from typing import Any

POLL_HORIZON_MS = 14 * 24 * 3600 * 1000
CURSOR_SKEW_SECONDS = 5
FAILURE_LIMIT = 3


def cursor_at(started: str) -> str:
    """Return a cursor timestamp skewed five seconds into the past."""
    moment = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return (moment - timedelta(seconds=CURSOR_SKEW_SECONDS)).isoformat()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def polled(
    subscribed: bool,
    last_trigger_at: str | None,
    ticket_end_at: str | None,
    now: str | None = None,
) -> bool:
    """Return whether a Thread should remain in the poll set."""
    if subscribed:
        return True
    moment = _parse(now) or datetime.now(UTC)
    newest = max(
        (item for item in (_parse(last_trigger_at), _parse(ticket_end_at)) if item is not None),
        default=None,
    )
    if newest is None:
        return False
    return moment - newest <= timedelta(milliseconds=POLL_HORIZON_MS)


async def poll_threads(
    threads: list[Any],
    read_one: Callable[[Any, str | None], Awaitable[dict[str, Any]]],
    signal: asyncio.Event | None = None,
    *,
    on_error: Callable[[Any, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Poll each Thread once with a cursor from its ``cursor`` attribute."""
    results: list[dict[str, Any]] = []
    for thread in threads:
        if signal is not None and signal.is_set():
            break
        cursor = getattr(thread, "cursor", None)
        try:
            results.append(await read_one(thread, cursor))
        except Exception as exc:  # noqa: BLE001 — one bad Thread must not stop the poll
            if on_error is not None:
                on_error(thread, str(exc))
    return results


__all__ = ["CURSOR_SKEW_SECONDS", "POLL_HORIZON_MS", "cursor_at", "polled", "poll_threads"]
