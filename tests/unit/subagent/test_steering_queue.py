"""Unit tests for the per-session steering queue runtime (plan task 6).

Covers the MUST-DO behaviors from the task spec:
1. enqueue → drain returns ALL items and transitions every row to CONSUMED (via Task 3 store)
2. Cross-instance rehydration: a NEW queue object on the SAME db sees PENDING rows
   on first access (crash-recovery contract)
3. Idempotent enqueue: duplicate run_id never duplicates in memory or SQLite
4. Per-session isolation + session-key normalization (announce prefix ↔ bare id)
5. drain reports per-item mark_consumed outcome honestly (consumed=False stays visible)
6. enqueue_steering = memory append + SQLite persist in ONE call (caller 零分心)

Known API gap (documented, NOT fixed here — Task 3 file is frozen):
    PendingInjection has no message-level completion-status field
    (completed/failed/interrupted). Rehydrated messages therefore carry
    metadata {internal, provenance, run_id} but NOT 'status'; the original
    status remains readable inside the stored marker text
    ("[subagent:{name} {status}]").
"""

from pathlib import Path

import aiosqlite
import pytest
from langchain_core.messages import HumanMessage

from agent.tools.subagent.announce.steering_queue import (
    SteeringQueue,
)
from agent.tools.subagent.registry.pending_injections import (
    PendingInjectionStatus,
    PendingInjectionStore,
)

pytestmark = pytest.mark.unit


def _make_message(
    run_id: str = "run-abc-123",
    child_name: str = "research",
    status: str = "completed",
    content: str = "subagent finished the task",
) -> HumanMessage:
    """Build a Task-4-shaped completion message (frozen metadata contract)."""
    text = f"[subagent:{child_name} {status}]\n{content}" if content else (
        f"[subagent:{child_name} {status}]"
    )
    return HumanMessage(
        content=text,
        metadata={
            "internal": True,
            "provenance": "subagent_completion",
            "run_id": run_id,
            "status": status,
        },
    )


def _make_queue(tmp_path: Path, db_name: str = "t.db") -> SteeringQueue:
    return SteeringQueue(store=PendingInjectionStore(db_path=tmp_path / db_name))


async def _count_rows(db_path: Path) -> int:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM pending_injections") as cursor:
            row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# QA Scenario 1: enqueue → drain → CONSUMED (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_drain_marks_consumed(tmp_path: Path):
    """drain returns every enqueued item, each marked consumed in SQLite."""
    queue = _make_queue(tmp_path)
    store = queue.store

    _ = await queue.enqueue_steering("sess-1", _make_message(run_id="run-1", content="first"))
    _ = await queue.enqueue_steering("sess-1", _make_message(run_id="run-2", content="second"))

    items = await queue.drain("sess-1")
    assert [item.run_id for item in items] == ["run-1", "run-2"]
    assert all(item.consumed for item in items)
    # Task 4 carrier type rides through untouched
    assert items[0].message.text.endswith("first")

    # Task 3 API verifies the durable side effect: PENDING → CONSUMED
    for run_id in ("run-1", "run-2"):
        record = await store.get(run_id)
        assert record is not None
        assert record.status == PendingInjectionStatus.CONSUMED

    # Queue is empty afterwards; second drain is a no-op
    assert await queue.drain("sess-1") == []


# ---------------------------------------------------------------------------
# QA Scenario 2: crash rehydration across instances (same db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_after_restart(tmp_path: Path):
    """A NEW queue instance on the same db rehydrates PENDING rows on first access."""
    db_file = tmp_path / "restart.db"
    store = PendingInjectionStore(db_path=db_file)
    queue_a = SteeringQueue(store=store)
    _ = await queue_a.enqueue_steering("sess-9", _make_message(run_id="run-9", content="pre-crash"))

    # "Restart": brand-new queue object, same SQLite file
    queue_b = SteeringQueue(store=PendingInjectionStore(db_path=db_file))
    hydrated = await queue_b.rehydrate("sess-9")
    assert [item.run_id for item in hydrated] == ["run-9"]
    # rebuilt metadata carries internal/provenance/run_id — 'status' is NOT
    # restorable (documented Task 3 API gap: no status accessor for rows)
    # getattr pattern mirrors steering_queue internals: langchain-core 1.x
    # stubs do not statically expose BaseMessage.metadata (runtime: native dict)
    rebuilt_meta = getattr(hydrated[0].message, "metadata", None) or {}
    assert rebuilt_meta == {
        "internal": True,
        "provenance": "subagent_completion",
        "run_id": "run-9",
    }

    # Rehydrated item is drainable and consumes the row exactly once
    items = await queue_b.drain("sess-9")
    assert [item.run_id for item in items] == ["run-9"]
    assert items[0].consumed is True
    assert items[0].message.text.endswith("pre-crash")
    record = await store.get("run-9")
    assert record is not None
    assert record.status == PendingInjectionStatus.CONSUMED


@pytest.mark.asyncio
async def test_rehydrate_is_first_access_only(tmp_path: Path):
    """rehydrate() on an already-hydrated session is a no-op (no double merge)."""
    queue = _make_queue(tmp_path)
    _ = await queue.enqueue_steering("sess-5", _make_message(run_id="run-5"))

    again = await queue.rehydrate("sess-5")
    assert [item.run_id for item in again] == ["run-5"]  # snapshot, not a duplicate merge


