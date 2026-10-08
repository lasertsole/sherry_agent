"""SQLite persistence layer for the session todo list (todos table).

Database path: agent/tools/todolist/data/todos.db
Table schema: todos(session_id TEXT NOT NULL, content TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'pending', priority TEXT NOT NULL DEFAULT 'medium',
position INTEGER NOT NULL, category TEXT NOT NULL DEFAULT 'quick',
delegation TEXT NOT NULL DEFAULT 'self', subagent_id TEXT, flow_id TEXT,
step_id TEXT, plan_ref TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
PRIMARY KEY (session_id, position)).

Connection lifecycle comes from ``agent.tools.pub_base.sqlite_store``
(``BaseSQLiteRepository``, the shared skeleton also used by the taskflow and
subagent registries): EVERY connection (aiosqlite and stdlib sqlite3 alike) is
configured with a 5s busy timeout as its first statement (or the equivalent
connect ``timeout``), so contended writes wait for the lock instead of failing
with ``sqlite3.OperationalError: database is locked``. The WAL check/switch and
the ``CREATE TABLE IF NOT EXISTS`` statements run ONCE per process, lazy,
loop-gated under an asyncio.Lock on the async path and thread-locked on the
sync path, never per operation.

The store keeps no DAG state: ``depends_on``/``wave_index`` are deliberately
absent. Dependency edges, unlock logic and the
``blocked/ready/dispatched/done`` step status live in TaskFlow and are reached
through ``flow_id``/``step_id``.
"""

import asyncio
import sqlite3
import threading
from pathlib import Path

from loguru import logger

from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository
from config.features import TODOLIST_INFRA
from ..config import (
    DEFAULT_CATEGORY,
    DEFAULT_DELEGATION,
    DEFAULT_PRIORITY,
    DEFAULT_STATUS,
    TABLE_NAME,
)

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "todos.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout, handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = TODOLIST_INFRA["store_busy_timeout_ms"]
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see ensure_db).
_INIT_WAIT_TIMEOUT_S = TODOLIST_INFRA["store_init_wait_timeout_s"]

# Column order is the contract for _row_to_todo; keep both in lockstep.
_TODO_COLUMNS = (
    "session_id, content, status, priority, position, category, delegation, "
    "subagent_id, flow_id, step_id, plan_ref, created_at"
)
_INSERT_COLUMNS = (
    "session_id, content, status, priority, position, category, delegation, "
    "subagent_id, flow_id, step_id, plan_ref"
)

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    session_id TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '{DEFAULT_STATUS}',
    priority TEXT NOT NULL DEFAULT '{DEFAULT_PRIORITY}',
    position INTEGER NOT NULL,
    category TEXT NOT NULL DEFAULT '{DEFAULT_CATEGORY}',
    delegation TEXT NOT NULL DEFAULT '{DEFAULT_DELEGATION}',
    subagent_id TEXT,
    flow_id TEXT,
    step_id TEXT,
    plan_ref TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
