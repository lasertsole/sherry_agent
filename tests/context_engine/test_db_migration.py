"""Unit tests for the single-baseline schema migration (db.py).

The former incremental chain (v1..v18) was collapsed into one complete
baseline: ``build_schema_v1`` creates the whole current schema in one shot,
a fresh database records exactly v1, and a stale high watermark left by the
former chain (e.g. the production v18 file) is normalized down to v1 without
replaying the baseline or touching existing schema/data. Normalization
matters: ``_migrate`` resumes at ``MAX(_migrations)``, so a watermark above
the step count would make every future append a silent no-op.

Covers:
- Fresh database → ``_migrations`` holds exactly v1 and the full schema is
  present: the complete ``messages`` column set (aligned with the INSERT
  column list in ``context_engine/store/core.py::add_messages``), every named
  index, both FTS5 tables with their six sync triggers, and the six auxiliary
  tables.
- Idempotency: running ``_migrate`` twice on the same connection neither
  raises nor changes the schema.
- Stale watermark: a database recorded at v18 is reset to exactly v1 — the
  baseline is not replayed and legacy schema/data survive untouched.
- Append effectiveness (regression): a monkeypatched extra step (a simulated
  future v2) runs on both a fresh database and a normalized former-v18
  database, so a stale high watermark can no longer swallow appended steps.
- Write smoke: the real ``add_messages`` persists a turn against a fresh
  baseline database (reasoning tokens, context eligibility, message tree).
- Legacy self-heal (regression): a database with watermark 9 and a base-only
  ``messages`` table is healed on the real connect path — the newer columns,
  every named index and all auxiliary tables appear while the existing row
  survives unchanged.
"""

from __future__ import annotations

import asyncio
import sqlite3

from pathlib import Path

import pytest

from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import core as store_core
from context_engine.store import db as db_module
from context_engine.store.db import _migrate, build_schema_v1

pytestmark = pytest.mark.unit

# The complete ``messages`` column set: the base schema plus every additive
# column, aligned with the INSERT column list in
# context_engine/store/core.py::add_messages.
EXPECTED_MESSAGE_COLUMNS = frozenset(
    {
        "id",
        "turn_num",
        "session_id",
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "tool_status",
        "tool_name",
        "timestamp",
        "ts_ms",
        "finish_reason",
        "reasoning",
        "reasoning_content",
        "images",
        "audios",
        "videos",
        "model_name",
        "input_tokens",
        "output_tokens",
        "origin",
        "reasoning_tokens",
        "idempotency_key",
        "context_eligible",
        "parent_message_id",
        "compacted",
        "compaction_checkpoint_id",
    }
)

EXPECTED_INDEXES = frozenset(
    {
        "idx_messages_timestamp",
        "idx_messages_turn_num",
        "idx_messages_session_role",
        "idx_messages_parent",
        "idx_messages_idempotency",
        "idx_embeddings_session",
        "idx_events_session",
        "idx_compaction_session",
    }
)

EXPECTED_FTS_TABLES = frozenset({"messages_fts", "messages_fts_trigram"})

EXPECTED_TRIGGERS = frozenset(
    {
        "messages_fts_insert",
        "messages_fts_delete",
        "messages_fts_update",
        "messages_fts_trigram_insert",
        "messages_fts_trigram_delete",
        "messages_fts_trigram_update",
    }
)

# "The other six tables" beyond ``messages`` and the two FTS tables.
EXPECTED_AUX_TABLES = frozenset(
    {
        "message_embeddings",
        "compression_locks",
        "events",
        "context_epoch",
        "session_leafs",
        "compaction_checkpoints",
    }
)


