"""Persistent parent links for ephemeral side-chat sessions.

A side chat is an ordinary session with one extra fact: it belongs to a parent
session. The link must survive a restart so a deleted parent can still cascade
into its children (and a boot sweep can remove orphans), so it lives in its own
small SQLite table instead of an in-memory register.

Only ids, the fork flag, the fork point and the bounded fallback text are
stored here; the conversation itself stays in the normal message/checkpoint
stores.

Connection lifecycle comes from the shared
:class:`~agent.tools.pub_base.sqlite_store.BaseSQLiteRepository` skeleton (the
same one the auth, todolist, taskflow and subagent stores use): the
``CREATE TABLE`` / index DDL runs ONCE per process, thread-locked, instead of
on every short-lived connection; the journal mode is switched to WAL once (the
mode lives in the database header, so later connections inherit it); and every
connection still sets ``PRAGMA busy_timeout`` as its first statement. The two
post-release columns an older database file lacks are converged once behind a
``PRAGMA table_info`` guard, and the idempotency unique index tolerates a table
that could not gain its column (a pre-column file keeps working without the
dedupe instead of failing every statement).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository
from config.path import SRC_DIR

__all__ = [
    "all_side_chats",
    "create_side_chat",
    "delete_side_chat",
    "find_by_request",
    "get_side_chat",
    "list_children",
    "side_chat_ids",
]

_DB_DIR: Path = SRC_DIR / "data"
_DB_PATH: Path = _DB_DIR / "side_chats.db"

#: Every connection waits (up to) this long for a contended lock instead of
#: failing immediately. Kept at the pre-base 30s so a boot-time writer cannot
#: turn a concurrent read into an exception.
_BUSY_TIMEOUT_MS = 30_000
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

_DDL = """
CREATE TABLE IF NOT EXISTS side_chats (
    child_session_id  TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    context_text      TEXT NOT NULL DEFAULT '',
    fork_mode         INTEGER NOT NULL DEFAULT 0,
    fork_turn_num     INTEGER,
    created_at        INTEGER NOT NULL,
    client_request_id TEXT
)
"""

#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` does
#: nothing to an existing file, so each one is added once, guarded by a PRAGMA
#: check (additive and non-destructive: old rows read as NULL — the legacy
#: "no idempotency key / default fork point" behaviour). ``client_request_id``
#: predates this module's first "added" list but the VERY first release did not
#: have it, and a two-generation-old file otherwise fails every statement that
#: names it (live-verified on this host: a side-chat POST 500'd with
#: "no such column: client_request_id").
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("client_request_id", "TEXT"),
    ("fork_turn_num", "INTEGER"),
)

_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_side_chats_parent ON side_chats (parent_session_id)"

# Creation idempotency (design concept #27): one client_request_id per parent
# names at most ONE child, so a retried POST returns the existing fork instead
# of building a second complete copy. NULLs (legacy/no-id rows) never collide —
# SQLite unique indexes ignore them. A pre-column table (an old side_chats.db,
# which is NOT migrated in place) keeps working without the dedupe.
_IDEMPOTENCY_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_side_chats_request "
    "ON side_chats (parent_session_id, client_request_id)"
)


class _SideChatRepository(BaseSQLiteRepository):
    """Base-class wiring for the link table: DDL hooks + WAL warning label."""

    wal_label = "side chats db"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_DDL,)

    def _index_ddls(self) -> tuple[str, ...]:
        return (_INDEX_DDL,)


# Once-per-process schema-init state. The base reads/writes the keys it needs
# through this module namespace (declared here so tests can monkeypatch them,
# like the other stores). ``_schema_ready`` covers the WAL switch and the
# additive-column convergence, which the base's DDL-only init cannot express.
_sync_tables_ready = False
_sync_init_lock = threading.Lock()
_schema_ready = False
_schema_lock = threading.Lock()

# The base reads/writes the module-level init state through this namespace.
_repository = _SideChatRepository(globals())


def _switch_to_wal_sync() -> None:
    """Switch the database to WAL mode once, unless it already is WAL.

    The journal-mode switch does not reliably honor busy_timeout: with an active
    writer it can raise ``OperationalError("database is locked")`` immediately.
    Once the file is WAL (the steady state after the first init) the pragma is
    skipped entirely. If a concurrent initializer is mid-switch, re-check and
    tolerate the outcome; init must never fail over the journal mode.
    """
    conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]) if row and row[0] else ""
        if mode.lower() == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Another connection may hold the exclusive lock for its own switch.
            row = conn.execute("PRAGMA journal_mode").fetchone()
            mode = str(row[0]) if row and row[0] else ""
            if mode.lower() != "wal":
                logger.warning(
                    "side chats db stays in {!r} journal mode (WAL switch contended); "
                    "proceeding without WAL",
                    mode,
                )
    finally:
        conn.close()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add the post-release columns an older database file lacks."""
    conn.row_factory = sqlite3.Row
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(side_chats)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE side_chats ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                # A concurrent initializer added it between the probe and write.
                pass


