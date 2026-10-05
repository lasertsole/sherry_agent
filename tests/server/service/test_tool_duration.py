"""Tool-duration observation, backend half: the frame field and the column.

Two measurements, one authority: the stream's ``tool_result.duration_ms`` is
what the running card shows, and the persisted ``messages.tool_duration_ms``
(measured by ``message_persistence`` at tool-return time) is what history
replay shows. Both are ``time.monotonic()`` differences clamped at 0 — the plan
calls this out as the one high-risk item, because a wall-clock subtraction goes
negative when NTP steps the clock back (ZCode ships that defect unguarded).
"""

import pytest
from langchain_core.messages import ToolMessage

from server.service import stream_dispatch as sd
from server.service.stream_dispatch import _consume_tool_duration, _note_tool_started

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _clean_timers():
    sd._tool_started_at.clear()
    yield
    sd._tool_started_at.clear()


def _clock(monkeypatch, *values: float) -> None:
    """Inject a scripted monotonic clock (see ``_now_monotonic``)."""
    remaining = list(values)

    def _now() -> float:
        return remaining.pop(0)

    monkeypatch.setattr(sd, "_now_monotonic", _now)


def test_a_tool_reports_its_execution_milliseconds(monkeypatch):
    _clock(monkeypatch, 100.0, 100.85)

    _note_tool_started("s1", "call-a")
    duration = _consume_tool_duration("s1", "call-a")

    assert duration == 850


def test_a_backward_wall_clock_step_cannot_make_a_negative_duration(monkeypatch):
    """The plan's regression case: wall clock rewinds, monotonic does not."""
    _clock(monkeypatch, 100.0, 100.25)
    wall = iter([1000.0, 995.0])  # NTP stepped the clock back 5s mid-call
    monkeypatch.setattr(sd.time, "time", lambda: next(wall))

    _note_tool_started("s1", "call-a")
    duration = _consume_tool_duration("s1", "call-a")

    assert duration == 250


def test_sessions_never_cross_read_each_others_timers(monkeypatch):
    # note s1 @10.0, note s2 @10.2, consume s2 @12.2, consume s1 @13.0
    _clock(monkeypatch, 10.0, 10.2, 12.2, 13.0)

    _note_tool_started("s1", "call-a")
    _note_tool_started("s2", "call-a")  # same tool id, different session
    assert _consume_tool_duration("s2", "call-a") == 2000
    assert _consume_tool_duration("s1", "call-a") == 3000


def test_an_unknown_or_empty_id_reports_none():
    _note_tool_started("s1", None)
    _note_tool_started("s1", "")

    assert _consume_tool_duration("s1", None) is None
    assert _consume_tool_duration("s1", "never-started") is None
    assert _consume_tool_duration("", "call-a") is None


def test_consuming_is_one_shot_and_buckets_are_released(monkeypatch):
    _clock(monkeypatch, 5.0, 5.1, 5.2)

    _note_tool_started("s1", "call-a")
    assert _consume_tool_duration("s1", "call-a") == 100
    # A repeated result frame for the same id reports unknown, not a growing time.
    assert _consume_tool_duration("s1", "call-a") is None
    # The empty session bucket is dropped: a long-lived process does not accrue.
    assert "s1" not in sd._tool_started_at


def test_the_tool_result_frame_carries_the_duration(monkeypatch):
    """The wire field the card consumes (additive: old clients ignore it)."""
    from server.service.stream_dispatch import StreamTurn

    builder = StreamTurn.__new__(StreamTurn)
    builder.session_id = "s1"
    builder._partial_tool_calls = {}
    _clock(monkeypatch, 20.0)
    _note_tool_started("s1", "call-a")
    _clock(monkeypatch, 20.4)

    frames = list(
        builder._tool_result_frames(
            ToolMessage(content="ok", tool_call_id="call-a", name="read_file")
        )
    )

    result = next(frame for frame in frames if frame["type"] == "tool_result")
    assert result["duration_ms"] == 400