"""

_SELECT_COLUMNS_SQL = f"SELECT {_TODO_COLUMNS} FROM {TABLE_NAME}"
_INSERT_SQL = (
    f"INSERT INTO {TABLE_NAME} ({_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# Once-per-process async schema-init state. asyncio primitives are single-loop
# by design, so the lock is only ever touched by the owning loop (see
# ensure_db); other loops poll _initialized instead of queueing on the lock.
_init_lock = asyncio.Lock()
_init_loop: asyncio.AbstractEventLoop | None = None
_initialized = False

# Once-per-process sync schema-init state (stdlib sqlite3 read paths may be
# called from arbitrary threads; threading.Lock is cross-thread safe).
_sync_tables_ready = False
_sync_init_lock = threading.Lock()


def _row_to_todo(row: tuple) -> dict:
    (
        session_id,
        content,
        status,
        priority,
        position,
        category,
        delegation,
        subagent_id,
        flow_id,
        step_id,
        plan_ref,
        created_at,
    ) = row
    return {
        "session_id": session_id,
        "content": content,
        "status": status,
        "priority": priority,
        "position": int(position),
        "category": category,
        "delegation": delegation,
        "subagent_id": subagent_id,
        "flow_id": flow_id,
        "step_id": step_id,
        "plan_ref": plan_ref,
        "created_at": created_at,
    }


def _normalize_rows(session_id: str, todos: list[dict]) -> list[tuple]:
    """Map caller dicts to INSERT tuples, applying config defaults.

    ``position`` falls back to the list index so callers may omit it; a missing
    or empty ``content`` is rejected because the column is NOT NULL.
    """
    rows: list[tuple] = []
    for index, todo in enumerate(todos):
        content = todo.get("content")
        if not content:
            raise ValueError("todo content must be a non-empty string")
        position = todo.get("position")
        rows.append(
            (
                session_id,
                content,
                todo.get("status") or DEFAULT_STATUS,
                todo.get("priority") or DEFAULT_PRIORITY,
                int(position) if position is not None else index,
                todo.get("category") or DEFAULT_CATEGORY,
                todo.get("delegation") or DEFAULT_DELEGATION,
                todo.get("subagent_id"),
                todo.get("flow_id"),
                todo.get("step_id"),
                todo.get("plan_ref"),
            )
        )
    return rows


# Query-coverage index (2026-09 SQLite index audit). get_todos_by_flow filters
# ``(session_id, flow_id)``; the ``(session_id, position)`` primary key only
# covers the session_id prefix, so flow_id is otherwise matched row by row.
# Created with ``IF NOT EXISTS`` on every schema init -- async and sync alike --
# so a database that predates it converges on its next open, not just a fresh one.
_INDEX_DDL: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_todolist_session_flow ON {TABLE_NAME}(session_id, flow_id)",
)


class _TodolistRepository(BaseSQLiteRepository):
    """Base-class wiring for the todos store: DDL hooks + WAL warning label."""

    wal_label = "todolist db"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_CREATE_TABLE_SQL,)

    def _index_ddls(self) -> tuple[str, ...]:
        return _INDEX_DDL


# The base reads/writes the module-level init state through this namespace, so
# the existing monkeypatch contract (``_DB_PATH``/``_initialized``/...) is intact.
_repository = _TodolistRepository(globals())

_connect = _repository.connect
_switch_to_wal_if_needed = _repository.switch_to_wal_if_needed
ensure_db = _repository.ensure_db
_ensure_tables_sync = _repository.ensure_tables_sync


async def replace_all(session_id: str, todos: list[dict]) -> None:
    """Replace the session's entire todo list: DELETE + INSERT in one transaction.

    ``todos`` is the full list, not a delta. An empty list clears the session.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    rows = _normalize_rows(session_id, todos)
    await ensure_db()
    async with _connect() as db:
        await db.execute(f"DELETE FROM {TABLE_NAME} WHERE session_id = ?", (session_id,))
        await db.executemany(_INSERT_SQL, rows)
        # aiosqlite defers until commit, so DELETE and INSERTs are one transaction.
        await db.commit()


async def get_todos(session_id: str) -> list[dict]:
    """Read the session's todos ordered by position."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE session_id = ? ORDER BY position",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_todo(row) for row in rows]


async def delete_todos_by_session(session_id: str) -> int:
    """Delete every todo row of a session; returns the row count.

    Session purge path (``server.DAO.messages.clear_session``): a cleared
    session leaves no todo debris behind.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"DELETE FROM {TABLE_NAME} WHERE session_id = ?", (session_id,))
        await db.commit()
        return int(cursor.rowcount or 0)


async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """Read the session's todos linked to one TaskFlow flow, ordered by position."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE session_id = ? AND flow_id = ? ORDER BY position",
            (session_id, flow_id),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_todo(row) for row in rows]


def get_todos_sync(session_id: str) -> list[dict]:
    """Synchronously read the session's todos (stdlib sqlite3, no event loop).

    For prompt injection / middlewares. Mirrors the taskflow blueprint's sync
    paths: threading.Lock-guarded one-time table creation, connect-level busy
    timeout, failures logged and swallowed with an empty-list return.
    """
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            rows = conn.execute(
                _SELECT_COLUMNS_SQL + " WHERE session_id = ? ORDER BY position",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_todo(row) for row in rows]
    except Exception as e:
        logger.warning("Failed to sync-read todos for session {}: {}", session_id, e)
        return []


__all__ = [
    "delete_todos_by_session",
    "ensure_db",
    "get_todos",
    "get_todos_by_flow",
    "get_todos_sync",
    "replace_all",
]
