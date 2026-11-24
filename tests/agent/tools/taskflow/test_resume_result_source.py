"""Resume injects the child's OWN stored result (2026-11-24 DeepSWE defect).

The parent only ever sees a ~4k delivery carrier of a long child report, so a
resume that requires the caller to retype the result invites a short summary in
its place. These tests pin the contract:

* omitting ``result`` injects ``completion.result_text``;
* a materially shorter caller-supplied result is refused while the child is
  recorded successful (``force_result`` submits a deliberate summary);
* a child that is not TERMINAL cannot be resumed (``force_result`` overrides).
"""

from __future__ import annotations

import time

import pytest

from agent.tools.subagent.registry import memory as run_memory
from agent.tools.subagent.types.registry import (
    CompletionState,
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-resume-source"
_LONG = "verified report line\n" * 200  # ~4,200 chars: far above the summary guard


def _run_key(tag: str) -> str:
    return f"agent:main:subagent:resume-src-{tag}"


def _record(
    tag: str,
    *,
    status: ExecutionStatus,
    result_text: str | None,
    outcome: RunOutcomeStatus | None = None,
    started_at: float | None = None,
) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=f"run-{tag}",
        child_session_key=_run_key(tag),
        requester_session_key="agent:main:session:" + _SESSION,
        task="research",
        execution=ExecutionState(
            status=status,
            started_at=started_at if started_at is not None else time.monotonic(),
            outcome=RunOutcome(status=outcome) if outcome is not None else None,
        ),
        completion=CompletionState(required=True, result_text=result_text),
    )


async def _seed_flow(flow_id: str, child_key: str, *, judge_criteria: str = "") -> None:
    step = {
        "step_id": "step-1",
        "task": "investigate",
        "depends_on": [],
        "status": "dispatched",
        "child_session_key": child_key,
    }
    if judge_criteria:
        step["judge_criteria"] = judge_criteria
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="resume source probe",
        initial_state={"steps": [step]},
    )
    assert "Error" not in out, out


async def _step_status(flow_id: str) -> str:
    flow = await store_sqlite.get_flow(flow_id, _SESSION)
    assert flow is not None
    return str(((flow["state"] or {}).get("steps") or [{}])[0].get("status"))


async def _results(flow_id: str) -> list[dict]:
    flow = await store_sqlite.get_flow(flow_id, _SESSION)
    assert flow is not None
    return list((flow["state"] or {}).get("results") or [])


@pytest.fixture()
def run_registry():
    """Insert run records into the process registry and remove them afterwards."""
    created: list[str] = []

    def _add(record: SubagentRunRecord) -> SubagentRunRecord:
        run_memory.set_run(record)
        created.append(record.run_id)
        return record

    yield _add
    for run_id in created:
        run_memory.delete(run_id)


@pytest.mark.asyncio
async def test_an_omitted_result_injects_the_childs_stored_text(isolated_db, run_registry):
    key = _run_key("stored")
    run_registry(_record("stored", status=ExecutionStatus.TERMINAL, result_text=_LONG))
    await _seed_flow("flow-src-stored", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION, flow_id="flow-src-stored", child_session_key=key
    )

    assert "Error" not in out, out
    assert "result_source=child_completion" in out
    results = await _results("flow-src-stored")
    assert len(results) == 1 and results[0]["result"] == _LONG


@pytest.mark.asyncio
async def test_a_short_caller_summary_is_refused(isolated_db, run_registry):
    key = _run_key("summary")
    run_registry(_record("summary", status=ExecutionStatus.TERMINAL, result_text=_LONG))
    await _seed_flow("flow-src-summary", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-summary",
        child_session_key=key,
        result="Investigation done: the choke point is consumeBodyStream.",
        step_outcome="success",
    )

    assert out.startswith("Error:")
    assert "already stored" in out
    assert await _results("flow-src-summary") == []


@pytest.mark.asyncio
async def test_a_partial_summary_is_refused_too(isolated_db, run_registry):
    """`partial` is success-shaped: a summary must not slip through under it."""
    key = _run_key("partial")
    run_registry(_record("partial", status=ExecutionStatus.TERMINAL, result_text=_LONG))
    await _seed_flow("flow-src-partial", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-partial",
        child_session_key=key,
        result="partial summary",
        step_outcome="partial",
    )

    assert out.startswith("Error:")
    assert await _results("flow-src-partial") == []


