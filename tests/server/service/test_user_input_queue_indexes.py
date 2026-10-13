"""Index-audit tests for the durable user-input queue (2026-09 SQLite index audit).

Locks two behaviors the audit required:

1. Upgrade path: a pre-audit database (table + the two existing indexes) gains
   ``idx_user_input_queue_session_status`` on first use, and repeated inits
   stay idempotent.
2. Query plans: EXPLAIN QUERY PLAN proves the planner uses the composite index
   for the claim subquery and the session-scoped count/list scans.
"""

import sqlite3
from pathlib import Path

import pytest

from server.queue import user_input_queue as queue_module
from server.queue.user_input_queue import UserInputQueue

pytestmark = [pytest.mark.unit]

_TABLE = "user_input_queue"
_SESSION_INDEX = "idx_user_input_queue_session"
_UNIQUE_INDEX = "uq_user_input_queue_client_msg_active"
_COMPOSITE_INDEX = "idx_user_input_queue_session_status"

# The pre-audit index set, frozen: reproducing an old database must not drift
# with the source's current _CREATE_INDEX_SQLS.
_PRE_AUDIT_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_user_input_queue_session ON user_input_queue (session_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_input_queue_client_msg_active "
    "ON user_input_queue (client_msg_id) "
    "WHERE client_msg_id IS NOT NULL AND status IN ('QUEUED', 'CLAIMED')",
)

_INSERT_SQL = (
    f"INSERT INTO {_TABLE} "
    "(id, session_id, payload, source, reply_target, client_msg_id, status, "
    "created_at, updated_at, expires_at) "
    "VALUES (?, ?, '{}', 'user', NULL, NULL, ?, ?, ?, ?)"
)


def _create_pre_audit_db(db_path: Path) -> None:
    """Reproduce a pre-audit database: table, old indexes, one row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(queue_module._CREATE_TABLE_SQL)
        for ddl in _PRE_AUDIT_INDEX_DDL:
            conn.execute(ddl)
        conn.execute(_INSERT_SQL, ("legacy-row", "sess-legacy", "QUEUED", 1.0, 1.0, 2.0))
        conn.commit()
    finally:
        conn.close()


def _index_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA index_list({_TABLE})")]
    finally:
        conn.close()


def _plan(db_path: Path, sql: str, params: tuple = ()) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return "\n".join(row[3] for row in rows)
    finally:
        conn.close()


def _seed_rows(db_path: Path, count: int = 1000) -> None:
    statuses = ["QUEUED", "CLAIMED", "DELIVERED", "FAILED", "VOIDED"]
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            _INSERT_SQL,
            [
                (
                    f"row-{i}",
                    f"sess-{i % 20}",
                    statuses[i % len(statuses)],
                    float(i),
                    float(i),
                    float(i) + 1000.0,
                )
                for i in range(count)
            ],
        )
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_old_db_gains_composite_index_and_keeps_rows(tmp_path: Path):
    db_path = tmp_path / "subagent_registry.db"
    _create_pre_audit_db(db_path)
    assert _COMPOSITE_INDEX not in _index_names(db_path)

    store = UserInputQueue(db_path=db_path)
    assert await store.count_active("sess-legacy") == 1

    names = _index_names(db_path)
    assert _COMPOSITE_INDEX in names
    assert {_SESSION_INDEX, _UNIQUE_INDEX} <= set(names)


@pytest.mark.asyncio
async def test_repeated_init_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "subagent_registry.db"
    _create_pre_audit_db(db_path)

    first = UserInputQueue(db_path=db_path)
    assert await first.count_active("sess-legacy") == 1

    second = UserInputQueue(db_path=db_path)
    assert await second.count_active("sess-legacy") == 1

    assert _index_names(db_path).count(_COMPOSITE_INDEX) == 1


@pytest.mark.asyncio
async def test_query_plans_use_composite_index(tmp_path: Path):
    """EXPLAIN QUERY PLAN proves the planner picks idx_user_input_queue_session_status."""
    db_path = tmp_path / "subagent_registry.db"
    store = UserInputQueue(db_path=db_path)
    await store.count_active("warm")  # run the one-time schema init
    _seed_rows(db_path)

    claim_plan = _plan(db_path, queue_module._CLAIM_NEXT_SQL, (0.0, "sess-0", 0.0))
    assert _COMPOSITE_INDEX in claim_plan, claim_plan

    count_plan = _plan(
        db_path,
        f"SELECT COUNT(*) FROM {_TABLE} "
        f"WHERE session_id = ? AND status IN {queue_module._ACTIVE_STATUSES_SQL}",
        ("sess-0",),
    )
    assert _COMPOSITE_INDEX in count_plan, count_plan

    list_plan = _plan(
        db_path,
        f"SELECT {queue_module._ROW_COLUMNS} FROM {_TABLE} "
        f"WHERE session_id = ? AND status IN {queue_module._ACTIVE_STATUSES_SQL} "
        "ORDER BY created_at ASC, id ASC",
        ("sess-0",),
    )
    assert _COMPOSITE_INDEX in list_plan, list_plan
