"""阶段 4: meeting conclusion → TaskFlow plan and dispatch."""

from __future__ import annotations

from typing import Any

import pytest

from agent.meeting.conclusion import (
    build_conclusion,
    conclusion_from_phases,
    make_conclusion_builder,
    plan_to_steps,
)
from agent.meeting.execution import FlowsPort, execute_conclusion
from agent.meeting.models import (
    MeetingConclusion,
    MeetingPhase,
    MeetingStepSpec,
    TaskFlowPlanFromMeeting,
)

pytestmark = [pytest.mark.unit]


def _phases() -> list[MeetingPhase]:
    return [
        MeetingPhase(
            id="p1",
            allowed_agent_ids=["fe"],
            completion_criteria="design the frontend",
        ),
        MeetingPhase(
            id="p2",
            allowed_agent_ids=["be"],
            prerequisites=["p1"],
            completion_criteria="build the backend",
        ),
    ]


def test_fallback_conclusion_chains_phases_by_expert():
    conclusion = conclusion_from_phases("topic", "the summary", _phases())
    assert conclusion.summary == "the summary"
    assert [s.expert for s in conclusion.taskflow_plan.steps] == ["fe", "be"]
    assert [s.depends_on for s in conclusion.taskflow_plan.steps] == [[], ["step-p1"]]
    assert conclusion.taskflow_plan.description == "Plan from the meeting on 'topic'"


def test_plan_to_steps_builds_valid_taskflow_steps():
    conclusion = conclusion_from_phases("topic", "summary", _phases())
    steps = plan_to_steps(conclusion)
    assert [s["step_id"] for s in steps] == ["step-p1", "step-p2"]
    assert steps[1]["depends_on"] == ["step-p1"]
    assert steps[0]["expert"] == "fe"


def test_plan_to_steps_resolves_alias_and_drops_unknown_deps():
    conclusion = MeetingConclusion(
        summary="s",
        taskflow_plan=TaskFlowPlanFromMeeting(
            description="d",
            steps=[
                MeetingStepSpec(step_id="a", task="first", expert="fe"),
                MeetingStepSpec(
                    step_id="b", task="second", expert="be", depends_on=["first", "ghost"]
                ),
            ],
        ),
    )
    steps = plan_to_steps(conclusion)
    assert steps[1]["depends_on"] == ["a"]  # "first" resolved, "ghost" dropped


def test_structured_conclusion_seam_injected():
    fake = MeetingConclusion(
        summary="model summary",
        taskflow_plan=TaskFlowPlanFromMeeting(
            description="model plan",
            steps=[MeetingStepSpec(task="x", expert="fe")],
        ),
    )
    builder = make_conclusion_builder(llm=object())
    # A fake llm without with_structured_output degrades to fallback; prove the
    # explicit builder param is honored by build_conclusion instead.
    result = build_conclusion([], _phases(), "t", "s", builder=lambda *_: fake)
    assert result.summary == "model summary"
    assert "with_structured_output" not in dir(builder)  # sanity: builder is a function wrapper


@pytest.mark.asyncio
async def test_execute_conclusion_uses_injected_port():
    calls: list[dict[str, Any]] = []

    class FakePort(FlowsPort):
        async def execute(self, conclusion, *, meeting_id, session_id):
            calls.append(
                {
                    "meeting_id": meeting_id,
                    "session_id": session_id,
                    "steps": len(conclusion.taskflow_plan.steps),
                }
            )
            return {"flow_id": f"meeting-{meeting_id}", "step_ids": ["step-p1"]}

    conclusion = conclusion_from_phases("topic", "summary", _phases())
    result = await execute_conclusion(conclusion, meeting_id="m1", session_id="s1", port=FakePort())
    assert result["flow_id"] == "meeting-m1"
    assert calls == [{"meeting_id": "m1", "session_id": "s1", "steps": 2}]


@pytest.mark.asyncio
async def test_conclusion_dispatch_failure_is_surfaced():
    from agent.meeting.execution import _TaskFlowFlowsPort, TaskFlowPort

    class FailingDispatch(TaskFlowPort):
        async def get_flow(self, flow_id, session_id):
            return None

        async def create_flow(self, flow_id, state, *, session_id):
            return {"flow_id": flow_id}

        async def dispatch(self, flow_id, step_ids, *, session_id):
            raise RuntimeError("spawn failed")

    port = _TaskFlowFlowsPort(taskflow=FailingDispatch())
    result = await port.execute(
        conclusion_from_phases("t", "s", _phases()), meeting_id="m", session_id="s"
    )
    assert result["flow_id"] == "meeting-m"
    assert result["dispatched"] is None
    assert result["dispatch_error"] == "spawn failed"


@pytest.mark.asyncio
async def test_execute_is_idempotent_when_flow_exists():
    from agent.meeting.execution import _TaskFlowFlowsPort, TaskFlowPort

    class ExistingFlow(TaskFlowPort):
        async def get_flow(self, flow_id, session_id):
            return {"flow_id": flow_id, "state": {"steps": [{"step_id": "step-p1"}]}}

        async def create_flow(self, flow_id, state, *, session_id):
            raise AssertionError("must not create a duplicate flow")

        async def dispatch(self, flow_id, step_ids, *, session_id):
            raise AssertionError("must not dispatch an existing flow")

    result = await _TaskFlowFlowsPort(taskflow=ExistingFlow()).execute(
        conclusion_from_phases("t", "s", _phases()), meeting_id="m", session_id="s"
    )
    assert result["already_exists"] is True
    assert result["step_ids"] == ["step-p1"]
