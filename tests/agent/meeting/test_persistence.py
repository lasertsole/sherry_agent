"""Meeting store: meetings + messages on the shared registry DB."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import agent.meeting.persistence as persistence
from agent.meeting.models import MeetingMessage

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> persistence.MeetingStore:
    persistence._reset_store_for_tests(tmp_path / "registry.db")
    store = persistence.MeetingStore()
    await store.ensure_db()
    return store


async def test_create_get_list_round_trip(store: persistence.MeetingStore):
    await store.create_meeting(
        "m1",
        session_id="sess-1",
        topic="architecture",
        participants=[{"agent_id": "fe", "name": "Frontend", "join_order": 1}],
    )

    meeting = await store.get_meeting("m1")
    assert meeting is not None
    assert meeting["topic"] == "architecture"
    assert meeting["status"] == "in_progress"
    assert meeting["flow_stage"] == "requirement_confirmation"
    assert meeting["participants"][0]["agent_id"] == "fe"
    assert [m["id"] for m in await store.list_meetings()] == ["m1"]


async def test_messages_append_and_sequence(store: persistence.MeetingStore):
    await store.create_meeting("m1", session_id="s", topic="t", participants=[])

    for kind, speaker in (("host", "main"), ("agent", "fe"), ("user", "user")):
        await store.append_message(
            MeetingMessage(
                meeting_id="m1",
                sequence=await store.next_sequence("m1"),
                speaker_kind=kind,
                speaker_id=speaker,
                speaker_name=speaker,
                content=f"from {speaker}",
            )
        )

    messages = await store.list_messages("m1")
    assert [m["sequence"] for m in messages] == [1, 2, 3]
    assert [m["speaker_kind"] for m in messages] == ["host", "agent", "user"]
    assert await store.next_sequence("m1") == 4


async def test_update_meeting_flow_and_status(store: persistence.MeetingStore):
    await store.create_meeting("m1", session_id="s", topic="t", participants=[])
    await store.update_meeting(
        "m1",
        flow_stage="dispatching",
        iteration_count=4,
        execution_plan_json=[{"id": "p1"}],
    )

    meeting = await store.get_meeting("m1")
    assert meeting["flow_stage"] == "dispatching"
    assert meeting["iteration_count"] == 4
    assert meeting["execution_plan"] == [{"id": "p1"}]

    await store.update_meeting("m1", status="ended", ended_at=123, final_summary="done")
    meeting = await store.get_meeting("m1")
    assert meeting["status"] == "ended"
    assert meeting["ended_at"] == 123
    assert meeting["final_summary"] == "done"


async def test_meetings_are_isolated_by_id(store: persistence.MeetingStore):
    await store.create_meeting("a", session_id="s", topic="t", participants=[])
    await store.create_meeting("b", session_id="s", topic="t", participants=[])
    await store.append_message(
        MeetingMessage(
            meeting_id="a",
            sequence=1,
            speaker_kind="host",
            speaker_id="main",
            speaker_name="H",
            content="for a only",
        )
    )

    assert len(await store.list_messages("a")) == 1
    assert await store.list_messages("b") == []
    assert await store.next_sequence("b") == 1
