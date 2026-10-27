"""SQLite persistence layer for the session todo list (todos table).

Database path: agent/tools/todolist/data/todos.db
Table schema: todos(session_id TEXT NOT NULL, content TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'pending', priority TEXT NOT NULL DEFAULT 'medium',
position INTEGER NOT NULL, category TEXT NOT NULL DEFAULT 'quick',
delegation TEXT NOT NULL DEFAULT 'self', subagent_id TEXT, flow_id TEXT,
step_id TEXT, plan_ref TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
todo_id INTEGER NOT NULL, PRIMARY KEY (session_id, position)).

``position`` is only the display order; ``todo_id`` is the stable per-session
identity used by the local patch/remove/reattach API. Inserting between rows
shifts positions but never changes todo_id.

Connection lifecycle comes from ``agent.tools.pub_base.sqlite_store``
(``BaseSQLiteRepository``, the shared skeleton also used by the taskflow and
subagent registries): EVERY connection (aiosqlite and stdlib sqlite3 alike) is
configured with a 5s busy timeout as its first statement (or the equivalent
connect ``timeout``), so contended writes wait for the lock instead of failing
with ``sqlite3.OperationalError: database is locked``. The WAL check/switch and
the ``CREATE TABLE IF NOT EXISTS`` statements run ONCE per process, lazy,
loop-gated under an asyncio.Lock on the async path and thread-locked on the
sync path, never per operation.

The store keeps no DAG state: ``depends_on``/DAG state are deliberately
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
    "subagent_id, flow_id, step_id, plan_ref, created_at, todo_id"
)
_INSERT_COLUMNS = (
    "session_id, content, status, priority, position, category, delegation, "
    "subagent_id, flow_id, step_id, plan_ref, todo_id"
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
    todo_id INTEGER NOT NULL,
    PRIMARY KEY (session_id, position)
);
"""

