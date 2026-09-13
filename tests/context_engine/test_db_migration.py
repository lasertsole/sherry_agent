"""Unit tests for the ``messages.origin`` schema migration (db.py).

Plan: subagent-origin-tagging, Task 1 — TDD RED->GREEN.

Covers:
- Old-schema upgrade: a v6 database (every pre-origin migration applied,
  no ``origin`` column) gains a nullable TEXT ``origin`` column when the
  ``_migrate`` path runs; pre-existing rows keep ``origin IS NULL``.
- Fresh database: a brand-new DB migrated from scratch has the column.
- Idempotency: running the full migration path twice on the same
  connection never raises nor duplicates the column, and calling
  ``add_origin_column`` directly twice hits the swallowed
  ``sqlite3.OperationalError`` branch without raising.
- ``idx_messages_session_role``: an old (v7) database gains the
  ``(session_id, role)`` index on upgrade, a fresh DB is created with it,
  and re-running the migration path never raises on the ``IF NOT EXISTS``
  step.

Contract (decisions.md): ``origin`` TEXT NULL — NULL = real user message,
``"subagent_completion"`` = background subagent-completion injection.
Old rows are never backfilled.
"""

from __future__ import annotations

import asyncio
import sqlite3

from typing import Any

import pytest

from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import db as store_db
from context_engine.store.db import (
    _migrate,
    add_audio_video_columns,
    add_images_column,
    add_model_token_columns,
    add_origin_column,
    build_messages_tb,
)

pytestmark = pytest.mark.unit


def _connect() -> sqlite3.Connection:
    """A throwaway connection mirroring get_db()'s key settings."""
    return sqlite3.connect(":memory:", isolation_level=None)


def _origin_columns(db: sqlite3.Connection) -> list[tuple[int, str, str, int, Any, int]]:
    """PRAGMA table_info rows for the ``origin`` column of ``messages``."""
    return [
        row for row in db.execute("PRAGMA table_info(messages)").fetchall() if row[1] == "origin"
    ]


def _build_pre_origin_schema(db: sqlite3.Connection) -> None:
    """Recreate the v6 (pre-origin) schema: every migration before origin.

    Builds the messages table via the real migration functions (so the
    fixture is exactly the shape a v6 production DB has), then seeds the
    ``_migrations`` bookkeeping table at v6.
    """
    build_messages_tb(db)
    add_images_column(db)
    add_audio_video_columns(db)
    add_model_token_columns(db)
    db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"
    )
    db.execute("INSERT INTO _migrations (v, at) VALUES (?, ?)", (6, 0))
    db.commit()


class TestOriginMigration:
    def test_old_schema_db_gains_origin_column(self):
        """Upgrading an old DB (v6, no origin column) adds origin TEXT NULL."""
        db = _connect()
        _build_pre_origin_schema(db)

        # Sanity: the fixture really is pre-origin.
        assert _origin_columns(db) == []

        # A legacy row written before the migration must survive it.
        # ts_ms is part of the base schema (NOT NULL) — provide it explicitly.
        db.execute(
            "INSERT INTO messages (turn_num, session_id, role, content, timestamp, ts_ms) "
            "VALUES (1, 's_old', 'human', 'legacy message', '20260101000000', 1767225600000)"
        )

        _migrate(db)

        cols = _origin_columns(db)
        assert len(cols) == 1
        # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
        _, name, col_type, notnull, _dflt, _pk = cols[0]
        assert name == "origin"
        assert col_type.upper() == "TEXT"
        assert notnull == 0  # nullable

        # The migration recorded the new step (steps list IS the version).
        assert db.execute("SELECT MAX(v) FROM _migrations").fetchone()[0] > 6

        # Backward compatible: pre-existing rows stay NULL (= real user msg).
        row = db.execute("SELECT origin FROM messages WHERE session_id = 's_old'").fetchone()
        assert row is not None
        assert row[0] is None

    def test_fresh_db_has_origin_column(self):
        """A brand-new DB migrated from scratch has the origin column."""
        db = _connect()
        _migrate(db)

        cols = _origin_columns(db)
        assert len(cols) == 1
        _, name, col_type, notnull, _dflt, _pk = cols[0]
        assert name == "origin"
        assert col_type.upper() == "TEXT"
        assert notnull == 0  # nullable

    def test_migrate_twice_is_idempotent(self):
        """The full migration path twice on one connection: no raise, no dup."""
        db = _connect()
        _migrate(db)
        _migrate(db)  # second pass is version-gated; must not raise

        assert len(_origin_columns(db)) == 1

    def test_add_origin_column_twice_swallows_operational_error(self):
        """Direct double-call hits the swallowed OperationalError branch."""
        db = _connect()
        build_messages_tb(db)

        add_origin_column(db)
        add_origin_column(db)  # duplicate ALTER -> sqlite3.OperationalError -> pass

        assert len(_origin_columns(db)) == 1


def _index_names(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA index_list(messages)").fetchall()}


def _build_pre_index_schema(db: sqlite3.Connection) -> None:
    """Recreate the v7 (pre-index) schema: every migration before the role index."""
    build_messages_tb(db)
    add_images_column(db)
    add_audio_video_columns(db)
    add_model_token_columns(db)
    add_origin_column(db)
    db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"
    )
    db.execute("INSERT INTO _migrations (v, at) VALUES (?, ?)", (7, 0))
    db.commit()


