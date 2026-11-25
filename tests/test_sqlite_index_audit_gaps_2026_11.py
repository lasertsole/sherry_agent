"""2026-11-23 "补齐 4 处缺失索引" plan regressions (EXPLAIN QUERY PLAN).

Locks the four medium-severity gaps fixed by that plan:

* task_flows ``superseded_by IS NULL`` partial index + ``expected_revision``
  ordering (plus the direct session/revision index EXPLAIN proved is required
  for the session-wide ORDER BY paths);
* memory_entries embedding IS NULL / IS NOT NULL partial indexes;
* evidence_events ``event IS NULL`` partial index.

All plans use in-memory replicas of the production DDL and queries — no model
key, Docker or server is needed. The planner assertions go beyond "the index
exists": a SCAN must become a SEARCH, the partial index must be the one chosen,
and ORDER BY paths must no longer need a temp B-tree.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]


def _plan(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    return "\n".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _index_partial_by_table(conn: sqlite3.Connection, table: str, index: str) -> int:
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if row[1] == index:
            return int(row[4])
    raise AssertionError(f"index {index} not found on {table}")


def _index_sql(conn: sqlite3.Connection, index: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (index,)
    ).fetchone()
    return str(row[0]) if row is not None else ""


# ── task_flows ──────────────────────────────────────────────────────────────


@pytest.fixture()
def taskflow_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE task_flows (
            flow_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, wait_json TEXT,
            expected_revision INTEGER NOT NULL, status TEXT NOT NULL,
            child_session_key TEXT, total_tokens INTEGER DEFAULT 0,
            total_cost REAL DEFAULT 0.0, token_budget INTEGER DEFAULT 0,
            deadline_ts REAL, session_id TEXT NOT NULL DEFAULT '',
            resumed_from TEXT, superseded_by TEXT, updated_at REAL, terminal_reason TEXT
        );
        CREATE INDEX idx_taskflow_status ON task_flows(status);
        CREATE INDEX idx_taskflow_session_status
            ON task_flows(session_id, status, expected_revision DESC);
        CREATE INDEX idx_taskflow_active
            ON task_flows(session_id, status) WHERE superseded_by IS NULL;
        CREATE INDEX idx_taskflow_session_rev
            ON task_flows(session_id, expected_revision DESC);
    """)
    yield conn
    conn.close()


def test_taskflow_active_index_exists(taskflow_db: sqlite3.Connection):
    """idx_taskflow_active is partial and physically holds only live flows."""
    assert "idx_taskflow_active" in _index_names(taskflow_db, "task_flows")
    assert _index_partial_by_table(taskflow_db, "task_flows", "idx_taskflow_active") == 1
    assert "SUPERSEDED_BY IS NULL" in _index_sql(taskflow_db, "idx_taskflow_active").upper()

    taskflow_db.executemany(
        "INSERT INTO task_flows "
        "(flow_id, state_json, expected_revision, status, session_id, superseded_by) "
        "VALUES (?, '{}', ?, ?, ?, ?)",
        [
            ("live", 1, "running", "s", None),
            ("superseded", 2, "running", "s", "other-flow"),
        ],
    )
    # Forcing the index proves the superseded row cannot hide inside it.
    visible = [
        row[0]
        for row in taskflow_db.execute(
            "SELECT flow_id FROM task_flows INDEXED BY idx_taskflow_active "
            "WHERE session_id = ? AND superseded_by IS NULL",
            ("s",),
        )
    ]
    assert visible == ["live"]


def test_taskflow_session_status_has_expected_revision(taskflow_db: sqlite3.Connection):
    """The status-prefixed index covers DESC ordering; session-wide reads use
    the companion (session_id, expected_revision DESC) index with no temp sort."""
    sql = _index_sql(taskflow_db, "idx_taskflow_session_status").upper()
    assert "EXPECTED_REVISION DESC" in sql
    cols = [row[2] for row in taskflow_db.execute("PRAGMA index_info(idx_taskflow_session_status)")]
    assert cols == ["session_id", "status", "expected_revision"]

    exact = _plan(
        taskflow_db,
        "SELECT flow_id FROM task_flows "
        "WHERE session_id = ? AND status = ? ORDER BY expected_revision DESC",
        ("s", "done"),
    )
    assert "idx_taskflow_session_status" in exact, exact
    assert "USE TEMP B-TREE FOR ORDER BY" not in exact, exact

    session_wide = _plan(
        taskflow_db,
        "SELECT flow_id FROM task_flows "
        "WHERE session_id = ? AND status IN (?, ?) AND superseded_by IS NULL "
        "ORDER BY expected_revision DESC",
        ("s", "running", "waiting"),
    )
    assert "idx_taskflow_session_rev" in session_wide, session_wide
    assert "USE TEMP B-TREE FOR ORDER BY" not in session_wide, session_wide


