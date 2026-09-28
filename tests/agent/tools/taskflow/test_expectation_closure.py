"""Expectation→actual closure: schema tier, declared outcome, bindings, compat.

Covers the mechanisms added on top of the pre-existing StepJudge tier:

* Tier 1 — ``response_schema`` on a step: the result is parsed/validated and the
  verdict is recorded (``structured_result`` + ``schema_validated``); a failure
  retries under the step's policy and NEVER spends a judge call, because a wrong
  structure is already known to be wrong.
* Tier 2 — a schema pass does not skip the judge: structure alone never proves
  the content is right.
* ``step_outcome`` — the caller's report outranks the gates: ``failure`` marks
  the step ``failed`` (dependents stay blocked, no judge call), ``skipped``
  cascades ``skipped`` into its dependents.
* ``input_bindings`` — upstream structured fields are appended to the dispatched
  task as data; an unresolvable binding degrades to ``null``.
* Backward compatibility — a step with no expectations behaves exactly as before
  (done + unlock).
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.step_judge import JudgeResult, StepVerdict
from agent.tools.taskflow.tools._shared import (
    build_task_with_bindings,
    new_step,
    step_status,
    unlock_dependents,
)

_SESSION = "sess-closure"

pytestmark = [pytest.mark.unit]

dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]
resume_module = sys.modules["agent.tools.taskflow.tools.taskflow_resume"]

_SCHEMA = {
    "type": "object",
    "properties": {"files": {"type": "array"}, "risk_count": {"type": "number"}},
    "required": ["files"],
}


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _recording_dispatch(keys: list[str], calls: list[dict[str, Any]] | None = None):
    """Dispatch double returning pre-seeded child keys and recording its kwargs."""
    iterator = iter(keys)

    async def _dispatch(
        task: str,
        requester_session_key: str,
        label: str | None = None,
        **kwargs: Any,
    ) -> str:
        if calls is not None:
            calls.append({"task": task, **kwargs})
        return next(iterator)

    return _dispatch


def _forbid_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if the judge is reached (Tier 1 failure must not call it)."""

    async def _boom(**_kwargs: Any) -> JudgeResult:
        raise AssertionError("judge must not be called for a Tier 1 failure")

    monkeypatch.setattr(resume_module, "judge_step_result", _boom)


def _patch_judge(monkeypatch: pytest.MonkeyPatch, verdict: StepVerdict, reason: str = "judged"):
    """Answer every judge call with one fixed verdict."""

    async def _judge(**_kwargs: Any) -> JudgeResult:
        return JudgeResult(verdict, reason, "")

    monkeypatch.setattr(resume_module, "judge_step_result", _judge)


async def _seed(
    flow_id: str,
    steps: list[dict],
    results: list[dict] | None = None,
    **extra: Any,
) -> None:
    state: dict[str, Any] = {"description": "closure probe", "steps": steps}
    if results is not None:
        state["results"] = results
    state.update(extra)
    await store_sqlite.create_flow(flow_id, state, session_id=_SESSION)


async def _flow(flow_id: str) -> dict:
    flow = await store_sqlite.get_flow(flow_id, session_id=_SESSION)
    assert flow is not None
    return flow


def _step_by_id(flow: dict, step_id: str) -> dict:
    return next(s for s in flow["state"]["steps"] if s.get("step_id") == step_id)


def _result_for(flow: dict, child_key: str) -> dict:
    return next(r for r in flow["state"]["results"] if r.get("child_session_key") == child_key)


