"""Subagent task survival + drift guard (2026-11-23 意图锚钉与漂移门 plan).

Locks the three-line-of-defence design:

1. the task lives in the child's system prompt (compression cannot evict a
   SystemMessage), plus the initial task message is marked ``origin: user`` so
   a compaction summary labels it ``[User]`` (2000 chars) instead of
   ``[System notice]`` (500 chars);
2. the task's acceptance clauses are persisted in the child's state register
   and cleared on release;
3. the completion judge can return DRIFTED, which injects a correction that
   refocuses the child, and three drifts report ``task_drift_3_times``.

The plan's "修 10" (opening ``need_update_system_prompt`` for children) is
deliberately NOT applied: a child's system prompt is outside the compressed
message list, and that flag would rebuild it through the MAIN agent's builder,
replacing the child prompt. ``test_child_prompt_survives_compression_without_flag``
locks that decision.
"""

from __future__ import annotations

import sys
import uuid

import pytest

import agent.tools.subagent.registry.lifecycle as registry_lifecycle
import agent.tools.subagent.spawn.completion_judge as completion_judge_module
import agent.tools.subagent.spawn.core as spawn_core
import agent.tools.subagent.spawn.lifecycle as spawn_lifecycle
import agent.tools.taskflow.evidence_collector as evidence_collector
from agent.tools.subagent.spawn.completion_judge import (
    CompletionJudgeResult,
    CompletionVerdict,
    _build_completion_prompt,
    _parse_completion_response,
)
from agent.tools.subagent.spawn.system_prompt import (
    SubagentPromptContext,
    build_subagent_system_prompt_from_context,
)
from agent.tools.subagent.types.registry import RunOutcomeStatus, SubagentRunRecord
from agent.tools.taskflow.checklist_extractor import extract_acceptance_checklist
from runtime import state_register_db, state_register_mem

pytestmark = [pytest.mark.unit]

_TASK = (
    "Fix the JSON BOM handler. It must reject invalid UTF-8 and return a clear "
    "error message. Only the loader module may be changed."
)


def _ctx(task: str) -> SubagentPromptContext:
    return SubagentPromptContext(role="executor", task=task, functional_role="general")


# ── 1. system prompt carries the task ───────────────────────────────────────


def test_system_prompt_includes_task():
    prompt = build_subagent_system_prompt_from_context(_ctx(_TASK))
    assert f"## Your Task\n\n{_TASK}" in prompt
    # The task sits right after the role section (role + task before rules).
    assert prompt.index("## Your Role") < prompt.index("## Your Task") < prompt.index("## Rules")


def test_system_prompt_empty_task():
    prompt = build_subagent_system_prompt_from_context(_ctx(""))
    assert "## Your Task" not in prompt


def test_child_prompt_survives_compression_without_flag():
    """A child's SystemMessage is not in the compressed message list.

    ``need_update_system_prompt`` stays OFF for children: turning it on would
    route the rebuild through the main builder and replace the child prompt
    (which now carries the task). This locks that decision at the spec seam.
    """
    from agent.middlewares import specs

    summarization = specs._build_child_summarization(
        specs.ChildBuildContext(
            auxiliary_llm=object(),
            main_llm_context_window=1000,
            iteration_limit=5,
            summary_total_max_tokens=500,
            compression_threshold=800,
        )
    )
    assert summarization._need_update_system_prompt is False


# ── 2. task HumanMessage origin ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_humanmessage_has_origin_user(monkeypatch: pytest.MonkeyPatch):
    """The first child turn is a HumanMessage tagged origin=user."""
    import agent.tools.subagent.spawn.lifecycle as lifecycle
    from langchain_core.messages import HumanMessage

    seen: list[list] = []

    async def _fake_build_child(**_: object):
        class _Agent:
            async def ainvoke(self, input: dict, config: dict) -> dict:
                seen.append(input["messages"])
                return {"messages": [HumanMessage(content="done")]}

        return _Agent()

    async def _noop(*_: object, **__: object):
        return None

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build_child)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    # The completion/finalize seams are re-exported from the registry module.
    monkeypatch.setattr(registry_lifecycle, "complete_subagent_run", _noop)
    monkeypatch.setattr(lifecycle, "_release_child_resources", _noop)
    monkeypatch.setattr(lifecycle, "_capture_child_transcript", _noop)
    monkeypatch.setattr(lifecycle, "_kill_descendants_after_failure", _noop)
    pub_func = sys.modules.get("pub.func")
    if pub_func is not None:
        monkeypatch.setattr(pub_func, "build_agent_config", lambda **_: {}, raising=False)
    monkeypatch.delenv("SHERRY_SUBAGENT_GOAL_MAX_TURNS", raising=False)

    run = SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        child_session_key=f"agent:main:subagent:{uuid.uuid4().hex[:12]}",
        requester_session_key="agent:main:session:test",
        task=_TASK,
        spawned_by="agent:main:session:test",
    )
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message=_TASK,
        tools=[],
        timeout_seconds=0.0,
    )
    assert seen, "child agent was never invoked"
    first = seen[0][0]
    assert isinstance(first, HumanMessage)
    assert (
        first.additional_kwargs.get("metadata", {}).get("origin") == "user"
        or getattr(first, "metadata", {}).get("origin") == "user"
    )


