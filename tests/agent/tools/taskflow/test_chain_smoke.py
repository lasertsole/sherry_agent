"""Integrated smoke: StepJudge -> retry feedback -> Goal Loop -> Finish Gate.

One hermetic file drives the whole new quality-gate chain with a **stub
auxiliary LLM** (never the network):

* ``test_taskflow_judge_chain_and_finish_gate_smoke`` walks the real TaskFlow
  tools -- a step judge RETRY re-dispatches the step with the judge's feedback
  injected into the replacement task, a PASS then marks it done -- and then
  exercises each finish gate's reject and pass branch (Gate A pending, Gate B
  blocked, Gate C failing/stale evidence, Gate D SisyphusVerifier).
* ``test_goal_loop_smoke`` drives ``_execute_subagent`` through two turns: the
  completion judge says CONTINUE (its continuation prompt becomes the next
  human turn) and then DONE, so the loop ends with the second turn's output.

Both tests assert the links actually fired: judge call counts, the injected
feedback / continuation text, the goal-loop turn count, and each gate verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow import step_judge
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.todolist.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]

SESSION = "sess-chain-smoke"
FLOW = "flow-chain-smoke"
FLOW_B = "flow-chain-smoke-blocked"

dispatch_mod = sys.modules["agent.tools.taskflow.tools._dispatch"]


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _ScriptedLLM:
    """A stub auxiliary LLM that answers with one scripted content string."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list[dict]] = []

    async def ainvoke(self, messages: list[dict]) -> _Response:
        self.calls.append(messages)
        return _Response(self._content)


class _ScriptedLLMFactory:
    """``build_auxiliary_llm`` stand-in: the Nth judge call gets the Nth script.

    Each ``judge_step_result`` / ``judge_completion`` invocation builds exactly
    one LLM and invokes it once, so advancing the script per build makes the
    sequence deterministic across separate judge calls. ``llms`` is the
    judge-invocation ledger -- its length is the number of judge calls made.
    """

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.llms: list[_ScriptedLLM] = []

    def __call__(self, temperature: float | None = None) -> _ScriptedLLM:
        index = min(len(self.llms), len(self._contents) - 1)
        llm = _ScriptedLLM(self._contents[index])
        self.llms.append(llm)
        return llm


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


def _key_dispatch(keys: list[str], calls: list[str]):
    iterator = iter(keys)

    async def _dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        calls.append(task)
        return next(iterator)

    return _dispatch


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    """Redirect the shared evidence ledger into a per-test file."""
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.jsonl"))
    return EvidenceLedger