_SELECT_COLUMNS_SQL = f"SELECT {_TODO_COLUMNS} FROM {TABLE_NAME}"
_INSERT_SQL = (
    f"INSERT INTO {TABLE_NAME} ({_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
        todo_id,
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
        "todo_id": int(todo_id),
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


async def _next_todo_id(db, session_id: str) -> int:
    """Allocate the next per-session todo_id (MAX+1). Call inside a transaction."""
    async with db.execute(
        f"SELECT COALESCE(MAX(todo_id), 0) + 1 FROM {TABLE_NAME} WHERE session_id = ?",
        (session_id,),
    ) as cursor:
        result = await cursor.fetchone()
    return int(result[0]) if result else 1


def _with_todo_ids(rows: list[tuple], start_id: int) -> list[tuple]:
    """Append a sequential todo_id to each normalized row."""
    return [(*row, start_id + index) for index, row in enumerate(rows)]


# Query-coverage indexes (2026-09 SQLite index audit). get_todos_by_flow filters
# ``(session_id, flow_id)``; the ``(session_id, position)`` primary key only
# covers the session_id prefix, so flow_id is otherwise matched row by row.
# ``idx_todolist_todo_id`` is the stable-identity lookup used by the local
# patch/remove/reattach API. Both are ``IF NOT EXISTS`` on every schema init.
_INDEX_DDL: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_todolist_session_flow ON {TABLE_NAME}(session_id, flow_id)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_todolist_todo_id ON {TABLE_NAME}(session_id, todo_id)",
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
    The DELETE and every INSERT commit together, so the unreferenced-flow
    sweep's root query can never observe the empty window between them. Every
    replacement row receives a fresh todo_id above the pre-delete maximum, so an
    old id can never point at a different todo after the rewrite.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    rows = _normalize_rows(session_id, todos)
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT COALESCE(MAX(todo_id), 0) + 1 FROM {TABLE_NAME} WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            result = await cursor.fetchone()
        start_id = int(result[0]) if result else 1
        rows = _with_todo_ids(rows, start_id)
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


async def get_active_flow_ids(session_id: str) -> set[str]:
    """Flow ids referenced by this session's non-terminal todos (the GC roots)."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT DISTINCT flow_id FROM {TABLE_NAME} "
            "WHERE session_id = ? AND status NOT IN (?, ?) "
            "AND flow_id IS NOT NULL AND flow_id != ''",
            (session_id, "completed", "cancelled"),
        ) as cursor:
            rows = await cursor.fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


async def append_todo(session_id: str, todo: dict) -> None:
    """Backward-compatible tail append; prefer :func:`add_todo`.

    Used by ``taskflow_create`` to bind a new flow to its GC root without
    clobbering a concurrent ``todowrite`` replacement.
    """
    await add_todo(session_id, todo)


async def add_todo(session_id: str, todo: dict, *, after_todo_id: int | None = None) -> dict | None:
    """Insert one todo, optionally directly after ``after_todo_id``.

    ``after_todo_id=None`` appends at the tail. An existing anchor shifts every
    later position by one (via a temporary offset, so the composite primary key
    never collides); todo_ids stay stable. Returns the inserted row, or None
    when the anchor id is not in this session.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    row = list(_normalize_rows(session_id, [todo])[0])
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT COALESCE(MAX(position), -1) FROM {TABLE_NAME} WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            max_position_result = await cursor.fetchone()
        max_position = int(max_position_result[0]) if max_position_result else -1
        if max_position >= int(TODOLIST_INFRA["position_shift_offset"]):
            raise ValueError("todo position exceeds the insertion offset")
        if after_todo_id is None:
            target_position = max_position + 1
        else:
            async with db.execute(
                f"SELECT position FROM {TABLE_NAME} WHERE session_id = ? AND todo_id = ?",
                (session_id, after_todo_id),
            ) as cursor:
                anchor = await cursor.fetchone()
            if anchor is None:
                return None
            target_position = int(anchor[0]) + 1
            offset = int(TODOLIST_INFRA["position_shift_offset"])
            await db.execute(
                f"UPDATE {TABLE_NAME} SET position = position + ? "
                "WHERE session_id = ? AND position >= ?",
                (offset, session_id, target_position),
            )
            await db.execute(
                f"UPDATE {TABLE_NAME} SET position = position - ? + 1 "
                f"WHERE session_id = ? AND position >= ?",
                (offset, session_id, offset + target_position),
            )
        todo_id = await _next_todo_id(db, session_id)
        row[4] = target_position
        row.append(todo_id)
        await db.execute(_INSERT_SQL, tuple(row))
        await db.commit()
    return await get_todo_by_id(session_id, todo_id)


async def get_todo_by_id(session_id: str, todo_id: int) -> dict | None:
    """Read one todo by its stable per-session id."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE session_id = ? AND todo_id = ?",
            (session_id, int(todo_id)),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_todo(row) if row else None


async def patch_todo_by_id(
    session_id: str,
    todo_id: int,
    *,
    content: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
) -> dict | None:
    """Update selected fields of one todo; flow_id/step_id/plan_ref stay put."""
    sets: list[str] = []
    params: list[object] = []
    if content is not None:
        sets.append("content = ?")
        params.append(content)
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if priority is not None:
        sets.append("priority = ?")
        params.append(priority)
    if category is not None:
        sets.append("category = ?")
        params.append(category)
    if not sets:
        return await get_todo_by_id(session_id, todo_id)
    params.extend([session_id, int(todo_id)])
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET {', '.join(sets)} WHERE session_id = ? AND todo_id = ?",
            tuple(params),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
    return await get_todo_by_id(session_id, todo_id)


async def remove_todo_by_id(session_id: str, todo_id: int) -> dict | None:
    """Delete one todo by id and return the removed row (None when absent)."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE session_id = ? AND todo_id = ?",
            (session_id, int(todo_id)),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        await db.execute(
            f"DELETE FROM {TABLE_NAME} WHERE session_id = ? AND todo_id = ?",
            (session_id, int(todo_id)),
        )
        await db.commit()
    return _row_to_todo(row)


async def set_todo_flow_id_by_id(session_id: str, todo_id: int, flow_id: str) -> dict | None:
    """Bind one existing todo as a flow's GC root (only flow_id changes)."""
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET flow_id = ? WHERE session_id = ? AND todo_id = ?",
            (flow_id, session_id, int(todo_id)),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
    return await get_todo_by_id(session_id, todo_id)


async def cancel_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
    """Cancel active todos that reference one of ``flow_ids``."""
    ids = [flow_id for flow_id in flow_ids if flow_id]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET status = ? "
            "WHERE session_id = ? AND status NOT IN (?, ?) "
            f"AND flow_id IN ({placeholders})",
            ("cancelled", session_id, "completed", "cancelled", *ids),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


async def complete_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
    """Mark active todos that reference a successfully finished flow completed."""
    ids = [flow_id for flow_id in flow_ids if flow_id]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET status = ? "
            "WHERE session_id = ? AND status NOT IN (?, ?) "
            f"AND flow_id IN ({placeholders})",
            ("completed", session_id, "completed", "cancelled", *ids),
        )
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
    "add_todo",
    "append_todo",
    "cancel_todos_for_flows",
    "complete_todos_for_flows",
    "delete_todos_by_session",
    "ensure_db",
    "get_todo_by_id",
    "get_todos",
    "get_todos_by_flow",
    "get_todos_sync",
    "patch_todo_by_id",
    "remove_todo_by_id",
    "replace_all",
    "set_todo_flow_id_by_id",
]