def test_taskflow_get_non_terminal_uses_index(taskflow_db: sqlite3.Connection):
    """Both non-terminal query shapes hit the partial index, not a table scan."""
    all_sessions = _plan(
        taskflow_db,
        "SELECT flow_id FROM task_flows "
        "WHERE status NOT IN ('done', 'failed', 'cancelled') AND superseded_by IS NULL",
    )
    assert "USING INDEX idx_taskflow_active" in all_sessions, all_sessions

    scoped = _plan(
        taskflow_db,
        "SELECT flow_id FROM task_flows "
        "WHERE status NOT IN ('done', 'failed', 'cancelled') "
        "AND superseded_by IS NULL AND session_id = ?",
        ("s",),
    )
    assert "SEARCH task_flows USING INDEX" in scoped, scoped
    assert "SCAN task_flows" not in scoped, scoped
    assert "USE TEMP B-TREE FOR ORDER BY" not in scoped, scoped


# ── memory_entries ──────────────────────────────────────────────────────────


MEMORY_TABLE = """
    CREATE TABLE memory_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target TEXT NOT NULL, content TEXT NOT NULL, entry_type TEXT,
        origin_session TEXT, created_at INTEGER NOT NULL, updated_at INTEGER,
        last_used INTEGER, usage_count INTEGER NOT NULL DEFAULT 0,
        char_count INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
        archived_at INTEGER, archived_reason TEXT, compaction_epoch INTEGER DEFAULT 0,
        embedding BLOB, embedding_model TEXT, embedding_dim INTEGER
    );
"""

MEMORY_EMBEDDING_INDEXES = """
    CREATE INDEX IF NOT EXISTS idx_memory_target ON memory_entries(target) WHERE archived = 0;
    CREATE INDEX IF NOT EXISTS idx_memory_entries_missing_embedding
        ON memory_entries(target, id) WHERE archived = 0 AND embedding IS NULL;
    CREATE INDEX IF NOT EXISTS idx_memory_entries_has_embedding
        ON memory_entries(target, id) WHERE archived = 0 AND embedding IS NOT NULL;
"""


@pytest.fixture()
def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(MEMORY_TABLE + MEMORY_EMBEDDING_INDEXES)
    yield conn
    conn.close()


def _seed_memory_skew(conn: sqlite3.Connection) -> None:
    """Realistic embedding job state: 5% un-embedded, ANALYZE'd so the planner
    sees the partial indexes' selectivity (stats-free small DBs pick the older
    idx_memory_target instead — that is a planner artifact, not a missing index)."""
    conn.executemany(
        "INSERT INTO memory_entries "
        "(target, content, created_at, char_count, archived, embedding) "
        "VALUES (?, ?, ?, 1, 0, ?)",
        [("memory", f"entry-{i}", i, None if i % 20 == 0 else b"vec") for i in range(2000)],
    )
    conn.execute("ANALYZE")
    conn.commit()


def test_memory_missing_embedding_index(memory_db: sqlite3.Connection):
    assert "idx_memory_entries_missing_embedding" in _index_names(memory_db, "memory_entries")
    assert (
        _index_partial_by_table(memory_db, "memory_entries", "idx_memory_entries_missing_embedding")
        == 1
    )
    sql = _index_sql(memory_db, "idx_memory_entries_missing_embedding").upper()
    assert "ARCHIVED = 0" in sql and "EMBEDDING IS NULL" in sql

    memory_db.executemany(
        "INSERT INTO memory_entries "
        "(target, content, created_at, char_count, archived, embedding) "
        "VALUES (?, 'x', ?, 1, ?, ?)",
        [("memory", 1, 0, None), ("memory", 2, 0, b"vec"), ("memory", 3, 1, None)],
    )
    visible = [
        row[0]
        for row in memory_db.execute(
            "SELECT id FROM memory_entries INDEXED BY idx_memory_entries_missing_embedding "
            "WHERE archived = 0 AND embedding IS NULL"
        )
    ]
    assert visible == [1]


def test_memory_has_embedding_index(memory_db: sqlite3.Connection):
    assert "idx_memory_entries_has_embedding" in _index_names(memory_db, "memory_entries")
    assert (
        _index_partial_by_table(memory_db, "memory_entries", "idx_memory_entries_has_embedding")
        == 1
    )
    sql = _index_sql(memory_db, "idx_memory_entries_has_embedding").upper()
    assert "ARCHIVED = 0" in sql and "EMBEDDING IS NOT NULL" in sql

    memory_db.executemany(
        "INSERT INTO memory_entries "
        "(target, content, created_at, char_count, archived, embedding) "
        "VALUES (?, 'x', ?, 1, ?, ?)",
        [("memory", 1, 0, None), ("memory", 2, 0, b"vec"), ("memory", 3, 1, b"vec")],
    )
    visible = [
        row[0]
        for row in memory_db.execute(
            "SELECT id FROM memory_entries INDEXED BY idx_memory_entries_has_embedding "
            "WHERE archived = 0 AND embedding IS NOT NULL"
        )
    ]
    assert visible == [2]

    # With the embedding state selective (1 of 3 active rows), count_embeddings
    # also picks the partial index. When ~95% of rows carry a vector a full scan
    # is the correct planner choice, so assert on the selective case only.
    count_plan = _plan(
        memory_db,
        "SELECT COUNT(*) FROM memory_entries "
        "WHERE archived = 0 AND embedding IS NOT NULL AND target = ?",
        ("memory",),
    )
    assert "idx_memory_entries_has_embedding" in count_plan, count_plan


