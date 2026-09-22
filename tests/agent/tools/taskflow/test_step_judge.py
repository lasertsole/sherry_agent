"""StepJudge parsing, fail-open, and prompt assembly."""

from __future__ import annotations

import pytest

from agent.tools.taskflow import step_judge
from agent.tools.taskflow.step_judge import (
    JudgeResult,
    StepVerdict,
    judge_step_result,
)

pytestmark = [pytest.mark.unit]


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _RecordingLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[dict]] = []

    async def ainvoke(self, messages: list[dict]) -> _Response:
        self.calls.append(messages)
        return _Response(self.content)


def _install_llm(monkeypatch: pytest.MonkeyPatch, content: str, recorder: list) -> None:
    def _build(temperature: float | None = None) -> _RecordingLLM:
        llm = _RecordingLLM(content)
        recorder.append(llm)
        return llm

    monkeypatch.setattr(step_judge, "build_auxiliary_llm", _build)


def _user_prompt(recorder: list) -> str:
    messages = recorder[0].calls[0]
    return str(messages[1]["content"])


@pytest.mark.asyncio
async def test_judge_parses_pass(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "pass", "reason": "criteria met"}', recorder)

    result = await judge_step_result("task", "must be green", "all green")

    assert result.verdict == StepVerdict.PASS
    assert result.reason == "criteria met"
    assert result.feedback == ""


@pytest.mark.asyncio
async def test_judge_parses_retry_with_feedback(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(
        monkeypatch,
        '{"verdict": "retry", "reason": "missing output", "feedback": "emit PASS token"}',
        recorder,
    )

    result = await judge_step_result("task", "must contain PASS", "nothing")

    assert result.verdict == StepVerdict.RETRY
    assert result.reason == "missing output"
    assert result.feedback == "emit PASS token"


@pytest.mark.asyncio
async def test_judge_parses_block(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "block", "reason": "dependency missing"}', recorder)

    result = await judge_step_result("task", "must deploy", "cannot")

    assert result.verdict == StepVerdict.BLOCK
    assert result.reason == "dependency missing"


@pytest.mark.asyncio
async def test_judge_tolerates_malformed_json_via_json_repair(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "block", "reason": "broken",', recorder)

    result = await judge_step_result("task", "criteria", "result")

    assert result.verdict == StepVerdict.BLOCK


@pytest.mark.asyncio
async def test_judge_fails_open_when_llm_unavailable(monkeypatch: pytest.MonkeyPatch):
    def _build(temperature: float | None = None):
        raise RuntimeError("aux llm offline")

    monkeypatch.setattr(step_judge, "build_auxiliary_llm", _build)

    result = await judge_step_result("task", "criteria", "result")

    assert result.verdict == StepVerdict.PASS
    assert "fail-open" in result.reason


@pytest.mark.asyncio
async def test_judge_fails_open_on_unparseable_response(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, "I am not sure what to say here.", recorder)

    result = await judge_step_result("task", "criteria", "result")

    assert result.verdict == StepVerdict.PASS
    assert "fail-open" in result.reason


@pytest.mark.asyncio
async def test_judge_accepts_missing_evidence_summary(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "pass", "reason": "ok"}', recorder)

    result = await judge_step_result("task", "criteria", "result", evidence_summary=None)

    assert result.verdict == StepVerdict.PASS
    assert "(none recorded)" in _user_prompt(recorder)


@pytest.mark.asyncio
async def test_judge_includes_evidence_summary_when_supplied(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "pass", "reason": "ok"}', recorder)

    await judge_step_result("task", "criteria", "result", evidence_summary="[pass] test: `pytest`")

    assert "[pass] test: `pytest`" in _user_prompt(recorder)


@pytest.mark.asyncio
async def test_judge_truncates_result_text_to_config_limit(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "pass", "reason": "ok"}', recorder)
    monkeypatch.setitem(step_judge.STEP_JUDGE, "max_result_chars", 10)

    await judge_step_result("task", "criteria", "X" * 50)

    prompt = _user_prompt(recorder)
    assert "X" * 10 in prompt
    assert "X" * 11 not in prompt


@pytest.mark.asyncio
async def test_judge_response_shape_is_named_tuple(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "pass", "reason": "ok"}', recorder)

    result = await judge_step_result("task", "criteria", "result")

    assert isinstance(result, JudgeResult)
