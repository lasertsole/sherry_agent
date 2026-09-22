"""End-to-end aggregation across the taskflow dispatch / retry paths.

Exercises ``_shared.build_task_with_dep_results`` through the REAL tools:
taskflow_run_task's immediate dispatch, taskflow_dispatch's batch path, the
StepJudge RETRY re-dispatch and the retry-policy re-dispatch paths
(taskflow_resume + taskflow_wait_all). The dispatch seam
(``_dispatch.dispatch_child``), the StepJudge and wait_all's registry seam are
monkeypatched; the real spawn pipeline and LLM are never touched.

Regression invariant: without ``aggregate_deps`` the dispatched task text is
byte-identical to the legacy plain task (the aggregation is opt-in only), and
the stored ``step["task"]`` is never rewritten - re-dispatch re-derives the
aggregation from the stable dependency results.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.step_judge import JudgeResult, StepVerdict

pytestmark = [pytest.mark.unit]

_SESSION = "sess-1"
_OWNER_KEY = "agent:main:session:sess-1"
_CRITERIA = "output must contain PASS"
_HEADER = "## Upstream Results"

dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]
resume_module = sys.modules["agent.tools.taskflow.tools.taskflow_resume"]
wait_module = importlib.import_module("agent.tools.taskflow.tools.taskflow_wait_all")


class _DispatchRecorder:
    """Fake ``_dispatch.dispatch_child`` recording each dispatched task text."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.calls: list[str] = []

    async def __call__(
        self, task: str, requester_session_key: str, label: str | None = None
    ) -> str:
        self.calls.append(task)
        if len(self.calls) > len(self.keys):
            raise AssertionError(f"unexpected extra spawn #{len(self.calls)}")
        return self.keys[len(self.calls) - 1]


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _done_dep(step_id: str, child: str, result: str) -> tuple[dict, dict]:
    step = {
        "step_id": step_id,
        "task": f"task-{step_id}",
        "depends_on": [],
        "status": str(StepStatus.DONE),
        "child_session_key": child,
        "retry_count": 0,
    }
    record = {"child_session_key": child, "result": result, "result_hash": f"hash::{child}"}
    return step, record


def _synthesis(step_id: str, task: str, *, status: StepStatus | str, **extra) -> dict:
    step = {
        "step_id": step_id,
        "task": task,
        "depends_on": ["step-1"],
        "status": str(status),
        "retry_count": 0,
    }
    step.update(extra)
    return step


async def _seed(flow_id: str, steps: list[dict], results: list[dict]) -> None:
    out = await _tools()["taskflow_create"].coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="synthesize probe",
        initial_state={"steps": steps, "results": results},
    )
    assert "Error" not in out, out


async def _flow(flow_id: str) -> dict:
    flow = await store_sqlite.get_flow(flow_id, _SESSION)
    assert flow is not None
    return flow


def _step_of(flow: dict, step_id: str) -> dict:
    return next(s for s in flow["state"]["steps"] if s.get("step_id") == step_id)


