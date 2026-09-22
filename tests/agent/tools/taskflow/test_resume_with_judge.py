"""taskflow_resume StepJudge integration + dispatch feedback injection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.step_judge import JudgeResult, StepVerdict

_SESSION = "sess-1"
_CRITERIA = "output must contain PASS and must not contain ERROR"

pytestmark = [pytest.mark.unit]

dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]
resume_module = sys.modules["agent.tools.taskflow.tools.taskflow_resume"]


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _key_dispatch(keys: list[str], calls: list | None = None):
    iterator = iter(keys)

    async def _dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        if calls is not None:
            calls.append(task)
        return next(iterator)

    return _dispatch


def _patch_judge(
    monkeypatch: pytest.MonkeyPatch,
    verdict: StepVerdict,
    *,
    reason: str = "judged",
    feedback: str = "",
    seen_criteria: list | None = None,
) -> None:
    async def _fake(
        step_task: str,
        criteria: str | None,
        result_text: str,
        evidence_summary: str | None = None,
    ) -> JudgeResult:
        if seen_criteria is not None:
            seen_criteria.append((step_task, criteria, result_text))
        return JudgeResult(verdict, reason, feedback)

    monkeypatch.setattr(resume_module, "judge_step_result", _fake)
    monkeypatch.setattr(resume_module, "collect_evidence_summary", lambda **_: None)


async def _create_dispatched(tools: dict, flow_id: str, task: str = "write report") -> None:
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id=flow_id, description="judge probe"
    )
    out = await tools["taskflow_run_task"].coroutine(
        flow_id=flow_id, task=task, validation_criteria=_CRITERIA, session_id=_SESSION
    )
    assert "dispatched" in out, out


async def _flow(flow_id: str) -> dict:
    flow = await store_sqlite.get_flow(flow_id, _SESSION)
    assert flow is not None
    return flow


# ---------------------------------------------------------------------------
# PASS -> done (legacy DONE semantics), dependents unlocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_marks_step_done_and_unlocks_dependents(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1"]),
    )
    seen: list = []
    _patch_judge(monkeypatch, StepVerdict.PASS, reason="criteria met", seen_criteria=seen)
    tools = _tools()
    await _create_dispatched(tools, "flow-1")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="dependent", depends_on=["step-1"], session_id=_SESSION
    )

    before = await _flow("flow-1")
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="PASS token present",
    )

    assert "judge: PASS criteria met" in out, out
    assert "needs validation" not in out
    assert "⚠" not in out
    assert "unlocked=[step-2]" in out, out
    assert seen[0][1] == _CRITERIA

    flow = await _flow("flow-1")
    # Persisted: the resume write bumped the revision and the step is done.
    assert flow["expected_revision"] == before["expected_revision"] + 1
    assert flow["state"]["steps"][0]["status"] == StepStatus.DONE
    assert flow["state"]["steps"][1]["status"] == StepStatus.READY


# ---------------------------------------------------------------------------
# RETRY within budget -> re-dispatch via the existing _retry helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_redispatches_and_persists_feedback(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list = []
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1", "agent:main:subagent:child-2"], calls),
    )
    _patch_judge(
        monkeypatch,
        StepVerdict.RETRY,
        reason="missing PASS token",
        feedback="emit the PASS token",
    )
    tools = _tools()
    await _create_dispatched(tools, "flow-1")

    before = await _flow("flow-1")
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="no token",
    )

    assert "judge: RETRY (1/2)" in out, out
    assert len(calls) == 2  # run_task spawn + judge re-dispatch
    # The in-place re-dispatch must carry the judge's guidance: a step marked
    # DISPATCHED never goes through taskflow_dispatch again, so this is the only
    # path that can hand the replacement child the feedback.
    assert calls[1].startswith("write report")
    assert "## Previous Attempt Feedback" in calls[1]
    assert "emit the PASS token" in calls[1]

    flow = await _flow("flow-1")
    assert flow["expected_revision"] == before["expected_revision"] + 1
    step = flow["state"]["steps"][0]
    assert step["status"] == StepStatus.DISPATCHED
    assert step["child_session_key"] == "agent:main:subagent:child-2"
    assert step["retry_count"] == 1  # reuses the existing counter, no new budget
    assert step["judge_feedback"] == "emit the PASS token"


# ---------------------------------------------------------------------------
# RETRY with the budget exhausted -> BLOCKED (fail-open to human review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_budget_exhausted_blocks_step(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1"]),
    )
    _patch_judge(monkeypatch, StepVerdict.RETRY, reason="still wrong", feedback="try again")
    tools = _tools()
    await _create_dispatched(tools, "flow-1")

    flow = await _flow("flow-1")
    state = dict(flow["state"])
    state["steps"][0]["retry_count"] = 2  # equal to STEP_JUDGE["max_retries"]
    await store_sqlite.update_flow(
        "flow-1", flow["expected_revision"], session_id=_SESSION, state=state
    )

    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="still no token",
    )

    assert "judge: BLOCKED retry budget exhausted" in out, out
    step = (await _flow("flow-1"))["state"]["steps"][0]
    assert step["status"] == StepStatus.BLOCKED
    assert "budget exhausted" in step["block_reason"]


# ---------------------------------------------------------------------------
# BLOCK -> step blocked with a reason, persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_marks_step_blocked(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1"]),
    )
    _patch_judge(monkeypatch, StepVerdict.BLOCK, reason="dependency missing")
    tools = _tools()
    await _create_dispatched(tools, "flow-1")

    before = await _flow("flow-1")
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="cannot proceed",
    )

    assert "judge: BLOCKED dependency missing" in out, out
    flow = await _flow("flow-1")
    assert flow["expected_revision"] == before["expected_revision"] + 1
    step = flow["state"]["steps"][0]
    assert step["status"] == StepStatus.BLOCKED
    assert step["block_reason"].startswith("StepJudge blocked:")


# ---------------------------------------------------------------------------
# fail-open: judge unavailable -> PASS -> step done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_failure_fails_open_to_done(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1"]),
    )
    _patch_judge(monkeypatch, StepVerdict.PASS, reason="judge error (fail-open): boom")
    tools = _tools()
    await _create_dispatched(tools, "flow-1")

    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="agent:main:subagent:child-1",
        result="whatever",
    )

    assert "judge: PASS" in out, out
    step = (await _flow("flow-1"))["state"]["steps"][0]
    assert step["status"] == StepStatus.DONE


# ---------------------------------------------------------------------------
# no criteria -> judge is never consulted (legacy path preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_criteria_skips_judge(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1"]),
    )

    async def _boom(*args, **kwargs):
        raise AssertionError("judge must not run without validation_criteria")

    monkeypatch.setattr(resume_module, "judge_step_result", _boom)
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

    assert "TaskFlow resumed" in out, out
    assert "validation_criteria" not in out
    assert "judge:" not in out
    step = (await _flow("flow-1"))["state"]["steps"][0]
    assert step["status"] == StepStatus.DONE


# ---------------------------------------------------------------------------
# dispatch consumes judge_feedback and clears it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_injects_and_clears_judge_feedback(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list = []
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-9"], calls),
    )
    tools = _tools()
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        description="feedback probe",
        initial_state={
            "steps": [
                {
                    "step_id": "step-1",
                    "task": "write report",
                    "depends_on": [],
                    "status": str(StepStatus.READY),
                    "retry_count": 0,
                    "judge_feedback": "emit the PASS token",
                }
            ]
        },
    )

    out = await tools["taskflow_dispatch"].coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )
    assert "Error" not in out, out

    assert len(calls) == 1
    assert "## Previous Attempt Feedback" in calls[0]
    assert "emit the PASS token" in calls[0]

    step = (await _flow("flow-1"))["state"]["steps"][0]
    assert step["status"] == StepStatus.DISPATCHED
    assert "judge_feedback" not in step
