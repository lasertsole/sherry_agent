"""Compaction checkpoints + soft-deleted message restore.

Compacted messages stay on disk but leave the context; restoring a checkpoint
unmarks everything up to it and compacts everything after it.
"""

import pytest
from langchain_core.messages import HumanMessage

from context_engine.store import add_messages
from context_engine.store import db as store_db
from context_engine.store.core import (
    create_compaction_checkpoint,
    get_compaction_checkpoint,
    get_history_by_turn_page,
    get_max_turn_num,
    mark_messages_compacted,
    restore_compaction_checkpoint,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    import context_engine.store.core as core

    monkeypatch.setattr(core, "_db", store_db.get_db())
    return db_path


async def test_checkpoint_crud_and_compacted_filtering(isolated_db):
    sid = "p11-crud"
    for i in range(3):
        await add_messages(sid, [HumanMessage(content=f"turn {i}")])
    assert get_max_turn_num(sid) == 3

    checkpoint_id = create_compaction_checkpoint(
        sid, pre_compaction_turn=1, post_compaction_turn=3, summary_text="summary"
    )
    checkpoint = get_compaction_checkpoint(sid, checkpoint_id)
    assert checkpoint is not None
    assert checkpoint["pre_compaction_turn"] == 1
    assert checkpoint["checkpoint_seq"] == 0

    # Mark turns 2-3 compacted: they leave the context but stay on disk.
    mark_messages_compacted(sid, from_turn=2, checkpoint_id=checkpoint_id)
    active = get_history_by_turn_page(sid)
    assert all(row["turn_num"] <= 1 for row in active)

    compacted = get_history_by_turn_page(sid, include_compacted=True)
    assert {row["turn_num"] for row in compacted} == {1, 2, 3}
    assert all(row["compacted"] == 0 for row in active)


async def test_restore_compaction_checkpoint_recovers_messages(isolated_db):
    sid = "p11-restore"
    await add_messages(sid, [HumanMessage(content="before compaction")])
    pre_turn = get_max_turn_num(sid)

    checkpoint_id = create_compaction_checkpoint(
        sid, pre_compaction_turn=pre_turn, post_compaction_turn=pre_turn
    )
    mark_messages_compacted(sid, from_turn=1, checkpoint_id=checkpoint_id)
    await add_messages(sid, [HumanMessage(content="after compaction")])
    assert get_max_turn_num(sid) == 2

    # Restore: post-compaction messages are compacted away, the prefix returns.
    restore_compaction_checkpoint(sid, checkpoint_id)
    rows = get_history_by_turn_page(sid)

    assert [row["content"] for row in rows] == ["before compaction"]
    restored_checkpoint = get_compaction_checkpoint(sid, checkpoint_id)
    assert restored_checkpoint is not None


async def test_unknown_checkpoint_raises(isolated_db):
    with pytest.raises(ValueError, match="checkpoint not found"):
        restore_compaction_checkpoint("p11-missing", 999)
