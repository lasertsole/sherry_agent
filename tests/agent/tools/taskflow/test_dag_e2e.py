"""End-to-end DAG + parallel dispatch flow (todo 9, taskflow-dag-phase1).

Drives the REAL taskflow tools through a dependency graph, a batch dispatch,
and a simulated process restart. Both external seams are faked so neither the
real spawn pipeline nor the real subagent registry is ever touched:

* ``agent.tools.taskflow.tools._dispatch.dispatch_child`` -- an in-memory
  recorder of every spawn; the real ``spawn_subagent_direct`` is never called.
* ``taskflow_wait_all``'s registry seam (``get_run_by_child_session_key`` /
  ``is_live_unended_run``) -- in-memory liveness that flips ``live -> settled``
  after the first poll; the real registry is never queried.

The fake liveness plus a 0.01s poll interval mean the test never depends on
network, wall-clock time, or a real child session.

Flow exercised:
    create -> run_task(A) [dispatched]
           -> run_task(B, depends_on=["step-1"]) [blocked, no spawn]
           -> run_task(C) [dispatched]
           -> resume(A) [step-1 done, step-2 ready reported, NO spawn for B]
           -> dispatch(["step-2"]) [spawned]
    == simulated restart (fresh store init + new event loop) ==
           -> wait_all [fake liveness flips settled]
           -> resume(B), resume(C)
           -> finish -> summary [counts line]

The same file also carries one E2E test per feature added after the original
DAG flow: budget tracking (GAP-3), deadlines (GAP-4), the progress report
(GAP-6), validation criteria (GAP-7), retry policy (GAP-8) and the
cross-session board (GAP-9). They share the same two fake seams, plus a frozen
clock for the deadline math, so no test depends on wall-clock time.
"""

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite

# Package-attribute traversal is shadowed by the re-exported tool objects
# (the family package binds the tool under the module's name); sys.modules
# keys are exact strings and immune to that shadowing.
dispatch_mod = sys.modules["agent.tools.taskflow.tools._dispatch"]
wait_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_wait_all")
create_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_create")
summary_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_summary")

pytestmark = [pytest.mark.unit]

FLOW = "flow-dag-e2e"

TASK_A = "collect data"
TASK_B = "aggregate data"
TASK_C = "write report"

CHILD_A = "agent:main:subagent:dag-a"
CHILD_B = "agent:main:subagent:dag-b"
CHILD_C = "agent:main:subagent:dag-c"

FLOW_BUDGET = "flow-dag-budget"
FLOW_DEADLINE = "flow-dag-deadline"
FLOW_PROGRESS = "flow-dag-progress"
FLOW_VALIDATION = "flow-dag-validation"
FLOW_RETRY = "flow-dag-retry"
FLOW_BOARD_A = "flow-board-a"
FLOW_BOARD_B = "flow-board-b"
FLOW_BOARD_DONE = "flow-board-done"

_CHILD_BY_TASK = {TASK_A: CHILD_A, TASK_B: CHILD_B, TASK_C: CHILD_C}


class _Run:
    """Minimal stand-in for a subagent run record (only liveness matters)."""

    def __init__(self, key: str, live) -> None:
        self.child_session_key = key
        self.live = live


def _flip_live() -> object:
    """A liveness callable that reports live once, then settled forever."""

    state = {"checks": 0}

    def _live() -> bool:
        state["checks"] += 1
        return state["checks"] == 1

    return _live


def _capture_statuses(flow: dict) -> dict[str, str]:
    return {step["step_id"]: step["status"] for step in flow["state"]["steps"]}


def _wire(monkeypatch: pytest.MonkeyPatch, dispatched: list, queried: list):
    """Install the fake dispatch recorder and the fake registry liveness seam."""

    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        dispatched.append((task, requester_session_key, label))
        return _CHILD_BY_TASK[task]

    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake_dispatch)

    # step-2 (B) and step-3 (C) are the dispatched steps when wait_all runs;
    # both flip from live to settled after their first check.
    runs = {CHILD_B: _Run(CHILD_B, _flip_live()), CHILD_C: _Run(CHILD_C, _flip_live())}

    def fake_get_run(child_session_key: str):
        queried.append(child_session_key)
        return runs.get(child_session_key)

    def fake_is_live(run: _Run) -> bool:
        live = run.live
        return bool(live()) if callable(live) else bool(live)

    monkeypatch.setattr(wait_mod, "get_run_by_child_session_key", fake_get_run)
    monkeypatch.setattr(wait_mod, "is_live_unended_run", fake_is_live)


