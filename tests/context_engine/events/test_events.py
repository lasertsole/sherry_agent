"""P2-1: event log append/read/replay + projector (SESSION plan)."""

import pytest
from langchain_core.messages import HumanMessage

from context_engine.events import EventType, append_event, get_events, replay_events
from context_engine.events.projector import make_default_projector
from context_engine.store.core import (
    create_compaction_checkpoint,
    get_compaction_checkpoint,
    mark_messages_compacted,
)
from context_engine.store import add_messages
from context_engine.store import db as store_db

pytestmark = [pytest.mark.unit]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    import context_engine.store.core as core

    monkeypatch.setattr(core, "_db", store_db.get_db())
    return db_path


def test_append_assigns_gapless_session_sequence(isolated_db):
    sid = "p21-seq"
    first = append_event(sid, EventType.MESSAGE_APPENDED, {"turn": 1})
    second = append_event(sid, EventType.MESSAGE_APPENDED, {"turn": 2})
    assert first["seq"] == 0
    assert second["seq"] == 1

    stored = get_events(sid)
    assert [e["type"] for e in stored] == [
        EventType.MESSAGE_APPENDED.value,
        EventType.MESSAGE_APPENDED.value,
    ]


def test_replay_returns_ordered_events(isolated_db):
    sid = "p21-replay"
    append_event(sid, EventType.COMPACTION_STARTED, {"trigger": "T2"})
    append_event(sid, EventType.COMPACTION_ENDED, {"turn": 5})

    replayed = replay_events(sid)
    assert [e["type"] for e in replayed] == [
        "compaction.started",
        "compaction.ended",
    ]


@pytest.mark.asyncio
async def test_default_projector_applies_checkpoint_events(isolated_db):
    sid = "p21-projector"
    add_messages(sid, [HumanMessage(content="prefix")])
    projector = make_default_projector()

    projector.project(
        {
            "session_id": sid,
            "type": EventType.CHECKPOINT_CREATED.value,
            "data": {"pre_compaction_turn": 1, "post_compaction_turn": 1},
        }
    )
    checkpoint = get_compaction_checkpoint(sid, 1)
    assert checkpoint is not None

    projector.project(
        {
            "session_id": sid,
            "type": EventType.CHECKPOINT_RESTORED.value,
            "data": {"from_turn": 1, "checkpoint_id": 1},
        }
    )
    rows = (
        store_db.get_db()
        .execute("SELECT compacted FROM messages WHERE session_id = ?", (sid,))
        .fetchall()
    )
    assert all(row["compacted"] == 0 for row in rows)


def test_checkpoint_store_roundtrip(isolated_db):
    sid = "p21-crud"
    checkpoint_id = create_compaction_checkpoint(
        sid, pre_compaction_turn=2, post_compaction_turn=3, summary_text="s"
    )
    mark_messages_compacted(sid, from_turn=3, checkpoint_id=checkpoint_id)
    checkpoint = get_compaction_checkpoint(sid, checkpoint_id)
    assert checkpoint["post_compaction_turn"] == 3
