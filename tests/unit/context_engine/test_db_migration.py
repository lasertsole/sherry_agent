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

Contract (decisions.md): ``origin`` TEXT NULL — NULL = real user message,
``"subagent_completion"`` = background subagent-completion injection.
Old rows are never backfilled.
"""

from __future__ import annotations

import sqlite3

from typing import Any

import pytest

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
        row
        for row in db.execute("PRAGMA table_info(messages)").fetchall()
        if row[1] == "origin"
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
        "CREATE TABLE IF NOT EXISTS _migrations "
        "(v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"
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
        db.execute(
            "INSERT INTO messages (turn_num, session_id, role, content, timestamp) "
            "VALUES (1, 's_old', 'human', 'legacy message', '20260101000000')"
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
        row = db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_old'"
        ).fetchone()
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