class _RecordingDispatch:
    """Fake ``_dispatch.dispatch_child`` recording every spawn in order."""

    def __init__(self, *child_keys: str) -> None:
        self.child_keys = list(child_keys)
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(
        self, task: str, requester_session_key: str, label: str | None = None
    ) -> str:
        self.calls.append((task, requester_session_key, label))
        if len(self.calls) > len(self.child_keys):
            raise AssertionError(
                f"unexpected spawn #{len(self.calls)} (only {len(self.child_keys)} key(s) seeded)"
            )
        return self.child_keys[len(self.calls) - 1]


def _patch_registry(monkeypatch: pytest.MonkeyPatch, runs: dict[str, _Run] | None = None) -> None:
    """Install a registry seam where an unknown child key counts as settled (dead)."""
    runs_map = dict(runs or {})

    def fake_get_run(child_session_key: str) -> _Run | None:
        return runs_map.get(child_session_key)

    def fake_is_live(run: _Run) -> bool:
        live = run.live
        return bool(live()) if callable(live) else bool(live)

    monkeypatch.setattr(wait_mod, "get_run_by_child_session_key", fake_get_run)
    monkeypatch.setattr(wait_mod, "is_live_unended_run", fake_is_live)


class _FrozenTime:
    """Minimal ``time`` module stand-in with a frozen wall clock.

    ``date`` rendering delegates to the real module, so summary lines about
    deadlines stay locale-correct while the duration math is deterministic.
    """

    def __init__(self, now: float) -> None:
        self._now = now

    @staticmethod
    def strftime(fmt: str, value: time.struct_time) -> str:
        return time.strftime(fmt, value)

    @staticmethod
    def localtime(ts: float) -> time.struct_time:
        return time.localtime(ts)

    def time(self) -> float:
        return self._now