def _connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """A throwaway connection mirroring get_db()'s key settings."""
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(db: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _trigger_names(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}


def _message_columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(messages)")}


def _versions(db: sqlite3.Connection) -> list[int]:
    return [row[0] for row in db.execute("SELECT v FROM _migrations ORDER BY v")]


class TestFreshBaseline:
    """A brand-new database gets the complete schema from the single v1 step."""

    def test_records_single_baseline_version(self):
        db = _connect()
        _migrate(db)

        assert _versions(db) == [1]

    def test_has_complete_message_columns(self):
        db = _connect()
        _migrate(db)

        assert _message_columns(db) == set(EXPECTED_MESSAGE_COLUMNS)

    def test_has_all_named_indexes(self):
        db = _connect()
        _migrate(db)

        assert _index_names(db) == set(EXPECTED_INDEXES)

    def test_has_fts_tables_and_six_triggers(self):
        db = _connect()
        _migrate(db)

        assert EXPECTED_FTS_TABLES <= _table_names(db)
        assert _trigger_names(db) == set(EXPECTED_TRIGGERS)

    def test_has_auxiliary_tables(self):
        db = _connect()
        _migrate(db)

        assert EXPECTED_AUX_TABLES <= _table_names(db)

    def test_build_schema_v1_directly_twice_is_idempotent(self):
        """Every statement is IF NOT EXISTS; a second call is a silent no-op."""
        db = _connect()
        build_schema_v1(db)
        build_schema_v1(db)  # must not raise, must not duplicate anything

        assert _message_columns(db) == set(EXPECTED_MESSAGE_COLUMNS)
        assert _index_names(db) == set(EXPECTED_INDEXES)
        assert _trigger_names(db) == set(EXPECTED_TRIGGERS)


class TestIdempotency:
    def test_migrate_twice_changes_nothing(self):
        db = _connect()
        _migrate(db)
        snapshot = (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
            _versions(db),
        )

        _migrate(db)  # second pass is version-gated; must not raise or alter

        assert (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
            _versions(db),
        ) == snapshot


class TestStaleWatermarkNormalization:
    """A watermark past the known steps is normalized down to the baseline."""

    def test_database_past_baseline_is_normalized_without_replay(self):
        """A v18 database is reset to v1 without replaying any step.

        The high watermark is bookkeeping only — the former chain already
        created the schema — so normalization must not re-run the baseline.
        """
        db = _connect()
        db.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO _migrations (v, at) VALUES (?, ?)", (18, 0))
        db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, legacy TEXT)")

        _migrate(db)

        assert _versions(db) == [1]  # == len(steps): the single v1 baseline
        # The baseline never ran: no baseline table, no column backfill.
        assert "events" not in _table_names(db)
        assert _message_columns(db) == {"id", "legacy"}

    def test_realistic_former_chain_database_keeps_schema_and_data(self):
        """The production upgrade path: a full-schema v18 database keeps its
        schema and rows; only the watermark is rewritten to v1."""
        db = _connect()
        _migrate(db)  # full baseline schema, recorded as v1
        db.execute(
            "INSERT INTO messages (turn_num, session_id, role, timestamp, ts_ms) "
            "VALUES (1, 'legacy-session', 'human', '20260101000000', 1)"
        )
        db.execute("DELETE FROM _migrations")
        db.execute("INSERT INTO _migrations (v, at) VALUES (?, ?)", (18, 0))
        schema_before = (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
        )

        _migrate(db)

        assert _versions(db) == [1]
        assert (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
        ) == schema_before
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


class TestFutureAppendEffectiveness:
    """Regression: an appended step must run on fresh and stale-watermark DBs."""

    def test_appended_step_runs_on_fresh_database(self, monkeypatch):
        ran: list[int] = []

        def fake_schema_v2(_db: sqlite3.Connection) -> None:
            ran.append(2)

        monkeypatch.setattr(
            db_module, "_migration_steps", lambda: [build_schema_v1, fake_schema_v2]
        )

        db = _connect()
        _migrate(db)

        assert _versions(db) == [1, 2]
        assert ran == [2]

        _migrate(db)  # now at v2: the appended step must not re-run
        assert _versions(db) == [1, 2]
        assert ran == [2]

    def test_appended_step_runs_on_normalized_former_high_watermark_database(self, monkeypatch):
        """A stale v18 watermark used to swallow every future append.

        ``range(18, len(steps))`` is empty, so the simulated v2 stays dormant
        until the watermark is normalized — and must then run.
        """
        db = _connect()
        _migrate(db)  # realistic former-chain database: full schema
        db.execute("DELETE FROM _migrations")
        db.execute("INSERT INTO _migrations (v, at) VALUES (?, ?)", (18, 0))

        _migrate(db)  # normalization pass against the single-step baseline
        assert _versions(db) == [1]

        ran: list[int] = []

        def fake_schema_v2(_db: sqlite3.Connection) -> None:
            ran.append(2)

        monkeypatch.setattr(
            db_module, "_migration_steps", lambda: [build_schema_v1, fake_schema_v2]
        )

        _migrate(db)

        assert _versions(db) == [1, 2]
        assert ran == [2]