def _patch_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry seam where every child is settled (dead)."""
    monkeypatch.setattr(wait_module, "get_run_by_child_session_key", lambda key: None)
    monkeypatch.setattr(wait_module, "is_live_unended_run", lambda run: False)


# ---------------------------------------------------------------------------
# run_task immediate dispatch: aggregates, but keeps the stored task original
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_aggregates_dependency_results(isolated_db: Path, monkeypatch):
    # Given a done dependency with a recorded result
    dep, record = _done_dep("step-1", "child-1", "FINDINGS-XYZ")
    fake = _DispatchRecorder("child-2")
    monkeypatch.setattr(dispatch_module, "dispatch_child", fake)
    await _seed("flow-1", [dep], [record])

    # When an opted-in synthesis step dispatches immediately
    out = await _tools()["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="Synthesize the findings.",
        depends_on=["step-1"],
        aggregate_deps=True,
        session_id=_SESSION,
    )

    # Then the child receives the upstream result...
    assert "child-2" in out, out
    assert len(fake.calls) == 1
    assert "FINDINGS-XYZ" in fake.calls[0]
    assert _HEADER in fake.calls[0]

    # ...while the stored task stays the ORIGINAL text (no fingerprint drift)
    step = _step_of(await _flow("flow-1"), "step-2")
    assert step["task"] == "Synthesize the findings."
    assert step["aggregate_deps"] is True


# ---------------------------------------------------------------------------
# taskflow_dispatch batch path: a flag-carrying step aggregates too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_dispatch_aggregates_dependency_results(isolated_db: Path, monkeypatch):
    # Given a done dependency and a blocked synthesis step carrying the flag
    dep, record = _done_dep("step-1", "child-1", "FINDINGS-XYZ")
    synthesis = _synthesis(
        "step-2",
        "Synthesize the findings.",
        status=StepStatus.BLOCKED,
        aggregate_deps=True,
    )
    fake = _DispatchRecorder("child-2")
    monkeypatch.setattr(dispatch_module, "dispatch_child", fake)
    await _seed("flow-1", [dep, synthesis], [record])

    # When the now-unblocked step is batch-dispatched
    out = await _tools()["taskflow_dispatch"].coroutine(
        flow_id="flow-1", step_ids=["step-2"], session_id=_SESSION
    )

    # Then the batch path aggregates as well
    assert "Error" not in out, out
    assert "FINDINGS-XYZ" in fake.calls[0]
    assert _HEADER in fake.calls[0]


# ---------------------------------------------------------------------------
# opt-out regression: the default dispatch text is byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_off_dispatch_is_unchanged(isolated_db: Path, monkeypatch):
    # Given a done dependency with a recorded result
    dep, record = _done_dep("step-1", "child-1", "FINDINGS-XYZ")
    fake = _DispatchRecorder("child-2")
    monkeypatch.setattr(dispatch_module, "dispatch_child", fake)
    await _seed("flow-1", [dep], [record])

    # When the synthesis step is dispatched WITHOUT opting in
    out = await _tools()["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="Synthesize the findings.",
        depends_on=["step-1"],
        session_id=_SESSION,
    )

    # Then the dispatched task is exactly the legacy plain text
    assert "Error" not in out, out
    assert fake.calls == ["Synthesize the findings."]
    step = _step_of(await _flow("flow-1"), "step-2")
    assert "aggregate_deps" not in step


# ---------------------------------------------------------------------------
# StepJudge RETRY: the replacement child re-derives the SAME aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_judge_retry_reproduces_aggregation(isolated_db: Path, monkeypatch):
    # Given a dispatched synthesis step whose judge returns RETRY
    dep, record = _done_dep("step-1", "child-1", "FINDINGS-XYZ")
    fake = _DispatchRecorder("child-2", "child-3")
    monkeypatch.setattr(dispatch_module, "dispatch_child", fake)

    async def _retry_judge(
        step_task: str, criteria: str | None, result_text: str, evidence_summary=None
    ) -> JudgeResult:
        return JudgeResult(StepVerdict.RETRY, "missing PASS", "emit the PASS token")

    monkeypatch.setattr(resume_module, "judge_step_result", _retry_judge)
    monkeypatch.setattr(resume_module, "collect_evidence_summary", lambda **_: None)

    await _seed("flow-1", [dep], [record])
    await _tools()["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="Synthesize the findings.",
        depends_on=["step-1"],
        aggregate_deps=True,
        validation_criteria=_CRITERIA,
        session_id=_SESSION,
    )
    assert "FINDINGS-XYZ" in fake.calls[0]

    # When the judge-driven re-dispatch happens
    out = await _tools()["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="child-2",
        result="no token",
    )

    # Then the replacement task carries the same upstream result + the feedback...
    assert "judge: RETRY" in out, out
    assert len(fake.calls) == 2
    assert "FINDINGS-XYZ" in fake.calls[1]
    assert _HEADER in fake.calls[1]
    assert "emit the PASS token" in fake.calls[1]

    # ...and the stored original task is still untouched
    step = _step_of(await _flow("flow-1"), "step-2")
    assert step["task"] == "Synthesize the findings."


# ---------------------------------------------------------------------------
# retry-policy re-dispatch (taskflow_resume failure path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_retry_reproduces_aggregation(isolated_db: Path, monkeypatch):
    # Given an opted-in synthesis step with a retry policy
    dep, record = _done_dep("step-1", "child-1", "FINDINGS-XYZ")
    fake = _DispatchRecorder("child-2", "child-3")
    monkeypatch.setattr(dispatch_module, "dispatch_child", fake)
    await _seed("flow-1", [dep], [record])
    await _tools()["taskflow_run_task"].coroutine(
        flow_id="flow-1",
        task="Synthesize the findings.",
        depends_on=["step-1"],
        aggregate_deps=True,
        retry_policy={"max_retries": 1, "retry_delay_seconds": 0.0, "retry_on": []},
        session_id=_SESSION,
    )
    assert "FINDINGS-XYZ" in fake.calls[0]

    # When a failing result triggers the policy re-dispatch
    await _tools()["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-1",
        child_session_key="child-2",
        result="ERROR: could not synthesize",
    )

    # Then the replacement re-derives the aggregation
    assert len(fake.calls) == 2
    assert "FINDINGS-XYZ" in fake.calls[1]
    assert _HEADER in fake.calls[1]


# ---------------------------------------------------------------------------
# retry-policy re-dispatch (taskflow_wait_all automatic path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_all_retry_reproduces_aggregation(isolated_db: Path, monkeypatch):
    # Given a dispatched opted-in synthesis step whose child is dead
    dep, record = _done_dep("step-1", "child-1", "FINDINGS-XYZ")
    synthesis = _synthesis(
        "step-2",
        "Synthesize the findings.",
        status=StepStatus.DISPATCHED,
        child_session_key="child-2",
        aggregate_deps=True,
        retry_policy={"max_retries": 1, "retry_delay_seconds": 0.0, "retry_on": []},
    )
    fake = _DispatchRecorder("child-3")
    monkeypatch.setattr(dispatch_module, "dispatch_child", fake)
    _patch_registry(monkeypatch)
    await _seed("flow-1", [dep, synthesis], [record])

    # When wait_all auto-retries the settled step
    out = await _tools()["taskflow_wait_all"].coroutine(
        flow_id="flow-1",
        timeout_seconds=1.0,
        poll_interval_seconds=0.01,
        session_id=_SESSION,
    )

    # Then the replacement carries the upstream result
    assert "re-dispatched" in out, out
    assert "FINDINGS-XYZ" in fake.calls[0]
    assert _HEADER in fake.calls[0]
