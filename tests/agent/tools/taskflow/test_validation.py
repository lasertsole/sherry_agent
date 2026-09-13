"""GAP-7: step result validation criteria plumbing.

``taskflow_run_task`` accepts natural-language ``validation_criteria`` and
stores them on the step. ``taskflow_resume`` echoes the criteria back together
with the injected result so the orchestrator LLM can judge the child output
against them (the tools never call an LLM themselves). ``taskflow_summary``
surfaces the criteria so pending manual checks are visible.

Backward compatibility is pinned: without criteria, every response keeps its
pre-GAP-7 shape and no ``validation_criteria`` key is written to the step.

The dispatch seam is monkeypatched; the real spawn pipeline is never invoked.
"""

import sys

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools._shared import step_status

pytestmark = [pytest.mark.unit]

taskflow_dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]

_CRITERIA = "output must contain PASS and must not contain ERROR"


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _fake_dispatch(child_key: str):
    async def _dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        return child_key

    return _dispatch


# ---------------------------------------------------------------------------
# run_task: criteria are persisted on the step (both DAG paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_stores_validation_criteria_on_dispatched_step(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="validation probe")

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="write report",
        validation_criteria=_CRITERIA,
        session_id="sess-1",
    )
    assert "child-1" in out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    step = flow["state"]["steps"][0]
    assert step["validation_criteria"] == _CRITERIA


@pytest.mark.asyncio
async def test_run_task_stores_validation_criteria_on_blocked_step(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="blocked probe")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="step one", session_id="sess-1"
    )

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="step two",
        depends_on=["step-1"],
        validation_criteria=_CRITERIA,
        session_id="sess-1",
    )
    assert "blocked" in out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    blocked = flow["state"]["steps"][1]
    assert step_status(blocked) == StepStatus.BLOCKED
    assert blocked["validation_criteria"] == _CRITERIA


# ---------------------------------------------------------------------------
# resume: criteria are echoed with a validation warning, step stays done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_echoes_criteria_and_warning_after_marking_done(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="resume probe")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="write report",
        validation_criteria=_CRITERIA,
        session_id="sess-1",
    )

    # Adversarial (misleading_success_output): the child claims "SUCCESS!" but
    # never emits the required PASS token. The tool must NOT silently accept
    # it: it still marks the step done (the orchestrator owns the verdict) but
    # the response carries the criteria + explicit warning.
    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="SUCCESS! Everything is fine, trust me.",
    )
    assert "TaskFlow resumed" in out
    assert f"\n  validation_criteria: {_CRITERIA}" in out
    assert "\n  ⚠ Result needs validation against criteria" in out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert step_status(flow["state"]["steps"][0]) == StepStatus.DONE


@pytest.mark.asyncio
async def test_resume_accepts_criteria_param_for_dispatch_without_criteria(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="late criteria probe")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="write report", session_id="sess-1"
    )

    criteria = "must be valid JSON"
    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result='{"ok": true}',
        validation_criteria=criteria,
    )
    assert f"\n  validation_criteria: {criteria}" in out
    assert "\n  ⚠ Result needs validation against criteria" in out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["state"]["steps"][0]["validation_criteria"] == criteria


# ---------------------------------------------------------------------------
# backward compatibility: no criteria -> byte-identical legacy behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_without_criteria_stores_no_key(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="compat probe")

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="plain step", session_id="sess-1"
    )
    assert "child-1" in out

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert "validation_criteria" not in flow["state"]["steps"][0]


@pytest.mark.asyncio
async def test_resume_without_criteria_returns_legacy_response(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="compat probe")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="plain step", session_id="sess-1"
    )

    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="plain result",
    )
    assert "TaskFlow resumed" in out
    assert "validation_criteria" not in out
    assert "needs validation" not in out


# ---------------------------------------------------------------------------
# summary: criteria are visible while the orchestrator assesses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_shows_validation_criteria(isolated_db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="summary probe")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="write report",
        validation_criteria=_CRITERIA,
        session_id="sess-1",
    )

    out = await tools["taskflow_summary"].coroutine(flow_id="flow-1")
    assert f"validation_criteria={_CRITERIA}" in out


@pytest.mark.asyncio
async def test_summary_omits_validation_criteria_when_absent(isolated_db):
    await store_sqlite.create_flow(
        "flow-legacy",
        {
            "description": "legacy probe",
            "steps": [
                {
                    "step_id": "step-1",
                    "task": "legacy task",
                    "depends_on": [],
                    "status": "done",
                    "child_session_key": "child-a",
                },
            ],
            "results": [],
        },
    )

    tools = _tools()
    out = await tools["taskflow_summary"].coroutine(flow_id="flow-legacy")
    assert "validation_criteria" not in out
