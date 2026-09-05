"""TDD tests for audit issue #21 — 1-second timestamp collisions.

Audit finding: the turn timestamp (``YYYYMMDDHHmmss``) has 1-second
resolution, so turns in the same second share a timestamp and
``get_session_ids`` (``MAX(timestamp) ... ORDER BY last_time DESC``) cannot
order sessions reliably.

Frontend impact analysis (pinned the design): the client parses ``createTime``
with a STRICT 14-digit format (``sessionFilter.parseSessionCreateTime``) and
``formatCompactTimeString`` rejects anything with ``length !== 14`` — so the
API surface (``last_time``, history row ``timestamp``) must stay 14 chars.
The fix therefore ADDS a ``ts_ms`` INTEGER column (epoch ms, strictly
increasing per process) via the store's versioned migration and orders
``get_session_ids`` by ``MAX(ts_ms)``; the visible formats never change.

Migration steps list is in ``context_engine/store/db.py``.
"""

import asyncio
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import core as store_core
from context_engine.store.db import _migrate

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

T0_S = 1_700_000_000  # fixed epoch seconds
# Local-timezone stamp of T0_S — strptime().timestamp() round-trips in the
# same tz, so backfill expectations stay self-consistent.
T0_STAMP = datetime.fromtimestamp(T0_S).strftime("%Y%m%d%H%M%S")


