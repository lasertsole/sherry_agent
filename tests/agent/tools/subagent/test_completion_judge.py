"""T2.1 — CompletionJudge parsing, fail-open, and prompt assembly."""

from __future__ import annotations

import pytest

from agent.tools.subagent.spawn import completion_judge
from agent.tools.subagent.spawn.completion_judge import (
    CompletionJudgeResult,
    CompletionVerdict,
    judge_completion,
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
    monkeypatch.setitem(completion_judge.COMPLETION_JUDGE, "enabled", True)

    def _build(temperature: float | None = None) -> _RecordingLLM:
        llm = _RecordingLLM(content)
        recorder.append(llm)
        return llm

    monkeypatch.setattr(completion_judge, "build_auxiliary_llm", _build)


def _user_prompt(recorder: list) -> str:
    messages = recorder[0].calls[0]
    return str(messages[1]["content"])


@pytest.mark.asyncio
async def test_judge_parses_done(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "done", "reason": "deliverable present"}', recorder)

    result = await judge_completion("do X", "X is done")

    assert result.verdict == CompletionVerdict.DONE
    assert result.reason == "deliverable present"
    assert result.continuation_prompt == ""


@pytest.mark.asyncio
async def test_judge_parses_continue_with_prompt(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(
        monkeypatch,
        '{"verdict": "continue", "reason": "tests not run", '
        '"continuation_prompt": "run pytest and report"}',
        recorder,
    )

    result = await judge_completion("do X", "partial")

    assert result.verdict == CompletionVerdict.CONTINUE
    assert result.reason == "tests not run"
    assert result.continuation_prompt == "run pytest and report"


@pytest.mark.asyncio
async def test_judge_tolerates_malformed_json_via_json_repair(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "continue", "reason": "broken",', recorder)

    result = await judge_completion("do X", "partial")

    assert result.verdict == CompletionVerdict.CONTINUE


@pytest.mark.asyncio
async def test_judge_fails_open_when_llm_unavailable(monkeypatch: pytest.MonkeyPatch):
    def _build(temperature: float | None = None):
        raise RuntimeError("aux llm offline")

    monkeypatch.setitem(completion_judge.COMPLETION_JUDGE, "enabled", True)
    monkeypatch.setattr(completion_judge, "build_auxiliary_llm", _build)

    result = await judge_completion("do X", "whatever")

    assert result.verdict == CompletionVerdict.DONE
    assert "judge error" in result.reason


@pytest.mark.asyncio
async def test_judge_fails_open_on_unparseable_response(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, "I cannot decide.", recorder)

    result = await judge_completion("do X", "whatever")

    assert result.verdict == CompletionVerdict.DONE
    assert "fail-open" in result.reason


@pytest.mark.asyncio
async def test_judge_disabled_short_circuits_without_llm(monkeypatch: pytest.MonkeyPatch):
    def _build(temperature: float | None = None):
        raise AssertionError("disabled judge must not build an LLM")

    monkeypatch.setattr(completion_judge, "build_auxiliary_llm", _build)
    monkeypatch.setitem(completion_judge.COMPLETION_JUDGE, "enabled", False)

    result = await judge_completion("do X", "whatever")

    assert result.verdict == CompletionVerdict.DONE
    assert "disabled" in result.reason


@pytest.mark.asyncio
async def test_judge_accepts_missing_evidence_summary(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "done", "reason": "ok"}', recorder)

    result = await judge_completion("do X", "done", evidence_summary=None)

    assert result.verdict == CompletionVerdict.DONE
    assert "(none recorded)" in _user_prompt(recorder)


@pytest.mark.asyncio
async def test_judge_includes_evidence_summary_when_supplied(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "done", "reason": "ok"}', recorder)

    await judge_completion("do X", "done", evidence_summary="[pass] test: `pytest`")

    assert "[pass] test: `pytest`" in _user_prompt(recorder)


@pytest.mark.asyncio
async def test_judge_truncates_long_response(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "done", "reason": "ok"}', recorder)

    await judge_completion("do X", "Y" * 9000)

    prompt = _user_prompt(recorder)
    assert "Y" * 8000 in prompt
    assert "Y" * 8001 not in prompt


@pytest.mark.asyncio
async def test_judge_response_shape_is_named_tuple(monkeypatch: pytest.MonkeyPatch):
    recorder: list = []
    _install_llm(monkeypatch, '{"verdict": "done", "reason": "ok"}', recorder)

    result = await judge_completion("do X", "done")

    assert isinstance(result, CompletionJudgeResult)
