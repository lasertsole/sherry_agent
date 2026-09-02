"""Unit tests for the durable user-input queue store (SQLite-backed).

Covers the Task 1 spec behaviors:
1. FIFO order + position correctness
2. Per-session cap=20 → QueueFullError (21st entry rejected, not persisted)
3. client_msg_id dedup against ACTIVE rows returns the existing row unchanged
4. Concurrent claim atomicity: 100 asyncio coroutines claim the same session,
   zero duplicate claims
5. CLAIMED rows are never re-claimed
6. Expired rows are not claimable; recover() voids them
7. Terminal rows are not counted/listed as active
8. Empty-queue claim returns None
9. Persistence across "restart" (new store instance, same db file)
"""

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from server.queue.user_input_queue import (
    MAX_ACTIVE_PER_SESSION,
    QueueFullError,
    UserInputQueue,
    UserInputQueueRow,
    UserInputQueueStatus,
)


def _payload(text: str) -> str:
    """Serialized user-message JSON, isomorphic to the WS frame text payload."""
    return f'{{"text": "{text}", "image_base64_list": []}}'


@pytest.fixture
def store(tmp_path: Path) -> UserInputQueue:
    """Fresh store on a hermetic tmp SQLite file (same DB layout as production)."""
    return UserInputQueue(db_path=tmp_path / "subagent_registry.db")


