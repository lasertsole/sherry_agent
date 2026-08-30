"""Unit tests for the pending-injection queue (SQLite-backed, idempotent on run_id).

Covers the four MUST-DO behaviors from the task spec:
1. Idempotent enqueue (double enqueue same run_id → one row, second call returns existing)
2. Persistence across "restart" (new instance on the same db file still lists pending)
3. mark_consumed removes from list_pending (race-safe: single winner)
4. provenance fixed to 'subagent_completion'
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.subagent.registry.pending_injections import (
    PendingInjection,
    PendingInjectionStatus,
    PendingInjectionStore,
)


def _make_injection(
    run_id: str = "run-abc-123",
    requester_session_key: str = "sess-1",
    content: str = "subagent finished the task",
) -> PendingInjection:
    return PendingInjection(
        run_id=run_id,
        requester_session_key=requester_session_key,
        child_session_key="agent:main:subagent:uuid-1",
        child_agent_id="main",
        child_name="research",
        content=content,
    )


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_on_run_id(tmp_path: Path):
    """Double enqueue same run_id → one row; second call returns the existing record."""
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    first = await store.enqueue(_make_injection())
    second = await store.enqueue(_make_injection(content="different content, ignored"))

    assert second.run_id == first.run_id
    assert second.content == first.content == "subagent finished the task"
    assert second.created_at == first.created_at

    pending = await store.list_pending()
    assert len(pending) == 1
    assert pending[0].run_id == "run-abc-123"


@pytest.mark.asyncio
async def test_persistence_across_restart(tmp_path: Path):
    """A new store instance opened on the same db file still lists the pending row."""
    db_file = tmp_path / "pending.db"
    store_a = PendingInjectionStore(db_path=db_file)
    await store_a.enqueue(_make_injection())

    store_b = PendingInjectionStore(db_path=db_file)
    pending = await store_b.list_pending()
    assert len(pending) == 1

    record = await store_b.get("run-abc-123")
    assert record is not None
    assert record.requester_session_key == "sess-1"
    assert record.content == "subagent finished the task"
    assert record.status == PendingInjectionStatus.PENDING


@pytest.mark.asyncio
async def test_mark_consumed_removes_from_list_pending(tmp_path: Path):
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    await store.enqueue(_make_injection())

    assert await store.mark_consumed("run-abc-123") is True

    assert await store.list_pending() == []
    consumed = await store.get("run-abc-123")
    assert consumed is not None
    assert consumed.status == PendingInjectionStatus.CONSUMED


@pytest.mark.asyncio
async def test_provenance_fixed_to_subagent_completion(tmp_path: Path):
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    record = await store.enqueue(_make_injection())
    assert record.provenance == "subagent_completion"

    fetched = await store.get("run-abc-123")
    assert fetched is not None
    assert fetched.provenance == "subagent_completion"


@pytest.mark.asyncio
async def test_mark_consumed_single_winner_under_concurrency(tmp_path: Path):
    """Two concurrent mark_consumed calls → exactly one wins (no double-consume race)."""
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    await store.enqueue(_make_injection())

    results = await asyncio.gather(
        store.mark_consumed("run-abc-123"),
        store.mark_consumed("run-abc-123"),
    )
    assert sorted(results) == [False, True]
    assert await store.list_pending() == []


@pytest.mark.asyncio
async def test_mark_consumed_missing_run_returns_false(tmp_path: Path):
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    assert await store.mark_consumed("no-such-run") is False


@pytest.mark.asyncio
async def test_get_missing_run_returns_none(tmp_path: Path):
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    assert await store.get("no-such-run") is None


@pytest.mark.asyncio
async def test_duplicate_enqueue_does_not_revive_consumed(tmp_path: Path):
    """Re-enqueue of an already-CONSUMED run_id is a no-op returning the CONSUMED record."""
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    await store.enqueue(_make_injection())
    await store.mark_consumed("run-abc-123")

    again = await store.enqueue(_make_injection())
    assert again.status == PendingInjectionStatus.CONSUMED
    assert await store.list_pending() == []


@pytest.mark.asyncio
async def test_new_injection_defaults_pending():
    record = _make_injection()
    assert record.status == PendingInjectionStatus.PENDING
    assert record.created_at > 0


@pytest.mark.asyncio
async def test_list_pending_orders_by_created_at(tmp_path: Path):
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    older = _make_injection(run_id="run-old")
    newer = _make_injection(run_id="run-new")
    older = older.model_copy(update={"created_at": 100.0})
    newer = newer.model_copy(update={"created_at": 200.0})
    await store.enqueue(newer)
    await store.enqueue(older)

    pending = await store.list_pending()
    assert [r.run_id for r in pending] == ["run-old", "run-new"]


@pytest.mark.asyncio
async def test_concurrent_distinct_enqueues_all_persisted(tmp_path: Path):
    """F3 regression: 16 same-loop concurrent enqueues (distinct run_ids) → zero row loss.

    Before the busy_timeout + once-only-init fix, the per-call _ensure_db
    (fresh connection + PRAGMA journal_mode=WAL per enqueue) raised
    OperationalError("database is locked") under this exact load and rows were
    silently lost.
    """
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")
    injections = [_make_injection(run_id=f"run-{i:02d}", content=f"payload {i}") for i in range(16)]

    results = await asyncio.gather(*(store.enqueue(inj) for inj in injections))

    assert len(results) == 16
    assert {r.run_id for r in results} == {inj.run_id for inj in injections}
    pending = await store.list_pending()
    assert len(pending) == 16
    for inj in injections:
        record = await store.get(inj.run_id)
        assert record is not None, f"row for {inj.run_id} was lost"
        assert record.content == inj.content
        assert record.status == PendingInjectionStatus.PENDING


@pytest.mark.asyncio
async def test_concurrent_duplicate_run_id_enqueues_yield_single_row(tmp_path: Path):
    """F3 regression: same-loop concurrent enqueues of ONE run_id → exactly one row.

    Idempotency must hold under concurrency: every caller gets the EXISTING
    record back (first writer wins), and no duplicate rows appear.
    """
    store = PendingInjectionStore(db_path=tmp_path / "pending.db")

    results = await asyncio.gather(*(store.enqueue(_make_injection()) for _ in range(16)))

    assert len(results) == 16
    assert all(r.run_id == "run-abc-123" for r in results)
    # Every caller sees the SAME persisted record (duplicates never overwrite).
    assert all(r.content == "subagent finished the task" for r in results)
    assert all(r.created_at == results[0].created_at for r in results)
    pending = await store.list_pending()
    assert len(pending) == 1
    row = await store.get("run-abc-123")
    assert row is not None
    assert row.content == "subagent finished the task"