# ── Tier 1: structure ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_pass_records_structure_and_marks_done(isolated_db):
    _forbid_judge_was_unneeded = None  # no judge configured → the block never runs
    step = new_step("step-1", "collect", response_schema=_SCHEMA, status=StepStatus.DISPATCHED)
    step["child_session_key"] = "child-1"
    await _seed("flow-schema-ok", [step])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-schema-ok",
        child_session_key="child-1",
        result='{"files": ["a.ts"], "risk_count": 1}',
    )

    assert "Error" not in out, out
    flow = await _flow("flow-schema-ok")
    assert step_status(_step_by_id(flow, "step-1")) == StepStatus.DONE.value
    record = _result_for(flow, "child-1")
    assert record["schema_validated"] is True
    assert record["structured_result"] == {"files": ["a.ts"], "risk_count": 1}
    assert record["step_id"] == "step-1"
    assert "schema: validated" in out


@pytest.mark.asyncio
async def test_schema_failure_retries_without_judging(isolated_db, monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dispatch_module, "dispatch_child", _recording_dispatch(["child-2"], calls))
    _forbid_judge(monkeypatch)

    step = new_step(
        "step-1",
        "collect",
        response_schema=_SCHEMA,
        retry_policy={"max_retries": 1, "retry_delay_seconds": 0},
        status=StepStatus.DISPATCHED,
    )
    step["child_session_key"] = "child-1"
    await _seed("flow-schema-retry", [step])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-schema-retry",
        child_session_key="child-1",
        result='{"risk_count": 3}',  # missing the required "files"
    )

    flow = await _flow("flow-schema-retry")
    refreshed = _step_by_id(flow, "step-1")
    assert step_status(refreshed) == StepStatus.DISPATCHED.value
    assert refreshed["child_session_key"] == "child-2"
    assert refreshed["retry_count"] == 1
    assert _result_for(flow, "child-1")["schema_validated"] is False
    assert "retry: re-dispatched" in out


@pytest.mark.asyncio
async def test_schema_failure_without_budget_fails_and_keeps_dependents_blocked(
    isolated_db, monkeypatch
):
    _forbid_judge(monkeypatch)
    failing = new_step("step-1", "collect", response_schema=_SCHEMA, status=StepStatus.DISPATCHED)
    failing["child_session_key"] = "child-1"
    dependent = new_step("step-2", "use it", depends_on=["step-1"], status=StepStatus.BLOCKED)
    await _seed("flow-schema-fail", [failing, dependent])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-schema-fail",
        child_session_key="child-1",
        result="not json at all",
    )

    flow = await _flow("flow-schema-fail")
    assert step_status(_step_by_id(flow, "step-1")) == StepStatus.FAILED.value
    assert "response_schema not satisfied" in _step_by_id(flow, "step-1")["fail_reason"]
    # The failure never unlocks the dependent.
    assert step_status(_step_by_id(flow, "step-2")) == StepStatus.BLOCKED.value
    assert "outcome: failed" in out


@pytest.mark.asyncio
async def test_schema_pass_still_runs_the_judge(isolated_db, monkeypatch):
    # B5: a structurally valid result can still be semantically poor.
    _patch_judge(monkeypatch, StepVerdict.RETRY, "only one of three files inspected")
    monkeypatch.setattr(dispatch_module, "dispatch_child", _recording_dispatch(["child-2"]))

    step = new_step(
        "step-1",
        "collect",
        response_schema=_SCHEMA,
        judge_criteria="every file inspected",
        status=StepStatus.DISPATCHED,
    )
    step["child_session_key"] = "child-1"
    await _seed("flow-schema-judge", [step])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-schema-judge",
        child_session_key="child-1",
        result='{"files": ["a.ts"]}',
    )

    flow = await _flow("flow-schema-judge")
    refreshed = _step_by_id(flow, "step-1")
    assert _result_for(flow, "child-1")["schema_validated"] is True
    # Judge RETRY re-dispatched (the step keeps its retry budget).
    assert refreshed["child_session_key"] == "child-2"
    assert "judge: RETRY" in out
    assert "judge_criteria: every file inspected" in out


