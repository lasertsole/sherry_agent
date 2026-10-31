"""Persistent parent links for ephemeral side-chat sessions.

A side chat is an ordinary session with one extra fact: it belongs to a parent
session. The link must survive a restart so a deleted parent can still cascade
into its children (and a boot sweep can remove orphans), so it lives in its own
small SQLite table instead of an in-memory register.

Only ids, the fork flag, the fork point and the bounded fallback text are
stored here; the conversation itself stays in the normal message/checkpoint
stores.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

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

_DB_PATH: Path = SRC_DIR / "data" / "side_chats.db"

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


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection with the table ready."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute(_DDL)
    _add_missing_columns(conn)
    conn.execute(_INDEX_DDL)
    try:
        conn.execute(_IDEMPOTENCY_INDEX_DDL)
    except sqlite3.OperationalError:
        pass  # a pre-column table: no dedupe, every other operation unchanged
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add the post-release columns an older database file lacks."""
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(side_chats)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE side_chats ADD COLUMN {name} {decl}")


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