@pytest.fixture()
def migrated_db():
    """Fully-migrated store on a temp file (production schema shape)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = sqlite3.connect(str(Path(tmpdir) / "store.db"))
        db.row_factory = sqlite3.Row
        _migrate(db)
        yield db
        db.close()


def _insert_legacy_row(db: sqlite3.Connection, *, session_id: str, stamp: str, ts_ms: int | None = None):
    """Insert one row the legacy way (timestamp only; ts_ms optional)."""
    cur = db.execute(
        "INSERT INTO messages (session_id, turn_num, role, content, timestamp, ts_ms)"
        " VALUES (?, 1, 'human', ?, ?, ?)",
        (session_id, '"legacy row"', stamp, ts_ms),
    )
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Migration: ts_ms column + backfill
# ---------------------------------------------------------------------------


class TestTsMsMigration:
    def test_migration_adds_column_and_backfills(self, migrated_db):
        """The new migration step backfills epoch-ms from legacy 14-char stamps."""
        _insert_legacy_row(migrated_db, session_id="s1", stamp=T0_STAMP)

        from context_engine.store.db import add_turn_ts_ms_column

        add_turn_ts_ms_column(migrated_db)

        row = migrated_db.execute(
            "SELECT ts_ms FROM messages WHERE session_id = 's1'"
        ).fetchone()
        assert row["ts_ms"] == T0_S * 1000

    def test_backfill_handles_empty_and_garbage(self, migrated_db):
        from context_engine.store.db import add_turn_ts_ms_column

        migrated_db.execute(
            "INSERT INTO messages (session_id, turn_num, role, content, timestamp)"
            " VALUES ('s1', 1, 'ai', '\"x\"', '')"
        )
        migrated_db.execute(
            "INSERT INTO messages (session_id, turn_num, role, content, timestamp)"
            " VALUES ('s2', 1, 'ai', '\"x\"', 'not-a-date')"
        )
        migrated_db.commit()

        add_turn_ts_ms_column(migrated_db)

        ms_vals = [
            r["ts_ms"]
            for r in migrated_db.execute("SELECT ts_ms FROM messages ORDER BY id")
        ]
        assert ms_vals == [0, 0], "unparseable/empty stamps must backfill to 0, not crash"


# ---------------------------------------------------------------------------
# Generation: monotonic per-process turn stamps
# ---------------------------------------------------------------------------


class TestNextTurnStamp:
    def test_monotonic_within_same_millisecond(self, monkeypatch):
        """Two turns issued in the same ms get strictly increasing stamps."""
        from datetime import datetime as real_dt

        frozen = real_dt.fromtimestamp(T0_S)

        class _FrozenDatetime:
            fromtimestamp = staticmethod(real_dt.fromtimestamp)

            @staticmethod
            def now():
                return frozen

        monkeypatch.setattr(store_core, "datetime", _FrozenDatetime)

        first_ms, first_stamp = store_core._next_turn_stamp()
        second_ms, second_stamp = store_core._next_turn_stamp()

        assert second_ms == first_ms + 1, "same-ms turns must be bumped apart"
        assert second_stamp >= first_stamp

    def test_display_stamp_is_14_chars(self, monkeypatch):
        """The 14-char display format is part of the client contract."""
        from datetime import datetime as real_dt

        frozen = real_dt.fromtimestamp(T0_S)

        class _FrozenDatetime:
            fromtimestamp = staticmethod(real_dt.fromtimestamp)

            @staticmethod
            def now():
                return frozen

        monkeypatch.setattr(store_core, "datetime", _FrozenDatetime)

        _, stamp = store_core._next_turn_stamp()

        assert len(stamp) == 14
        assert stamp == frozen.strftime("%Y%m%d%H%M%S")


# ---------------------------------------------------------------------------
# add_messages: writes ts_ms, keeps the 14-char display timestamp
# ---------------------------------------------------------------------------


class TestAddMessagesWritesTsMs:
    def test_two_same_ms_turns_get_increasing_ts_ms(self, migrated_db, monkeypatch):
        """Consecutive turns issued in the same millisecond must not tie."""
        monkeypatch.setattr(store_core, "_db", migrated_db)

        stamps = iter([(T0_S * 1000, T0_STAMP), (T0_S * 1000 + 1, T0_STAMP)])
        monkeypatch.setattr(store_core, "_next_turn_stamp", lambda: next(stamps))

        asyncio.run(store_core.add_messages("s1", [HumanMessage(content="a")]))
        asyncio.run(store_core.add_messages("s2", [AIMessage(content="b")]))

        rows = migrated_db.execute(
            "SELECT session_id, ts_ms, timestamp FROM messages ORDER BY ts_ms"
        ).fetchall()
        assert [r["ts_ms"] for r in rows] == [T0_S * 1000, T0_S * 1000 + 1]
        assert all(len(r["timestamp"]) == 14 for r in rows), (
            "the 14-char display format is a client contract — must not change"
        )


# ---------------------------------------------------------------------------
# get_session_ids: activity ordering within the same second
# ---------------------------------------------------------------------------


class TestSessionOrdering:
    def test_same_second_sessions_order_by_activity(self, migrated_db, monkeypatch):
        """Two sessions started in the SAME second: the later one lists first.

        Old code ordered by MAX(timestamp) — tied within the second, so the
        list order was unstable. New code orders by MAX(ts_ms).
        """
        monkeypatch.setattr(store_core, "_db", migrated_db)

        _insert_legacy_row(migrated_db, session_id="first", stamp=T0_STAMP, ts_ms=T0_S * 1000)
        _insert_legacy_row(migrated_db, session_id="second", stamp=T0_STAMP, ts_ms=T0_S * 1000 + 5)

        sessions = store_core.get_session_ids()

        assert [s["session_id"] for s in sessions] == ["second", "first"]

    def test_last_time_stays_14_char(self, migrated_db, monkeypatch):
        """``last_time`` is client-facing — must keep the legacy 14-char format."""
        monkeypatch.setattr(store_core, "_db", migrated_db)

        _insert_legacy_row(migrated_db, session_id="first", stamp=T0_STAMP, ts_ms=T0_S * 1000)

        sessions = store_core.get_session_ids()

        assert sessions[0]["last_time"] == T0_STAMP
        assert len(sessions[0]["last_time"]) == 14


# ---------------------------------------------------------------------------
# API surface: history rows keep the legacy shape
# ---------------------------------------------------------------------------


class TestHistoryApiShape:
    def test_history_rows_hide_ts_ms_and_keep_14_char(self, migrated_db, monkeypatch):
        """History rows must stay byte-compatible: 14-char timestamp, no ts_ms."""
        monkeypatch.setattr(store_core, "_db", migrated_db)

        stamps = iter([(T0_S * 1000, T0_STAMP)])
        monkeypatch.setattr(store_core, "_next_turn_stamp", lambda: next(stamps))
        asyncio.run(store_core.add_messages("s1", [HumanMessage(content="hello")]))

        rows = store_core.get_history_by_turn_page("s1")

        assert len(rows) == 1
        assert "ts_ms" not in rows[0], "internal ordering column must not leak to the API"
        assert len(rows[0]["timestamp"]) == 14