def test_dag_e2e_parallel_flow_across_restart(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    # Given the real tool family with the two external seams faked
    dispatched: list = []
    queried: list = []
    _wire(monkeypatch, dispatched, queried)
    tools = {t.name: t for t in build_taskflow_tools()}

    async def phase1() -> dict[str, str]:
        # Given a fresh flow
        out = await tools["taskflow_create"].coroutine(flow_id=FLOW, description="dag e2e")
        assert "revision=1" in out

        # When A is dispatched
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW, task=TASK_A, session_id="sess-1"
        )
        assert "step-1" in out and "dispatched" in out

        # And B is registered while its dependency (step-1) is not done
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW, task=TASK_B, depends_on=["step-1"], session_id="sess-1"
        )
        # Then B is blocked and NOT spawned (only A's single spawn so far)
        assert "blocked" in out, out
        assert "pending=[step-1]" in out, out
        assert len(dispatched) == 1, "a blocked step must not spawn a child"

        # And an independent C is dispatched (parallel work)
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW, task=TASK_C, session_id="sess-1"
        )
        assert "step-3" in out and "dispatched" in out
        assert len(dispatched) == 2

        flow = await store_sqlite.get_flow(FLOW)
        assert flow is not None
        assert _capture_statuses(flow) == {
            "step-1": "dispatched",
            "step-2": "blocked",
            "step-3": "dispatched",
        }

        # When A's result is injected
        out = await tools["taskflow_resume"].coroutine(
            flow_id=FLOW, child_session_key=CHILD_A, result="data collected"
        )
        # Then step-1 is done, step-2 is reported ready, and B was NOT spawned
        assert "TaskFlow resumed" in out, out
        assert "step_id=step-1" in out, out
        assert "unlocked=[step-2]" in out, out
        assert len(dispatched) == 2, "resume must not auto-spawn the unlocked step"

        flow = await store_sqlite.get_flow(FLOW)
        assert flow is not None
        assert _capture_statuses(flow) == {
            "step-1": "done",
            "step-2": "ready",
            "step-3": "dispatched",
        }

        # When the unlocked step is dispatched explicitly
        out = await tools["taskflow_dispatch"].coroutine(
            flow_id=FLOW, step_ids=["step-2"], session_id="sess-1"
        )
        # Then it spawns (count -> 3) and becomes dispatched
        assert "step-2" in out and "dispatched" in out, out
        assert len(dispatched) == 3

        flow = await store_sqlite.get_flow(FLOW)
        assert flow is not None
        pre_restart = _capture_statuses(flow)
        assert pre_restart == {
            "step-1": "done",
            "step-2": "dispatched",
            "step-3": "dispatched",
        }
        return pre_restart

    pre_restart = asyncio.run(phase1())  # event loop 1 (first "process")

    # Simulated restart: fresh once-per-process init state; same db file
    # (exactly the pattern in test_taskflow_tools.py:121-143).
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)

    async def phase2() -> None:
        # Then the DAG survives the restart unchanged (stale-state guard)
        flow = await store_sqlite.get_flow(FLOW)
        assert flow is not None
        assert _capture_statuses(flow) == pre_restart, "DAG state must survive a restart"

        # When wait_all runs with the fake liveness flipping live -> settled
        out = await tools["taskflow_wait_all"].coroutine(
            flow_id=FLOW, timeout_seconds=5.0, poll_interval_seconds=0.01
        )
        # Then both dispatched steps are reported settled after at least one poll
        assert "Error" not in out, out
        assert out.count("settled=True") == 2, out
        assert "timeout" not in out.lower(), out
        assert queried.count(CHILD_B) >= 2 and queried.count(CHILD_C) >= 2, queried

        # When both remaining child results are injected
        for child_key, result in ((CHILD_B, "aggregated"), (CHILD_C, "report written")):
            out = await tools["taskflow_resume"].coroutine(
                flow_id=FLOW, child_session_key=child_key, result=result
            )
            assert "TaskFlow resumed" in out, out
        # Then no resume spawned anything
        assert len(dispatched) == 3, "resume must never spawn"

        # And every step is done with three injected results
        flow = await store_sqlite.get_flow(FLOW)
        assert flow is not None
        assert _capture_statuses(flow) == {
            "step-1": "done",
            "step-2": "done",
            "step-3": "done",
        }
        assert len(flow["state"]["results"]) == 3

        # When the flow is finished
        out = await tools["taskflow_finish"].coroutine(flow_id=FLOW, summary="dag complete")
        assert "status=done" in out

        # Then the summary shows the step-status counts line
        summary = await tools["taskflow_summary"].coroutine(flow_id=FLOW)
        assert "step statuses: blocked=0 ready=0 dispatched=0 done=3" in summary, summary
        assert "results: 3" in summary, summary

    asyncio.run(phase2())  # event loop 2 (second "process")

    # The dispatch seam was invoked exactly once per spawned step, in order.
    assert [task for task, _, _ in dispatched] == [TASK_A, TASK_C, TASK_B]
    assert all(key == "agent:main:session:sess-1" for _, key, _ in dispatched)


