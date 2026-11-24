"""2026-11-24 SQLite index audit regressions (EXPLAIN QUERY PLAN).

Companion to the 2026-09 audit tests (taskflow/todolist/user_input_queue).
Locks the newly added high-priority indexes so they cannot silently regress:
an index must not only EXIST, the planner must actually USE it for the hot
query — and, for ORDER BY ... LIMIT paths, the temp B-tree sort must be gone.

All plans are reproduced in-memory with the same DDL/queries as production;
no model key, Docker or server is needed.
"""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.unit]


def _plan(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    return "\n".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


# ── memory_entries: aging expression + prompt sort ──────────────────────────


@pytest.fixture()
def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL, content TEXT NOT NULL, entry_type TEXT,
            origin_session TEXT, created_at INTEGER NOT NULL, updated_at INTEGER,
            last_used INTEGER, usage_count INTEGER NOT NULL DEFAULT 0,
            char_count INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER, archived_reason TEXT, compaction_epoch INTEGER DEFAULT 0,
            embedding BLOB, embedding_model TEXT, embedding_dim INTEGER
        );
        CREATE INDEX idx_memory_target ON memory_entries(target) WHERE archived = 0;
        CREATE INDEX idx_memory_usage ON memory_entries(last_used) WHERE archived = 0;
        CREATE INDEX idx_memory_origin ON memory_entries(origin_session);
        CREATE INDEX idx_memory_entries_aging
            ON memory_entries(COALESCE(last_used, created_at), id) WHERE archived = 0;
        CREATE INDEX idx_memory_entries_target_created
            ON memory_entries(target, created_at, id) WHERE archived = 0;
        CREATE INDEX idx_memory_entries_target_usage
            ON memory_entries(target, usage_count, id) WHERE archived = 0;
    """)
    yield conn
    conn.close()


def test_prune_unused_uses_expression_index(memory_db: sqlite3.Connection):
    assert "idx_memory_entries_aging" in _index_names(memory_db, "memory_entries")
    plan = _plan(
        memory_db,
        "SELECT id FROM memory_entries WHERE archived = 0 "
        "AND COALESCE(last_used, created_at) < ? "
        "ORDER BY COALESCE(last_used, created_at) ASC, id ASC LIMIT ?",
        (100, 10),
    )
    assert "idx_memory_entries_aging" in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan


@pytest.mark.parametrize(
    ("order_by", "index_name"),
    [
        ("created_at ASC, id ASC", "idx_memory_entries_target_created"),
        ("usage_count ASC, id ASC", "idx_memory_entries_target_usage"),
    ],
)
def test_active_entries_ordered_uses_covering_index(
    memory_db: sqlite3.Connection, order_by: str, index_name: str
):
    plan = _plan(
        memory_db,
        f"SELECT * FROM memory_entries WHERE target = ? AND archived = 0 "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?",
        ("memory", 50, 0),
    )
    assert index_name in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan


# ── symbols + edge tables ───────────────────────────────────────────────────


@pytest.fixture()
def code_intel_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            file_path TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
            parent_id INTEGER, language TEXT NOT NULL, source_snippet TEXT, doc_string TEXT
        );
        CREATE INDEX idx_symbols_name ON symbols(name);
        CREATE INDEX idx_symbols_name_lower ON symbols(lower(name));
        CREATE TABLE import_edges (
            id INTEGER PRIMARY KEY, importer_id INTEGER NOT NULL, imported_name TEXT NOT NULL,
            imported_id INTEGER, file_path TEXT NOT NULL, line INTEGER NOT NULL,
            resolved INTEGER DEFAULT 0
        );
        CREATE TABLE inherit_edges (
            id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL, parent_name TEXT NOT NULL,
            parent_id INTEGER, inherit_type TEXT NOT NULL, file_path TEXT NOT NULL,
            line INTEGER NOT NULL, resolved INTEGER DEFAULT 0
        );
        CREATE TABLE mixin_edges (
            id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL, mixin_name TEXT NOT NULL,
            mixin_id INTEGER, file_path TEXT NOT NULL, line INTEGER NOT NULL,
            resolved INTEGER DEFAULT 0
        );
        CREATE INDEX idx_imports_resolved ON import_edges(resolved) WHERE resolved = 0;
        CREATE INDEX idx_inherits_resolved ON inherit_edges(resolved) WHERE resolved = 0;
        CREATE INDEX idx_mixins_resolved ON mixin_edges(resolved) WHERE resolved = 0;
    """)
    yield conn
    conn.close()


def test_symbol_lower_name_lookup_uses_expression_index(code_intel_db: sqlite3.Connection):
    plan = _plan(code_intel_db, "SELECT id FROM symbols WHERE lower(name) = ? LIMIT 50", ("foo",))
    assert "idx_symbols_name_lower" in plan, plan
    assert "SCAN symbols" not in plan, plan


@pytest.mark.parametrize(
    "table",
    ["import_edges", "inherit_edges", "mixin_edges"],
)
def test_resolve_pass_uses_partial_index(code_intel_db: sqlite3.Connection, table: str):
    index = f"idx_{'imports' if table == 'import_edges' else 'inherits' if table == 'inherit_edges' else 'mixins'}_resolved"
    assert index in _index_names(code_intel_db, table)
    plan = _plan(code_intel_db, f"SELECT id FROM {table} WHERE resolved = 0")
    assert index in plan, plan
    assert f"SCAN {table}" not in plan, plan


# ── trajectory_events: session+seq ordering ─────────────────────────────────


def test_trajectory_session_seq_order_has_no_sort():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE trajectory_events (
            event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            seq INTEGER NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
            is_error INTEGER DEFAULT 0, summary TEXT, payload_json TEXT
        );
        CREATE INDEX idx_trajectory_session_turn ON trajectory_events(session_id, turn_id, seq);
        CREATE INDEX idx_trajectory_session_seq ON trajectory_events(session_id, seq);
    """)
    plan = _plan(
        conn,
        "SELECT event_id FROM trajectory_events WHERE session_id = ? ORDER BY seq LIMIT ? OFFSET ?",
        ("s", 20, 0),
    )
    assert "idx_trajectory_session_seq" in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan
    conn.close()


# ── meetings list ordering ──────────────────────────────────────────────────


def test_meetings_list_uses_created_at_index():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE meetings (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, topic TEXT NOT NULL,
            host_agent_id TEXT NOT NULL DEFAULT 'main', participants_json TEXT NOT NULL,
            status INTEGER NOT NULL DEFAULT 0,
            flow_stage TEXT NOT NULL DEFAULT 'requirement_confirmation',
            execution_plan_json TEXT, final_summary TEXT,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, ended_at INTEGER
        );
        CREATE INDEX idx_meetings_created_at ON meetings(created_at DESC);
    """)
    plan = _plan(conn, "SELECT * FROM meetings ORDER BY created_at DESC")
    assert "idx_meetings_created_at" in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan
    conn.close()