def test_compression_labels_task_as_user():
    """A task message with origin=user is summarized as [User] at 2000 chars."""
    from langchain_core.messages import HumanMessage

    from agent.middlewares.summarization.summary_generation import _serialize_for_summary

    task = "must " + "x" * 2500
    message = HumanMessage(content=task, metadata={"origin": "user"})
    rendered = _serialize_for_summary([message])
    assert "[User]:" in rendered
    # 2000 chars of the task survive (content[:2000]); the unmarked path keeps 500.
    assert "x" * 1990 in rendered
    assert "[System notice]" not in rendered
    bare = _serialize_for_summary([HumanMessage(content=task)])
    assert "[System notice]:" in bare
    assert "x" * 490 in bare


# ── 3. acceptance persistence ───────────────────────────────────────────────


def test_acceptance_written_on_spawn(monkeypatch: pytest.MonkeyPatch):
    session = f"agent:main:subagent:{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(spawn_lifecycle, "_extract_acceptance", lambda _t: ["must pass tests"])
    acceptance = spawn_lifecycle._store_acceptance(session, _TASK)
    assert acceptance == ["must pass tests"]
    assert state_register_mem.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY) == acceptance
    assert state_register_db.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY) == acceptance
    # cleanup for the test process
    spawn_lifecycle._clear_acceptance(session)


def test_acceptance_cleared_on_completion(monkeypatch: pytest.MonkeyPatch):
    session = f"agent:main:subagent:{uuid.uuid4().hex[:12]}"
    state_register_mem.set_state(session, spawn_lifecycle._ACCEPTANCE_KEY, ["must pass"])
    spawn_lifecycle._clear_acceptance(session)
    assert state_register_mem.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY) is None


def test_acceptance_cleared_on_failure(monkeypatch: pytest.MonkeyPatch):
    """release_child_session_state clears acceptance even on non-done paths."""
    session = f"agent:main:subagent:{uuid.uuid4().hex[:12]}"
    state_register_mem.set_state(session, spawn_lifecycle._ACCEPTANCE_KEY, ["must pass"])
    run = SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        child_session_key=session,
        requester_session_key="agent:main:session:test",
        task=_TASK,
        spawned_by="agent:main:session:test",
    )
    spawn_lifecycle.release_child_session_state(run)
    assert state_register_mem.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY) is None


def test_acceptance_extract_from_task():
    clauses = extract_acceptance_checklist(_TASK)
    assert clauses, "the plan's sample task must yield acceptance clauses"
    assert any("reject" in c.lower() for c in clauses)


# ── 4. completion judge DRIFTED ─────────────────────────────────────────────


def test_completion_judge_drifted():
    result = _parse_completion_response(
        '{"verdict": "drifted", "reason": "refactoring unrelated code", '
        '"drift_correction": "return to the BOM handler"}'
    )
    assert result.verdict is CompletionVerdict.DRIFTED
    assert result.reason == "refactoring unrelated code"
    assert result.drift_correction == "return to the BOM handler"
    assert result.continuation_prompt == ""


def test_completion_judge_continue_when_aligned():
    result = _parse_completion_response(
        '{"verdict": "continue", "reason": "half done", "continuation_prompt": "add tests"}'
    )
    assert result.verdict is CompletionVerdict.CONTINUE
    assert result.continuation_prompt == "add tests"
    assert result.drift_correction == ""


def test_completion_judge_acceptance_passed():
    """_build_completion_prompt renders acceptance clauses when provided."""
    prompt = _build_completion_prompt(_TASK, "output", None, ["must reject", "only loader"])
    assert "## Acceptance Criteria (assess alignment)" in prompt
    assert "1. must reject" in prompt
    assert "2. only loader" in prompt
    prompt_without = _build_completion_prompt(_TASK, "output", None)
    assert "## Acceptance Criteria" not in prompt_without


def test_judge_completion_forwards_acceptance(monkeypatch: pytest.MonkeyPatch):
    """judge_completion passes acceptance through to the prompt builder."""
    captured: dict = {}

    class _LLM:
        async def ainvoke(self, messages):
            captured["prompt"] = messages[1]["content"]
            return type("R", (), {"content": '{"verdict": "done", "reason": "ok"}'})()

    monkeypatch.setattr(completion_judge_module, "build_auxiliary_llm", lambda **_: _LLM())

    import asyncio

    result = asyncio.run(
        completion_judge_module.judge_completion(
            _TASK, "output", acceptance=["must reject invalid UTF-8"]
        )
    )
    assert result.verdict is CompletionVerdict.DONE
    assert "must reject invalid UTF-8" in captured["prompt"]


# ── 5. goal loop drift correction ────────────────────────────────────────────


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
        task=_TASK,
        spawned_by="agent:main:session:test",
    )


