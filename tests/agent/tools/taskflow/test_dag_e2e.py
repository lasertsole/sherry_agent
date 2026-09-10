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
"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite

# Package-attribute traversal is shadowed by the re-exported tool objects
# (the family package binds the tool under the module's name); sys.modules
# keys are exact strings and immune to that shadowing.
dispatch_mod = sys.modules["agent.tools.taskflow.tools._dispatch"]
wait_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_wait_all")

pytestmark = [pytest.mark.unit]

FLOW = "flow-dag-e2e"

TASK_A = "collect data"
TASK_B = "aggregate data"
TASK_C = "write report"

CHILD_A = "agent:main:subagent:dag-a"
CHILD_B = "agent:main:subagent:dag-b"
CHILD_C = "agent:main:subagent:dag-c"

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
