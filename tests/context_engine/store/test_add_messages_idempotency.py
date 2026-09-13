"""P1-2: crash-retry idempotency for MesMemory message persistence.

A retried add_messages call (same message objects, e.g. after a mid-write
crash) must not double-write: already-flushed messages are skipped via the
"_db_persisted" marker, and the storage-level unique index on idempotency_key
absorbs cross-process replays. Fresh content in a new turn is never deduped.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import add_messages
from context_engine.store.core import get_max_turn_num
from context_engine.store import db as store_db
from context_engine.store.core import get_history_by_turn_page

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    import context_engine.store.core as core

    monkeypatch.setattr(core, "_db", store_db.get_db())
    return db_path


async def test_retry_of_same_message_objects_writes_once(isolated_db):
    # Given a batch flushed once.
    sid = "p12-retry"
    batch = [HumanMessage(content="hello"), AIMessage(content="hi")]
    await add_messages(sid, batch)
    turns_after_first = get_max_turn_num(sid)
    assert turns_after_first == 1

    # When the exact same message objects are flushed again (crash retry):
    # the "_db_persisted" marker skips them entirely.
    await add_messages(sid, batch)

    # Then no duplicate turn was created.
    assert get_max_turn_num(sid) == turns_after_first


async def test_fresh_message_in_new_turn_is_never_deduped(isolated_db):
    sid = "p12-fresh"

    # Given a prior turn used the same text.
    first = HumanMessage(content="ok")
    await add_messages(sid, [first])

    # When a NEW (unmarked) message with identical content arrives next turn.
    second = HumanMessage(content="ok")
    await add_messages(sid, [second])

    # Then it is persisted — the dedup key is turn-scoped, not content-global.
    assert get_max_turn_num(sid) == 2


async def test_storage_level_unique_index_absorbs_replays(isolated_db):
    sid = "p12-unique"
    await add_messages(sid, [HumanMessage(content="payload")])
    turn_num = get_max_turn_num(sid)

    rows = get_history_by_turn_page(sid, turn_page_size=1)
    key = rows[0]["idempotency_key"] if "idempotency_key" in rows[0] else None

    # The raw column must not leak into client-facing rows...
    assert key is None

    # ...but it is stored, and a replay of the same key is ignored by the index.
    stored_key = (
        store_db.get_db()
        .execute("SELECT idempotency_key FROM messages WHERE session_id = ?", (sid,))
        .fetchone()[0]
    )
    assert stored_key

    with store_db.get_db() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO messages (session_id, turn_num, role, content, "
            "timestamp, ts_ms, idempotency_key) VALUES (?, ?, 'ai', 'replay', "
            "'20260101000000', 0, ?)",
            (sid, turn_num + 1, stored_key),
        )
        assert cursor.rowcount == 0

    assert get_max_turn_num(sid) == turn_num
