"""SQLite persistence layer for the session todo list (todos table).

Database path: agent/tools/todolist/data/todos.db
Table schema: todos(session_id TEXT NOT NULL, content TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'pending', priority TEXT NOT NULL DEFAULT 'medium',
position INTEGER NOT NULL, category TEXT NOT NULL DEFAULT 'quick',
delegation TEXT NOT NULL DEFAULT 'self', subagent_id TEXT, flow_id TEXT,
step_id TEXT, plan_ref TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
PRIMARY KEY (session_id, position)).

Connection lifecycle mirrors agent/tools/taskflow/registry/store_sqlite.py
(which in turn mirrors the subagent registry blueprint): EVERY connection
(aiosqlite and stdlib sqlite3 alike) is configured with a 5s busy timeout as
its first statement (or the equivalent connect ``timeout``), so contended
writes wait for the lock instead of failing with ``sqlite3.OperationalError:
database is locked``. The WAL check/switch and the ``CREATE TABLE IF NOT
EXISTS`` statements run ONCE per process, lazy, loop-gated under an asyncio.Lock
on the async path and thread-locked on the sync path, never per operation.

The store keeps no DAG state: ``depends_on``/``wave_index`` are deliberately
absent. Dependency edges, unlock logic and the
``blocked/ready/dispatched/done`` step status live in TaskFlow and are reached
through ``flow_id``/``step_id``.
"""

import asyncio
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from loguru import logger

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


@asynccontextmanager
async def _connect() -> AsyncGenerator[aiosqlite.Connection]:
    """Open a short-lived connection; busy_timeout is always the FIRST statement."""
    db = await aiosqlite.connect(_DB_PATH)
    try:
        await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        yield db
    finally:
        await db.close()


async def _switch_to_wal_if_needed(db: aiosqlite.Connection) -> None:
    """Switch the database to WAL mode, unless it is already WAL.

    The journal-mode switch does NOT reliably honor busy_timeout: with an active
    writer it can raise OperationalError("database is locked") immediately. Once
    the file is WAL (the steady state after the first init) the pragma is
    skipped entirely. If a concurrent initializer is mid-switch, re-check and
    tolerate the outcome; init must never fail over the journal mode.
    """
    async with db.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
    mode = str(row[0]) if row and row[0] else ""
    if mode.lower() == "wal":
        return
    try:
        await db.execute("PRAGMA journal_mode=WAL")
    except aiosqlite.OperationalError:
        # Another connection may hold the exclusive lock for its own switch.
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        mode = str(row[0]) if row and row[0] else ""
        if mode.lower() != "wal":
            logger.warning(
                "todolist db stays in {!r} journal mode (WAL switch contended); "
                "proceeding without WAL",
                mode,
            )


async def _init_db() -> None:
    """One-time schema setup; safe to run concurrently (busy_timeout + IF NOT EXISTS)."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        await _switch_to_wal_if_needed(db)
        await db.execute(_CREATE_TABLE_SQL)
        await db.commit()


async def ensure_db() -> None:
    """Ensure the database directory and the todos table exist (once per process).

    Fast-returns once initialized. On first use the calling loop takes ownership
    and runs the schema init under its asyncio.Lock. Callers from other event
    loops never touch that lock (a foreign-thread release would wake a queued
    waiter via a non-threadsafe call_soon that can leave their loop asleep
    forever); they poll for the owner's one-time init and only self-init as a
    last resort, avoiding a rollback-journal stampede of concurrent DDL.
    """
    global _initialized, _init_loop
    if _initialized:
        return
    loop = asyncio.get_running_loop()
    if _init_loop is None:
        _init_loop = loop
    if _init_loop is loop:
        async with _init_lock:
            if _initialized:
                return
            await _init_db()
            _initialized = True
        return
    # Non-owning loop: wait for the owning loop to finish its one-time init.
    deadline = time.monotonic() + _INIT_WAIT_TIMEOUT_S
    while not _initialized and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if not _initialized:
        # Owning loop never finished (died mid-init): initialize ourselves.
        await _init_db()
        _initialized = True


def _ensure_tables_sync() -> None:
    """One-time table creation for the sync (stdlib sqlite3) paths.

    Thread-locked so concurrent sync callers cannot race on DDL; keeps the
    logged-and-swallowed contract of the sync read path.
    """
    global _sync_tables_ready
    if _sync_tables_ready:
        return
    with _sync_init_lock:
        if _sync_tables_ready:
            return
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()
        _sync_tables_ready = True


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
    "ensure_db",
    "get_todos",
    "get_todos_by_flow",
    "get_todos_sync",
    "replace_all",
]