# ── Declared outcome ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_declared_failure_marks_failed_without_judging(isolated_db, monkeypatch):
    _forbid_judge(monkeypatch)
    step = new_step(
        "step-1",
        "collect",
        judge_criteria="anything",
        status=StepStatus.DISPATCHED,
    )
    step["child_session_key"] = "child-1"
    dependent = new_step("step-2", "after", depends_on=["step-1"], status=StepStatus.BLOCKED)
    await _seed("flow-declared-fail", [step, dependent])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-declared-fail",
        child_session_key="child-1",
        result="child crashed",
        step_outcome="failure",
    )

    flow = await _flow("flow-declared-fail")
    assert step_status(_step_by_id(flow, "step-1")) == StepStatus.FAILED.value
    assert step_status(_step_by_id(flow, "step-2")) == StepStatus.BLOCKED.value
    assert _result_for(flow, "child-1")["step_outcome"] == "failure"
    assert "outcome: failed (declared by the caller)" in out


@pytest.mark.asyncio
async def test_declared_skip_cascades_into_dependents(isolated_db, monkeypatch):
    _forbid_judge(monkeypatch)
    step = new_step("step-1", "collect", status=StepStatus.DISPATCHED)
    step["child_session_key"] = "child-1"
    dependent = new_step("step-2", "after", depends_on=["step-1"], status=StepStatus.BLOCKED)
    await _seed("flow-declared-skip", [step, dependent])

    tools = _tools()
    await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-declared-skip",
        child_session_key="child-1",
        result="not needed after all",
        step_outcome="skipped",
    )

    flow = await _flow("flow-declared-skip")
    assert step_status(_step_by_id(flow, "step-1")) == StepStatus.SKIPPED.value
    dependent_after = _step_by_id(flow, "step-2")
    assert step_status(dependent_after) == StepStatus.SKIPPED.value
    assert dependent_after["skip_reason"] == "dependency skipped or cancelled"


@pytest.mark.asyncio
async def test_unknown_declared_outcome_is_rejected_before_any_write(isolated_db):
    step = new_step("step-1", "collect", status=StepStatus.DISPATCHED)
    step["child_session_key"] = "child-1"
    await _seed("flow-bad-outcome", [step])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-bad-outcome",
        child_session_key="child-1",
        result="done",
        step_outcome="whatever",
    )

    assert out.startswith("Error: step_outcome must be one of")
    flow = await _flow("flow-bad-outcome")
    assert not flow["state"].get("results")  # nothing was recorded


# ── Backward compatibility ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_step_behaves_as_before(isolated_db):
    step = new_step("step-1", "collect", status=StepStatus.DISPATCHED)
    step["child_session_key"] = "child-1"
    dependent = new_step("step-2", "after", depends_on=["step-1"], status=StepStatus.BLOCKED)
    await _seed("flow-plain", [step, dependent])

    tools = _tools()
    out = await tools["taskflow_resume"].coroutine(
        session_id=_SESSION,
        flow_id="flow-plain",
        child_session_key="child-1",
        result="plain result",
    )

    assert "Error" not in out, out
    flow = await _flow("flow-plain")
    assert step_status(_step_by_id(flow, "step-1")) == StepStatus.DONE.value
    assert step_status(_step_by_id(flow, "step-2")) == StepStatus.READY.value
    record = _result_for(flow, "child-1")
    # No expectation configured → no gate invented out of thin air.
    assert record["schema_validated"] is None
    assert "structured_result" not in record


# ── new_step / bindings helpers ─────────────────────────────────────────


def test_new_step_writes_only_provided_expectations():
    plain = new_step("s", "task")
    assert set(plain) == {"step_id", "task", "depends_on", "status", "retry_count"}

    configured = new_step("s", "task", response_schema=_SCHEMA, judge_criteria="x", priority=0)
    assert configured["response_schema"] == _SCHEMA
    assert configured["judge_criteria"] == "x"
    assert configured["priority"] == 0

    with pytest.raises(ValueError, match="unknown step field"):
        new_step("s", "task", expectation_typo=1)


