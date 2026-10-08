"""Index-audit tests for the taskflow store (2026-09 SQLite index audit).

Locks two behaviors the audit required:

1. Upgrade path: a ``task_flows`` database created before the audit (table
   present, no indexes) receives ``idx_taskflow_status`` and the PARTIAL
   ``idx_taskflow_deadline`` on its next init -- through the async
   ``ensure_db`` path AND the stdlib sqlite3 sync path -- and repeated inits
   stay idempotent.
2. Query plans: SQLite's planner demonstrably uses the new indexes for the
   audit's query patterns (EXPLAIN QUERY PLAN assertions below).
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.tools.taskflow.config import TABLE_NAME, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]

_STATUS_INDEX = "idx_taskflow_status"
_DEADLINE_INDEX = "idx_taskflow_deadline"
_SESSION_INDEX = "idx_taskflow_session_status"


def _reset_init_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a process restart: fresh once-per-process init state."""
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)


def _create_pre_audit_db(db_path: Path) -> None:
    """Reproduce the pre-audit database: the table plus one row, no indexes."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(store_sqlite._CREATE_TABLE_SQL)
        conn.execute(
            f"INSERT INTO {TABLE_NAME} "
            "(flow_id, state_json, wait_json, expected_revision, status, child_session_key) "
            "VALUES ('flow-legacy', '{}', NULL, 1, ?, NULL)",
            (TaskFlowStatus.RUNNING.value,),
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


def _seed_flows(db_path: Path, count: int = 1000) -> None:
    statuses = [status.value for status in TaskFlowStatus]
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            f"INSERT INTO {TABLE_NAME} "
            "(flow_id, state_json, wait_json, expected_revision, status, child_session_key, "
            "total_tokens, total_cost, token_budget, deadline_ts) "
            "VALUES (?, '{}', NULL, ?, ?, NULL, 0, 0.0, 0, ?)",
            [
                (f"flow-{i}", i, statuses[i % len(statuses)], float(i) if i % 2 == 0 else None)
                for i in range(count)
            ],
        )
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_old_db_gains_indexes_and_keeps_rows_via_async_ensure_db(isolated_db: Path):
    _create_pre_audit_db(isolated_db)
    pre_audit = _index_rows(isolated_db)
    assert _STATUS_INDEX not in pre_audit
    assert _DEADLINE_INDEX not in pre_audit

    await store_sqlite.ensure_db()

    indexes = _index_rows(isolated_db)
    assert indexes[_STATUS_INDEX] == 0
    assert indexes[_DEADLINE_INDEX] == 1, "idx_taskflow_deadline must stay partial"
    flow = await store_sqlite.get_flow("flow-legacy", "")
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.RUNNING.value


def test_old_db_gains_indexes_via_sync_path(isolated_db: Path):
    """The stdlib sqlite3 path (no event loop) must also converge an old db."""
    _create_pre_audit_db(isolated_db)
    pre_audit = _index_rows(isolated_db)
    assert _STATUS_INDEX not in pre_audit
    assert _DEADLINE_INDEX not in pre_audit

    flow = store_sqlite.get_flow_sync("flow-legacy", "")

    assert flow is not None
    indexes = _index_rows(isolated_db)
    assert _STATUS_INDEX in indexes
    assert indexes[_DEADLINE_INDEX] == 1


@pytest.mark.asyncio
async def test_repeated_init_is_idempotent(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    _create_pre_audit_db(isolated_db)

    await store_sqlite.ensure_db()
    first = _index_rows(isolated_db)

    _reset_init_state(monkeypatch)
    await store_sqlite.ensure_db()

    assert _index_rows(isolated_db) == first


@pytest.mark.asyncio
async def test_query_plans_use_new_indexes(isolated_db: Path):
    """EXPLAIN QUERY PLAN proves the planner picks the audit's indexes."""
    _create_pre_audit_db(isolated_db)
    await store_sqlite.ensure_db()
    _seed_flows(isolated_db)

    status_plan = _plan(
        isolated_db, store_sqlite._SELECT_COLUMNS_SQL + " WHERE status = ?", ("waiting",)
    )
    assert _STATUS_INDEX in status_plan, status_plan

    active_plan = _plan(
        isolated_db,
        store_sqlite._SELECT_COLUMNS_SQL
        + " WHERE session_id = ? AND status IN (?, ?) ORDER BY expected_revision DESC",
        ("", TaskFlowStatus.RUNNING.value, TaskFlowStatus.WAITING.value),
    )
    assert _SESSION_INDEX in active_plan, active_plan

    overdue_plan = _plan(
        isolated_db,
        store_sqlite._SELECT_COLUMNS_SQL
        + " WHERE deadline_ts IS NOT NULL AND deadline_ts < ? AND status NOT IN (?, ?)",
        (500.0, TaskFlowStatus.DONE.value, TaskFlowStatus.FAILED.value),
    )
    assert _DEADLINE_INDEX in overdue_plan, overdue_plan