class TestSessionRoleIndexMigration:
    def test_old_schema_db_gains_session_role_index(self):
        """Upgrading an old DB (v7, no role index) creates idx_messages_session_role."""
        db = _connect()
        _build_pre_index_schema(db)
        assert "idx_messages_session_role" not in _index_names(db)

        _migrate(db)

        assert "idx_messages_session_role" in _index_names(db)
        assert db.execute("SELECT MAX(v) FROM _migrations").fetchone()[0] > 7

    def test_fresh_db_has_session_role_index(self):
        """A brand-new DB migrated from scratch has idx_messages_session_role."""
        db = _connect()
        _migrate(db)

        assert "idx_messages_session_role" in _index_names(db)

    def test_migrate_twice_keeps_session_role_index(self):
        """The IF NOT EXISTS step is idempotent across a double migration."""
        db = _connect()
        _migrate(db)
        _migrate(db)

        assert "idx_messages_session_role" in _index_names(db)


def _message_columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()}


def _build_pre_guard_schema(db: sqlite3.Connection) -> None:
    """Recreate the production defect state: watermark 17, guard not yet applied.

    Every migration step before ``ensure_message_columns`` has run — the
    watermark is past the point where ``add_reasoning_tokens_column`` entered
    the list — yet the column itself is missing, exactly like the real
    ``src/store/mes_memory/mes_memory.db``.
    """
    store_db.build_messages_tb(db)
    store_db.build_messages_fts_tb(db)
    store_db.build_messages_fts_trigram_tb(db)
    store_db.add_images_column(db)
    store_db.add_audio_video_columns(db)
    store_db.add_model_token_columns(db)
    store_db.add_origin_column(db)
    store_db.add_session_role_index(db)
    store_db.add_reasoning_tokens_column(db)
    store_db.build_compression_locks_tb(db)
    store_db.add_idempotency_key_column(db)
    store_db.add_context_eligible_column(db)
    store_db.build_message_embeddings_tb(db)
    store_db.build_message_tree_tb(db)
    store_db.build_compaction_checkpoints_tb(db)
    store_db.build_events_tb(db)
    store_db.build_context_epoch_tb(db)
    db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"
    )
    db.execute("INSERT INTO _migrations (v, at) VALUES (?, ?)", (17, 0))
    db.commit()


class TestWatermarkPassedColumnBackfill:
    def test_missing_reasoning_tokens_is_backfilled_and_writable(self, monkeypatch, tmp_path):
        """A watermark past the step index still gets the missing column back."""
        db = sqlite3.connect(
            tmp_path / "mes_memory_defect.db",
            check_same_thread=False,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        _build_pre_guard_schema(db)

        # Simulate the defect: watermark 17 (past the reasoning_tokens step),
        # column absent because the step was inserted after that index.
        db.execute("ALTER TABLE messages DROP COLUMN reasoning_tokens")
        assert "reasoning_tokens" not in _message_columns(db)
        assert db.execute("SELECT MAX(v) FROM _migrations").fetchone()[0] == 17

        # The real store path must run against the temp DB (test_store_origin pattern).
        from context_engine.store import core as store_core

        monkeypatch.setattr(store_core, "_db", db)

        _migrate(db)

        assert "reasoning_tokens" in _message_columns(db)
        assert db.execute("SELECT MAX(v) FROM _migrations").fetchone()[0] > 17

        # The production failure path: add_messages must now persist a turn.
        ai = AIMessage(
            content="pong",
            usage_metadata={
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "output_token_details": {"reasoning_tokens": 7},
            },
        )
        asyncio.run(
            store_core.add_messages(
                "sess-migration-backfill",
                [HumanMessage(content="ping"), ai],
            )
        )
        rows = db.execute(
            "SELECT role, reasoning_tokens FROM messages WHERE session_id = ? ORDER BY id",
            ("sess-migration-backfill",),
        ).fetchall()
        assert [row["role"] for row in rows] == ["human", "ai"]
        assert [row["reasoning_tokens"] for row in rows] == [None, 7]
        db.close()

    def test_ensure_message_columns_backfills_every_expected_column(self):
        """A bare base table gains the whole expected set; a second pass is a no-op."""
        db = _connect()
        build_messages_tb(db)

        store_db.ensure_message_columns(db)
        store_db.ensure_message_columns(db)

        expected = {name for name, _decl in store_db._EXPECTED_MESSAGE_COLUMNS}
        assert expected <= _message_columns(db)

    def test_add_reasoning_tokens_column_is_idempotent(self):
        """The PRAGMA guard makes a direct double-call a silent no-op."""
        db = _connect()
        build_messages_tb(db)

        store_db.add_reasoning_tokens_column(db)
        store_db.add_reasoning_tokens_column(db)

        assert "reasoning_tokens" in _message_columns(db)

    def test_add_reasoning_tokens_column_propagates_real_errors(self):
        """A non-duplicate OperationalError must propagate, not be swallowed."""
        db = _connect()

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            store_db.add_reasoning_tokens_column(db)
