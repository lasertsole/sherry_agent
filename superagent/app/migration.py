"""SQLite schema bootstrap and 10→11 migration (prototype ``src/app/migration.ts``)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from superagent.app.store_read import EVAL_META_KEY
from superagent.core.model import Eval, SourceRef, to_dict
from superagent.kv import KV

SCHEMA_VERSION = 11

SCHEMA_11: dict[str, str] = {
    "trigger": """
        CREATE TABLE trigger (
          id TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          actor_json TEXT NOT NULL,
          human INTEGER NOT NULL,
          addressed TEXT,
          at TEXT NOT NULL,
          text TEXT NOT NULL,
          channel_json TEXT,
          url TEXT,
          meta_json TEXT NOT NULL
        )
    """,
    "disposition": """
        CREATE TABLE disposition (
          trigger_id TEXT PRIMARY KEY REFERENCES trigger(id),
          filtered INTEGER NOT NULL,
          filter_reason TEXT,
          eval_id TEXT,
          decision_id TEXT,
          reached_json TEXT,
          handed_back_json TEXT,
          acknowledged_json TEXT,
          requeued INTEGER DEFAULT 0
        )
    """,
    "trigger_thread": """
        CREATE TABLE trigger_thread (
          trigger_id TEXT NOT NULL REFERENCES trigger(id),
          thread_key TEXT NOT NULL,
          source TEXT NOT NULL,
          thread_id TEXT NOT NULL,
          at TEXT NOT NULL,
          human INTEGER NOT NULL,
          PRIMARY KEY (trigger_id, thread_key)
        )
    """,
    "thread": """
        CREATE TABLE thread (
          thread_key TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          thread_id TEXT NOT NULL,
          subscriptions_json TEXT NOT NULL,
          context_json TEXT,
          queued_trigger_ids_json TEXT,
          eval_id TEXT,
          expired_count INTEGER DEFAULT 0,
          meta_json TEXT,
          has_queued INTEGER NOT NULL DEFAULT 0,
          has_eval INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER GENERATED ALWAYS AS (has_queued OR has_eval) VIRTUAL,
          is_subscribed INTEGER NOT NULL DEFAULT 0
        )
    """,
    "ticket": """
        CREATE TABLE ticket (
          id TEXT PRIMARY KEY,
          origin_json TEXT NOT NULL,
          mandate_json TEXT NOT NULL,
          execution_json TEXT NOT NULL,
          channels_json TEXT NOT NULL,
          inputs_json TEXT NOT NULL,
          follows_json TEXT NOT NULL,
          assignments_json TEXT NOT NULL,
          latest_report_json TEXT,
          ended_json TEXT,
          ended_at TEXT,
          is_open INTEGER GENERATED ALWAYS AS (
            CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END
          ) VIRTUAL,
          latest_assignment_status TEXT,
          has_undelivered_input INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER GENERATED ALWAYS AS (
            CASE WHEN ended_at IS NULL AND (
              has_undelivered_input = 1
              OR latest_assignment_status IN ('pending', 'running')
            ) THEN 1 ELSE 0 END
          ) VIRTUAL
        )
    """,
    "decision": """
        CREATE TABLE decision (
          id TEXT PRIMARY KEY,
          thread_key TEXT NOT NULL,
          outcome TEXT NOT NULL,
          reason_json TEXT NOT NULL,
          at TEXT NOT NULL,
          trigger_ids_json TEXT NOT NULL
        )
    """,
    "effect": """
        CREATE TABLE effect (
          kind TEXT NOT NULL,
          key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER DEFAULT 0,
          last_error TEXT,
          due_at TEXT,
          UNIQUE(kind, key)
        )
    """,
    "outbox": """
        CREATE TABLE outbox (
          id TEXT PRIMARY KEY,
          ticket_id TEXT,
          channel_json TEXT NOT NULL,
          purpose TEXT NOT NULL,
          text TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER DEFAULT 0,
          last_error TEXT,
          dropped_at TEXT,
          dropped_reason TEXT,
          is_dropped INTEGER GENERATED ALWAYS AS (
            CASE WHEN status = 'dropped' THEN 1 ELSE 0 END
          ) VIRTUAL
        )
    """,
    "kv": """
        CREATE TABLE kv (
          key TEXT PRIMARY KEY,
          value TEXT
        )
    """,
}

INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ix_trigger_thread_thread",
        "CREATE INDEX ix_trigger_thread_thread ON trigger_thread(thread_key, at, trigger_id)",
    ),
    ("ix_thread_active", "CREATE INDEX ix_thread_active ON thread(is_active) WHERE is_active = 1"),
    (
        "ix_thread_subscribed",
        "CREATE INDEX ix_thread_subscribed ON thread(is_subscribed) WHERE is_subscribed = 1",
    ),
    ("ix_ticket_open", "CREATE INDEX ix_ticket_open ON ticket(is_open) WHERE is_open = 1"),
    ("ix_ticket_active", "CREATE INDEX ix_ticket_active ON ticket(is_active) WHERE is_active = 1"),
    ("ix_decision_thread", "CREATE INDEX ix_decision_thread ON decision(thread_key, at)"),
    ("ix_effect_due", "CREATE INDEX ix_effect_due ON effect(due_at) WHERE status = 'pending'"),
    (
        "ix_outbox_ticket_conv",
        "CREATE INDEX ix_outbox_ticket_conv ON outbox(ticket_id, channel_json)",
    ),
    (
        "ix_outbox_dropped",
        "CREATE INDEX ix_outbox_dropped ON outbox(is_dropped) WHERE is_dropped = 1",
    ),
)

THREAD_INDEXES = ("ix_thread_active", "ix_thread_subscribed")
TICKET_INDEXES = ("ix_ticket_open", "ix_ticket_active")
_INDEX_SQL = {name: sql for name, sql in INDEXES}

_REQUIRED_TABLES = frozenset(SCHEMA_11)
_EXPLICIT_COLUMNS = {
    "thread": frozenset({"has_queued", "has_eval", "is_subscribed"}),
    "ticket": frozenset({"ended_at", "latest_assignment_status", "has_undelivered_input"}),
}


class MigrationError(RuntimeError):
    """Raised when a database cannot be opened at the current schema version."""


def create_schema_11(conn: sqlite3.Connection) -> None:
    """Create every schema-11 table and index."""
    for statement in SCHEMA_11.values():
        conn.execute(statement)
    for _, statement in INDEXES:
        conn.execute(statement)


def create_schema_10_fixture(conn: sqlite3.Connection) -> None:
    """Create a compact schema-10 database for migration tests."""
    create_schema_11(conn)
    for name in (*THREAD_INDEXES, *TICKET_INDEXES):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE thread")
    conn.execute("DROP TABLE ticket")
    conn.execute(
        """
        CREATE TABLE thread (
          thread_key TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          thread_id TEXT NOT NULL,
          subscriptions_json TEXT NOT NULL,
          context_json TEXT,
          queued_trigger_ids_json TEXT,
          eval_id TEXT,
          expired_count INTEGER DEFAULT 0,
          meta_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ticket (
          id TEXT PRIMARY KEY,
          origin_json TEXT NOT NULL,
          mandate_json TEXT NOT NULL,
          execution_json TEXT NOT NULL,
          channels_json TEXT NOT NULL,
          inputs_json TEXT NOT NULL,
          follows_json TEXT NOT NULL,
          assignments_json TEXT NOT NULL,
          latest_report_json TEXT,
          ended_json TEXT
        )
        """
    )
    conn.execute("PRAGMA user_version = 10")


def ensure_schema_11(conn: sqlite3.Connection) -> None:
    """Bootstrap or migrate ``conn`` to schema 11."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        existing = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if existing & _REQUIRED_TABLES:
            raise MigrationError("unversioned database already contains superagent tables")
        create_schema_11(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    if version == 10:
        _migrate_10_to_11(conn)
        return
    raise MigrationError(
        "a database migrated by a newer version cannot be opened by an older one "
        f"(found {version}, expected {SCHEMA_VERSION})"
    )


def _has_columns(conn: sqlite3.Connection, table: str, names: frozenset[str]) -> bool:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return names <= present


def _json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json_object(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _migrate_10_to_11(conn: sqlite3.Connection) -> None:
    """Rebuild thread/ticket with explicit columns while preserving rows."""
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = _REQUIRED_TABLES - existing
    if missing:
        raise MigrationError(f"schema-10 database is missing tables: {sorted(missing)}")

    if all(_has_columns(conn, table, columns) for table, columns in _EXPLICIT_COLUMNS.items()):
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        thread_rows = conn.execute(
            "SELECT thread_key, source, thread_id, subscriptions_json, context_json, "
            "queued_trigger_ids_json, eval_id, expired_count, meta_json FROM thread"
        ).fetchall()
        ticket_rows = conn.execute(
            "SELECT id, origin_json, mandate_json, execution_json, channels_json, "
            "inputs_json, follows_json, assignments_json, latest_report_json, ended_json "
            "FROM ticket"
        ).fetchall()

        for name in (*THREAD_INDEXES, *TICKET_INDEXES):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("DROP TABLE thread")
        conn.execute("DROP TABLE ticket")
        conn.execute(SCHEMA_11["thread"])
        conn.execute(SCHEMA_11["ticket"])

        for row in thread_rows:
            subscriptions = _json_list(row[3])
            queued = [str(item) for item in _json_list(row[5])]
            meta = _json_object(row[8]) or {}
            if row[6]:
                meta[EVAL_META_KEY] = to_dict(
                    Eval(
                        id=str(row[6]),
                        thread=SourceRef(source=str(row[1]), id=str(row[2])),
                        primary=queued[0] if queued else "",
                        trigger_ids=queued,
                    )
                )
            meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True) if meta else None
            conn.execute(
                """
                INSERT INTO thread(
                  thread_key, source, thread_id, subscriptions_json, context_json,
                  queued_trigger_ids_json, eval_id, expired_count, meta_json,
                  has_queued, has_eval, is_subscribed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    meta_json,
                    int(bool(queued)),
                    int(bool(row[6])),
                    int(bool(subscriptions)),
                ),
            )

        for row in ticket_rows:
            inputs = _json_list(row[5])
            assignments = _json_list(row[7])
            ended = _json_object(row[9])
            latest_status = None
            if assignments and isinstance(assignments[-1], dict):
                latest_status = assignments[-1].get("status")
            conn.execute(
                """
                INSERT INTO ticket(
                  id, origin_json, mandate_json, execution_json, channels_json,
                  inputs_json, follows_json, assignments_json, latest_report_json,
                  ended_json, ended_at, latest_assignment_status, has_undelivered_input
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    ended.get("at") if ended else None,
                    latest_status,
                    int(
                        any(
                            not item.get("delivered", False)
                            for item in inputs
                            if isinstance(item, dict)
                        )
                    ),
                ),
            )

        for name in (*THREAD_INDEXES, *TICKET_INDEXES):
            conn.execute(_INDEX_SQL[name])
        KV.set(conn, KV.SCHEMA, SCHEMA_VERSION)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
