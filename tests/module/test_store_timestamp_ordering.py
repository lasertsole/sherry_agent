"""TDD tests for audit issue #21 — 1-second timestamp collisions.

Audit finding: the turn timestamp (``YYYYMMDDHHmmss``) has 1-second
resolution, so turns in the same second share a timestamp and
``get_session_ids`` (``MAX(timestamp) ... ORDER BY last_time DESC``) cannot
order sessions reliably.

Frontend impact analysis (pinned the design): the client parses ``createTime``
with a STRICT 14-digit format (``sessionFilter.parseSessionCreateTime``) and
``formatCompactTimeString`` rejects anything with ``length !== 14`` — so the
API surface (``last_time``, history row ``timestamp``) must stay 14 chars.
The fix therefore carries a ``ts_ms`` INTEGER NOT NULL column (epoch ms,
strictly increasing per process) in the base schema and orders
``get_session_ids`` by ``MAX(ts_ms)``; the visible formats never change.
The migration/backfill steps that once added the column to pre-existing
databases were removed once every tracked database had them applied —
fresh databases get ``ts_ms`` straight from ``build_messages_tb``.
"""

import asyncio
import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import core as store_core
from context_engine.store.db import _migrate

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

T0_S = 1_700_000_000  # fixed epoch seconds
# Local-timezone stamp of T0_S — strptime().timestamp() round-trips in the
# same tz, so stamp expectations stay self-consistent.
T0_STAMP = datetime.fromtimestamp(T0_S).strftime("%Y%m%d%H%M%S")


@pytest.fixture()
def migrated_db():
    """Fully-migrated store on a temp file (production schema shape)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # check_same_thread=False mirrors production get_db() — the
        # stamp/turn race test drives two writer threads through one connection.
        db = sqlite3.connect(str(Path(tmpdir) / "store.db"), check_same_thread=False)
        db.row_factory = sqlite3.Row
        _migrate(db)
        yield db
        db.close()


def _insert_raw_row(db: sqlite3.Connection, *, session_id: str, stamp: str, ts_ms: int):
    """Insert one row directly via SQL (bypassing add_messages)."""
    cur = db.execute(
        "INSERT INTO messages (session_id, turn_num, role, content, timestamp, ts_ms)"
        " VALUES (?, 1, 'human', ?, ?, ?)",
        (session_id, '"legacy row"', stamp, ts_ms),
    )
    db.commit()
    return cur.lastrowid


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
        # isolate from earlier real-time stamps: a leftover _last_turn_ms
        # would outrun the frozen clock and trigger the monotonic bump branch
        monkeypatch.setattr(store_core, "_last_turn_ms", None)

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
        monkeypatch.setattr(store_core, "_last_turn_ms", None)

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

        _insert_raw_row(migrated_db, session_id="first", stamp=T0_STAMP, ts_ms=T0_S * 1000)
        _insert_raw_row(migrated_db, session_id="second", stamp=T0_STAMP, ts_ms=T0_S * 1000 + 5)

        sessions = store_core.get_session_ids()

        assert [s["session_id"] for s in sessions] == ["second", "first"]

    def test_last_time_stays_14_char(self, migrated_db, monkeypatch):
        """``last_time`` is client-facing — must keep the legacy 14-char format."""
        monkeypatch.setattr(store_core, "_db", migrated_db)

        _insert_raw_row(migrated_db, session_id="first", stamp=T0_STAMP, ts_ms=T0_S * 1000)

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


# ---------------------------------------------------------------------------
# add_messages: stamp order == turn order even under writer interleaving
# ---------------------------------------------------------------------------


class TestStampTurnAtomicity:
    def test_stamp_order_matches_turn_order_under_interleaving(self, migrated_db, monkeypatch):
        """The turn stamp must be taken atomically with turn assignment.

        Regression guard for the history-jumble race: ``add_messages`` used to
        call ``_next_turn_stamp()`` while building rows, BEFORE acquiring
        ``_turn_assign_lock`` for the turn number. Two concurrent writers could
        interleave as: A stamps (early) → B stamps (late) → B wins the lock and
        gets turn N → A gets turn N+1 — persisting turn N+1 with an EARLIER
        ts_ms than turn N, inverting the timeline against ``ORDER BY turn_num``.

        This test forces exactly that interleaving deterministically: the first
        stamping thread (A) is held out of the assign lock until the second
        writer (B) has fully committed its turn.
        """
        monkeypatch.setattr(store_core, "_db", migrated_db)

        a_stamped = threading.Event()
        b_done = threading.Event()
        gated = {"done": False}
        state: dict[str, threading.Thread | None] = {"first_stamper": None}

        # Stamp 1 goes to whichever thread stamps first (A), stamp 2 to the
        # next one (B) — the earlier ts must pair with the EARLIER turn once
        # the fix lands.
        stamps = iter(
            [
                (T0_S * 1000, T0_STAMP),
                (T0_S * 1000 + 1, T0_STAMP),
            ]
        )

        def fake_stamp():
            if state["first_stamper"] is None:
                state["first_stamper"] = threading.current_thread()
                a_stamped.set()
            return next(stamps)

        monkeypatch.setattr(store_core, "_next_turn_stamp", fake_stamp)

        real_lock = store_core._turn_assign_lock

        class GatedLock:
            """Context-manager shim that holds the first stamper out of the
            assign lock until the other writer has fully committed."""

            def __enter__(self):
                if state["first_stamper"] is threading.current_thread() and not gated["done"]:
                    gated["done"] = True
                    assert b_done.wait(timeout=10), "interleaving gate timed out"
                real_lock.acquire()
                return real_lock

            def __exit__(self, *exc_info):
                real_lock.release()
                return False

        monkeypatch.setattr(store_core, "_turn_assign_lock", GatedLock())

        def run_add(sid: str) -> None:
            asyncio.run(store_core.add_messages(sid, [HumanMessage(content=sid)]))

        thread_a = threading.Thread(target=run_add, args=("s_race",), name="writer-A")
        thread_a.start()
        assert a_stamped.wait(timeout=10), "writer A never stamped"

        thread_b = threading.Thread(target=run_add, args=("s_race",), name="writer-B")
        thread_b.start()
        thread_b.join(timeout=10)
        assert not thread_b.is_alive(), "writer B deadlocked"
        b_done.set()
        thread_a.join(timeout=10)
        assert not thread_a.is_alive(), "writer A deadlocked"

        rows = migrated_db.execute(
            "SELECT turn_num, ts_ms FROM messages WHERE session_id = 's_race' ORDER BY turn_num"
        ).fetchall()
        assert [r["turn_num"] for r in rows] == [1, 2], "two turns must persist"
        assert rows[0]["ts_ms"] < rows[1]["ts_ms"], (
            "timestamp order must follow turn order even when the first "
            "stamper is descheduled before turn assignment"
        )