@pytest.mark.asyncio
async def test_a_declared_failure_may_carry_the_callers_own_note(isolated_db, run_registry):
    key = _run_key("failed-note")
    run_registry(_record("failed-note", status=ExecutionStatus.TERMINAL, result_text=_LONG))
    await _seed_flow("flow-src-failed-note", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-failed-note",
        child_session_key=key,
        result="the child produced an unusable dump",
        step_outcome="failure",
    )

    assert "Error" not in out, out
    results = await _results("flow-src-failed-note")
    assert results and results[0]["result"] == "the child produced an unusable dump"


@pytest.mark.asyncio
async def test_force_result_accepts_a_deliberate_summary(isolated_db, run_registry):
    key = _run_key("forced")
    run_registry(_record("forced", status=ExecutionStatus.TERMINAL, result_text=_LONG))
    await _seed_flow("flow-src-forced", key)
    summary = "Deliberate summary."

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-forced",
        child_session_key=key,
        result=summary,
        force_result=True,
    )

    assert "Error" not in out, out
    assert "result_source=caller" in out
    results = await _results("flow-src-forced")
    assert results and results[0]["result"] == summary


@pytest.mark.asyncio
async def test_a_running_child_cannot_be_resumed(isolated_db, run_registry):
    key = _run_key("running")
    run_registry(
        _record(
            "running",
            status=ExecutionStatus.RUNNING,
            result_text=None,
            started_at=time.monotonic() - 36.0,
        )
    )
    await _seed_flow("flow-src-running", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-running",
        child_session_key=key,
        result="speaking for a live child",
    )

    assert out.startswith("Error:")
    assert "still running" in out and "force_result=true" in out
    assert await _results("flow-src-running") == []


@pytest.mark.asyncio
async def test_force_result_resumes_a_running_child(isolated_db, run_registry):
    key = _run_key("running-forced")
    run_registry(_record("running-forced", status=ExecutionStatus.RUNNING, result_text=None))
    await _seed_flow("flow-src-running-forced", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-running-forced",
        child_session_key=key,
        result="explicit override",
        force_result=True,
    )

    assert "Error" not in out, out
    assert len(await _results("flow-src-running-forced")) == 1


@pytest.mark.asyncio
async def test_no_run_record_keeps_the_legacy_path(isolated_db):
    """A child the registry does not know (old tests / delivery paths) is unchanged."""
    key = _run_key("unknown")
    await _seed_flow("flow-src-unknown", key)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-unknown",
        child_session_key=key,
        result="caller text",
    )

    assert "Error" not in out, out
    results = await _results("flow-src-unknown")
    assert results and results[0]["result"] == "caller text"


@pytest.mark.asyncio
async def test_redeclaring_the_same_result_as_success_rejudges_it(
    isolated_db, run_registry, monkeypatch
):
    """The delivery auto-resume records the stored text as BLOCKED; the agent must
    be able to re-declare that SAME text as a success so the judge sees it."""
    from agent.tools.taskflow.step_judge import JudgeResult, StepVerdict

    key = _run_key("redeclare")
    run_registry(
        _record(
            "redeclare",
            status=ExecutionStatus.TERMINAL,
            result_text=_LONG,
            outcome=RunOutcomeStatus.BUDGET_EXHAUSTED,
        )
    )
    await _seed_flow("flow-src-redeclare", key, judge_criteria="the full report")
    blocked = await taskflow_resume.coroutine(
        session_id=_SESSION,
        flow_id="flow-src-redeclare",
        child_session_key=key,
        result=_LONG,
        step_outcome="blocked",
    )
    assert "Error" not in blocked, blocked
    assert await _step_status("flow-src-redeclare") == "blocked"

    seen: dict = {}

    async def _judge(**kwargs):
        seen["result_text"] = kwargs["result_text"]
        return JudgeResult(StepVerdict.PASS, "full report judged", "")

    import sys

    # The package attribute is the decorated TOOL; the module lives in sys.modules.
    resume_mod = sys.modules["agent.tools.taskflow.tools.taskflow_resume"]
    monkeypatch.setattr(resume_mod, "judge_step_result", _judge)

    out = await taskflow_resume.coroutine(
        session_id=_SESSION, flow_id="flow-src-redeclare", child_session_key=key
    )

    assert "already resumed" not in out, out
    assert seen.get("result_text") == _LONG, "the judge must see the stored report"
    assert await _step_status("flow-src-redeclare") == "done"
