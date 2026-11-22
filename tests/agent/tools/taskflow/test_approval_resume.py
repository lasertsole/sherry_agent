"""Approval resume: re-dispatch a step whose child bubbled [NEEDS_APPROVAL]."""

from __future__ import annotations

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools._retry import step_spawn_kwargs
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_SESSION = "sess-approval-resume"
_OLD_CHILD = "agent:main:subagent:old"


def _step(step_id: str, status: str, *, child: str = "", depends_on=None) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": f"task-{step_id}",
        "depends_on": depends_on or [],
        "status": status,
    }
    if child:
        step["child_session_key"] = child
    return step


async def _seed(flow_id: str, steps: list[dict]) -> None:
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="approval resume probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


@pytest.mark.asyncio
async def test_approved_context_redispatches_the_step(isolated_db, _isolated_child_dispatch):
    await _seed(
        "flow-approve",
        [
            _step("s1", "dispatched", child=_OLD_CHILD),
            _step("s2", "blocked", depends_on=["s1"]),
        ],
    )

    out = await taskflow_resume.coroutine(
        flow_id="flow-approve",
        step_id="s1",
        step_outcome="approved",
        approval_context={"keyword": "payment", "tool": "terminal"},
        session_id=_SESSION,
    )

    assert "approved and re-dispatched" in out, out
    assert _isolated_child_dispatch, "the approved step must spawn a replacement"
    spawned = _isolated_child_dispatch[0]
    assert spawned["step_id"] == "s1"
    assert spawned["approval_context"]["keyword"] == "payment"
    assert spawned["approval_context"]["tool"] == "terminal"

    flow = await store_sqlite.get_flow("flow-approve", _SESSION)
    assert flow is not None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert by_id["s1"]["status"] == "dispatched"
    assert by_id["s1"]["child_session_key"] != _OLD_CHILD
    assert by_id["s1"]["approval_context"]["keyword"] == "payment"
    assert by_id["s1"]["retry_count"] == 1
    assert by_id["s2"]["status"] == "blocked", "the dependent waits for the new child"


@pytest.mark.asyncio
async def test_a_retry_of_an_approved_step_keeps_the_approval(
    isolated_db, _isolated_child_dispatch
):
    await _seed("flow-approve-retry", [_step("s1", "dispatched", child=_OLD_CHILD)])
    await taskflow_resume.coroutine(
        flow_id="flow-approve-retry",
        step_id="s1",
        step_outcome="approved",
        approval_context={"keyword": "payment", "tool": "mcp__pay__charge"},
        session_id=_SESSION,
    )
    flow = await store_sqlite.get_flow("flow-approve-retry", _SESSION)
    step = flow["state"]["steps"][0]
    kwargs = step_spawn_kwargs(step, flow_id="flow-approve-retry")
    assert kwargs["approval_context"]["keyword"] == "payment"


@pytest.mark.asyncio
async def test_approved_requires_an_approval_context(isolated_db):
    await _seed("flow-approve-missing", [_step("s1", "dispatched", child=_OLD_CHILD)])
    out = await taskflow_resume.coroutine(
        flow_id="flow-approve-missing", step_id="s1", step_outcome="approved", session_id=_SESSION
    )
    assert "requires approval_context" in out


@pytest.mark.asyncio
async def test_approval_context_requires_the_approved_outcome(isolated_db):
    await _seed("flow-approve-outcome", [_step("s1", "dispatched", child=_OLD_CHILD)])
    out = await taskflow_resume.coroutine(
        flow_id="flow-approve-outcome",
        step_id="s1",
        approval_context={"keyword": "payment"},
        session_id=_SESSION,
    )
    assert "requires step_outcome" in out


@pytest.mark.asyncio
async def test_a_done_step_cannot_be_approved_again(isolated_db):
    await _seed("flow-approve-done", [_step("s1", "done", child=_OLD_CHILD)])
    out = await taskflow_resume.coroutine(
        flow_id="flow-approve-done",
        step_id="s1",
        step_outcome="approved",
        approval_context={"keyword": "payment"},
        session_id=_SESSION,
    )
    assert "already done" in out


@pytest.mark.asyncio
async def test_unsatisfied_dependencies_block_the_approval(isolated_db):
    await _seed(
        "flow-approve-deps",
        [
            _step("s1", "blocked"),
            _step("s2", "dispatched", child=_OLD_CHILD, depends_on=["s1"]),
        ],
    )
    out = await taskflow_resume.coroutine(
        flow_id="flow-approve-deps",
        step_id="s2",
        step_outcome="approved",
        approval_context={"keyword": "payment"},
        session_id=_SESSION,
    )
    assert "dependencies are not satisfied" in out
