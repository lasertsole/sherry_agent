"""The four legacy registry DBs merge into one shared registry file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from server.service.registry_migration import migrate_legacy_registry_dbs
from config.path import resolve_registry_db_path

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_OLD_EVIDENCE_SCHEMA = """
CREATE TABLE evidence_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event TEXT,
    file_path TEXT,
    command TEXT,
    status TEXT,
    exit_code INTEGER,
    kind TEXT,
    modified_files_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL
);
CREATE TABLE verification_state (
    session_id TEXT PRIMARY KEY,
    changed_paths_json TEXT NOT NULL DEFAULT '[]',
    last_edit_at TEXT
);
"""


def _legacy_evidence(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_OLD_EVIDENCE_SCHEMA)
        conn.execute(
            "INSERT INTO evidence_events "
            "(timestamp, session_id, event, command, status, payload_json) "
            "VALUES ('2026-01-01', 's1', 'verification', 'pytest -q', 'passed', '{}')"
        )
        conn.execute(
            "INSERT INTO verification_state (session_id, changed_paths_json) "
            "VALUES ('s1', '[\"a.py\"]')"
        )
        conn.commit()
    finally:
        conn.close()


def test_the_stores_share_one_registry_file():
    from agent.tools.pub_base.evidence_ledger import EvidenceLedger
    from agent.tools.subagent.registry import pending_injections, store_sqlite as subagent
    from agent.tools.taskflow.registry import store_sqlite as taskflow
    from agent.tools.todolist.registry import store_sqlite as todolist
    from server.queue import user_input_queue

    expected = resolve_registry_db_path()
    assert Path(subagent._DB_PATH) == expected
    assert Path(taskflow._DB_PATH) == expected
    assert Path(todolist._DB_PATH) == expected
    assert Path(pending_injections._DB_PATH) == expected
    assert Path(user_input_queue._DB_PATH) == expected
    assert Path(EvidenceLedger.LEDGER_PATH) == expected


def test_legacy_tables_are_copied_and_the_old_file_is_backed_up(tmp_path: Path):
    legacy = tmp_path / "evidence-ledger.db"
    _legacy_evidence(legacy)
    registry = tmp_path / "registry.db"

    report = migrate_legacy_registry_dbs(registry_path=registry, legacy_paths=[legacy])

    assert report[str(legacy)]["evidence_events"] == 1
    assert report[str(legacy)]["verification_state"] == 1
    assert not legacy.exists()
    assert (tmp_path / "evidence-ledger.db.bak").exists()

    conn = sqlite3.connect(registry)
    try:
        row = conn.execute("SELECT session_id, command, flow_id FROM evidence_events").fetchone()
        state = conn.execute(
            "SELECT session_id, changed_paths_json FROM verification_state"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("s1", "pytest -q", None), "missing flow_id in the old schema is NULLed"
    assert state == ("s1", '["a.py"]')


def test_a_second_run_is_a_no_op(tmp_path: Path):
    legacy = tmp_path / "evidence-ledger.db"
    _legacy_evidence(legacy)
    registry = tmp_path / "registry.db"
    migrate_legacy_registry_dbs(registry_path=registry, legacy_paths=[legacy])

    assert migrate_legacy_registry_dbs(registry_path=registry, legacy_paths=[legacy]) == {}


def test_a_failing_legacy_copy_leaves_the_file_in_place(tmp_path: Path):
    legacy = tmp_path / "broken.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE evidence_events (event_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    registry = tmp_path / "registry.db"

    report = migrate_legacy_registry_dbs(registry_path=registry, legacy_paths=[legacy])

    # The table has no usable columns in common -> the copy is skipped, but the
    # file still must not be renamed as if it succeeded.
    assert legacy.exists()
    assert str(legacy) not in report or report[str(legacy)] == {}


def test_every_store_can_initialize_the_shared_file(tmp_path: Path, monkeypatch):
    from agent.tools.subagent.registry import store_sqlite as subagent
    from agent.tools.todolist.registry import store_sqlite as todolist
    from agent.tools.taskflow.registry import store_sqlite as taskflow

    registry = tmp_path / "registry.db"

    def point(module):
        monkeypatch.setattr(module, "_DB_DIR", registry.parent)
        monkeypatch.setattr(module, "_DB_PATH", registry)
        monkeypatch.setattr(module, "_sync_tables_ready", False)
        module._ensure_tables_sync()

    point(subagent)
    point(taskflow)
    point(todolist)

    conn = sqlite3.connect(registry)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"subagent_runs", "task_flows", "todos"} <= tables