def _reset_ledger() -> None:
    Path(EvidenceLedger.LEDGER_PATH).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_taskflow_judge_chain_and_finish_gate_smoke(
    isolated_db: Path, ledger: EvidenceLedger, monkeypatch: pytest.MonkeyPatch
):
    # Given the real tool family, a recording dispatch seam, and a stub LLM that
    # answers the step judge with RETRY first and PASS second.
    dispatches: list[str] = []
    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_child",
        _key_dispatch(["agent:main:subagent:c1", "agent:main:subagent:c2"], dispatches),
    )
    judge_llm = _ScriptedLLMFactory(
        [
            '{"verdict": "retry", "reason": "missing token", "feedback": "emit the PASS token"}',
            '{"verdict": "pass", "reason": "criteria met"}',
        ]
    )
    monkeypatch.setattr(step_judge, "build_auxiliary_llm", judge_llm)

    criteria = "output must contain PASS"
    tools = _tools()
    await tools["taskflow_create"].coroutine(
        session_id=SESSION, flow_id=FLOW, description="chain smoke"
    )
    out = await tools["taskflow_run_task"].coroutine(
        flow_id=FLOW, task="write report", validation_criteria=criteria, session_id=SESSION
    )
    assert "dispatched" in out, out
    assert len(dispatches) == 1

    # Gate A reject: a dispatched step is not finishable.
    out = await tools["taskflow_finish"].coroutine(session_id=SESSION, flow_id=FLOW)
    assert out.startswith("Error: Cannot finish"), out
    assert "not done/blocked" in out, out

    # When the first result is judged RETRY: the step is re-dispatched and the
    # judge's feedback rides along on the replacement task.
    out = await tools["taskflow_resume"].coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        child_session_key="agent:main:subagent:c1",
        result="no token",
    )
    assert "judge: RETRY (1/2)" in out, out
    assert len(dispatches) == 2
    assert "## Previous Attempt Feedback" in dispatches[1], dispatches[1]
    assert "emit the PASS token" in dispatches[1], dispatches[1]
    assert len(judge_llm.llms) == 1, "exactly one judge call for the first result"

    # When the replacement result is judged PASS: the step is done.
    out = await tools["taskflow_resume"].coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        child_session_key="agent:main:subagent:c2",
        result="PASS token present",
    )
    assert "judge: PASS criteria met" in out, out
    assert len(judge_llm.llms) == 2, "two judge calls total"
    flow = await store_sqlite.get_flow(FLOW, SESSION)
    assert flow is not None
    assert flow["state"]["steps"][0]["status"] == "done"
    assert len(dispatches) == 2, "resume must not spawn beyond the judge retry"

    # Gate C reject (failing evidence) ...
    ledger.for_session(FLOW).append(kind="test", command="pytest -q", status="failed")
    out = await tools["taskflow_finish"].coroutine(session_id=SESSION, flow_id=FLOW)
    assert "failing verification evidence" in out, out

    # ... Gate C reject (stale evidence) ...
    _reset_ledger()
    ledger.for_session(FLOW).append(kind="test", command="pytest foo.py", status="passed")
    ledger.for_session(FLOW).mark_stale_for_path("foo.py")
    out = await tools["taskflow_finish"].coroutine(session_id=SESSION, flow_id=FLOW)
    assert "stale evidence" in out, out

    # ... Gate D reject (the verifier cannot reread the plan) ...
    _reset_ledger()
    out = await tools["taskflow_finish"].coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        todo={"flow_id": FLOW, "step_id": "step-1"},
        plan_path="/nonexistent/plan.md",
        checkbox_label="wire the gate",
    )
    assert "SisyphusVerifier" in out, out
    assert "plan not found" in out, out

    # ... and Gate D pass + Gate A/B/C pass: a real plan whose linked step is
    # done admits the finish.
    plan = Path(EvidenceLedger.LEDGER_PATH).parent / "plan.md"
    plan.write_text(
        "- [ ] wire the gate\n"
        "  Acceptance: the chain is wired end to end\n"
        "  Verification: pytest -q\n",
        encoding="utf-8",
    )
    out = await tools["taskflow_finish"].coroutine(
        session_id=SESSION,
        flow_id=FLOW,
        summary="chain smoke done",
        todo={"flow_id": FLOW, "step_id": "step-1"},
        plan_path=str(plan),
        checkbox_label="wire the gate",
    )
    assert "TaskFlow finished" in out and "status=done" in out, out

    # Gate B reject: a blocked step cannot be finished.
    await store_sqlite.create_flow(
        FLOW_B,
        {
            "description": "blocked probe",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "do s1",
                    "depends_on": [],
                    "status": "blocked",
                    "block_reason": "acceptance failed",
                }
            ],
            "results": [],
        },
        session_id=SESSION,
    )
    out = await tools["taskflow_finish"].coroutine(session_id=SESSION, flow_id=FLOW_B)
    assert out.startswith("Error: Cannot finish"), out
    assert "blocked" in out and "acceptance failed" in out, out


@pytest.mark.asyncio
async def test_goal_loop_smoke(monkeypatch: pytest.MonkeyPatch):
    # Lazy imports keep the subagent machinery out of the StepJudge test above.
    import agent.tools.subagent.registry.lifecycle as lifecycle
    import agent.tools.subagent.spawn.completion_judge as completion_judge
    import agent.tools.subagent.spawn.core as spawn_core
    import agent.tools.taskflow.evidence_collector as evidence_collector
    from agent.tools.subagent.types.registry import RunOutcomeStatus, SubagentRunRecord

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

    child = _FakeChildAgent(["turn-1 output", "turn-2 output"])
    completed: list = []

    async def _fake_build(**_: object) -> _FakeChildAgent:
        return child

    async def _fake_complete(run_id, outcome, result_text, expected_generation=None) -> None:
        completed.append((run_id, outcome, result_text))

    async def _noop(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build)
    monkeypatch.setattr(lifecycle, "complete_subagent_run", _fake_complete)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", lambda **_: None)
    # The subagent conftest's pub.func stub can shadow build_agent_config in a
    # whole-suite run; pin the seam when the stub is present.
    pub_func = sys.modules.get("pub.func")
    if pub_func is not None:
        monkeypatch.setattr(pub_func, "build_agent_config", lambda **_: {}, raising=False)

    judge_llm = _ScriptedLLMFactory(
        [
            '{"verdict": "continue", "reason": "tests not run", '
            '"continuation_prompt": "run pytest and report"}',
            '{"verdict": "done", "reason": "deliverable present"}',
        ]
    )
    monkeypatch.setattr(completion_judge, "build_auxiliary_llm", judge_llm)

    run = SubagentRunRecord(
        run_id="smoke-run",
        child_session_key="agent:main:subagent:smoke-child",
        requester_session_key="agent:main:session:test",
        task="produce the artifact",
        spawned_by="agent:main:session:test",
    )

    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message="produce the artifact",
        forked_messages=[],
        tools=[],
        timeout_seconds=0.0,
        goal_max_turns=3,
    )

    # The loop ran two turns: the judge's continuation prompt became turn 2.
    assert len(child.inputs) == 2, child.inputs
    assert child.inputs[1]["messages"][0].content == "run pytest and report"
    assert len(judge_llm.llms) == 2, "one completion-judge call per turn"
    assert completed[0][1].status == RunOutcomeStatus.OK
    assert completed[0][1].error is None
    assert completed[0][2] == "turn-2 output"
