"""Judgement-gate corrections on the retry path (schema feedback + spawn contract).

Gap A — a Tier 1 schema failure short-circuits BEFORE the judge, so the step's
``judge_feedback`` is empty and the replacement child used to be re-spawned
with the bare task, no hint of what was wrong. The retry now carries the
validator's own message (field path + expected/got).

Gap B — the replacement child inherits the step's spawn configuration
(``response_schema`` → the child's structured-output contract, functional
role, model override, run timeout). Without it a schema-failed retry could not
even see the full schema it was supposed to satisfy.

Both are pinned through the real ``taskflow_resume`` entry with the dispatch
seam monkeypatched, so the task text and the spawn kwargs are read exactly as
the child would receive them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-gate"
_CHILD = "agent:main:subagent:first"

_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["status", "count"],
    "additionalProperties": False,
}


class _FullRecorder:
    """Fake dispatch seam capturing the ENTIRE spawn call (task + kwargs)."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.calls: list[dict] = []

    async def __call__(self, task: str, requester_session_key: str, **kwargs) -> str:
        self.calls.append({"task": task, "requester": requester_session_key, **kwargs})
        return self.keys[len(self.calls) - 1]

    @property
    def last(self) -> dict:
        assert self.calls, "no spawn happened"
        return self.calls[-1]


def _step(**overrides) -> dict:
    step = {
        "step_id": "step-1",
        "task": "report the status",
        "depends_on": [],
        "status": "dispatched",
        "child_session_key": _CHILD,
        "retry_policy": {"max_retries": 2, "retry_delay_seconds": 0.0, "retry_on": []},
    }
    step.update(overrides)
    return step


async def _seed(flow_id: str, step: dict) -> None:
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="gate probe",
        initial_state={"steps": [step]},
    )
    assert "Error" not in out, out


# ---------------------------------------------------------------------------
# Gap A — the schema error reaches the replacement child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_failure_feedback_includes_the_validator_error(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _seed("flow-a", _step(response_schema=_SCHEMA))
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    # Valid JSON, wrong shape: `count` is missing → the validator names it.
    out = await taskflow_resume.coroutine(
        flow_id="flow-a",
        child_session_key=_CHILD,
        result=json.dumps({"status": "ok"}),
        session_id=_SESSION,
    )

    assert "re-dispatched" in out, out
    task = recorder.last["task"]
    assert "missing required field 'count'" in task, task
    assert "## Previous Attempt Feedback" in task
    assert "failed schema validation" in task


@pytest.mark.asyncio
async def test_a_json_parse_failure_is_fed_back_too(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _seed("flow-a2", _step(response_schema=_SCHEMA))
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    out = await taskflow_resume.coroutine(
        flow_id="flow-a2",
        child_session_key=_CHILD,
        result="not json at all",
        session_id=_SESSION,
    )

    assert "re-dispatched" in out, out
    assert "is not JSON" in recorder.last["task"], recorder.last["task"]


@pytest.mark.asyncio
async def test_a_text_failure_still_uses_the_judge_feedback(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # No schema: the retry comes from the failure classifier, so the feedback is
    # whatever the step already carries — the judge's guidance, not a schema note.
    await _seed("flow-a3", _step(judge_feedback="tighten the summary"))
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    out = await taskflow_resume.coroutine(
        flow_id="flow-a3",
        child_session_key=_CHILD,
        result="Error: the tool crashed",
        session_id=_SESSION,
    )

    assert "re-dispatched" in out, out
    assert "tighten the summary" in recorder.last["task"]
    assert "failed schema validation" not in recorder.last["task"]


@pytest.mark.asyncio
async def test_a_schema_failure_without_a_retry_policy_spawns_nothing(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    step = _step(response_schema=_SCHEMA)
    del step["retry_policy"]
    await _seed("flow-a4", step)
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    out = await taskflow_resume.coroutine(
        flow_id="flow-a4",
        child_session_key=_CHILD,
        result=json.dumps({"status": "ok"}),
        session_id=_SESSION,
    )

    assert recorder.calls == [], "no retry policy, no re-dispatch"
    assert "outcome: failed (schema, no retry left" in out, out


# ---------------------------------------------------------------------------
# Gap B — the replacement inherits the step's spawn configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_replacement_inherits_every_spawn_knob(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _seed(
        "flow-b",
        _step(
            response_schema=_SCHEMA,
            functional_role="reviewer",
            step_model="deepseek-flash",
            step_timeout_seconds=1800.0,
        ),
    )
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    await taskflow_resume.coroutine(
        flow_id="flow-b",
        child_session_key=_CHILD,
        result=json.dumps({"status": "ok"}),
        session_id=_SESSION,
    )

    call = recorder.last
    assert call["output_schema"] == _SCHEMA, "the child must see the schema it must satisfy"
    assert call["functional_role"] == "reviewer"
    assert call["step_model"] == "deepseek-flash"
    assert call["step_timeout_seconds"] == 1800.0
    assert call["requester"] == "agent:main:session:sess-gate"


@pytest.mark.asyncio
async def test_a_step_without_spawn_config_sends_none(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _seed("flow-b2", _step())
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    out = await taskflow_resume.coroutine(
        flow_id="flow-b2",
        child_session_key=_CHILD,
        result="Error: boom",
        session_id=_SESSION,
    )

    assert "re-dispatched" in out, out
    call = recorder.last
    assert call["output_schema"] is None
    assert call["functional_role"] is None
    assert call["step_model"] is None
    assert call["step_timeout_seconds"] is None


# ---------------------------------------------------------------------------
# The round trip: schema failure → informed retry → compliant result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_retry_round_trip_lands_done(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _seed("flow-c", _step(response_schema=_SCHEMA))
    recorder = _FullRecorder("agent:main:subagent:retry-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", recorder)

    first = await taskflow_resume.coroutine(
        flow_id="flow-c",
        child_session_key=_CHILD,
        result=json.dumps({"status": "ok"}),
        session_id=_SESSION,
    )
    assert "re-dispatched" in first
    retry_child = "agent:main:subagent:retry-1"
    assert retry_child in first, "the answer names the replacement child"

    # The replacement reports a compliant result: the step lands done and no
    # further spawn happens.
    second = await taskflow_resume.coroutine(
        flow_id="flow-c",
        child_session_key=retry_child,
        result=json.dumps({"status": "ok", "count": 3}),
        session_id=_SESSION,
    )

    assert "done=1" in second and "failed=0" in second, second
    assert len(recorder.calls) == 1, "the compliant result triggered no re-dispatch"
    flow = await store_sqlite.get_flow("flow-c", _SESSION)
    assert flow is not None
    steps = flow["state"]["steps"]
    assert steps[0]["status"] == "done"
    assert steps[0]["retry_count"] == 1
