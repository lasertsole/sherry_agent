"""Step result validation criteria plumbing.

``taskflow_run_task`` accepts natural-language ``validation_criteria`` and
stores them on the step. ``taskflow_resume`` judges the injected result against
those criteria with the auxiliary-LLM :mod:`step_judge` (PASS -> done, RETRY ->
re-dispatch, BLOCK -> blocked). ``taskflow_summary`` surfaces the criteria so
the verdict stays visible.

Backward compatibility is pinned: without criteria, every response keeps its
legacy shape, no ``validation_criteria`` key is written to the step, and the
judge is never consulted.

The dispatch and judge seams are monkeypatched; neither the real spawn pipeline
nor a real LLM is ever invoked.
"""

import sys

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.step_judge import JudgeResult, StepVerdict
from agent.tools.taskflow.tools._shared import step_status

_SESSION = "sess-1"

pytestmark = [pytest.mark.unit]

taskflow_dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]
taskflow_resume_module = sys.modules["agent.tools.taskflow.tools.taskflow_resume"]

_CRITERIA = "output must contain PASS and must not contain ERROR"


def _fake_dispatch(child_key: str):
    async def _dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        return child_key

    return _dispatch


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _patch_judge_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _judge(
        step_task: str,
        criteria: str | None,
        result_text: str,
        evidence_summary: str | None = None,
    ) -> JudgeResult:
        return JudgeResult(StepVerdict.PASS, "criteria met", "")

    monkeypatch.setattr(taskflow_resume_module, "judge_step_result", _judge)
    monkeypatch.setattr(taskflow_resume_module, "collect_evidence_summary", lambda **_: None)


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
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="validation probe"
    )

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="write report",
        validation_criteria=_CRITERIA,
        session_id=_SESSION,
    )
    assert "child-1" in out

    flow = await store_sqlite.get_flow("flow-1", _SESSION)
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
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="blocked probe"
    )
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="step one", session_id=_SESSION
    )

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="step two",
        depends_on=["step-1"],
        validation_criteria=_CRITERIA,
        session_id=_SESSION,
    )
    assert "blocked" in out

    flow = await store_sqlite.get_flow("flow-1", _SESSION)
    assert flow is not None
    blocked = flow["state"]["steps"][1]
    assert step_status(blocked) == StepStatus.BLOCKED
    assert blocked["validation_criteria"] == _CRITERIA


# ---------------------------------------------------------------------------
# resume: criteria are judged, step reflects the verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_judges_criteria_pass_and_marks_done(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    _patch_judge_pass(monkeypatch)
    tools = _tools()
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="resume probe"
    )
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="write report",
        validation_criteria=_CRITERIA,
        session_id=_SESSION,
    )

    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="PASS token present",
    )
    assert "TaskFlow resumed" in out
    assert f"\n  validation_criteria: {_CRITERIA}" in out
    assert "\n  judge: PASS criteria met" in out
    assert "needs validation" not in out

    flow = await store_sqlite.get_flow("flow-1", _SESSION)
    assert flow is not None
    assert step_status(flow["state"]["steps"][0]) == StepStatus.DONE


@pytest.mark.asyncio
async def test_resume_accepts_criteria_param_for_dispatch_without_criteria(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        taskflow_dispatch_module, "dispatch_child", _fake_dispatch("agent:main:subagent:child-1")
    )
    _patch_judge_pass(monkeypatch)
    tools = _tools()
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="late criteria probe"
    )
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="write report", session_id=_SESSION
    )

    criteria = "must be valid JSON"
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result='{"ok": true}',
        validation_criteria=criteria,
    )
    assert f"\n  validation_criteria: {criteria}" in out
    assert "\n  judge: PASS criteria met" in out
    assert "needs validation" not in out

    flow = await store_sqlite.get_flow("flow-1", _SESSION)
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
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="compat probe"
    )

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="plain step", session_id=_SESSION
    )
    assert "child-1" in out

    flow = await store_sqlite.get_flow("flow-1", _SESSION)
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
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="compat probe"
    )
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="plain step", session_id=_SESSION
    )

    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
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
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-1", description="summary probe"
    )
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="write report",
        validation_criteria=_CRITERIA,
        session_id=_SESSION,
    )

    out = await tools["taskflow_summary"].coroutine(session_id=_SESSION, flow_id="flow-1")
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
        session_id=_SESSION,
    )

    tools = _tools()
    out = await tools["taskflow_summary"].coroutine(session_id=_SESSION, flow_id="flow-legacy")
    assert "validation_criteria" not in out
