"""Plan/Replan loop closure: the planner, the replanner and the goal gate."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
import importlib

# The package re-exports each StructuredTool under its module's name, so the
# MODULES (where the helpers live) must be imported by path.
finish_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_finish")
plan_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_plan")
replan_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_replan")
from agent.tools.taskflow.tools._shared import new_step, step_status

pytestmark = [pytest.mark.unit]

_SESSION = "sess-loop"


class _FakeLLM:
    """One-shot planner: ``ainvoke`` returns a scripted answer."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[list[dict]] = []

    async def ainvoke(self, messages, **_kwargs):
        self.prompts.append(messages)
        return SimpleNamespace(content=self.answer)


@pytest.fixture()
def planner(monkeypatch):
    def _install(answer) -> _FakeLLM:
        llm = _FakeLLM(answer if isinstance(answer, str) else json.dumps(answer))
        monkeypatch.setattr("models.build_auxiliary_llm", lambda **_kw: llm)
        return llm

    return _install


async def _seed_flow(flow_id: str, steps: list[dict], *, description: str = "goal") -> None:
    await store_sqlite.create_flow(
        flow_id,
        {"description": description, "steps": steps, "results": []},
        session_id=_SESSION,
    )


# ---------------------------------------------------------------------------
# transitive invalidation (senpi-task's amendRun idea)
# ---------------------------------------------------------------------------


def test_transitive_dependents_walks_the_whole_chain():
    steps = [
        new_step("a", "a"),
        new_step("b", "b", depends_on=["a"]),
        new_step("c", "c", depends_on=["b"]),
        new_step("d", "d"),
    ]

    assert replan_mod._transitive_dependents(steps, {"a"}) == {"b", "c"}


def test_transitive_dependents_terminates_on_a_cycle():
    steps = [
        new_step("a", "a", depends_on=["b"]),
        new_step("b", "b", depends_on=["a"]),
    ]

    assert replan_mod._transitive_dependents(steps, {"a"}) == {"a", "b"}


# ---------------------------------------------------------------------------
# taskflow_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_creates_the_flow_without_dispatching(isolated_db, planner):
    planner(
        [
            {"step_id": "step-1", "task": "collect", "depends_on": []},
            {"step_id": "step-2", "task": "write", "depends_on": ["step-1"]},
        ]
    )

    out = await plan_mod.taskflow_plan.coroutine(
        goal="ship the report", flow_id="flow-plan", session_id=_SESSION
    )

    assert "TaskFlow planned" in out, out
    assert "nothing dispatched" in out
    flow = await store_sqlite.get_flow("flow-plan", _SESSION)
    assert flow is not None
    steps = flow["state"]["steps"]
    assert [step["step_id"] for step in steps] == ["step-1", "step-2"]
    # No dependencies -> ready; with dependencies -> blocked until unlocked.
    assert step_status(steps[0]) == StepStatus.READY
    assert step_status(steps[1]) == StepStatus.BLOCKED
    assert not [s for s in steps if step_status(s) == StepStatus.DISPATCHED]


@pytest.mark.asyncio
async def test_plan_drops_dependencies_on_unknown_steps(isolated_db, planner):
    planner([{"step_id": "step-1", "task": "x", "depends_on": ["ghost"]}])

    await plan_mod.taskflow_plan.coroutine(goal="g", flow_id="flow-ghost", session_id=_SESSION)

    flow = await store_sqlite.get_flow("flow-ghost", _SESSION)
    assert flow is not None
    step = flow["state"]["steps"][0]
    assert step["depends_on"] == []
    assert step_status(step) == StepStatus.READY


@pytest.mark.asyncio
async def test_plan_rejects_an_existing_flow_id(isolated_db, planner):
    planner([{"step_id": "step-1", "task": "x"}])
    await _seed_flow("flow-dup", [new_step("step-1", "existing")])

    out = await plan_mod.taskflow_plan.coroutine(goal="g", flow_id="flow-dup", session_id=_SESSION)

    assert out.startswith("Error: flow_id 'flow-dup' already exists")


@pytest.mark.asyncio
async def test_plan_reports_a_planner_failure(isolated_db, planner):
    planner("not json at all")

    out = await plan_mod.taskflow_plan.coroutine(goal="g", flow_id="flow-bad", session_id=_SESSION)

    assert out.startswith("Error: plan generation failed")
    assert await store_sqlite.get_flow("flow-bad", _SESSION) is None


