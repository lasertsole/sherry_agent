"""Index-audit tests for the todolist store (2026-09 SQLite index audit).

Locks two behaviors the audit required:

1. Upgrade path: a ``todos`` database created before the audit (table present,
   no new index) receives ``idx_todolist_session_flow`` on its next init --
   through the async ``ensure_db`` path AND the stdlib sqlite3 sync path -- and
   repeated inits stay idempotent.
2. Query plan: EXPLAIN QUERY PLAN proves the planner uses the composite index
   for ``WHERE session_id = ? AND flow_id = ? ORDER BY position``.

Honest planner note (2026-09): without ``sqlite_stat1`` the planner prefers the
``(session_id, position)`` PK autoindex -- it narrows by session_id AND
satisfies ORDER BY position without a temp B-tree. After ``ANALYZE`` the
composite index wins because flow_id shrinks the estimated row count. The index
therefore pays off in statistics-informed plans; small sessions were never the
bottleneck either way.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.tools.todolist.config import TABLE_NAME
from agent.tools.todolist.registry import store_sqlite

pytestmark = [pytest.mark.unit]

_FLOW_INDEX = "idx_todolist_session_flow"


def _reset_init_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a process restart: fresh once-per-process init state."""
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)


def _create_pre_audit_db(db_path: Path) -> None:
    """Reproduce the pre-audit database: the table plus one row, no new index."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(store_sqlite._CREATE_TABLE_SQL)
        conn.execute(
            f"INSERT INTO {TABLE_NAME} "
            "(session_id, content, status, priority, position, flow_id) "
            "VALUES ('sess-legacy', 'legacy todo', 'pending', 'medium', 0, 'flow-legacy')"
        )
        conn.commit()
    finally:
        conn.close()


def _index_rows(db_path: Path) -> dict[str, int]:
    """``PRAGMA index_list`` as ``{index_name: partial_flag}``."""
    conn = sqlite3.connect(db_path)
    try:
        return {row[1]: int(row[4]) for row in conn.execute(f"PRAGMA index_list({TABLE_NAME})")}
    finally:
        conn.close()


def _plan(db_path: Path, sql: str, params: tuple = ()) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return "\n".join(row[3] for row in rows)
    finally:
        conn.close()


def _seed_todos(db_path: Path, count: int = 2000) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            f"INSERT INTO {TABLE_NAME} "
            "(session_id, content, status, priority, position, flow_id) "
            "VALUES (?, ?, 'pending', 'medium', ?, ?)",
            [(f"sess-{i % 20}", f"todo-{i}", i, f"flow-{i % 7}") for i in range(count)],
        )
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_old_db_gains_index_and_keeps_rows_via_async_ensure_db(isolated_db: Path):
    _create_pre_audit_db(isolated_db)
    assert _FLOW_INDEX not in _index_rows(isolated_db)

    await store_sqlite.ensure_db()

    assert _FLOW_INDEX in _index_rows(isolated_db)
    assert [t["content"] for t in await store_sqlite.get_todos("sess-legacy")] == ["legacy todo"]


def test_old_db_gains_index_via_sync_path(isolated_db: Path):
    """The stdlib sqlite3 path (no event loop) must also converge an old db."""
    _create_pre_audit_db(isolated_db)
    assert _FLOW_INDEX not in _index_rows(isolated_db)

    rows = store_sqlite.get_todos_sync("sess-legacy")

    assert [t["content"] for t in rows] == ["legacy todo"]
    assert _FLOW_INDEX in _index_rows(isolated_db)


@pytest.mark.asyncio
async def test_repeated_init_is_idempotent(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    _create_pre_audit_db(isolated_db)

    await store_sqlite.ensure_db()
    first = _index_rows(isolated_db)

    _reset_init_state(monkeypatch)
    await store_sqlite.ensure_db()

    assert _index_rows(isolated_db) == first


@pytest.mark.asyncio
async def test_query_plan_uses_flow_index(isolated_db: Path):
    """EXPLAIN QUERY PLAN proves the planner picks idx_todolist_session_flow."""
    _create_pre_audit_db(isolated_db)
    await store_sqlite.ensure_db()
    _seed_todos(isolated_db)

    plan = _plan(
        isolated_db,
        store_sqlite._SELECT_COLUMNS_SQL
        + " WHERE session_id = ? AND flow_id = ? ORDER BY position",
        ("sess-3", "flow-5"),
    )

    assert _FLOW_INDEX in plan, plan