def _ensure_schema_sync() -> None:
    """One-time (per process) schema setup: WAL, tables, additive columns.

    The base's thread-locked ``ensure_tables_sync`` is what creates the table
    and the parent index (once, never per connection). The WAL switch and the
    additive-column pass run around it because the base deliberately has no
    additive-migration hook; skipping them on a later call is what keeps a
    short-lived connection cheap.
    """
    global _schema_ready
    if _schema_ready:
        return
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _schema_lock:
        if _schema_ready:
            return
        _switch_to_wal_sync()
        _repository.ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            _add_missing_columns(conn)
            try:
                conn.execute(_IDEMPOTENCY_INDEX_DDL)
            except sqlite3.OperationalError:
                pass  # a pre-column table: no dedupe, every other operation unchanged
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection with the schema ready."""
    _ensure_schema_sync()
    conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def create_side_chat(
    child_session_id: str,
    parent_session_id: str,
    context_text: str = "",
    *,
    fork_mode: bool = False,
    client_request_id: str | None = None,
    fork_turn_num: int | None = None,
) -> None:
    """Insert (or replace) one side-chat link.

    ``fork_turn_num`` is the parent turn the child was branched from, or ``None``
    for a side chat / a legacy link that forks at the parent's last answer.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO side_chats "
            "(child_session_id, parent_session_id, context_text, fork_mode, fork_turn_num, "
            "created_at, client_request_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                child_session_id,
                parent_session_id,
                context_text,
                1 if fork_mode else 0,
                fork_turn_num,
                int(time.time()),
                client_request_id,
            ),
        )
        conn.commit()


def find_by_request(parent_session_id: str, client_request_id: str) -> dict[str, Any] | None:
    """The child created for one (parent, client_request_id) pair, or None."""
    if not client_request_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT child_session_id, parent_session_id, context_text, fork_mode, fork_turn_num, "
            "created_at FROM side_chats WHERE parent_session_id = ? AND client_request_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (parent_session_id, client_request_id),
        ).fetchone()
    return dict(row) if row is not None else None


def get_side_chat(child_session_id: str) -> dict[str, Any] | None:
    """Return one link row, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT child_session_id, parent_session_id, context_text, fork_mode, fork_turn_num, "
            "created_at FROM side_chats WHERE child_session_id = ?",
            (child_session_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_children(parent_session_id: str) -> list[str]:
    """Child session ids belonging to one parent, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT child_session_id FROM side_chats "
            "WHERE parent_session_id = ? ORDER BY created_at ASC",
            (parent_session_id,),
        ).fetchall()
    return [str(row["child_session_id"]) for row in rows]


def all_side_chats() -> list[dict[str, Any]]:
    """Every link row (used by the boot orphan sweep)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT child_session_id, parent_session_id, context_text, fork_mode, fork_turn_num, "
            "created_at FROM side_chats"
        ).fetchall()
    return [dict(row) for row in rows]


def side_chat_ids() -> set[str]:
    """All child ids."""
    with _connect() as conn:
        rows = conn.execute("SELECT child_session_id FROM side_chats").fetchall()
    return {str(row["child_session_id"]) for row in rows}


def delete_side_chat(child_session_id: str) -> bool:
    """Remove one link row; True when a row was deleted."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM side_chats WHERE child_session_id = ?", (child_session_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