def test_memory_list_missing_uses_index(memory_db: sqlite3.Connection):
    _seed_memory_skew(memory_db)
    plan = _plan(
        memory_db,
        "SELECT * FROM memory_entries "
        "WHERE archived = 0 AND embedding IS NULL AND target = ? "
        "ORDER BY id ASC LIMIT ?",
        ("memory", 500),
    )
    assert "idx_memory_entries_missing_embedding" in plan, plan
    assert "SCAN memory_entries" not in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan


# ── evidence_events ─────────────────────────────────────────────────────────

EVIDENCE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS evidence_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, session_id TEXT NOT NULL, event TEXT,
        file_path TEXT, command TEXT, status TEXT, exit_code INTEGER, kind TEXT,
        modified_files_json TEXT NOT NULL DEFAULT '[]', payload_json TEXT NOT NULL,
        flow_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence_events(session_id, event_id);
    CREATE INDEX IF NOT EXISTS idx_evidence_timestamp ON evidence_events(timestamp);
    CREATE INDEX IF NOT EXISTS idx_evidence_flow ON evidence_events(flow_id, event_id);
    CREATE INDEX IF NOT EXISTS idx_evidence_unmarked
        ON evidence_events(session_id, event_id) WHERE event IS NULL;
"""

# Verbatim from agent.tools.pub_base.evidence_ledger._EVIDENCE_PATH_MATCH.
EVIDENCE_PATH_MATCH = (
    "EXISTS (SELECT 1 FROM json_each(COALESCE(modified_files_json, '[]')) "
    "WHERE value = ? OR value LIKE ? ESCAPE '\\')"
)


@pytest.fixture()
def evidence_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(EVIDENCE_SCHEMA)
    yield conn
    conn.close()


def test_evidence_unmarked_index(evidence_db: sqlite3.Connection):
    assert "idx_evidence_unmarked" in _index_names(evidence_db, "evidence_events")
    assert _index_partial_by_table(evidence_db, "evidence_events", "idx_evidence_unmarked") == 1
    sql = _index_sql(evidence_db, "idx_evidence_unmarked").upper()
    assert "EVENT IS NULL" in sql

    evidence_db.executemany(
        "INSERT INTO evidence_events "
        "(timestamp, session_id, event, modified_files_json, payload_json) "
        "VALUES ('t', ?, ?, '[]', '{}')",
        [("s", None), ("s", "stale")],
    )
    visible = [
        row[0]
        for row in evidence_db.execute(
            "SELECT event_id FROM evidence_events INDEXED BY idx_evidence_unmarked "
            "WHERE event IS NULL"
        )
    ]
    assert visible == [1]


def test_evidence_mark_stale_uses_index(evidence_db: sqlite3.Connection):
    plan = _plan(
        evidence_db,
        "SELECT COUNT(*) FROM evidence_events "
        f"WHERE event IS NULL AND {EVIDENCE_PATH_MATCH} AND session_id = ?",
        ("a.py", "a.py%", "s"),
    )
    assert "idx_evidence_unmarked" in plan, plan
    assert "SCAN evidence_events" not in plan, plan


# ── idempotency ─────────────────────────────────────────────────────────────


def test_index_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real taskflow sync init replays DROP+CREATE cleanly; the memory and
    evidence index DDL likewise survive a second application unchanged."""
    db_path = tmp_path / "registry.db"
    monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
    monkeypatch.setattr(store_sqlite, "_DB_PATH", db_path)
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)

    store_sqlite._ensure_tables_sync()
    conn = sqlite3.connect(db_path)
    first = _index_names(conn, "task_flows")
    conn.close()
    assert {
        "idx_taskflow_session_status",
        "idx_taskflow_active",
        "idx_taskflow_session_rev",
    } <= first

    # Force a second full replay (the once-per-process flag normally short-circuits).
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    store_sqlite._ensure_tables_sync()
    conn = sqlite3.connect(db_path)
    assert _index_names(conn, "task_flows") == first
    conn.close()

    # memory_entries: the table DDL has no IF NOT EXISTS (isolated test DBs are
    # fresh), so replay only the index DDL — exactly what schema init does.
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(MEMORY_TABLE)
        mem.executescript(MEMORY_EMBEDDING_INDEXES)
        before = _index_names(mem, "memory_entries")
        mem.executescript(MEMORY_EMBEDDING_INDEXES)
        assert _index_names(mem, "memory_entries") == before
    finally:
        mem.close()

    evidence = sqlite3.connect(":memory:")
    try:
        evidence.executescript(EVIDENCE_SCHEMA)
        before = _index_names(evidence, "evidence_events")
        evidence.executescript(EVIDENCE_SCHEMA)
        assert _index_names(evidence, "evidence_events") == before
    finally:
        evidence.close()