def test_dag_e2e_budget_set_resume_and_query(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """GAP-3: a budget is set, resume accumulates usage, query renders it."""
    # Given the real tool family and a recording dispatch seam
    fake = _RecordingDispatch("agent:main:subagent:budget-a")
    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake)
    tools = {t.name: t for t in build_taskflow_tools()}

    async def scenario() -> None:
        # Given a fresh flow with a 50k token budget
        out = await tools["taskflow_create"].coroutine(
            flow_id=FLOW_BUDGET, description="budget e2e"
        )
        assert "revision=1" in out, out
        out = await tools["taskflow_budget"].coroutine(
            flow_id=FLOW_BUDGET, action="set", token_budget=50000
        )
        assert "Budget set: flow_id=flow-dag-budget, token_budget=50,000, revision=2" in out, out

        # And one dispatched step
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW_BUDGET, task="estimate cost", session_id="sess-budget"
        )
        assert "step-1" in out and "dispatched" in out, out

        # When the child result is injected WITH its token usage
        out = await tools["taskflow_resume"].coroutine(
            flow_id=FLOW_BUDGET,
            child_session_key="agent:main:subagent:budget-a",
            result="cost estimated",
            token_usage={"input_tokens": 5000, "output_tokens": 2000, "model_name": "glm-5"},
        )
        assert "TaskFlow resumed" in out, out

        # Then the flow accumulated tokens and cost
        flow = await store_sqlite.get_flow(FLOW_BUDGET)
        assert flow is not None
        assert flow["total_tokens"] == 7000
        assert flow["total_cost"] == pytest.approx(0.0055)
        assert flow["token_budget"] == 50000

        # And the budget query renders tokens, cost, remaining and status exactly
        out = await tools["taskflow_budget"].coroutine(flow_id=FLOW_BUDGET, action="query")
        assert "  tokens: 7,000 / 50,000 (14.0%)" in out, out
        assert "  cost: $0.0055" in out, out
        assert "  remaining: 43,000 tokens" in out, out
        assert "  status: ok" in out, out

    asyncio.run(scenario())
    # The budget mutation and the resume spawn consumed the fake seam once each
    assert [call[0] for call in fake.calls] == ["estimate cost"]


def test_dag_e2e_deadline_not_overdue(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """GAP-4: a 24h deadline is created, rendered, and not yet overdue."""
    # Given a frozen wall clock so the deadline math is exact
    frozen_now = 1_800_000_000.0
    monkeypatch.setattr(create_mod, "time", _FrozenTime(frozen_now))
    monkeypatch.setattr(summary_mod, "time", _FrozenTime(frozen_now))
    tools = {t.name: t for t in build_taskflow_tools()}

    async def scenario() -> None:
        # When a flow is created with a 24h deadline
        out = await tools["taskflow_create"].coroutine(
            flow_id=FLOW_DEADLINE, description="deadline e2e", deadline_hours=24.0
        )
        # Then the create response and the row carry the deadline
        assert "deadline=" in out, out
        flow = await store_sqlite.get_flow(FLOW_DEADLINE)
        assert flow is not None
        assert flow["deadline_ts"] == frozen_now + 24 * 3600

        # And the summary renders the remaining hours
        summary = await tools["taskflow_summary"].coroutine(flow_id=FLOW_DEADLINE)
        assert "deadline:" in summary, summary
        assert "(24.0h remaining)" in summary, summary

        # And the overdue query is empty before the deadline ...
        overdue_now = await store_sqlite.get_overdue_flows(frozen_now)
        assert [item["flow_id"] for item in overdue_now] == []
        # ... but returns the flow once the deadline has passed
        overdue_later = await store_sqlite.get_overdue_flows(frozen_now + 25 * 3600)
        assert [item["flow_id"] for item in overdue_later] == [FLOW_DEADLINE]

    asyncio.run(scenario())


def test_dag_e2e_progress_report_tracks_unlock(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """GAP-6: the progress report reflects a done step unlocking a blocked one."""
    # Given the real tool family and a recording dispatch seam
    fake = _RecordingDispatch(CHILD_A)
    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake)
    tools = {t.name: t for t in build_taskflow_tools()}

    async def scenario() -> None:
        # Given step-1 dispatched and step-2 blocked on it
        out = await tools["taskflow_create"].coroutine(
            flow_id=FLOW_PROGRESS, description="progress e2e"
        )
        assert "revision=1" in out, out
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW_PROGRESS, task=TASK_A, session_id="sess-progress"
        )
        assert "dispatched" in out, out
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW_PROGRESS,
            task=TASK_B,
            depends_on=["step-1"],
            session_id="sess-progress",
        )
        assert "blocked" in out, out

        # When step-1's result is injected and step-2 unlocks
        out = await tools["taskflow_resume"].coroutine(
            flow_id=FLOW_PROGRESS, child_session_key=CHILD_A, result="data collected"
        )
        assert "unlocked=[step-2]" in out, out

        # Then the progress report renders completion, breakdown, next steps
        out = await tools["taskflow_progress"].coroutine(flow_id=FLOW_PROGRESS)
        assert f"Progress Report: {FLOW_PROGRESS}" in out, out
        assert "  Status: running" in out, out
        assert "  Completion: 1/2 steps (50%)" in out, out
        assert "  Breakdown: done=1 · dispatched=0 · ready=1 · blocked=0" in out, out
        assert "  Next steps:" in out, out
        assert f"    ○ [step-2] {TASK_B}" in out, out
        assert "  Results injected: 1" in out, out

    asyncio.run(scenario())
    assert len(fake.calls) == 1, "only step-1 may spawn; the blocked step never does"


