"""WorkMode stage 3: opt-in scored self-evaluation for TaskFlow steps.

Pins:
* the score parser (1-10 accepted; outage/garbage -> score=None, never a pass);
* the LLM-backed self-eval call;
* the resume consumer: score >= min -> DONE; score < min -> retry with score
  feedback; retry budget exhausted -> FORCED pass with an audit marker; outage
  follows the on_judge_error policy; and the DEFAULT binary judge is untouched
  when self_evaluate is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import importlib

from agent.tools.taskflow import build_legacy_taskflow_tools, step_judge
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.step_judge import (
    SelfEvalResult,
    judge_step_self_eval,
    _parse_self_eval_response,
)

_SESSION = "sess-selfeval"
_CRITERIA = "result must contain PASS"

pytestmark = [pytest.mark.unit]

# Import the MODULES (package attributes are @tool StructuredTool objects).
dispatch_module = importlib.import_module("agent.tools.taskflow.tools._dispatch")
resume_module = importlib.import_module("agent.tools.taskflow.tools.taskflow_resume")


def _tools() -> dict:
    return {t.name: t for t in build_legacy_taskflow_tools()}


def _key_dispatch(keys: list[str]):
    iterator = iter(keys)

    async def _dispatch(task, requester_session_key, label=None, **_kwargs):
        return next(iterator)

    return _dispatch


def _patch_self_eval(monkeypatch, scores) -> list[dict]:
    """Patch resume's self-eval with a sequence of SelfEvalResult/score ints."""
    sequence = list(scores)
    seen: list[dict] = []

    async def _fake(
        step_task, criteria, result_text, evidence_summary=None, structured_result=None
    ):
        seen.append({"task": step_task, "criteria": criteria, "result": result_text})
        value = sequence.pop(0) if sequence else 0
        if isinstance(value, SelfEvalResult):
            return value
        return SelfEvalResult(int(value), "scored", "do better")

    monkeypatch.setattr(resume_module, "judge_step_self_eval", _fake)
    monkeypatch.setattr(resume_module, "collect_evidence_summary", lambda **_: None)
    return seen


class TestSelfEvalParser:
    def test_valid_scores(self):
        for score in (1, 5, 7, 10):
            result = _parse_self_eval_response(f'{{"score": {score}, "reason": "ok"}}')
            assert result.score == score
            assert result.reason == "ok"

    @pytest.mark.parametrize(
        "raw",
        [
            '{"reason": "no score"}',
            '{"score": 0}',
            '{"score": 11}',
            '{"score": "abc"}',
            "not json",
            "",
        ],
    )
    def test_outage_returns_none(self, raw):
        assert _parse_self_eval_response(raw).score is None


