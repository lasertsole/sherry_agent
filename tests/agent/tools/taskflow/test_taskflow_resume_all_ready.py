"""A resume starts every ready step, not only the dependencies it unlocked.

The sibling branch in these flows was ready before the resume and had never been
dispatched. The all-ready scan is what prevents a single-branch run from parking
it until ``taskflow_finish``'s Gate A. The config switch and the no-double-spawn
guard are pinned here too.
"""

from __future__ import annotations

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-resume-all"
_ROOT_CHILD = "agent:main:subagent:root"


def _step(
    step_id: str,
    status: str,
    *,
    task: str = "",
    child: str = "",
    depends_on: list[str] | None = None,
) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": task or f"task-{step_id}",
        "depends_on": depends_on or [],
        "status": status,
    }
    if child:
        step["child_session_key"] = child
    return step


async def _seed_flow(flow_id: str, steps: list[dict]) -> None:
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="sibling resume probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


@pytest.mark.asyncio
async def test_resume_dispatches_unlocked_downstream_and_ready_sibling(
    isolated_db, _isolated_child_dispatch
):
    await _seed_flow(
        "flow-siblings",
        [
            _step("root", "dispatched", child=_ROOT_CHILD),
            _step("sibling", "ready", task="sibling branch"),
            _step("downstream", "blocked", depends_on=["root"]),
        ],
    )

    out = await taskflow_resume.coroutine(
        flow_id="flow-siblings",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    assert "TaskFlow resumed" in out, out
    flow = await store_sqlite.get_flow("flow-siblings", _SESSION)
    assert flow is not None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert by_id["root"]["status"] == "done"
    assert by_id["sibling"]["status"] == "dispatched"
    assert by_id["downstream"]["status"] == "dispatched"
    assert by_id["sibling"]["child_session_key"]
    assert by_id["downstream"]["child_session_key"]
    assert len(_isolated_child_dispatch) == 2
    assert "auto_dispatched=[downstream,sibling]" in out, out


@pytest.mark.asyncio
async def test_the_all_ready_scan_can_be_opted_out(
    isolated_db, _isolated_child_dispatch, monkeypatch
):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "auto_dispatch_all_ready_on_resume", False)
    await _seed_flow(
        "flow-downstream-only",
        [
            _step("root", "dispatched", child=_ROOT_CHILD),
            _step("sibling", "ready", task="sibling branch"),
            _step("downstream", "blocked", depends_on=["root"]),
        ],
    )

    out = await taskflow_resume.coroutine(
        flow_id="flow-downstream-only",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    flow = await store_sqlite.get_flow("flow-downstream-only", _SESSION)
    assert flow is not None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert by_id["downstream"]["status"] == "dispatched"
    assert by_id["sibling"]["status"] == "ready"
    assert len(_isolated_child_dispatch) == 1
    assert "auto_dispatched=[downstream]" in out, out


@pytest.mark.asyncio
async def test_an_already_dispatched_sibling_is_not_spawned_twice(
    isolated_db, _isolated_child_dispatch
):
    await _seed_flow(
        "flow-no-double",
        [
            _step("root", "dispatched", child=_ROOT_CHILD),
            _step("sibling", "dispatched", child="agent:main:subagent:sibling"),
            _step("downstream", "blocked", depends_on=["root"]),
        ],
    )

    await taskflow_resume.coroutine(
        flow_id="flow-no-double",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    assert len(_isolated_child_dispatch) == 1, "only the unlocked downstream is spawned"
    flow = await store_sqlite.get_flow("flow-no-double", _SESSION)
    assert flow is not None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert by_id["sibling"]["child_session_key"] == "agent:main:subagent:sibling"


@pytest.mark.asyncio
async def test_a_sibling_spawn_failure_does_not_block_the_unlocked_downstream(
    isolated_db, _isolated_child_dispatch, monkeypatch
):
    async def _reject_sibling(task: str, requester_session_key: str, **_kwargs) -> str:
        if "sibling branch" in task:
            raise RuntimeError("lane rejected")
        return "agent:auto:subagent:ok"

    monkeypatch.setattr(_dispatch, "dispatch_child", _reject_sibling)
    await _seed_flow(
        "flow-partial",
        [
            _step("root", "dispatched", child=_ROOT_CHILD),
            _step("sibling", "ready", task="sibling branch"),
            _step("downstream", "blocked", depends_on=["root"]),
        ],
    )

    out = await taskflow_resume.coroutine(
        flow_id="flow-partial",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    flow = await store_sqlite.get_flow("flow-partial", _SESSION)
    assert flow is not None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert by_id["downstream"]["status"] == "dispatched"
    assert by_id["sibling"]["status"] == "ready"
    assert "auto_dispatch_failed=" in out
