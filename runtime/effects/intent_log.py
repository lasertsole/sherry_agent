"""The restart-recovery intent log: a durable, generation-guarded effect queue.

One row per ``(kind, key)``. Enqueueing the same pair again is an UPSERT that
increments ``generation`` and resets the attempt counter, so a slow worker that
finishes an OLD generation cannot mark the NEW one done — every terminal
transition is a CAS on ``(id, generation, status='in_flight')``.

Timestamps use wall-clock seconds, not ``time.monotonic()``: the queue must
survive a process restart, and a monotonic reading from the previous process is
meaningless in the next one. Pending rows due in the future keep their delay;
overdue rows fire immediately after ``replay_pending()``.

The SQLite file is separate from every existing store (``src/data/effects/
effects.db``), so no existing table is altered. All calls are synchronous: this
store is used by boot recovery and the worker loop, never by an async
connection whose event loop may not exist yet.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.path import APP_HOME

__all__ = [
    "IntentRow",
    "enqueue",
    "fetch_pending",
    "mark_abandoned",
    "mark_done",
    "mark_in_flight",
    "mark_retry",
    "replay_pending",
]

_DB_DIR: Path = APP_HOME / "src" / "data" / "effects"
_DB_PATH: Path = _DB_DIR / "effects.db"
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = 5.0
_sync_tables_ready = False
_sync_init_lock = threading.Lock()

_EFFECT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS effect_intent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 1,
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    due_at      REAL    NOT NULL,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    UNIQUE(kind, key)
)
"""

_EFFECT_INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_effect_intent_status_due ON effect_intent(status, due_at)",
)


def _ensure_tables_sync() -> None:
    """Create the one table + index once per process (thread-locked DDL)."""
    global _sync_tables_ready
    if _sync_tables_ready:
        return
    with _sync_init_lock:
        if _sync_tables_ready:
            return
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            for ddl in (_EFFECT_TABLE_DDL, *_EFFECT_INDEX_DDLS):
                conn.execute(ddl)
            conn.commit()
        finally:
            conn.close()
        _sync_tables_ready = True


def _connect_sync() -> sqlite3.Connection:
    """Open a WAL connection after ensuring the schema once per process."""
    _ensure_tables_sync()
    conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
    except Exception:
        conn.close()
        raise
    return conn


@dataclass(frozen=True)
class IntentRow:
    id: int
    kind: str
    key: str
    payload: dict
    generation: int
    status: str
    attempts: int
    due_at: float


def enqueue(kind: str, key: str, payload: dict, due_at: float | None = None) -> tuple[int, int]:
    """Upsert by ``(kind, key)``; returns ``(intent_id, generation)``.

    Wall-clock ``due_at`` (default now). A re-enqueue of an in-flight row
    increments the generation, which makes the old worker's terminal CAS fail.
    """
    now = time.time()
    due = now if due_at is None else due_at
    payload_json = json.dumps(payload, ensure_ascii=False)
    conn = _connect_sync()
    try:
        conn.execute(
            """INSERT INTO effect_intent
                   (kind, key, payload, generation, status, attempts, due_at, created_at, updated_at)
               VALUES (?, ?, ?, 1, 'pending', 0, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET
                   payload = excluded.payload,
                   generation = effect_intent.generation + 1,
                   status = 'pending',
                   attempts = 0,
                   due_at = excluded.due_at,
                   updated_at = excluded.updated_at""",
            (kind, key, payload_json, due, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, generation FROM effect_intent WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
        if row is None:  # pragma: no cover - the upsert above guarantees it
            raise RuntimeError("effect_intent upsert vanished")
        return int(row[0]), int(row[1])
    finally:
        conn.close()


def mark_in_flight(intent_id: int, generation: int) -> bool:
    """CAS ``pending → in_flight`` for one generation; True if acquired."""
    conn = _connect_sync()
    try:
        cursor = conn.execute(
            "UPDATE effect_intent SET status = 'in_flight', updated_at = ? "
            "WHERE id = ? AND generation = ? AND status = 'pending'",
            (time.time(), intent_id, generation),
        )
        conn.commit()
        return int(cursor.rowcount) > 0
    finally:
        conn.close()


def mark_done(intent_id: int, generation: int) -> bool:
    """CAS the exact generation from ``in_flight`` to ``done``."""
    conn = _connect_sync()
    try:
        cursor = conn.execute(
            "UPDATE effect_intent SET status = 'done', updated_at = ? "
            "WHERE id = ? AND generation = ? AND status = 'in_flight'",
            (time.time(), intent_id, generation),
        )
        conn.commit()
        return int(cursor.rowcount) > 0
    finally:
        conn.close()


def mark_retry(intent_id: int, generation: int, next_due_at: float) -> bool:
    """CAS the exact generation back to ``pending`` with one more attempt."""
    conn = _connect_sync()
    try:
        cursor = conn.execute(
            "UPDATE effect_intent SET status = 'pending', attempts = attempts + 1, "
            "due_at = ?, updated_at = ? "
            "WHERE id = ? AND generation = ? AND status = 'in_flight'",
            (next_due_at, time.time(), intent_id, generation),
        )
        conn.commit()
        return int(cursor.rowcount) > 0
    finally:
        conn.close()


def mark_abandoned(intent_id: int, generation: int) -> bool:
    """CAS the exact generation to ``abandoned`` (terminal)."""
    conn = _connect_sync()
    try:
        cursor = conn.execute(
            "UPDATE effect_intent SET status = 'abandoned', updated_at = ? "
            "WHERE id = ? AND generation = ? AND status = 'in_flight'",
            (time.time(), intent_id, generation),
        )
        conn.commit()
        return int(cursor.rowcount) > 0
    finally:
        conn.close()


def _row_from_db(row: sqlite3.Row) -> IntentRow:
    return IntentRow(
        id=int(row["id"]),
        kind=str(row["kind"]),
        key=str(row["key"]),
        payload=json.loads(row["payload"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        due_at=float(row["due_at"]),
    )


def fetch_pending(limit: int = 50) -> list[IntentRow]:
    """Rows that are due now, oldest due first."""
    conn = _connect_sync()
    try:
        rows = conn.execute(
            """SELECT id, kind, key, payload, generation, status, attempts, due_at
               FROM effect_intent
               WHERE status = 'pending' AND due_at <= ?
               ORDER BY due_at ASC, id ASC
               LIMIT ?""",
            (time.time(), int(limit)),
        ).fetchall()
        return [_row_from_db(row) for row in rows]
    finally:
        conn.close()


def replay_pending() -> list[IntentRow]:
    """Restart recovery: return every in-flight row to pending and rebase it.

    Pending rows keep their wall-clock ``due_at``: a future delay survives the
    restart, an overdue row is immediately eligible. No monotonic timestamp is
    compared across processes.
    """
    now = time.time()
    conn = _connect_sync()
    try:
        conn.execute(
            "UPDATE effect_intent SET status = 'pending', due_at = ?, updated_at = ? "
            "WHERE status = 'in_flight'",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    pending = fetch_pending(limit=1000)
    logger.info("effects: replayed {} pending intent(s) after restart", len(pending))
    return pending
