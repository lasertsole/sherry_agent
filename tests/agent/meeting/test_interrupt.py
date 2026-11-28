"""阶段 2: interrupt classification, local rollback and the host gate."""

from __future__ import annotations

import pytest

from agent.meeting.flow import apply_interruption
from agent.meeting.host import filter_host_tools, phase_allows_speaker, stage_allows_host_speaking
from agent.meeting.interrupt import (
    InterruptRequest,
    apply_local_rollback,
    classify_by_keywords,
    needs_rollback,
)
from agent.meeting.models import (
    InterruptType,
    MeetingFlowStage,
    MeetingPhase,
)

pytestmark = [pytest.mark.unit]


def _plan() -> list[MeetingPhase]:
    return [
        MeetingPhase(id="p1", allowed_agent_ids=["fe"]),
        MeetingPhase(id="p2", allowed_agent_ids=["be"], prerequisites=["p1"]),
        MeetingPhase(id="p3", allowed_agent_ids=["dba"], prerequisites=["p2"]),
    ]


def test_keyword_classifier_covers_six_types():
    cases = {
        "end the meeting please": InterruptType.END_REQUEST,
        "let's switch to microservices": InterruptType.SCOPE_CHANGE,
        "优先级先做后端": InterruptType.PRIORITY_CHANGE,
        "actually that's wrong": InterruptType.CORRECTION,
        "also add caching": InterruptType.SUPPLEMENT,
        "random unrelated words": InterruptType.SUPPLEMENT,
    }
    for text, expected in cases.items():
        verdict = classify_by_keywords(InterruptRequest(content=text), _plan(), [])
        assert verdict.interrupt_type is expected


def test_rollback_marks_affected_phase_and_transitive_dependents():
    verdict = classify_by_keywords(InterruptRequest(content="scope change"), _plan(), [])
    assert verdict.interrupt_type is InterruptType.SCOPE_CHANGE
    assert needs_rollback(verdict)
    rolled = apply_local_rollback(_plan(), verdict.affected_phase_ids)
    assert [p.status for p in rolled] == ["needs_rediscussion"] * 3
    # input plan is untouched (new instances)
    assert all(p.status == "pending" for p in _plan())


def test_supplement_does_not_rollback():
    verdict = classify_by_keywords(InterruptRequest(content="also note X"), _plan(), [])
    assert not needs_rollback(verdict)
    assert verdict.affected_phase_ids == []


def test_host_tool_allowlist_strips_everything_else():
    names = ["agent_list", "ask_agent", "read_file", "terminal", "memory_search"]
    assert filter_host_tools(names) == ["agent_list", "ask_agent", "memory_search"]
    tools = [{"name": "output_summary"}, {"name": "terminal"}, {"name": "confirm_meeting_end"}]
    assert filter_host_tools(tools) == [{"name": "output_summary"}, {"name": "confirm_meeting_end"}]


def test_phase_allowlist_gate():
    plan = _plan()
    assert phase_allows_speaker("p1", "fe", plan)
    assert not phase_allows_speaker("p1", "be", plan)
    assert phase_allows_speaker(None, "anyone", plan)  # no phase → unrestricted
    assert phase_allows_speaker("missing", "anyone", plan)  # unknown phase → unrestricted


def test_non_dispatching_stages_are_host_only():
    assert stage_allows_host_speaking(MeetingFlowStage.REQUIREMENT_CONFIRMATION)
    assert not stage_allows_host_speaking(MeetingFlowStage.DISPATCHING)
    assert stage_allows_host_speaking(MeetingFlowStage.END_CONFIRMATION)


@pytest.mark.asyncio
async def test_apply_interruption_persists_user_message_and_rollback(store):
    meeting = await store.create_meeting(
        "m1", session_id="s", topic="t", participants=[{"agent_id": "fe", "name": "Frontend"}]
    )
    assert meeting is not None
    await store.update_meeting(
        "m1",
        execution_plan_json=[p.model_dump() for p in _plan()],
        flow_stage=MeetingFlowStage.DISPATCHING.value,
    )
    result = await apply_interruption(store, "m1", "scope change: use microservices")

    assert result["verdict"].interrupt_type is InterruptType.SCOPE_CHANGE
    assert result["rolled_back"] == ["p1", "p2", "p3"]
    assert result["flow_stage"] == MeetingFlowStage.RECOVERY.value
    assert not result["should_end"]
    messages = await store.list_messages("m1")
    assert messages[-1]["speaker_kind"] == "user"
    updated = await store.get_meeting("m1")
    assert all(p["status"] == "needs_rediscussion" for p in updated["execution_plan"])


@pytest.mark.asyncio
async def test_end_request_sets_end_confirmation(store):
    await store.create_meeting(
        "m2", session_id="s", topic="t", participants=[{"agent_id": "fe", "name": "Frontend"}]
    )
    result = await apply_interruption(store, "m2", "end the meeting")
    assert result["should_end"]
    assert result["flow_stage"] == MeetingFlowStage.END_CONFIRMATION.value