@pytest.mark.asyncio
async def test_self_eval_llm_call(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []

    class Response:
        content = '{"score": 8, "reason": "good", "feedback": "minor"}'

    class LLM:
        async def ainvoke(self, messages):
            recorder.append(messages)
            return Response()

    monkeypatch.setattr(step_judge, "build_taskflow_llm", lambda temperature=None: LLM())
    result = await judge_step_self_eval("task", "criteria", "result text")
    assert result.score == 8
    system_prompt = recorder[0][0]["content"].lower()
    assert "score the result from 1 to 10" in system_prompt  # the scoring prompt, not binary


@pytest.mark.asyncio
async def test_self_eval_llm_outage(monkeypatch: pytest.MonkeyPatch):
    def boom(temperature=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(step_judge, "build_taskflow_llm", boom)
    result = await judge_step_self_eval("task", "criteria", "result")
    assert result.score is None


# ---------------------------------------------------------------------------
# Resume consumer
# ---------------------------------------------------------------------------


async def _create_self_eval_step(flow_id="flow-se", *, min_score=None, max_retries=None):
    tools = _tools()
    await tools["taskflow_create"].coroutine(session_id=_SESSION, flow_id=flow_id, description="se")
    kwargs = dict(
        flow_id=flow_id,
        task="do the work",
        validation_criteria=_CRITERIA,
        self_evaluate=True,
        session_id=_SESSION,
    )
    if min_score is not None:
        kwargs["self_eval_min_score"] = min_score
    if max_retries is not None:
        kwargs["self_eval_max_retries"] = max_retries
    out = await tools["taskflow_run_task"].coroutine(**kwargs)
    assert "dispatched" in out, out
    return tools


async def _resume(tools, flow_id="flow-se", key="agent:main:subagent:child-1"):
    return await tools["taskflow_resume"].coroutine(
        session_id=_SESSION, flow_id=flow_id, child_session_key=key, result="PASS done"
    )


@pytest.mark.asyncio
async def test_score_above_min_marks_done(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dispatch_module, "dispatch_child", _key_dispatch(["agent:main:subagent:child-1"])
    )
    _patch_self_eval(monkeypatch, [8])
    tools = await _create_self_eval_step(min_score=7)
    out = await _resume(tools)
    assert "self-eval PASS score=8/7" in out, out
    flow = await store_sqlite.get_flow("flow-se", _SESSION)
    step = flow["state"]["steps"][0]
    assert step["status"] == StepStatus.DONE
    assert step["self_eval_score"] == 8
    assert not step.get("self_eval_forced_pass")


@pytest.mark.asyncio
async def test_score_below_min_retries_with_feedback(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:child-1", "agent:main:subagent:child-2"]),
    )
    _patch_self_eval(monkeypatch, [4])
    tools = await _create_self_eval_step(min_score=7, max_retries=5)
    out = await _resume(tools)
    assert "self-eval RETRY score=4/7" in out, out
    flow = await store_sqlite.get_flow("flow-se", _SESSION)
    step = flow["state"]["steps"][0]
    assert step["status"] != StepStatus.DONE
    assert step["retry_count"] == 1
    assert "Self-eval score 4/10" in step["judge_feedback"]


@pytest.mark.asyncio
async def test_retry_budget_exhausted_forces_pass(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        dispatch_module, "dispatch_child", _key_dispatch(["agent:main:subagent:child-1"])
    )
    _patch_self_eval(monkeypatch, [3])  # low score
    tools = await _create_self_eval_step(min_score=7, max_retries=0)  # no retries allowed
    out = await _resume(tools)
    assert "FORCED PASS" in out, out
    flow = await store_sqlite.get_flow("flow-se", _SESSION)
    step = flow["state"]["steps"][0]
    assert step["status"] == StepStatus.DONE
    assert step["self_eval_forced_pass"] is True


@pytest.mark.asyncio
async def test_outage_blocks_by_default(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dispatch_module, "dispatch_child", _key_dispatch(["agent:main:subagent:child-1"])
    )
    _patch_self_eval(monkeypatch, [SelfEvalResult(None, "provider down", "")])
    tools = await _create_self_eval_step(min_score=7)
    out = await _resume(tools)
    assert "BLOCKED" in out, out
    flow = await store_sqlite.get_flow("flow-se", _SESSION)
    step = flow["state"]["steps"][0]
    assert step["status"] == StepStatus.BLOCKED


@pytest.mark.asyncio
async def test_default_binary_judge_untouched(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """A step WITHOUT self_evaluate still uses judge_step_result (binary)."""
    monkeypatch.setattr(
        dispatch_module, "dispatch_child", _key_dispatch(["agent:main:subagent:child-1"])
    )

    async def binary_judge(*a, **k):
        return __import__(
            "agent.tools.taskflow.step_judge", fromlist=["JudgeResult", "StepVerdict"]
        ).JudgeResult(
            __import__(
                "agent.tools.taskflow.step_judge", fromlist=["StepVerdict"]
            ).StepVerdict.PASS,
            "binary pass",
            "",
        )

    monkeypatch.setattr(resume_module, "judge_step_result", binary_judge)
    monkeypatch.setattr(resume_module, "collect_evidence_summary", lambda **_: None)
    tools = _tools()
    await tools["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-bin", description="bin"
    )
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-bin", task="work", validation_criteria=_CRITERIA, session_id=_SESSION
    )
    flow_before = await store_sqlite.get_flow("flow-bin", _SESSION)
    assert "self_evaluate" not in flow_before["state"]["steps"][0]
    out = await _resume(tools, flow_id="flow-bin")
    assert "judge: PASS binary pass" in out, out
