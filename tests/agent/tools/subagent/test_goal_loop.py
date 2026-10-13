"""Subagent goal loop: verdicts, budget exhaustion, fail-open, pass-through."""

from __future__ import annotations

import asyncio
import sys
import uuid

import pytest

import agent.tools.subagent.registry.lifecycle as lifecycle
import agent.tools.subagent.spawn.completion_judge as completion_judge
import agent.tools.subagent.spawn.core as spawn_core
import agent.tools.taskflow.evidence_collector as evidence_collector
from agent.tools.subagent.spawn.completion_judge import (
    CompletionJudgeResult,
    CompletionVerdict,
)
from agent.tools.subagent.types.registry import RunOutcomeStatus, SubagentRunRecord
from config.features import COMPLETION_JUDGE

pytestmark = [pytest.mark.unit]

_GOAL_TASK = "produce the artifact"


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChildAgent:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.inputs: list[dict] = []

    async def ainvoke(self, input: dict, config: dict) -> dict:
        self.inputs.append(input)
        index = min(len(self.inputs) - 1, len(self.responses) - 1)
        return {"messages": [_Msg(self.responses[index])]}


def _make_run() -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        child_session_key=f"agent:main:subagent:{uuid.uuid4().hex[:12]}",
        requester_session_key="agent:main:session:test",
        task=_GOAL_TASK,
        spawned_by="agent:main:session:test",
    )


def _install_deps(monkeypatch: pytest.MonkeyPatch, child: _FakeChildAgent) -> list:
    """Patch the child builder, completion seam, hooks, and evidence collector."""
    captured: list = []

    async def _fake_build(**_: object) -> _FakeChildAgent:
        return child

    async def _fake_complete(
        run_id: str, outcome, result_text, expected_generation: int | None = None
    ) -> None:
        captured.append((run_id, outcome, result_text))

    async def _noop(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build)
    monkeypatch.setattr(lifecycle, "complete_subagent_run", _fake_complete)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", lambda **_: None)
    # The subagent conftest's pub.func stub is clobbered in whole-suite runs
    # (build_agent_config resolves to the module, not a callable); pin the seam.
    pub_func = sys.modules.get("pub.func")
    if pub_func is not None:
        monkeypatch.setattr(pub_func, "build_agent_config", lambda **_: {}, raising=False)
    return captured


def _install_judge(monkeypatch: pytest.MonkeyPatch, decisions: list[CompletionJudgeResult]):
    recorded: list[tuple[str, str]] = []
    iterator = iter(decisions)

    async def _fake(task_text: str, last_response: str, evidence_summary: str | None = None):
        recorded.append((task_text, last_response))
        return next(iterator)

    monkeypatch.setattr(completion_judge, "judge_completion", _fake)
    return recorded


def _done() -> CompletionJudgeResult:
    return CompletionJudgeResult(CompletionVerdict.DONE, "complete", "")


def _continue(prompt: str = "keep going") -> CompletionJudgeResult:
    return CompletionJudgeResult(CompletionVerdict.CONTINUE, "more work", prompt)


async def _run_goal(monkeypatch, run, child, *, goal_max_turns=5) -> list:
    captured = _install_deps(monkeypatch, child)
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message=_GOAL_TASK,
        tools=[],
        timeout_seconds=0.0,
        goal_max_turns=goal_max_turns,
    )
    return captured


@pytest.mark.asyncio
async def test_goal_loop_done_on_first_turn(monkeypatch: pytest.MonkeyPatch):
    run = _make_run()
    child = _FakeChildAgent(["turn-1 output"])
    recorded = _install_judge(monkeypatch, [_done()])

    captured = await _run_goal(monkeypatch, run, child)

    assert len(child.inputs) == 1
    assert len(recorded) == 1
    assert recorded[0][1] == "turn-1 output"
    assert captured[0][1].error is None
    assert captured[0][2] == "turn-1 output"


@pytest.mark.asyncio
async def test_goal_loop_continues_then_done(monkeypatch: pytest.MonkeyPatch):
    run = _make_run()
    child = _FakeChildAgent(["turn-1 output", "turn-2 output"])
    _install_judge(monkeypatch, [_continue("run the tests"), _done()])

    captured = await _run_goal(monkeypatch, run, child)

    assert len(child.inputs) == 2
    continuation = child.inputs[1]["messages"][0]
    assert continuation.content == "run the tests"
    assert captured[0][1].error is None
    assert captured[0][2] == "turn-2 output"


