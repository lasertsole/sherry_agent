"""Intent-log CRUD, generation-guarded CAS, due ordering and restart replay."""

from __future__ import annotations

import sqlite3
import time

import pytest

from runtime.effects import intent_log

pytestmark = [pytest.mark.unit]


def _row(db_path, intent_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM effect_intent WHERE id = ?", (intent_id,)).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def _set_due(db_path, intent_id: int, due_at: float) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE effect_intent SET due_at = ? WHERE id = ?", (due_at, intent_id))
        conn.commit()
    finally:
        conn.close()


def test_enqueue_inserts_pending_with_generation_one(effects_db) -> None:
    intent_id, generation = intent_log.enqueue("kind", "key", {"a": 1})

    row = _row(effects_db, intent_id)
    assert generation == 1
    assert row["generation"] == 1
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["payload"] == '{"a": 1}'


def test_enqueue_upserts_and_bumps_generation(effects_db) -> None:
    intent_id, first = intent_log.enqueue("kind", "key", {"a": 1})
    assert intent_log.mark_in_flight(intent_id, first) is True

    same_id, second = intent_log.enqueue("kind", "key", {"a": 2, "b": 3})

    assert same_id == intent_id
    assert second == first + 1
    row = _row(effects_db, intent_id)
    assert row["generation"] == second
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["payload"] == '{"a": 2, "b": 3}'


def test_mark_in_flight_is_a_generation_cas(effects_db) -> None:
    intent_id, generation = intent_log.enqueue("kind", "key", {})

    assert intent_log.mark_in_flight(intent_id, generation) is True
    assert intent_log.mark_in_flight(intent_id, generation) is False


def test_mark_done_requires_the_same_generation(effects_db) -> None:
    intent_id, generation = intent_log.enqueue("kind", "key", {})
    assert intent_log.mark_in_flight(intent_id, generation)

    same_id, new_generation = intent_log.enqueue("kind", "key", {})

    # The stale worker must not complete the new generation.
    assert intent_log.mark_done(same_id, generation) is False
    assert intent_log.mark_done(same_id, new_generation) is False  # it is pending, not in_flight
    assert _row(effects_db, same_id)["status"] == "pending"


def test_mark_done_transitions_in_flight(effects_db) -> None:
    intent_id, generation = intent_log.enqueue("kind", "key", {})
    assert intent_log.mark_in_flight(intent_id, generation)

    assert intent_log.mark_done(intent_id, generation) is True
    assert _row(effects_db, intent_id)["status"] == "done"
    assert intent_log.fetch_pending() == []


def test_mark_retry_increments_attempts_and_sets_due(effects_db) -> None:
    intent_id, generation = intent_log.enqueue("kind", "key", {})
    assert intent_log.mark_in_flight(intent_id, generation)
    due = time.time() + 10

    assert intent_log.mark_retry(intent_id, generation, due) is True

    row = _row(effects_db, intent_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["due_at"] == pytest.approx(due)
    assert intent_log.fetch_pending() == []  # not due yet


def test_mark_abandoned_is_terminal(effects_db) -> None:
    intent_id, generation = intent_log.enqueue("kind", "key", {})
    assert intent_log.mark_in_flight(intent_id, generation)

    assert intent_log.mark_abandoned(intent_id, generation) is True
    assert _row(effects_db, intent_id)["status"] == "abandoned"
    assert intent_log.fetch_pending() == []


def test_fetch_pending_returns_due_rows_oldest_first(effects_db) -> None:
    now = time.time()
    first, _ = intent_log.enqueue("kind", "first", {}, due_at=now - 5)
    second, _ = intent_log.enqueue("kind", "second", {}, due_at=now - 3)
    intent_log.enqueue("kind", "future", {}, due_at=now + 60)

    rows = intent_log.fetch_pending()

    assert [row.id for row in rows] == [first, second]
    assert [row.key for row in rows] == ["first", "second"]


def test_replay_resets_in_flight_and_preserves_future_pending(effects_db) -> None:
    now = time.time()
    in_flight, generation = intent_log.enqueue("kind", "running", {}, due_at=now - 10)
    assert intent_log.mark_in_flight(in_flight, generation)
    future, _ = intent_log.enqueue("kind", "future", {}, due_at=now + 60)

    replayed = intent_log.replay_pending()

    assert [row.id for row in replayed] == [in_flight]
    assert _row(effects_db, in_flight)["status"] == "pending"
    assert _row(effects_db, in_flight)["due_at"] <= time.time()
    assert _row(effects_db, future)["status"] == "pending"
    assert _row(effects_db, future)["due_at"] == pytest.approx(now + 60)