def test_dag_e2e_validation_criteria_echo_and_summary(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """GAP-7: criteria are stored, echoed with a warning, and shown in summary."""
    # Adversarial (misleading_success_output): the child result claims SUCCESS
    # without satisfying the criteria; the tool must still surface the criteria
    # and the explicit warning instead of silently accepting the result.
    criteria = "output must contain PASS and must not contain ERROR"
    fake = _RecordingDispatch(CHILD_A)
    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake)
    tools = {t.name: t for t in build_taskflow_tools()}

    async def scenario() -> None:
        # Given a step dispatched with validation criteria
        out = await tools["taskflow_create"].coroutine(
            flow_id=FLOW_VALIDATION, description="validation e2e"
        )
        assert "revision=1" in out, out
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW_VALIDATION,
            task="write report",
            validation_criteria=criteria,
            session_id="sess-validation",
        )
        assert "dispatched" in out, out

        # Then the criteria are persisted on the step
        flow = await store_sqlite.get_flow(FLOW_VALIDATION)
        assert flow is not None
        assert flow["state"]["steps"][0]["validation_criteria"] == criteria

        # When a misleading "SUCCESS" result is injected
        out = await tools["taskflow_resume"].coroutine(
            flow_id=FLOW_VALIDATION,
            child_session_key=CHILD_A,
            result="SUCCESS! Everything is fine, trust me.",
        )
        # Then the resume echoes the criteria and the validation warning
        assert "TaskFlow resumed" in out, out
        assert f"\n  validation_criteria: {criteria}" in out, out
        assert "\n  ⚠ Result needs validation against criteria" in out, out

        # And the summary surfaces the criteria for the orchestrator's verdict
        summary = await tools["taskflow_summary"].coroutine(flow_id=FLOW_VALIDATION)
        assert f"validation_criteria={criteria}" in summary, summary
        flow = await store_sqlite.get_flow(FLOW_VALIDATION)
        assert flow is not None
        assert flow["state"]["steps"][0]["status"] == "done"

    asyncio.run(scenario())


def test_dag_e2e_retry_policy_redispatch_and_exhaustion(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """GAP-8: a dead child is re-dispatched once, then exhausted into a note."""
    task = "flaky aggregate"
    policy = {"max_retries": 1, "retry_delay_seconds": 0.0, "retry_on": []}
    fake = _RecordingDispatch("agent:main:subagent:retry-1", "agent:main:subagent:retry-2")
    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake)
    _patch_registry(monkeypatch)  # every seeded key is unknown -> settled (dead)
    tools = {t.name: t for t in build_taskflow_tools()}

    async def scenario() -> None:
        # Given a dispatched step carrying the retry policy
        out = await tools["taskflow_create"].coroutine(
            flow_id=FLOW_RETRY, description="retry e2e", session_id="sess-retry"
        )
        assert "revision=1" in out, out
        out = await tools["taskflow_run_task"].coroutine(
            flow_id=FLOW_RETRY, task=task, retry_policy=policy, session_id="sess-retry"
        )
        assert "child_session_key=agent:main:subagent:retry-1" in out, out

        # When wait_all sees the first child settle dead
        out = await tools["taskflow_wait_all"].coroutine(
            flow_id=FLOW_RETRY, timeout_seconds=5.0, poll_interval_seconds=0.01
        )
        # Then one replacement is spawned and the counter increments to the cap
        assert "settled=True" in out, out
        assert (
            "retry: re-dispatched step_id=step-1 -> "
            "child_session_key=agent:main:subagent:retry-2, "
            "retry_count=1/1, retry_delay_seconds=0.0" in out
        ), out
        assert len(fake.calls) == 2, fake.calls
        assert [call[0] for call in fake.calls] == [task, task]
        assert fake.calls[1][1] == "agent:main:session:sess-retry", fake.calls[1]
        flow = await store_sqlite.get_flow(FLOW_RETRY)
        assert flow is not None
        step = flow["state"]["steps"][0]
        assert step["retry_count"] == 1
        assert step["child_session_key"] == "agent:main:subagent:retry-2"
        assert step["status"] == "dispatched"

        # When the replacement child also settles dead
        out = await tools["taskflow_wait_all"].coroutine(
            flow_id=FLOW_RETRY, timeout_seconds=5.0, poll_interval_seconds=0.01
        )
        # Then the budget is exhausted: no third spawn, step done, failure note
        assert (
            "retry: step_id=step-1 exhausted (retry_count=1/1); "
            "marked done with a failure note" in out
        ), out
        assert len(fake.calls) == 2, fake.calls
        flow = await store_sqlite.get_flow(FLOW_RETRY)
        assert flow is not None
        step = flow["state"]["steps"][0]
        assert step["status"] == "done"
        assert step["retry_count"] == 1
        notes = [record for record in flow["state"]["results"] if record.get("retry_exhausted")]
        assert len(notes) == 1, notes
        assert notes[0]["child_session_key"] == "agent:main:subagent:retry-2"
        assert "retry budget exhausted" in notes[0]["result"]

    asyncio.run(scenario())