async def _backdate_expires_at(db_path: Path, session_id: str, expires_at: float) -> None:
    """Test helper: force rows' expires_at into the past (simulates 24h aging)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE user_input_queue SET expires_at = ? WHERE session_id = ?",
            (expires_at, session_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# FIFO order / position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_positions_are_fifo_sequential(store: UserInputQueue):
    """Sequential enqueues get positions 1, 2, 3... (drain order)."""
    ids = []
    for i in range(3):
        row, position = await store.enqueue(f"s1", _payload(f"msg-{i}"), source="user")
        ids.append(row.id)
        assert position == i + 1
    assert len(set(ids)) == 3

    rows = await store.list_active("s1")
    assert [r.payload for r in rows] == [_payload(f"msg-{i}") for i in range(3)]
    assert all(r.status == UserInputQueueStatus.QUEUED for r in rows)


@pytest.mark.asyncio
async def test_claim_next_returns_oldest_first(store: UserInputQueue):
    """claim_next drains strictly in FIFO (created_at ascending) order."""
    for i in range(3):
        await store.enqueue("s1", _payload(f"msg-{i}"), source="user")

    claimed: list[UserInputQueueRow] = []
    for _ in range(3):
        row = await store.claim_next("s1")
        assert row is not None, "queue drained early"
        claimed.append(row)
    assert [r.payload for r in claimed] == [_payload(f"msg-{i}") for i in range(3)]
    assert all(r.status == UserInputQueueStatus.CLAIMED for r in claimed)


@pytest.mark.asyncio
async def test_positions_are_per_session(store: UserInputQueue):
    """Two sessions have independent FIFO positions."""
    _, pos_a = await store.enqueue("sA", _payload("a1"), source="user")
    _, pos_b = await store.enqueue("sB", _payload("b1"), source="user")
    assert pos_a == 1 and pos_b == 1

    count_a = await store.count_active("sA")
    count_b = await store.count_active("sB")
    assert count_a == 1 and count_b == 1


# ---------------------------------------------------------------------------
# Cap = 20 → QueueFullError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_at_20_raises_and_does_not_persist(store: UserInputQueue):
    """21st active row for one session → QueueFullError; the row is NOT inserted."""
    for i in range(MAX_ACTIVE_PER_SESSION):
        _row, position = await store.enqueue("s1", _payload(f"m{i}"), source="user")
        assert position == i + 1

    with pytest.raises(QueueFullError) as exc_info:
        await store.enqueue("s1", _payload("overflow"), source="user")
    assert "s1" in str(exc_info.value)
    assert str(MAX_ACTIVE_PER_SESSION) in str(exc_info.value)

    assert await store.count_active("s1") == MAX_ACTIVE_PER_SESSION
    rows = await store.list_active("s1")
    assert _payload("overflow") not in [r.payload for r in rows]


@pytest.mark.asyncio
async def test_terminal_rows_free_up_capacity(store: UserInputQueue):
    """Marking rows terminal frees slots: after 20 delivered, new enqueue succeeds."""
    rows = []
    for i in range(MAX_ACTIVE_PER_SESSION):
        row, _ = await store.enqueue("s1", _payload(f"m{i}"), source="user")
        rows.append(row)
    for row in rows:
        await store.mark_terminal(row.id, UserInputQueueStatus.DELIVERED)

    row, position = await store.enqueue("s1", _payload("fresh"), source="user")
    assert position == 1
    assert await store.count_active("s1") == 1


# ---------------------------------------------------------------------------
# client_msg_id dedup (active rows only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_returns_existing_row_unchanged(store: UserInputQueue):
    """Same client_msg_id while active → the EXISTING row, no duplicate enqueue."""
    first, pos_first = await store.enqueue(
        "s1", _payload("hello"), source="user", client_msg_id="ws-msg-42"
    )
    second, pos_second = await store.enqueue(
        "s1", _payload("hello (retry)"), source="user", client_msg_id="ws-msg-42"
    )

    assert second.id == first.id
    assert second.payload == first.payload == _payload("hello")
    assert second.created_at == first.created_at
    # The existing row is the only queued one: it sits at position 1.
    assert pos_first == 1 and pos_second == 1
    assert await store.count_active("s1") == 1


@pytest.mark.asyncio
async def test_dedup_applies_to_claimed_rows_too(store: UserInputQueue):
    """A CLAIMED row is still active: re-enqueue with its client_msg_id is a no-op."""
    row, _ = await store.enqueue("s1", _payload("hello"), source="user", client_msg_id="ws-1")
    claimed = await store.claim_next("s1")
    assert claimed is not None and claimed.id == row.id

    again, _ = await store.enqueue("s1", _payload("hello"), source="user", client_msg_id="ws-1")
    assert again.id == row.id
    assert await store.count_active("s1") == 1


@pytest.mark.asyncio
async def test_dedup_does_not_apply_after_terminal(store: UserInputQueue):
    """client_msg_id on a DELIVERED row no longer dedups: a new row is enqueued."""
    first, _ = await store.enqueue("s1", _payload("v1"), source="user", client_msg_id="ws-1")
    await store.mark_terminal(first.id, UserInputQueueStatus.DELIVERED)

    second, pos = await store.enqueue("s1", _payload("v2"), source="user", client_msg_id="ws-1")
    assert second.id != first.id
    assert second.payload == _payload("v2")
    assert pos == 1
    assert await store.count_active("s1") == 1


@pytest.mark.asyncio
async def test_null_client_msg_id_never_dedups(store: UserInputQueue):
    """Two enqueues without client_msg_id are independent rows (NULL is not a key)."""
    a, _ = await store.enqueue("s1", _payload("x"), source="user")
    b, _ = await store.enqueue("s1", _payload("y"), source="user")
    assert a.id != b.id
    assert await store.count_active("s1") == 2


# ---------------------------------------------------------------------------
# Claim semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_next_empty_queue_returns_none(store: UserInputQueue):
    assert await store.claim_next("no-such-session") is None


@pytest.mark.asyncio
async def test_claimed_rows_are_not_reclaimed(store: UserInputQueue):
    """CLAIMED rows are skipped: draining stops at None once every row is claimed."""
    for i in range(2):
        await store.enqueue("s1", _payload(f"m{i}"), source="user")

    first = await store.claim_next("s1")
    second = await store.claim_next("s1")
    assert first is not None and second is not None
    assert first.id != second.id

    assert await store.claim_next("s1") is None
    assert await store.count_active("s1") == 2  # both still CLAIMED (active)
    rows = await store.list_active("s1")
    assert all(r.status == UserInputQueueStatus.CLAIMED for r in rows)


@pytest.mark.asyncio
async def test_concurrent_claim_is_race_free(store: UserInputQueue):
    """100 coroutines race-claim a 20-row session: exactly 20 winners, zero duplicates.

    Every successful claim returns a DISTINCT row id; losers get None; after the
    storm no row is left QUEUED and no id is claimed twice.
    """
    total = MAX_ACTIVE_PER_SESSION
    enqueued = set()
    for i in range(total):
        row, _ = await store.enqueue("s1", _payload(f"m{i}"), source="user")
        enqueued.add(row.id)
    assert len(enqueued) == total

    results = await asyncio.gather(
        *(store.claim_next("s1") for _ in range(100)), return_exceptions=True
    )
    claimed: list[UserInputQueueRow] = []
    for r in results:
        assert not isinstance(r, BaseException), f"claim raised: {r!r}"
        if r is not None:
            claimed.append(r)
    assert len(claimed) == total, f"expected {total} winners, got {len(claimed)}"
    claimed_ids = [r.id for r in claimed]
    assert len(set(claimed_ids)) == total, "duplicate claim detected"
    assert set(claimed_ids) == enqueued

    # All rows CLAIMED, none QUEUED, none double-claimed.
    rows = await store.list_active("s1")
    assert all(r.status == UserInputQueueStatus.CLAIMED for r in rows)
    assert {r.id for r in rows} == enqueued


# ---------------------------------------------------------------------------
# Expiry + recover()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_rows_are_not_claimable(store: UserInputQueue, tmp_path: Path):
    """Rows past expires_at are invisible to claim_next (24h crash-recovery rule)."""
    _row, _ = await store.enqueue("s1", _payload("stale"), source="user")
    await _backdate_expires_at(tmp_path / "subagent_registry.db", "s1", expires_at=1.0)

    assert await store.claim_next("s1") is None
    # The row still exists, still QUEUED (recover is what voids it).
    assert await store.count_active("s1") == 1


@pytest.mark.asyncio
async def test_recover_voids_only_expired_rows(store: UserInputQueue, tmp_path: Path):
    """recover() voids expired QUEUED/CLAIMED rows and leaves fresh rows alone."""
    fresh, _ = await store.enqueue("s1", _payload("fresh"), source="user")
    _stale, _ = await store.enqueue("s1", _payload("stale"), source="user")
    db_path = tmp_path / "subagent_registry.db"
    await _backdate_expires_at(db_path, "s1", expires_at=1.0)
    # Restore only the fresh row's expiry to the future.
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE user_input_queue SET expires_at = ? WHERE id = ?", (9e9, fresh.id))
        await db.commit()

    voided = await store.recover("s1")
    assert voided == 1

    rows = await store.list_active("s1")
    assert [r.id for r in rows] == [fresh.id]
    assert await store.claim_next("s1") is not None
    assert await store.claim_next("s1") is None


@pytest.mark.asyncio
async def test_recover_also_voids_expired_claimed_rows(store: UserInputQueue, tmp_path: Path):
    """A CLAIMED row that expired (crash mid-turn) is voided by recover()."""
    await store.enqueue("s1", _payload("m"), source="user")
    claimed = await store.claim_next("s1")
    assert claimed is not None
    db_path = tmp_path / "subagent_registry.db"
    await _backdate_expires_at(db_path, "s1", expires_at=1.0)

    voided = await store.recover("s1")
    assert voided == 1
    assert await store.count_active("s1") == 0


@pytest.mark.asyncio
async def test_recover_returns_zero_when_nothing_expired(store: UserInputQueue):
    await store.enqueue("s1", _payload("m"), source="user")
    assert await store.recover("s1") == 0


# ---------------------------------------------------------------------------
# mark_terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_terminal_moves_row_out_of_active(store: UserInputQueue):
    row, _ = await store.enqueue("s1", _payload("m"), source="user")
    assert await store.count_active("s1") == 1

    await store.mark_terminal(row.id, UserInputQueueStatus.DELIVERED)

    assert await store.count_active("s1") == 0
    assert await store.list_active("s1") == []
    assert await store.claim_next("s1") is None


@pytest.mark.asyncio
async def test_mark_terminal_accepts_only_terminal_statuses(store: UserInputQueue):
    """QUEUED/CLAIMED are NOT terminal statuses: passing them must raise."""
    row, _ = await store.enqueue("s1", _payload("m"), source="user")
    with pytest.raises(ValueError):
        await store.mark_terminal(row.id, UserInputQueueStatus.QUEUED)
    with pytest.raises(ValueError):
        await store.mark_terminal(row.id, UserInputQueueStatus.CLAIMED)
    # Row untouched.
    assert await store.count_active("s1") == 1


@pytest.mark.asyncio
async def test_mark_terminal_missing_row_is_noop(store: UserInputQueue):
    await store.mark_terminal("no-such-id", UserInputQueueStatus.FAILED)  # must not raise


# ---------------------------------------------------------------------------
# source / reply_target / persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_is_persisted_as_row_column(store: UserInputQueue):
    """source is a persisted column (never inferred from payload at runtime)."""
    row, _ = await store.enqueue("s1", _payload("u"), source="user")
    cron_row, _ = await store.enqueue("s1", _payload("c"), source="cron")
    assert row.source == "user"
    assert cron_row.source == "cron"

    rows = await store.list_active("s1")
    assert [r.source for r in rows] == ["user", "cron"]


@pytest.mark.asyncio
async def test_invalid_source_is_rejected(store: UserInputQueue):
    with pytest.raises(ValueError):
        await store.enqueue("s1", _payload("m"), source="heartbeat")  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_reply_target_round_trips_channel_routing_json(store: UserInputQueue):
    """reply_target carries the channel routing JSON (channel path); None default (WS)."""
    target = '{"channel": "qq", "chat_id": "12345", "reply_to": "msg-9"}'
    ws_row, _ = await store.enqueue("s1", _payload("ws"), source="user")
    ch_row, _ = await store.enqueue("s1", _payload("qq"), source="user", reply_target=target)

    assert ws_row.reply_target is None
    assert ch_row.reply_target == target
    fetched = (await store.list_active("s1"))[1]
    assert fetched.reply_target == target


@pytest.mark.asyncio
async def test_rows_survive_restart_on_same_db_file(tmp_path: Path):
    """A new store instance over the same SQLite file sees the same rows."""
    db_file = tmp_path / "subagent_registry.db"
    store_a = UserInputQueue(db_path=db_file)
    row, _ = await store_a.enqueue("s1", _payload("durable"), source="user", client_msg_id="c1")

    store_b = UserInputQueue(db_path=db_file)
    assert await store_b.count_active("s1") == 1
    rows = await store_b.list_active("s1")
    assert rows[0].id == row.id
    assert rows[0].payload == _payload("durable")
    assert rows[0].client_msg_id == "c1"
    claimed = await store_b.claim_next("s1")
    assert claimed is not None and claimed.id == row.id
