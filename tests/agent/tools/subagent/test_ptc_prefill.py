"""Spawn-side PTC replay: executor gate, success, and React progress."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.subagent.spawn import core
from agent.tools.subagent.types.functional_role import FunctionalRole
from config.features import PTC
from agent.tools.subagent.types.registry import SubagentRunRecord

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_PLAN = "0123456789ab"


def _run(role: FunctionalRole = FunctionalRole.EXECUTOR) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id="run-1",
        child_session_key="agent:executor:subagent:child",
        requester_session_key="agent:main:session:parent",
        controller_session_key="agent:main:session:parent",
        task="step task",
        functional_role=role,
        step_id="step-1",
        spawned_cwd="/tmp",
    )


def _patch_candidate(monkeypatch, *, script: str | None = "print('x')") -> None:
    monkeypatch.setitem(PTC, "ptc_prefill_enabled", True)
    monkeypatch.setattr(core, "_ptc_tools_map", lambda *_args: {"read_file": object()})
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.identity.resolve_session_plan_identity",
        lambda _sid: SimpleNamespace(key=_PLAN),
    )
    monkeypatch.setattr(
        "agent.tools.ptc.scripts.find_step_script",
        lambda *_args, **_kwargs: Path("/skills/auto/a/scripts/ptc.py") if script else None,
    )
    monkeypatch.setattr(
        "agent.tools.ptc.scripts.read_step_script",
        lambda *_args, **_kwargs: script,
    )


def _patch_run_ptc(monkeypatch, payload: dict) -> None:
    async def _run_ptc(*_args, **_kwargs) -> str:
        return json.dumps(payload)

    monkeypatch.setattr("agent.tools.ptc.runner.run_ptc", _run_ptc)


@pytest.mark.asyncio
async def test_executor_with_a_script_succeeds_and_skips_react(monkeypatch) -> None:
    _patch_candidate(monkeypatch)
    _patch_run_ptc(
        monkeypatch,
        {
            "status": "ok",
            "exit_code": 0,
            "output": "final answer",
            "calls_log": [
                {"tool": "read_file", "args": {}, "result": "ok", "ok": True},
            ],
        },
    )

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert progress is None
    assert result is not None
    assert result["output"] == "final answer"
    assert result["calls_log"][0]["tool"] == "read_file"


@pytest.mark.asyncio
async def test_failure_renders_the_completed_prefix_and_failed_step(monkeypatch) -> None:
    _patch_candidate(monkeypatch)
    _patch_run_ptc(
        monkeypatch,
        {
            "status": "error",
            "exit_code": 1,
            "output": "",
            "calls_log": [
                {"tool": "read_file", "args": {"file_path": "a"}, "result": "ok", "ok": True},
                {"tool": "write_file", "args": {}, "error": "denied", "ok": False},
            ],
        },
    )

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert result is None
    assert progress is not None
    assert "[PTC Pre-run Progress]" in progress
    assert "read_file" in progress
    assert "DO NOT redo" in progress
    assert "Failed at step 2" in progress
    assert "denied" in progress


@pytest.mark.asyncio
async def test_empty_output_or_missing_calls_log_falls_back(monkeypatch) -> None:
    _patch_candidate(monkeypatch)
    _patch_run_ptc(
        monkeypatch,
        {"status": "ok", "exit_code": 0, "output": "   ", "calls_log": []},
    )

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert result is None
    assert progress is not None
    assert "no tool calls" in progress


@pytest.mark.asyncio
async def test_schema_miss_falls_back_with_a_failed_output_schema_step(monkeypatch) -> None:
    _patch_candidate(monkeypatch)
    _patch_run_ptc(
        monkeypatch,
        {
            "status": "ok",
            "exit_code": 0,
            "output": "not json",
            "calls_log": [{"tool": "read_file", "args": {}, "result": "ok", "ok": True}],
        },
    )

    result, progress = await core._try_ptc_prefill(
        _run(),
        [],
        [],
        {"type": "object", "required": ["answer"]},
    )

    assert result is None
    assert progress is not None
    assert "output_schema" in progress


@pytest.mark.asyncio
async def test_non_executor_never_prefills(monkeypatch) -> None:
    _patch_candidate(monkeypatch)

    async def _boom(*_args, **_kwargs):
        raise AssertionError("run_ptc must not run for a read-only role")

    monkeypatch.setattr("agent.tools.ptc.runner.run_ptc", _boom)

    result, progress = await core._try_ptc_prefill(
        _run(FunctionalRole.RESEARCHER), [], [], None
    )

    assert result is None
    assert progress is None


@pytest.mark.asyncio
async def test_missing_script_is_a_silent_react_fallback(monkeypatch) -> None:
    _patch_candidate(monkeypatch, script=None)

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert result is None
    assert progress is None


@pytest.mark.asyncio
async def test_disabled_flag_never_prefills(monkeypatch) -> None:
    _patch_candidate(monkeypatch)
    monkeypatch.setitem(PTC, "ptc_prefill_enabled", False)

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert result is None
    assert progress is None


@pytest.mark.asyncio
async def test_scoped_execution_returns_ptc_output_without_building_react(monkeypatch) -> None:
    finalized: list[tuple] = []

    async def _prefill(*_args, **_kwargs):
        return {"output": "ptc answer", "calls_log": [{"tool": "read_file", "ok": True}]}, None

    async def _release(*_args, **_kwargs) -> None:
        return None

    async def _finalize(run, outcome, result_text, handoff=None) -> None:
        finalized.append((run, outcome, result_text, handoff))

    async def _no_build(**_kwargs):
        raise AssertionError("the child agent must not be built after a PTC hit")

    monkeypatch.setattr(core, "_try_ptc_prefill", _prefill)
    monkeypatch.setattr(core, "_build_child_agent", _no_build)
    monkeypatch.setattr(core, "_release_child_resources", _release)
    monkeypatch.setattr(core, "_finalize_child_run", _finalize)

    await core._execute_subagent_scoped(_run(), "sys", "task", [], 1.0)

    assert len(finalized) == 1
    _run_record, outcome, result_text, handoff = finalized[0]
    assert outcome.status is core.RunOutcomeStatus.OK
    assert result_text == "ptc answer"
    assert handoff is None


@pytest.mark.asyncio
async def test_scoped_execution_injects_progress_before_react(monkeypatch) -> None:
    from langchain_core.messages import AIMessage

    captured: list[list] = []
    finalized: list[tuple] = []

    async def _prefill(*_args, **_kwargs):
        return None, "[PTC Pre-run Progress]\nCompleted (DO NOT redo):\n  1. read_file → ok"

    class _Agent:
        async def ainvoke(self, input, **_kwargs):
            captured.append(input["messages"])
            return {"messages": [AIMessage(content="react answer")]}

    async def _build(**_kwargs):
        return _Agent()

    async def _goal(*_args, **_kwargs):
        return "react answer", None

    async def _release(*_args, **_kwargs) -> None:
        return None

    async def _finalize(run, outcome, result_text, handoff=None) -> None:
        finalized.append((outcome, result_text, handoff))

    monkeypatch.setattr(core, "_try_ptc_prefill", _prefill)
    monkeypatch.setattr(core, "_build_child_agent", _build)
    monkeypatch.setattr(core, "_bind_child_run_context", lambda _run: {})
    monkeypatch.setattr(core, "_run_goal_loop", _goal)
    monkeypatch.setattr(core, "_release_child_resources", _release)
    monkeypatch.setattr(core, "_finalize_child_run", _finalize)

    await core._execute_subagent_scoped(_run(), "sys", "task", [], 1.0)

    assert captured, "React was never invoked"
    assert "[PTC Pre-run Progress]" in captured[0][0].content
    assert "DO NOT redo" in captured[0][0].content
    assert finalized and finalized[0][1] == "react answer"
