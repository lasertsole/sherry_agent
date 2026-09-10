"""DAG behavior tests for taskflow_resume (todo 4, taskflow-dag-phase1).

Resume must, after injecting a result:
  (a) mark the matching dispatched step ``done``;
  (b) unlock a dependent ``blocked`` step to ``ready`` and report its id;
  (c) leave a dependent ``blocked`` while another dep is still incomplete;
  (d) PARTIAL COMPLETION: with two dispatched steps, resuming one leaves the
      sibling ``dispatched``;
  (e) a duplicate resume is a byte-identical no-op (no revision bump, no
      re-unlock).

The dispatch seam is monkeypatched; the real spawn pipeline is never invoked.
Blocked dependents are seeded directly into state because ``depends_on`` on
``taskflow_run_task`` belongs to a sibling todo; this suite pins resume's
contract independently.
"""

import asyncio
import sys

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools._shared import new_step, step_status

pytestmark = [pytest.mark.unit]

taskflow_dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _fake_dispatch(child_key: str, calls: list | None = None):
    async def _dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        if calls is not None:
            calls.append((task, requester_session_key, label))
        return child_key

    return _dispatch


async def _seed_step(
    flow_id: str,
    *,
    step_id: str,
    task: str,
    status: StepStatus,
    depends_on: list[str] | None = None,
) -> None:
    """Append a step directly to the persisted state (sibling-todo-free seed)."""
    flow = await store_sqlite.get_flow(flow_id)
    assert flow is not None
    state = dict(flow["state"])
    steps = list(state.get("steps") or [])
    steps.append(new_step(step_id, task, depends_on=depends_on, status=status))
    state["steps"] = steps
    await store_sqlite.update_flow(flow_id, flow["expected_revision"], state=state)


async def _create_dispatched(tools: dict, flow_id: str, step_key: str, task: str) -> None:
    await tools["taskflow_create"].coroutine(flow_id=flow_id, description="dag probe")
    await tools["taskflow_run_task"].coroutine(flow_id=flow_id, task=task, session_id="sess-1")


# ---------------------------------------------------------------------------
# (a) resuming a dispatched step sets it done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_marks_dispatched_step_done(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await _create_dispatched(tools, "flow-1", "agent:main:subagent:child-1", "step one")

    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="R1",
    )
    assert "TaskFlow resumed" in out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert step_status(flow["state"]["steps"][0]) == StepStatus.DONE
    assert "done=1" in out


# ---------------------------------------------------------------------------
# (b) a dependent blocked step becomes ready and its id is reported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_unlocks_dependent_and_reports_id(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    calls: list = []
    monkeypatch.setattr(
        taskflow_dispatch_module,
        "dispatch_child",
        _fake_dispatch("agent:main:subagent:child-1", calls),
    )
    tools = _tools()
    await _create_dispatched(tools, "flow-1", "agent:main:subagent:child-1", "step one")
    await _seed_step(
        "flow-1",
        step_id="step-2",
        task="dependent",
        status=StepStatus.BLOCKED,
        depends_on=["step-1"],
    )

    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="R1",
    )
    assert "unlocked=[step-2]" in out, out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert step_status(flow["state"]["steps"][0]) == StepStatus.DONE
    assert step_status(flow["state"]["steps"][1]) == StepStatus.READY
    # resume must NEVER spawn the newly-ready step.
    assert len(calls) == 1
    assert "ready=1" in out


# ---------------------------------------------------------------------------
# (c) a dependent with another still-incomplete dep stays blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependent_with_incomplete_dep_stays_blocked(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await _create_dispatched(tools, "flow-1", "agent:main:subagent:child-1", "step one")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="step two", session_id="sess-1"
    )
    await _seed_step(
        "flow-1",
        step_id="step-3",
        task="needs both",
        status=StepStatus.BLOCKED,
        depends_on=["step-1", "step-2"],
    )

    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="R1",
    )
    assert "unlocked=[]" in out, out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert step_status(flow["state"]["steps"][2]) == StepStatus.BLOCKED
    assert "blocked=1" in out


# ---------------------------------------------------------------------------
# (d) PARTIAL COMPLETION: sibling dispatched step is untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_completion_leaves_sibling_dispatched(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    keys = iter(["agent:main:subagent:child-1", "agent:main:subagent:child-2"])

    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        return next(keys)

    monkeypatch.setattr(taskflow_dispatch_module, "dispatch_child", fake_dispatch)
    tools = _tools()
    await _create_dispatched(tools, "flow-1", "agent:main:subagent:child-1", "step one")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="step two", session_id="sess-1"
    )

    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="R1",
    )

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    steps = flow["state"]["steps"]
    assert step_status(steps[0]) == StepStatus.DONE
    assert step_status(steps[1]) == StepStatus.DISPATCHED
    # create + run_task + run_task + resume
    assert flow["expected_revision"] == 4
    assert "done=1" in out and "dispatched=1" in out


# ---------------------------------------------------------------------------
# (e) duplicate resume is byte-identical and does not bump the revision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_resume_is_byte_identical_noop(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await _create_dispatched(tools, "flow-1", "agent:main:subagent:child-1", "step one")
    await _seed_step(
        "flow-1",
        step_id="step-2",
        task="dependent",
        status=StepStatus.BLOCKED,
        depends_on=["step-1"],
    )

    first = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="R1",
    )
    assert "TaskFlow resumed" in first
    flow_after_first = await store_sqlite.get_flow("flow-1")
    assert flow_after_first is not None
    assert flow_after_first["expected_revision"] == 4  # create + run + seed + resume

    second = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="R1",
    )
    assert "already resumed" in second

    flow_after_second = await store_sqlite.get_flow("flow-1")
    assert flow_after_second is not None
    assert flow_after_second["expected_revision"] == flow_after_first["expected_revision"]
    assert flow_after_second["state"]["steps"] == flow_after_first["state"]["steps"]
    assert flow_after_second["state"]["results"] == flow_after_first["state"]["results"]