def test_bindings_append_resolved_values_and_degrade_to_null():
    source = new_step("step-A", "collect")
    source["child_session_key"] = "child-a"
    steps = [source]
    results = [
        {
            "child_session_key": "child-a",
            "structured_result": {"files": ["x.ts", "y.ts"], "risk_count": 2},
        }
    ]
    step = new_step(
        "step-B",
        "fix them",
        input_bindings={
            "target_files": "step-A.structured_result.files",
            "risk": "step-A.structured_result.risk_count",
            "absent": "step-A.structured_result.nope",
            "unknown_step": "step-Z.structured_result.x",
        },
    )

    text = build_task_with_bindings("fix them", step, steps, results)

    assert text.startswith("fix them")
    assert "## Input parameters" in text
    assert '"x.ts"' in text and '"risk": 2' in text
    assert '"absent": null' in text and '"unknown_step": null' in text


def test_bindings_leave_a_plain_step_untouched():
    step = new_step("step-B", "just do it")
    assert build_task_with_bindings("just do it", step, [], []) == "just do it"


def test_failure_aware_unlock_semantics():
    done = new_step("a", "A", status="done")
    ok = new_step("b", "B", depends_on=["a"], status="blocked")
    assert unlock_dependents([done, ok]) == ["b"]
    assert step_status(ok) == StepStatus.READY.value

    failed = new_step("a", "A", status="failed")
    waiting = new_step("b", "B", depends_on=["a"], status="blocked")
    assert unlock_dependents([failed, waiting]) == []
    assert step_status(waiting) == StepStatus.BLOCKED.value

    skipped = new_step("a", "A", status="skipped")
    cascaded = new_step("b", "B", depends_on=["a"], status="blocked")
    assert unlock_dependents([skipped, cascaded]) == []
    assert step_status(cascaded) == StepStatus.SKIPPED.value


# ── Expectation pass-through to the spawn layer ─────────────────────────


@pytest.mark.asyncio
async def test_run_task_forwards_schema_role_model_and_timeout(isolated_db, monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dispatch_module, "dispatch_child", _recording_dispatch(["child-1"], calls))
    await _seed("flow-forward", [])

    tools = _tools()
    out = await tools["taskflow_run_task"].coroutine(
        session_id=_SESSION,
        flow_id="flow-forward",
        task="collect findings",
        response_schema=_SCHEMA,
        functional_role="researcher",
        step_model="glm-4.6",
        step_timeout_seconds=120.0,
    )

    assert "Error" not in out, out
    assert len(calls) == 1
    assert calls[0]["output_schema"] == _SCHEMA
    assert calls[0]["functional_role"] == "researcher"
    assert calls[0]["step_model"] == "glm-4.6"
    assert calls[0]["step_timeout_seconds"] == 120.0

    flow = await _flow("flow-forward")
    step = _step_by_id(flow, "step-1")
    assert step["response_schema"] == _SCHEMA
    assert step["functional_role"] == "researcher"


@pytest.mark.asyncio
async def test_run_task_dispatches_bound_upstream_values(isolated_db, monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dispatch_module, "dispatch_child", _recording_dispatch(["child-2"], calls))
    producer = new_step("step-1", "collect", status=StepStatus.DONE)
    producer["child_session_key"] = "child-1"
    await _seed(
        "flow-bind",
        [producer],
        results=[
            {
                "child_session_key": "child-1",
                "result": "found files",
                "structured_result": {"files": ["a.ts", "b.ts"]},
            }
        ],
    )

    tools = _tools()
    out = await tools["taskflow_run_task"].coroutine(
        session_id=_SESSION,
        flow_id="flow-bind",
        task="fix the files",
        depends_on=["step-1"],
        input_bindings={"target_files": "step-1.structured_result.files"},
    )

    assert "Error" not in out, out
    assert len(calls) == 1
    assert "## Input parameters" in calls[0]["task"]
    assert '"a.ts"' in calls[0]["task"]
