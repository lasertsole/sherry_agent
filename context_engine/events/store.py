"""Event append/read/replay on the MesMemory database."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from context_engine.events.types import EventType


def append_event(
    session_id: str,
    event_type: EventType,
    data: dict[str, Any],
    db: Any = None,
) -> dict[str, Any]:
    """Append one event with the next gapless per-session sequence number."""
    db = (
        db
        if db is not None
        else __import__("context_engine.store.db", fromlist=["get_db"]).get_db()
    )
    seq_row = db.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    seq = seq_row[0]
    created = datetime.now().strftime("%Y%m%d%H%M%S")
    db.execute(
        "INSERT INTO events (session_id, type, data, seq, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, event_type.value, json.dumps(data, ensure_ascii=False), seq, created),
    )
    return {"session_id": session_id, "type": event_type.value, "data": data, "seq": seq}


def get_events(session_id: str) -> list[dict[str, Any]]:
    """All events of a session in sequence order."""
    db = __import__("context_engine.store.db", fromlist=["get_db"]).get_db()
    rows = db.execute(
        "SELECT id, session_id, type, data, seq, created_at FROM events "
        "WHERE session_id = ? ORDER BY seq ASC",
        (session_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "type": row["type"],
            "data": json.loads(row["data"]),
            "seq": row["seq"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def replay_events(session_id: str) -> list[dict[str, Any]]:
    """Replay a session's events in order — the reconstruction entry point.

    Returns the ordered event list; consumers apply their own projections.
    """
    return get_events(session_id)
