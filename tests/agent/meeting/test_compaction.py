"""阶段 3: discussion compaction and boot auto-adjourn."""

from __future__ import annotations

import pytest

from agent.meeting.compaction import compact_history, static_compactor
from agent.meeting.models import MeetingMessage, SpeakerKind
from agent.meeting.persistence import MeetingStore
from agent.meeting.startup import ADJOURN_REASON, auto_adjourn_at_boot

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _seed_messages(store: MeetingStore, meeting_id: str, count: int) -> None:
    for i in range(count):
        await store.append_message(
            MeetingMessage(
                meeting_id=meeting_id,
                sequence=i + 1,
                speaker_kind=SpeakerKind.AGENT.value,
                speaker_id="fe",
                speaker_name="Frontend",
                content=f"statement number {i} about the architecture decision",
            )
        )


async def test_compaction_is_noop_below_threshold(store):
    await store.create_meeting("m", session_id="s", topic="t", participants=[])
    await _seed_messages(store, "m", 10)
    result = await compact_history(store, "m")
    assert result["compacted"] is False
    assert await store.list_compaction_segments("m") == []


async def test_compaction_persists_segment_and_summary_message(store):
    await store.create_meeting("m", session_id="s", topic="t", participants=[])
    await _seed_messages(store, "m", 30)
    result = await compact_history(store, "m", keep_recent=5, threshold=20)
    assert result["compacted"] is True
    assert result["compacted_count"] == 25
    segments = await store.list_compaction_segments("m")
    assert len(segments) == 1
    assert segments[0]["start_sequence"] == 1
    assert segments[0]["end_sequence"] == 25
    messages = await store.list_messages("m")
    assert messages[-1]["message_role"] == "summary"
    assert "statement" in result["summary"]


async def test_compactor_seam_accepts_injected_callable(store):
    await store.create_meeting("m", session_id="s", topic="t", participants=[])
    await _seed_messages(store, "m", 30)
    result = await compact_history(
        store, "m", keep_recent=5, threshold=20, compactor=lambda _msgs: "INJECTED SUMMARY"
    )
    assert result["summary"] == "INJECTED SUMMARY"
    # The reused fallback always renders a structured summary doc (even empty).
    assert "Latest Unresolved User Request" in static_compactor([])


async def test_boot_adjourns_live_meetings_only(store):
    for mid, status in [("live", "in_progress"), ("waiting", "waiting_user"), ("done", "ended")]:
        await store.create_meeting(mid, session_id="s", topic="t", participants=[])
        if status != "in_progress":
            await store.update_meeting(mid, status=status)

    result = await auto_adjourn_at_boot(store)
    assert result == {"scanned": 2, "adjourned": 2, "errors": 0}
    assert (await store.get_meeting("live"))["status"] == "cancelled"
    assert (await store.get_meeting("waiting"))["status"] == "cancelled"
    assert (await store.get_meeting("done"))["status"] == "ended"
    summary = (await store.get_meeting("live"))["final_summary"]
    assert ADJOURN_REASON in summary