class TestAddMessagesSmoke:
    def test_real_add_messages_writes_on_baseline_schema(self, monkeypatch, tmp_path):
        """The real write path persists a turn against a fresh baseline DB."""
        db = _connect(tmp_path / "mes_memory_smoke.db")
        _migrate(db)
        monkeypatch.setattr(store_core, "_db", db)
        try:
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
                    "sess-baseline-smoke",
                    [HumanMessage(content="ping"), ai],
                )
            )

            rows = db.execute(
                "SELECT role, reasoning_tokens, context_eligible, parent_message_id "
                "FROM messages WHERE session_id = ? ORDER BY id",
                ("sess-baseline-smoke",),
            ).fetchall()
            assert [row["role"] for row in rows] == ["human", "ai"]
            assert [row["reasoning_tokens"] for row in rows] == [None, 7]
            assert [row["context_eligible"] for row in rows] == [1, 1]
            # Message tree: the human row is a root, the ai row chains to it.
            assert rows[0]["parent_message_id"] is None
            assert rows[1]["parent_message_id"] is not None
        finally:
            db.close()


class TestLegacySchemaSelfHeal:
    """Regression: a legacy database is healed unconditionally on connect.

    A legacy DB (former incremental chain) records a watermark above the step
    count, so ``_migrate`` normalizes it without replaying the baseline; the
    real connect path must therefore repair the missing schema itself.
    """

    # The base ``messages`` columns a former-chain database carries before the
    # newer additive columns existed (matches the live defect report).
    _LEGACY_MESSAGE_DDL = """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_num INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_status TEXT,
            tool_name TEXT,
            timestamp TEXT NOT NULL,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            images TEXT,
            audios TEXT,
            videos TEXT,
            model_name TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            origin TEXT,
            ts_ms INTEGER NOT NULL
        )
    """

    _HEALED_COLUMNS = frozenset(
        {
            "reasoning_tokens",
            "idempotency_key",
            "context_eligible",
            "parent_message_id",
            "compacted",
            "compaction_checkpoint_id",
        }
    )

    def test_connect_heals_watermark_9_legacy_database(self, monkeypatch, tmp_path):
        """``_connect_with_retry()`` heals a watermark-9 DB missing the newer
        columns; the pre-existing row survives untouched."""
        path = tmp_path / "legacy_mes_memory.db"
        legacy = sqlite3.connect(path)
        try:
            legacy.execute(
                "CREATE TABLE _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"
            )
            legacy.execute("INSERT INTO _migrations (v, at) VALUES (9, 0)")
            legacy.execute(self._LEGACY_MESSAGE_DDL)
            legacy.execute(
                "INSERT INTO messages (turn_num, session_id, role, content, timestamp, ts_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (1, "legacy-session", "human", "ping", "20260101000000", 42),
            )
            legacy.commit()
        finally:
            legacy.close()

        monkeypatch.setattr(db_module, "_db_path", path)

        db = db_module._connect_with_retry()
        try:
            # The newer columns were added; the column set is now complete.
            assert self._HEALED_COLUMNS <= _message_columns(db)
            assert _message_columns(db) == set(EXPECTED_MESSAGE_COLUMNS)
            # The missing indexes and auxiliary tables were re-created.
            assert EXPECTED_INDEXES <= _index_names(db)
            assert EXPECTED_AUX_TABLES <= _table_names(db)
            # The baseline was not replayed: watermark normalized to v1 only.
            assert _versions(db) == [1]
            # The pre-existing row survived unchanged, new columns defaulted.
            row = db.execute(
                "SELECT turn_num, session_id, role, content, timestamp, ts_ms, "
                "context_eligible, compacted FROM messages ORDER BY id"
            ).fetchone()
            assert row is not None
            assert (
                row["turn_num"],
                row["session_id"],
                row["role"],
                row["content"],
                row["timestamp"],
                row["ts_ms"],
            ) == (1, "legacy-session", "human", "ping", "20260101000000", 42)
            assert row["context_eligible"] == 1
            assert row["compacted"] == 0
        finally:
            db.close()

        # A second real connect is a clean, idempotent no-op.
        db = db_module._connect_with_retry()
        try:
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            assert _message_columns(db) == set(EXPECTED_MESSAGE_COLUMNS)
            assert _versions(db) == [1]
        finally:
            db.close()