def test_the_persistence_stamp_lands_on_the_tool_message(monkeypatch):
    """The authoritative value: measured where the tool actually returned."""
    from agent.middlewares.message_persistence.core import _stamp_tool_duration

    import agent.middlewares.message_persistence.core as persistence

    stamp_clock = iter([50.42])
    monkeypatch.setattr(persistence, "_now_monotonic", lambda: next(stamp_clock))
    message = ToolMessage(content="ok", tool_call_id="call-a", name="read_file")

    _stamp_tool_duration(message, 50.0)

    assert message.additional_kwargs["tool_duration_ms"] == 420


def test_a_backward_wall_clock_step_cannot_make_the_stamp_negative(monkeypatch):
    from agent.middlewares.message_persistence.core import _stamp_tool_duration

    import agent.middlewares.message_persistence.core as persistence

    # The wall clock rewinds; the monotonic clock does not (and the stamp only
    # ever reads the monotonic one — this pins that the wall clock is unused).
    stamp_clock = iter([80.05])
    monkeypatch.setattr(persistence, "_now_monotonic", lambda: next(stamp_clock))
    monkeypatch.setattr(persistence.time, "time", lambda: 1.0)
    message = ToolMessage(content="ok", tool_call_id="call-a", name="read_file")

    _stamp_tool_duration(message, 80.0)

    assert message.additional_kwargs["tool_duration_ms"] == 50


def test_the_column_round_trips_through_mes_memory(tmp_path, monkeypatch):
    """Stage 2 acceptance: the row the history replays carries the duration."""
    import context_engine.store.core as core
    from context_engine.store import db as store_db
    from langchain_core.messages import ToolMessage as LCToolMessage

    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(core, "_db", store_db.get_db())

    stamped = LCToolMessage(content="ok", tool_call_id="call-a", name="read_file", status="success")
    stamped.additional_kwargs["tool_duration_ms"] = 1234
    plain = LCToolMessage(content="ok", tool_call_id="call-b", name="read_file", status="success")

    import asyncio

    asyncio.run(core.add_messages("s-dur", [stamped, plain]))
    rows = core.get_history_by_turn_page(
        "s-dur", min_turn_num=1, turn_page_size=10, turn_page_num=1
    )

    by_call = {row["tool_call_id"]: row for row in rows}
    assert by_call["call-a"]["tool_duration_ms"] == 1234
    # No stamp (a tool that never returned): unknown, never 0.
    assert by_call["call-b"]["tool_duration_ms"] is None


def test_a_legacy_database_without_the_column_reads_back_null(tmp_path, monkeypatch):
    """R6: an old database heals by ADD COLUMN and old rows read as NULL."""
    import sqlite3

    import context_engine.store.core as core
    from context_engine.store import db as store_db

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, turn_num INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, tool_call_id TEXT, tool_calls TEXT, tool_status TEXT,
            tool_name TEXT, timestamp TEXT, ts_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    legacy.execute(
        "INSERT INTO messages (session_id, turn_num, role, content, tool_call_id, tool_name, "
        "timestamp) VALUES ('s-old', 1, 'tool', '\"ok\"', 'call-old', 'read_file', 't')"
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    connection = store_db.get_db()
    monkeypatch.setattr(core, "_db", connection)

    columns = [row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()]
    assert "tool_duration_ms" in columns  # the heal added it

    rows = core.get_history_by_turn_page(
        "s-old", min_turn_num=1, turn_page_size=10, turn_page_num=1
    )
    assert rows[0]["tool_duration_ms"] is None


def test_the_frame_prefers_the_persisted_stamp(monkeypatch):
    """Live and replay must show the same number: the stamp is authoritative."""
    from server.service.stream_dispatch import StreamTurn

    builder = StreamTurn.__new__(StreamTurn)
    builder.session_id = "s1"
    builder._partial_tool_calls = {}
    _clock(monkeypatch, 20.0)
    _note_tool_started("s1", "call-a")  # the frame path measured 400 ms...
    _clock(monkeypatch, 20.4)
    message = ToolMessage(
        content="ok",
        tool_call_id="call-a",
        name="read_file",
        additional_kwargs={"tool_duration_ms": 6},  # ...but the tool really took 6 ms
    )

    frames = list(builder._tool_result_frames(message))

    result = next(frame for frame in frames if frame["type"] == "tool_result")
    assert result["duration_ms"] == 6
    assert "s1" not in sd._tool_started_at  # the entry was still consumed
