"""Read-only queries and row decoders for orchestrator SQLite (``store-read.ts``)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from superagent.core.model import (
    Decision,
    Effect,
    Receipt,
    Ticket,
    Thread,
    Trigger,
    decision_from_dict,
    effect_from_dict,
    receipt_from_dict,
    thread_from_dict,
    ticket_from_dict,
    trigger_from_dict,
)

EVAL_META_KEY = "__superagent_eval"


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    """Return a plain dict from a SQLite row."""
    return dict(row) if row is not None else {}


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def decode_trigger(conn: sqlite3.Connection, row: sqlite3.Row) -> Trigger:
    """Decode a trigger row plus its thread associations."""
    data = _row_dict(row)
    thread_rows = conn.execute(
        "SELECT thread_key, source, thread_id, at FROM trigger_thread "
        "WHERE trigger_id = ? ORDER BY at, thread_key",
        (data.get("id"),),
    ).fetchall()
    data.update(
        {
            "actor": _loads(data.get("actor_json"), {}),
            "channel": _loads(data.get("channel_json"), None),
            "meta": _loads(data.get("meta_json"), {}),
            "threads": [
                {"source": item["source"], "id": item["thread_id"]} for item in thread_rows
            ],
        }
    )
    return trigger_from_dict(data)


def decode_thread(row: sqlite3.Row) -> Thread:
    """Decode a thread row, including the internal Eval envelope."""
    data = _row_dict(row)
    meta = _loads(data.get("meta_json"), None)
    eval_data = None
    if isinstance(meta, dict) and EVAL_META_KEY in meta:
        eval_data = meta.pop(EVAL_META_KEY)
    data.update(
        {
            "key": data.get("thread_key"),
            "id": data.get("thread_id"),
            "subscriptions": _loads(data.get("subscriptions_json"), []),
            "context": _loads(data.get("context_json"), None),
            "queued_trigger_ids": _loads(data.get("queued_trigger_ids_json"), []),
            "eval": eval_data,
            "meta": meta if meta else None,
        }
    )
    return thread_from_dict(data)


def decode_ticket(row: sqlite3.Row) -> Ticket:
    """Decode a ticket row and its JSON columns."""
    data = _row_dict(row)
    for column, field in (
        ("origin_json", "origin"),
        ("mandate_json", "mandate"),
        ("execution_json", "execution"),
        ("channels_json", "channels"),
        ("inputs_json", "inputs"),
        ("follows_json", "follows"),
        ("assignments_json", "assignments"),
        ("latest_report_json", "latest_report"),
        ("ended_json", "ended"),
    ):
        data[field] = _loads(data.get(column), None if field in {"latest_report", "ended"} else {})
    return ticket_from_dict(data)


def decode_decision(row: sqlite3.Row) -> Decision:
    """Decode a decision row, including the Source envelope in ``reason_json``."""
    data = _row_dict(row)
    envelope = _loads(data.get("reason_json"), {})
    source = ""
    thread_id = str(data.get("thread_key", ""))
    reason = envelope
    if isinstance(envelope, dict) and "__source" in envelope:
        source = str(envelope.get("__source", ""))
        thread_id = str(envelope.get("__thread_id", thread_id))
        reason = envelope.get("__reason", {})
    data.update(
        {
            "thread": {"source": source, "id": thread_id},
            "reason": reason,
            "trigger_ids": _loads(data.get("trigger_ids_json"), []),
        }
    )
    return decision_from_dict(data)


def decode_effect(row: sqlite3.Row) -> Effect:
    """Decode an effect row."""
    data = _row_dict(row)
    data["payload"] = _loads(data.get("payload_json"), {})
    return effect_from_dict(data)


def decode_receipt(row: sqlite3.Row) -> Receipt:
    """Decode an outbox receipt row."""
    data = _row_dict(row)
    data["channel"] = _loads(data.get("channel_json"), {})
    data["dropped"] = _loads(data.get("dropped_json"), None)
    data["dropped"] = data.get("dropped") or (
        {"at": data["dropped_at"], "reason": data["dropped_reason"]}
        if data.get("dropped_at")
        else None
    )
    return receipt_from_dict(data)


class StoreRead:
    """Panel/search queries; every method issues SELECT only."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def thread_triggers(
        self,
        thread: str,
        cursor: str | tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[Trigger]:
        """Return a Thread's Triggers in chronological order."""
        sql = (
            "SELECT t.* FROM trigger t JOIN trigger_thread tt ON tt.trigger_id = t.id "
            "WHERE tt.thread_key = ?"
        )
        params: list[Any] = [thread]
        if cursor is not None:
            at, last_id = cursor if isinstance(cursor, tuple) else (cursor, "")
            sql += " AND (t.at > ? OR (t.at = ? AND t.id > ?))"
            params.extend([at, at, last_id])
        sql += " ORDER BY t.at, t.id LIMIT ?"
        params.append(limit)
        return [
            decode_trigger(self._conn, row) for row in self._conn.execute(sql, params).fetchall()
        ]

    def trigger_stats(self, source: str, since: str) -> dict[str, int]:
        """Return 24h-style counts for one Source."""
        rows = self._conn.execute(
            "SELECT human, COUNT(*) AS total FROM trigger "
            "WHERE source = ? AND at >= ? GROUP BY human",
            (source, since),
        ).fetchall()
        result = {"total": 0, "human": 0, "bot": 0}
        for row in rows:
            total = int(row["total"])
            result["total"] += total
            result["human" if int(row["human"]) else "bot"] += total
        return result

    def search(self, query: str, limit: int = 50) -> list[Trigger]:
        """Search Trigger ids, text, URL and stored Source metadata."""
        like = f"%{query}%"
        rows = self._conn.execute(
            "SELECT * FROM trigger WHERE id LIKE ? OR text LIKE ? OR url LIKE ? "
            "OR meta_json LIKE ? OR channel_json LIKE ? ORDER BY at DESC, id DESC LIMIT ?",
            (like, like, like, like, like, limit),
        ).fetchall()
        return [decode_trigger(self._conn, row) for row in rows]

    def dropped_replies(self, source: str, since: str) -> list[Receipt]:
        """Return dropped outbox receipts for a Source, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE status = 'dropped' AND channel_json LIKE ? "
            "AND COALESCE(dropped_at, '') >= ? ORDER BY dropped_at DESC, id DESC",
            (f'%"source":"{source}"%', since),
        ).fetchall()
        return [decode_receipt(row) for row in rows]

    def tickets_open(self) -> list[Ticket]:
        """Return open Tickets via the generated index."""
        rows = self._conn.execute(
            "SELECT * FROM ticket WHERE is_open = 1 ORDER BY rowid"
        ).fetchall()
        return [decode_ticket(row) for row in rows]

    def tickets_active(self) -> list[Ticket]:
        """Return active Tickets via the generated index."""
        rows = self._conn.execute(
            "SELECT * FROM ticket WHERE is_active = 1 ORDER BY rowid"
        ).fetchall()
        return [decode_ticket(row) for row in rows]

    def subscribed_threads(self, source: str) -> list[str]:
        """Return subscribed canonical thread keys for a Source."""
        rows = self._conn.execute(
            "SELECT thread_key FROM thread WHERE is_subscribed = 1 AND source = ? "
            "ORDER BY thread_key",
            (source,),
        ).fetchall()
        return [str(row["thread_key"]) for row in rows]

    def failing_replies(self) -> list[Receipt]:
        """Return pending Receipts that already failed at least once."""
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' AND attempts > 0 ORDER BY rowid"
        ).fetchall()
        return [decode_receipt(row) for row in rows]

    def dropped_since(self, since: str) -> list[Receipt]:
        """Return all Receipts dropped at or after ``since``."""
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE dropped_at >= ? ORDER BY dropped_at DESC, id DESC",
            (since,),
        ).fetchall()
        return [decode_receipt(row) for row in rows]


__all__ = [
    "EVAL_META_KEY",
    "StoreRead",
    "decode_decision",
    "decode_effect",
    "decode_receipt",
    "decode_thread",
    "decode_ticket",
    "decode_trigger",
]
