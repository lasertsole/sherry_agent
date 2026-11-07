"""``taskflow_plan`` starts every initial ready step, not only the first one.

The planner's no-dependency steps are the parallel siblings a single-branch run
forgets. These tests pin the automatic start, the dependency boundary (blocked
steps stay blocked), the config opt-out, and the fail-open path that leaves a
failed spawn ready for a manual retry / finish's Gate A.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools._shared import step_status
import importlib

plan_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_plan")
finish_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_finish")

pytestmark = [pytest.mark.unit]

_SESSION = "sess-plan-auto"


class _FakeLLM:
    def __init__(self, answer: list[dict] | str) -> None:
        self.answer = answer if isinstance(answer, str) else json.dumps(answer)

    async def ainvoke(self, _messages, **_kwargs):
        return SimpleNamespace(content=self.answer)


@pytest.fixture()
def planner(monkeypatch):
    def _install(answer: list[dict] | str) -> None:
        llm = _FakeLLM(answer)
        monkeypatch.setattr("models.build_taskflow_llm", lambda **_kw: llm)

    return _install


async def _plan(goal: str, flow_id: str) -> str:
    return await plan_mod.taskflow_plan.coroutine(goal=goal, flow_id=flow_id, session_id=_SESSION)


@pytest.mark.asyncio
async def test_plan_dispatches_every_initial_ready_sibling(
    isolated_db, planner, _isolated_child_dispatch
):
    planner(
        [
            {"step_id": "s1", "task": "implement", "depends_on": []},
            {"step_id": "s2", "task": "test", "depends_on": []},
            {"step_id": "s3", "task": "docs", "depends_on": []},
            {"step_id": "s4", "task": "integrate", "depends_on": ["s1", "s2", "s3"]},
        ]
    )

    out = await _plan("ship it", "flow-auto")

    assert "TaskFlow planned" in out, out
    assert "auto_dispatched" in out or "auto-dispatched" in out, out
    flow = await store_sqlite.get_flow("flow-auto", _SESSION)
    assert flow is not None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert {by_id[sid]["status"] for sid in ("s1", "s2", "s3")} == {StepStatus.DISPATCHED.value}
    assert by_id["s4"]["status"] == StepStatus.BLOCKED.value
    # One spawn per initial ready step, and each got its own child key.
    assert len(_isolated_child_dispatch) == 3
    assert len({by_id[sid]["child_session_key"] for sid in ("s1", "s2", "s3")}) == 3


@pytest.mark.asyncio
async def test_plan_can_opt_out_of_initial_auto_dispatch(
    isolated_db, planner, _isolated_child_dispatch, monkeypatch
):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "auto_dispatch_initial_ready", False)
    planner([{"step_id": "s1", "task": "implement", "depends_on": []}])

    out = await _plan("review first", "flow-manual")

    assert "disabled by config" in out, out
    assert _isolated_child_dispatch == []
    flow = await store_sqlite.get_flow("flow-manual", _SESSION)
    assert flow is not None
    assert step_status(flow["state"]["steps"][0]) == StepStatus.READY


@pytest.mark.asyncio
async def test_a_failed_initial_spawn_leaves_the_step_ready(
    isolated_db, planner, _isolated_child_dispatch, monkeypatch
):
    from agent.tools.taskflow.tools import _dispatch

    async def _reject(*_args, **_kwargs):
        raise RuntimeError("subagent lane rejected")

    monkeypatch.setattr(_dispatch, "dispatch_child", _reject)
    planner([{"step_id": "s1", "task": "implement", "depends_on": []}])

    out = await _plan("retry later", "flow-fail")

    assert "Error: dispatch failed" in out, out
    flow = await store_sqlite.get_flow("flow-fail", _SESSION)
    assert flow is not None
    assert step_status(flow["state"]["steps"][0]) == StepStatus.READY


@pytest.mark.asyncio
async def test_finish_gate_a_is_the_fallback_after_a_failed_auto_dispatch(
    isolated_db, planner, _isolated_child_dispatch, monkeypatch
):
    from agent.tools.taskflow.tools import _dispatch

    async def _reject(*_args, **_kwargs):
        raise RuntimeError("subagent lane rejected")

    monkeypatch.setattr(_dispatch, "dispatch_child", _reject)
    planner([{"step_id": "s1", "task": "implement", "depends_on": []}])
    await _plan("finish fallback", "flow-gate")

    out = await finish_mod.taskflow_finish.coroutine(
        flow_id="flow-gate", summary="trying to finish", session_id=_SESSION
    )

    assert out.startswith("Error: Cannot finish"), out
    assert "have not started" in out
