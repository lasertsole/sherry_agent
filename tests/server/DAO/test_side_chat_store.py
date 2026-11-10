"""The side-chat link store's SQLite lifecycle (shared base + WAL + upgrades).

Contract under test:

* the ``CREATE TABLE`` / index DDL runs ONCE per process, not on every
  short-lived connection (the pre-base store re-ran it on each connect);
* the file is switched to WAL (the mode is persistent, so later connections
  inherit it) and EVERY connection still sets ``PRAGMA busy_timeout``;
* an old database file keeps its rows, gains the post-release columns once, and
  gets both indexes — statements that name the new columns stop failing;
* concurrent writers all land (no lock storm from per-connection DDL).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from server.DAO import side_chat_store

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test DB file + a fresh once-per-process schema state."""
    db_path = tmp_path / "side_chats.db"
    monkeypatch.setattr(side_chat_store, "_DB_DIR", tmp_path)
    monkeypatch.setattr(side_chat_store, "_DB_PATH", db_path)
    monkeypatch.setattr(side_chat_store, "_sync_tables_ready", False)
    monkeypatch.setattr(side_chat_store, "_schema_ready", False)
    return db_path


def _query(db_path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_the_schema_is_initialized_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1")

    # Poison the DDL: if a later call re-ran the schema init, SQLite would raise
    # a syntax error. A once-per-process init never reads it again.
    monkeypatch.setattr(side_chat_store, "_DDL", "THIS IS NOT VALID SQL")

    assert side_chat_store.get_side_chat("child-1") is not None
    side_chat_store.create_side_chat("child-2", "parent-1")
    assert side_chat_store.list_children("parent-1") == ["child-1", "child-2"]


def test_the_database_is_switched_to_wal(_isolated_store: Path) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1")

    row = _query(_isolated_store, "PRAGMA journal_mode")[0]
    assert str(row[0]).lower() == "wal"


def test_every_connection_sets_the_busy_timeout() -> None:
    side_chat_store.get_side_chat("missing")  # runs the one-time schema init

    conn = side_chat_store._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    finally:
        conn.close()


def test_an_old_file_keeps_its_rows_and_gains_the_columns_and_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy_side_chats.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE side_chats ("
        "child_session_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL, "
        "context_text TEXT NOT NULL DEFAULT '', fork_mode INTEGER NOT NULL DEFAULT 0, "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO side_chats (child_session_id, parent_session_id, created_at) "
        "VALUES ('old-child', 'parent-1', 1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(side_chat_store, "_DB_PATH", legacy)
    # The fixture armed the flags for its own file; a relocated path re-arms
    # them (a real process never moves the database).
    monkeypatch.setattr(side_chat_store, "_sync_tables_ready", False)
    monkeypatch.setattr(side_chat_store, "_schema_ready", False)

    row = side_chat_store.get_side_chat("old-child")

    assert row is not None
    assert row["fork_turn_num"] is None  # old rows read as the legacy default
    columns = {str(entry[1]) for entry in _query(legacy, "PRAGMA table_info(side_chats)")}
    assert {"client_request_id", "fork_turn_num"} <= columns
    indexes = {str(entry[1]) for entry in _query(legacy, "PRAGMA index_list(side_chats)")}
    assert {"idx_side_chats_parent", "idx_side_chats_request"} <= indexes

    # The idempotency lookup and the fork point work on the upgraded file.
    side_chat_store.create_side_chat(
        "new-child", "parent-1", fork_mode=True, fork_turn_num=2, client_request_id="req-1"
    )
    found = side_chat_store.find_by_request("parent-1", "req-1")
    assert found is not None and found["child_session_id"] == "new-child"


def test_a_legacy_file_without_the_idempotency_column_still_initializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idempotency index failure is tolerated, not fatal (defensive path)."""
    legacy = tmp_path / "very_old.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE side_chats ("
        "child_session_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL, "
        "context_text TEXT NOT NULL DEFAULT '', fork_mode INTEGER NOT NULL DEFAULT 0, "
        "created_at INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(side_chat_store, "_DB_PATH", legacy)
    monkeypatch.setattr(side_chat_store, "_sync_tables_ready", False)
    monkeypatch.setattr(side_chat_store, "_schema_ready", False)
    # Simulate the ALTER being impossible (e.g. a read-only file): the index
    # DDL then fails with "no such column" and init must keep going.
    monkeypatch.setattr(side_chat_store, "_add_missing_columns", lambda _conn: None)

    side_chat_store._ensure_schema_sync()  # must not raise

    assert side_chat_store._schema_ready is True


def test_concurrent_writers_all_land() -> None:
    errors: list[BaseException] = []

    def _write(index: int) -> None:
        try:
            side_chat_store.create_side_chat(f"child-{index}", f"parent-{index % 4}")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(side_chat_store.side_chat_ids()) == 16
