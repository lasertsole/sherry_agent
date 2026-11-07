"""The PTC compiler: JSON contract, validation, and one retry."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from agent.tools.ptc import compiler

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_VALID = "from sherry_tools import read_file\nprint(read_file(file_path='x'))\n"


class _Model:
    def __init__(self, replies: list[str]) -> None:
        self._replies = iter(replies)
        self.prompts: list[str] = []

    async def ainvoke(self, messages, **_kwargs):
        self.prompts.append(messages[-1].content)
        return AIMessage(content=next(self._replies))


def _patch_model(monkeypatch, model: _Model) -> None:
    monkeypatch.setattr("models.build_auxiliary_llm", lambda **_kwargs: model)


async def _compile(monkeypatch, replies: list[str]) -> tuple[str | None, _Model]:
    model = _Model(replies)
    _patch_model(monkeypatch, model)
    code = await compiler.compile_step_script(
        plan_key="0123456789ab",
        plan_name="plan",
        step_id="step-1",
        flow_id="flow-1",
        task="read the file",
        success_steps=[
            {"step": "read", "tool": "read_file", "params": "file_path='x'", "outcome": "ok"}
        ],
        hard_constraints=["always use the relative path"],
        skill_excerpt="# skill\nUse read_file.",
        tool_signatures=["read_file(file_path)"],
        allowed_tools={"read_file"},
    )
    return code, model


@pytest.mark.asyncio
async def test_compiles_the_returned_source(monkeypatch) -> None:
    code, model = await _compile(
        monkeypatch,
        [json.dumps({"status": "compiled", "code": _VALID, "reason": "ok"})],
    )

    assert code == _VALID
    assert "read the file" in model.prompts[0]
    assert "read_file(file_path)" in model.prompts[0]


@pytest.mark.asyncio
async def test_skip_returns_none(monkeypatch) -> None:
    code, _ = await _compile(
        monkeypatch,
        [json.dumps({"status": "skip", "reason": "dynamic parameter"})],
    )

    assert code is None


@pytest.mark.asyncio
async def test_invalid_first_script_is_retried_once(monkeypatch) -> None:
    code, model = await _compile(
        monkeypatch,
        [
            json.dumps({"status": "compiled", "code": "import os\nprint('x')\n"}),
            json.dumps({"status": "compiled", "code": _VALID, "reason": "fixed"}),
        ],
    )

    assert code == _VALID
    assert len(model.prompts) == 2
    assert "import not allowed" in model.prompts[1]


@pytest.mark.asyncio
async def test_exhausted_attempts_return_none(monkeypatch) -> None:
    code, model = await _compile(
        monkeypatch,
        [
            json.dumps({"status": "compiled", "code": "print('x')\n"}),
            json.dumps({"status": "compiled", "code": "print('y')\n"}),
        ],
    )

    assert code is None
    assert len(model.prompts) == 2
