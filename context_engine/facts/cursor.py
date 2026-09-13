"""Dual-watermark cursor for facts extraction (crash-safe consumption).

``enqueued`` advances when a persisted turn is queued for extraction;
``consumed`` advances only after facts are written. Both are monotonic and
durable (state_register_db), so a crash between enqueue and consume replays
the range instead of losing it.
"""

from __future__ import annotations

import json

from runtime import state_register_db

_ENQUEUED_KEY = "facts_cursor_enqueued"
_CONSUMED_KEY = "facts_cursor_consumed"


def _read(key: str, session_id: str) -> int:
    raw = state_register_db.get_state(session_id, key, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def get_cursor(session_id: str) -> dict[str, int]:
    return {
        "enqueued_through_turn": _read(_ENQUEUED_KEY, session_id),
        "consumed_through_turn": _read(_CONSUMED_KEY, session_id),
    }


def advance_enqueued(session_id: str, turn_num: int) -> None:
    current = _read(_ENQUEUED_KEY, session_id)
    state_register_db.set_state(session_id, _ENQUEUED_KEY, json.dumps(max(current, int(turn_num))))


def advance_consumed(session_id: str, turn_num: int) -> None:
    enqueued = _read(_ENQUEUED_KEY, session_id)
    target = int(turn_num)
    if target > enqueued:
        raise ValueError(f"consumed({target}) > enqueued({enqueued}) for session {session_id}")
    current = _read(_CONSUMED_KEY, session_id)
    state_register_db.set_state(session_id, _CONSUMED_KEY, json.dumps(max(current, target)))


def get_pending(session_id: str) -> tuple[int, int]:
    """Return the (start, end) inclusive range of turns awaiting extraction."""
    cursor: dict[str, int] = get_cursor(session_id)
    start = cursor["consumed_through_turn"] + 1
    end = cursor["enqueued_through_turn"]
    return (start, end) if end >= start else (0, 0)