def test_dag_e2e_cross_session_board_and_status_filter(isolated_db: Path):
    """GAP-9: the board lists flows from different sessions and filters status."""
    tools = {t.name: t for t in build_taskflow_tools()}

    async def scenario() -> None:
        # Given active flows created by two DIFFERENT sessions plus a finished one
        for flow_id, session_id, description in (
            (FLOW_BOARD_A, "sess-board-a", "first board flow"),
            (FLOW_BOARD_B, "sess-board-b", "second board flow"),
        ):
            out = await tools["taskflow_create"].coroutine(
                flow_id=flow_id, description=description, session_id=session_id
            )
            assert "revision=1" in out, out
        out = await tools["taskflow_create"].coroutine(
            flow_id=FLOW_BOARD_DONE, description="finished board flow", session_id="sess-board-c"
        )
        assert "revision=1" in out, out
        out = await tools["taskflow_finish"].coroutine(
            flow_id=FLOW_BOARD_DONE, summary="board done"
        )
        assert "status=done" in out, out

        # Then the default active board lists both sessions' flows, no terminal one
        out = await tools["taskflow_list"].coroutine()
        assert "TaskFlow board (active): 2 flow(s)" in out, out
        assert FLOW_BOARD_A in out and FLOW_BOARD_B in out, out
        assert FLOW_BOARD_DONE not in out, out
        assert "first board flow" in out and "second board flow" in out, out

        # And status_filter="done" narrows to the finished flow only
        out = await tools["taskflow_list"].coroutine(status_filter="done")
        assert "TaskFlow board (done): 1 flow(s)" in out, out
        assert FLOW_BOARD_DONE in out, out
        assert FLOW_BOARD_A not in out and FLOW_BOARD_B not in out, out

        # And status_filter="all" includes active and terminal flows
        out = await tools["taskflow_list"].coroutine(status_filter="all")
        assert "TaskFlow board (all): 3 flow(s)" in out, out
        for flow_id in (FLOW_BOARD_A, FLOW_BOARD_B, FLOW_BOARD_DONE):
            assert flow_id in out, out

        # And the store proves the two active flows came from different sessions
        flow_a = await store_sqlite.get_flow(FLOW_BOARD_A)
        flow_b = await store_sqlite.get_flow(FLOW_BOARD_B)
        assert flow_a is not None
        assert flow_b is not None
        creator_a = flow_a["state"]["creator_session_key"]
        creator_b = flow_b["state"]["creator_session_key"]
        assert creator_a == "agent:main:session:sess-board-a"
        assert creator_b == "agent:main:session:sess-board-b"

    asyncio.run(scenario())