def _done() -> CompletionJudgeResult:
    return CompletionJudgeResult(CompletionVerdict.DONE, "complete", "")


def _drifted(correction: str = "") -> CompletionJudgeResult:
    return CompletionJudgeResult(CompletionVerdict.DRIFTED, "off task", "", correction)


def _continue(prompt: str = "keep going") -> CompletionJudgeResult:
    return CompletionJudgeResult(CompletionVerdict.CONTINUE, "more work", prompt)


def _install_goal_deps(
    monkeypatch: pytest.MonkeyPatch, child: _FakeChildAgent, decisions: list[CompletionJudgeResult]
) -> tuple[list, list]:
    captured: list = []
    recorded_acceptance: list = []

    async def _fake_build(**_: object) -> _FakeChildAgent:
        return child

    async def _fake_complete(run_id, outcome, result_text, expected_generation=None, handoff=None):
        captured.append((run_id, outcome, result_text, handoff))

    async def _noop(*_: object, **__: object) -> None:
        return None

    async def _fake_judge(
        task_text, last_response, evidence_summary=None, acceptance=None, **_
    ) -> CompletionJudgeResult:
        recorded_acceptance.append(acceptance)
        return decisions.pop(0)

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build)
    monkeypatch.setattr(registry_lifecycle, "complete_subagent_run", _fake_complete)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", lambda **_: None)
    monkeypatch.setattr(completion_judge_module, "judge_completion", _fake_judge)
    monkeypatch.setattr(
        spawn_lifecycle, "_read_acceptance", lambda _k: ["must reject invalid UTF-8"]
    )
    pub_func = sys.modules.get("pub.func")
    if pub_func is not None:
        monkeypatch.setattr(pub_func, "build_agent_config", lambda **_: {}, raising=False)
    return captured, recorded_acceptance


@pytest.mark.asyncio
async def test_drift_correction_injected(monkeypatch: pytest.MonkeyPatch):
    run = _make_run()
    child = _FakeChildAgent(["drifted output", "back on task"])
    _, recorded = _install_goal_deps(
        monkeypatch, child, [_drifted("Return to the BOM handler now."), _done()]
    )
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message=_TASK,
        tools=[],
        timeout_seconds=0.0,
        goal_max_turns=5,
    )
    assert len(child.inputs) >= 2
    correction = child.inputs[1]["messages"][0]
    assert "Return to the BOM handler now." in correction.content
    assert correction.metadata == {"internal": True, "provenance": "drift_correction"}
    assert recorded and recorded[0] == ["must reject invalid UTF-8"]


@pytest.mark.asyncio
async def test_consecutive_drift_reports(monkeypatch: pytest.MonkeyPatch):
    run = _make_run()
    child = _FakeChildAgent(["d1", "d2", "d3"])
    captured, _ = _install_goal_deps(monkeypatch, child, [_drifted(), _drifted(), _drifted()])
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message=_TASK,
        tools=[],
        timeout_seconds=0.0,
        goal_max_turns=10,
    )
    assert captured, "run must be finalized with an outcome"
    outcome = captured[0][1]
    assert outcome is not None
    assert outcome.status is RunOutcomeStatus.BUDGET_EXHAUSTED
    assert outcome.error == "task_drift_3_times"


@pytest.mark.asyncio
async def test_drift_count_accumulates(monkeypatch: pytest.MonkeyPatch):
    """DRIFTED -> CONTINUE -> DRIFTED must not reset the counter."""
    run = _make_run()
    child = _FakeChildAgent(["d1", "c2", "d3", "d4"])
    captured, _ = _install_goal_deps(
        monkeypatch,
        child,
        [
            _drifted(),
            _continue("keep working"),
            _drifted(),
            _drifted(),
        ],
    )
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message=_TASK,
        tools=[],
        timeout_seconds=0.0,
        goal_max_turns=10,
    )
    outcome = captured[0][1]
    assert outcome is not None
    assert outcome.error == "task_drift_3_times"


@pytest.mark.asyncio
async def test_judge_drifted_before_turn_budget(monkeypatch: pytest.MonkeyPatch):
    """A drift on the final budgeted turn is reported as drift, not plain budget exhaustion."""
    run = _make_run()
    child = _FakeChildAgent(["d1", "d2", "d3"])
    captured, _ = _install_goal_deps(monkeypatch, child, [_drifted(), _drifted(), _drifted()])
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message=_TASK,
        tools=[],
        timeout_seconds=0.0,
        goal_max_turns=3,
    )
    outcome = captured[0][1]
    assert outcome is not None
    assert outcome.error == "task_drift_3_times"


def test_state_keys_uses_child_session_bucket():
    """A child's acceptance state is isolated by its child_session_key."""
    session = f"agent:main:subagent:{uuid.uuid4().hex[:12]}"
    spawn_lifecycle._store_acceptance(session, _TASK + " must pass")
    assert state_register_mem.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY)
    assert state_register_db.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY)
    spawn_lifecycle._clear_acceptance(session)
    assert state_register_mem.get_state(session, spawn_lifecycle._ACCEPTANCE_KEY) is None