# ---------------------------------------------------------------------------
# Idempotency: duplicate run_id never duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_run_id_no_duplicate(tmp_path: Path):
    """Second enqueue with the same run_id is a no-op: first message wins everywhere."""
    queue = _make_queue(tmp_path)

    _ = await queue.enqueue_steering("sess-2", _make_message(run_id="run-x", content="original"))
    second = await queue.enqueue_steering(
        "sess-2", _make_message(run_id="run-x", content="different, ignored")
    )
    assert second is not None and second.run_id == "run-x"

    items = await queue.drain("sess-2")
    assert len(items) == 1
    assert items[0].message.text.endswith("original")

    # SQLite kept exactly one row with the ORIGINAL payload
    assert await _count_rows(tmp_path / "t.db") == 1
    pending_after = await queue.store.list_pending()
    assert pending_after == []  # consumed by drain
    record = await queue.store.get("run-x")
    assert record is not None
    assert record.content.endswith("original")


@pytest.mark.asyncio
async def test_consumed_dup_not_revived(tmp_path: Path):
    """Re-enqueue of an already-CONSUMED run_id never re-injects (Task 3 contract)."""
    db_file = tmp_path / "revive.db"
    store = PendingInjectionStore(db_path=db_file)
    queue_a = SteeringQueue(store=store)
    _ = await queue_a.enqueue_steering("sess-3", _make_message(run_id="run-r", content="done once"))
    _ = await queue_a.drain("sess-3")  # consumed

    queue_b = SteeringQueue(store=PendingInjectionStore(db_path=db_file))
    result = await queue_b.enqueue_steering("sess-3", _make_message(run_id="run-r"))
    assert result is None  # honest: not queued

    assert await queue_b.drain("sess-3") == []
    record = await store.get("run-r")
    assert record is not None
    assert record.status == PendingInjectionStatus.CONSUMED


# ---------------------------------------------------------------------------
# Session isolation + key normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_isolation(tmp_path: Path):
    queue = _make_queue(tmp_path)
    _ = await queue.enqueue_steering("sess-a", _make_message(run_id="run-a"))
    _ = await queue.enqueue_steering("sess-b", _make_message(run_id="run-b"))

    got_a = await queue.drain("sess-a")
    assert [item.run_id for item in got_a] == ["run-a"]
    got_b = await queue.drain("sess-b")
    assert [item.run_id for item in got_b] == ["run-b"]


@pytest.mark.asyncio
async def test_prefixed_and_bare_keys_share_one_queue(tmp_path: Path):
    """agent:main:session:{id} and the bare {id} land on the same queue (Task 1 normalize)."""
    queue = _make_queue(tmp_path)
    _ = await queue.enqueue_steering("Agent:Main:Session:Foo", _make_message(run_id="run-p"))
    items = await queue.drain("Foo")
    assert [item.run_id for item in items] == ["run-p"]


@pytest.mark.asyncio
async def test_child_keys_pass_through_unchanged(tmp_path: Path):
    """Child/swarm keys (no session prefix) form their own queues, key verbatim."""
    queue = _make_queue(tmp_path)
    child_key = "agent:main:subagent:uuid-1"
    _ = await queue.enqueue_steering(child_key, _make_message(run_id="run-c"))
    items = await queue.drain(child_key)
    assert [item.run_id for item in items] == ["run-c"]


# ---------------------------------------------------------------------------
# Honest per-item reporting + input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_reports_failed_consumption_per_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If mark_consumed fails for one run_id, that item is still returned with consumed=False."""
    queue = _make_queue(tmp_path)
    _ = await queue.enqueue_steering("sess-4", _make_message(run_id="run-ok"))
    _ = await queue.enqueue_steering("sess-4", _make_message(run_id="run-vanished"))

    original = queue.store.mark_consumed

    async def flaky(run_id: str) -> bool:
        if run_id == "run-vanished":
            return False  # row vanished: single-winner UPDATE hits nothing
        return await original(run_id)

    monkeypatch.setattr(queue.store, "mark_consumed", flaky)

    items = await queue.drain("sess-4")
    by_run = {item.run_id: item.consumed for item in items}
    assert by_run == {"run-ok": True, "run-vanished": False}


@pytest.mark.asyncio
async def test_missing_run_id_rejected(tmp_path: Path):
    """HumanMessage without (or with an empty) metadata run_id → ValueError."""
    queue = _make_queue(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        _ = await queue.enqueue_steering(
            "sess-6",
            HumanMessage(content="[subagent:x completed]", metadata={"internal": True}),
        )
    with pytest.raises(ValueError, match="run_id"):
        _ = await queue.enqueue_steering(
            "sess-6",
            HumanMessage(content="[subagent:x completed]", metadata={"run_id": ""}),
        )


@pytest.mark.asyncio
async def test_enqueue_persists_message_text_to_sqlite(tmp_path: Path):
    """One call = memory + SQLite: the record's content is the full message text."""
    queue = _make_queue(tmp_path)
    message = _make_message(run_id="run-s", child_name="writer", content="persisted body")
    _ = await queue.enqueue_steering("sess-7", message)

    pending = await queue.store.list_pending()
    assert len(pending) == 1
    record = pending[0]
    assert record.run_id == "run-s"
    assert record.content == message.text
    assert record.provenance == "subagent_completion"