# ---------------------------------------------------------------------------
# taskflow_replan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replan_keeps_done_steps_and_replaces_the_rest(isolated_db, planner):
    done = new_step("step-1", "collect", status=StepStatus.DONE)
    failed = new_step("step-2", "broken", depends_on=["step-1"], status=StepStatus.FAILED)
    downstream = new_step("step-3", "after", depends_on=["step-2"], status=StepStatus.BLOCKED)
    await _seed_flow("flow-re", [done, failed, downstream])
    planner(
        [
            {"step_id": "step-2", "task": "fixed", "depends_on": ["step-1"]},
            {"step_id": "step-3", "task": "after", "depends_on": ["step-2"]},
        ]
    )

    out = await replan_mod.taskflow_replan.coroutine(
        flow_id="flow-re", reason="step-2 failed", blocked_step_id="step-2", session_id=_SESSION
    )

    assert "TaskFlow replanned" in out, out
    assert "replan=1/3" in out
    flow = await store_sqlite.get_flow("flow-re", _SESSION)
    assert flow is not None
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert step_status(by_id["step-1"]) == StepStatus.DONE  # kept
    assert by_id["step-2"]["task"] == "fixed"  # replaced
    assert flow["state"]["replan_count"] == 1


@pytest.mark.asyncio
async def test_replan_refuses_past_the_budget(isolated_db, planner):
    planner([{"step_id": "step-2", "task": "x", "depends_on": ["step-1"]}])
    done = new_step("step-1", "collect", status=StepStatus.DONE)
    failed = new_step("step-2", "broken", depends_on=["step-1"], status=StepStatus.FAILED)
    await _seed_flow("flow-max", [done, failed])
    for _ in range(replan_mod.REPLAN_MAX):
        flow = await store_sqlite.get_flow("flow-max", _SESSION)
        assert flow is not None
        state = dict(flow["state"])
        state["replan_count"] = int(state.get("replan_count") or 0) + 1
        await store_sqlite.update_flow(
            "flow-max", flow["expected_revision"], session_id=_SESSION, state=state
        )

    out = await replan_mod.taskflow_replan.coroutine(flow_id="flow-max", session_id=_SESSION)

    assert out.startswith("Error:")
    assert "taskflow_fail" in out


@pytest.mark.asyncio
async def test_replan_keeps_in_flight_steps(isolated_db, planner):
    done = new_step("step-1", "collect", status=StepStatus.DONE)
    in_flight = new_step("step-2", "running", depends_on=["step-1"], status=StepStatus.DISPATCHED)
    in_flight["child_session_key"] = "child-live"
    await _seed_flow("flow-live", [done, in_flight])
    planner([{"step_id": "step-3", "task": "extra", "depends_on": ["step-1"]}])

    await replan_mod.taskflow_replan.coroutine(flow_id="flow-live", session_id=_SESSION)

    flow = await store_sqlite.get_flow("flow-live", _SESSION)
    assert flow is not None
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert step_status(by_id["step-2"]) == StepStatus.DISPATCHED
    assert by_id["step-2"]["child_session_key"] == "child-live"


# ---------------------------------------------------------------------------
# Gate E — the goal judge
# ---------------------------------------------------------------------------


def _flow_with_results(description: str = "ship it") -> dict:
    return {
        "flow_id": "f",
        "state": {"description": description, "results": [{"step_id": "s1", "result": "done"}]},
    }


@pytest.mark.asyncio
async def test_goal_gate_passes_on_goal_met(planner):
    planner("GOAL_MET: the report is shipped")

    assert await finish_mod._goal_gate(_flow_with_results()) is None


@pytest.mark.asyncio
async def test_goal_gate_rejects_and_points_at_replan(planner):
    planner("GOAL_NOT_MET: the appendix is missing")

    result = await finish_mod._goal_gate(_flow_with_results())

    assert result is not None
    assert "appendix" in result
    assert "taskflow_replan" in result


@pytest.mark.asyncio
async def test_goal_gate_fails_open_on_a_judge_error(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr("models.build_auxiliary_llm", _boom)

    assert await finish_mod._goal_gate(_flow_with_results()) is None


@pytest.mark.asyncio
async def test_goal_gate_skips_a_flow_without_a_description(planner):
    assert await finish_mod._goal_gate({"state": {"results": [{"result": "x"}]}}) is None


@pytest.mark.asyncio
async def test_goal_gate_honours_the_switch(monkeypatch, planner):
    monkeypatch.setitem(finish_mod.GOAL_GATE, "enabled", False)
    planner("GOAL_NOT_MET: nope")

    assert await finish_mod._goal_gate(_flow_with_results()) is None