@pytest.mark.asyncio
async def test_goal_loop_budget_exhausted(monkeypatch: pytest.MonkeyPatch):
    run = _make_run()
    child = _FakeChildAgent(["turn-1 output", "turn-2 output"])
    recorded = _install_judge(monkeypatch, [_continue(), _continue(), _continue()])

    captured = await _run_goal(monkeypatch, run, child, goal_max_turns=2)

    assert len(child.inputs) == 2
    assert len(recorded) == 2
    outcome = captured[0][1]
    assert outcome.status == RunOutcomeStatus.OK
    assert outcome.error == "goal_loop_budget_exhausted"
    assert captured[0][2] == "turn-2 output"


@pytest.mark.asyncio
async def test_goal_loop_budget_of_one_is_single_turn(monkeypatch: pytest.MonkeyPatch):
    run = _make_run()
    child = _FakeChildAgent(["only turn"])

    async def _boom(*_a, **_k):
        raise AssertionError("judge must not run when the budget is one turn")

    monkeypatch.setattr(completion_judge, "judge_completion", _boom)
    captured = await _run_goal(monkeypatch, run, child, goal_max_turns=1)

    assert len(child.inputs) == 1
    assert captured[0][1].error is None


# ---------------------------------------------------------------------------
# Parameter pass-through: schema -> spawn_subagent_direct -> lane -> executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_wrapper_passes_goal_params(monkeypatch: pytest.MonkeyPatch):
    from agent.tools.subagent.registry import clear as clear_registry
    from agent.tools.subagent.registry import set_run

    captured: list = []

    async def _fake_execute(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent", _fake_execute)
    clear_registry()
    try:
        run = _make_run()
        set_run(run)
        await spawn_core._execute_subagent_with_lane(
            run=run,
            system_prompt="sys",
            user_message=_GOAL_TASK,
            tools=[],
            timeout_seconds=0.0,
            goal_max_turns=3,
        )
    finally:
        clear_registry()

    assert captured[0]["goal_max_turns"] == 3


@pytest.mark.asyncio
async def test_spawn_direct_passes_goal_params(monkeypatch: pytest.MonkeyPatch):
    from agent.tools.subagent.registry import clear as clear_registry
    from agent.tools.subagent.spawn.core import spawn_subagent_direct

    captured: list = []

    async def _fake_lane(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    try:
        result = await spawn_subagent_direct(
            task=_GOAL_TASK,
            requester_session_key="agent:main:session:test",
            goal_max_turns=4,
        )
        await asyncio.sleep(0.01)
    finally:
        clear_registry()

    assert result.status == "accepted"
    assert captured[0]["goal_max_turns"] == 4


@pytest.mark.asyncio
async def test_spawn_direct_defaults_budget_from_config(monkeypatch: pytest.MonkeyPatch):
    from agent.tools.subagent.registry import clear as clear_registry
    from agent.tools.subagent.spawn.core import spawn_subagent_direct

    captured: list = []

    async def _fake_lane(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    monkeypatch.setitem(COMPLETION_JUDGE, "goal_max_turns", 7)
    clear_registry()
    try:
        await spawn_subagent_direct(
            task=_GOAL_TASK,
            requester_session_key="agent:main:session:test",
        )
        await asyncio.sleep(0.01)
    finally:
        clear_registry()

    assert captured[0]["goal_max_turns"] == 7


@pytest.mark.asyncio
async def test_sessions_spawn_schema_forwards_goal_params(monkeypatch: pytest.MonkeyPatch):
    from agent.tools.subagent.tools import sessions_spawn as sessions_spawn_module
    from agent.tools.subagent.spawn.core import SpawnResult

    captured: dict = {}

    async def _fake_spawn(**kwargs: object) -> SpawnResult:
        captured.update(kwargs)
        return SpawnResult(status="accepted", run_id="run-1", child_session_key="child-1")

    monkeypatch.setattr(sessions_spawn_module, "spawn_subagent_direct", _fake_spawn)
    tool = sessions_spawn_module.SessionsSpawnTool(session_id="s-1")

    await tool._arun(task=_GOAL_TASK, goal_max_turns=3)

    assert captured["goal_max_turns"] == 3
