"""Unit tests for the name-keyed schema migrations (db.py).

Each migration step carries a stable name recorded in the
``_migrations(name, at)`` tracking table, and ``_migrate`` runs every step
whose name is not yet applied — in list order, but independent of a database's
position in that list. The central regression: a step inserted in the MIDDLE
of the list still runs on a database that has already applied the steps around
it, whereas the former index-keyed resume (``MAX(v)`` over a positional list)
skipped it permanently — how the ``reasoning_tokens`` column was lost in
production.

Covers:
- Fresh database → ``_migrations`` records exactly ``0001_initial_schema`` and
  the full schema is present: the complete ``messages`` column set (aligned
  with the INSERT column list in ``context_engine/store/core.py::add_messages``),
  every named index, both FTS5 tables with their six sync triggers, and the six
  auxiliary tables.
- Idempotency: a second ``_migrate`` neither raises nor changes anything and
  does not re-execute an already-applied step.
- Middle-insert regression: with ``[0001, 0002, 0003]`` after a database has
  applied ``[0001, 0003]``, ``0002`` runs and ``0003`` does not re-run.
- Legacy ``v`` tracking table: replaced by the name shape without pre-seeding
  any name, so the idempotent baseline re-runs and records itself while schema
  and rows survive; the stale watermark cannot swallow new names.
- Write smoke: the real ``add_messages`` persists a turn against a fresh
  baseline database (reasoning tokens, context eligibility, message tree).
- Legacy self-heal: a database with a watermark-9 table and a base-only
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

BASELINE_NAME = "0001_initial_schema"


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


def _applied(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM _migrations")}


def _migration_table_shape(db: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(column, declared type, pk flag) of the tracking table, in declared order."""
    return [(row[1], row[2], row[5]) for row in db.execute("PRAGMA table_info(_migrations)")]


class TestFreshBaseline:
    """A brand-new database gets the complete schema from the single baseline."""

    def test_records_single_baseline_name(self):
        db = _connect()
        _migrate(db)

        assert _applied(db) == {BASELINE_NAME}
        assert _migration_table_shape(db) == [("name", "TEXT", 1), ("at", "INTEGER", 0)]

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
            _applied(db),
            _migration_table_shape(db),
        )

        _migrate(db)  # second pass is name-gated; must not raise or alter

        assert (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
            _applied(db),
            _migration_table_shape(db),
        ) == snapshot

    def test_applied_step_is_not_executed_again(self, monkeypatch):
        db = _connect()
        _migrate(db)  # records BASELINE_NAME

        replayed: list[str] = []

        def counting_baseline(_db: sqlite3.Connection) -> None:
            replayed.append(BASELINE_NAME)

        monkeypatch.setattr(
            db_module,
            "_migration_steps",
            lambda: [(BASELINE_NAME, counting_baseline)],
        )

        _migrate(db)

        assert replayed == []
        assert _applied(db) == {BASELINE_NAME}


class TestMiddleInsertRegression:
    """A step inserted before a passed position must still run (name-keyed)."""

    def test_steps_run_in_list_order(self, monkeypatch):
        order: list[str] = []

        def step(name: str):
            def _run(_db: sqlite3.Connection) -> None:
                order.append(name)

            return _run

        monkeypatch.setattr(
            db_module,
            "_migration_steps",
            lambda: [("a", step("a")), ("b", step("b")), ("c", step("c"))],
        )

        db = _connect()
        _migrate(db)

        assert order == ["a", "b", "c"]
        assert _applied(db) == {"a", "b", "c"}

    def test_middle_insert_runs_and_applied_tail_does_not(self, monkeypatch):
        """A database at ``[0001, 0003]`` must still execute ``0002``.

        The former index-keyed resume read ``MAX(v) = 2`` and iterated
        ``range(2, 3)`` — empty, so 0002 was skipped forever.
        """
        db = _connect()

        after_runs: list[str] = []

        def after_step(_db: sqlite3.Connection) -> None:
            after_runs.append("0003_after")

        monkeypatch.setattr(
            db_module,
            "_migration_steps",
            lambda: [(BASELINE_NAME, build_schema_v1), ("0003_after", after_step)],
        )
        _migrate(db)
        assert _applied(db) == {BASELINE_NAME, "0003_after"}
        assert after_runs == ["0003_after"]

        middle_runs: list[str] = []

        def middle_step(_db: sqlite3.Connection) -> None:
            middle_runs.append("0002_middle")

        monkeypatch.setattr(
            db_module,
            "_migration_steps",
            lambda: [
                (BASELINE_NAME, build_schema_v1),
                ("0002_middle", middle_step),
                ("0003_after", after_step),
            ],
        )
        _migrate(db)  # 0001 and 0003 are applied; only 0002 must run

        assert middle_runs == ["0002_middle"]
        assert after_runs == ["0003_after"]
        assert _applied(db) == {BASELINE_NAME, "0002_middle", "0003_after"}

        _migrate(db)  # all names applied: nothing runs again

        assert middle_runs == ["0002_middle"]
        assert after_runs == ["0003_after"]


class TestLegacyIndexedTrackingTable:
    """A former (v) tracking table is replaced: schema/rows survive, no
    applied name is pre-seeded, and the idempotent baseline re-records itself."""

    _LEGACY_TRACKING_DDL = "CREATE TABLE _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)"

    def _legacy_v_database(self, tmp_path) -> sqlite3.Connection:
        db = _connect(tmp_path / "mes_memory_legacy_v.db")
        build_schema_v1(db)  # the former chain already carried the full schema
        db.execute(
            "INSERT INTO messages (turn_num, session_id, role, content, timestamp, ts_ms) "
            "VALUES (1, 'legacy-session', 'human', 'ping', '20260101000000', 42)"
        )
        db.execute(self._LEGACY_TRACKING_DDL)
        db.execute("INSERT INTO _migrations (v, at) VALUES (18, 0)")
        return db

    def test_v_table_replaced_and_baseline_recorded_without_schema_drift(self, tmp_path):
        db = self._legacy_v_database(tmp_path)
        before = (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
        )

        _migrate(db)

        assert _migration_table_shape(db) == [("name", "TEXT", 1), ("at", "INTEGER", 0)]
        assert _applied(db) == {BASELINE_NAME}
        assert (
            _table_names(db),
            _index_names(db),
            _trigger_names(db),
            _message_columns(db),
        ) == before
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1

    def test_legacy_watermark_does_not_swallow_a_new_name(self, monkeypatch, tmp_path):
        """No name is pre-seeded, so a step added after the upgrade runs."""
        db = self._legacy_v_database(tmp_path)

        _migrate(db)
        assert _applied(db) == {BASELINE_NAME}

        ran: list[str] = []

        def next_step(_db: sqlite3.Connection) -> None:
            ran.append("0002_next")

        monkeypatch.setattr(
            db_module,
            "_migration_steps",
            lambda: [(BASELINE_NAME, build_schema_v1), ("0002_next", next_step)],
        )

        _migrate(db)

        assert ran == ["0002_next"]
        assert _applied(db) == {BASELINE_NAME, "0002_next"}


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

    A former-chain DB records a positional watermark whose baseline must be
    replayed once the (v) table is replaced; the heal therefore runs first, so
    the baseline's index DDL finds every column it references.
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
            legacy.execute("CREATE TABLE _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)")
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
            # The legacy tracking table was replaced and the baseline recorded.
            assert _migration_table_shape(db) == [("name", "TEXT", 1), ("at", "INTEGER", 0)]
            assert _applied(db) == {BASELINE_NAME}
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
            assert _applied(db) == {BASELINE_NAME}
        finally:
            db.close()
