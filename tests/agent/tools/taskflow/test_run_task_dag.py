"""Dependency-aware behavior tests for ``taskflow_run_task`` (todo 2).

TDD for taskflow-dag-phase1 todo 2: these pin the ``depends_on`` contract on
the run_task tool itself. The dispatch seam is the shared module-level
``_dispatch.dispatch_child``; every test injects a fake and the real spawn
pipeline is never invoked.

Contracts covered:
(a) ``depends_on=["step-1"]`` before step-1 is done -> the new step is stored
    ``blocked``, the fake dispatch is called 0 times, and the revision is bumped
    by exactly 1.
(b) once a dependency step is ``done``, a dependent call with satisfied deps
    spawns exactly once and records the returned child key with status
    ``dispatched``.
(c) ``depends_on=["ghost"]`` (unknown id) -> ``Error: unknown depends_on step
    id(s) ...`` and NO state change (revision unchanged, zero spawns).
(d) the dispatched path still sets the flow-level ``child_session_key``.
"""

import asyncio

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_run_task import taskflow_run_task

pytestmark = [pytest.mark.unit]


def _recording_dispatch(child_key: str, calls: list):
    async def _dispatch_call(
        task: str, requester_session_key: str, label: str | None = None
    ) -> str:
        calls.append((task, requester_session_key, label))
        return child_key

    return _dispatch_call


# ---------------------------------------------------------------------------
# (a) unmet dependency: registered blocked, zero spawns, exactly one revision bump
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmet_dependency_registers_blocked_with_zero_spawns(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow with one already-dispatched step ("step-1")
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch("child-1", calls))
    await taskflow_create.coroutine(flow_id="flow-1", description="blocked probe")
    await taskflow_run_task.coroutine(flow_id="flow-1", task="first", session_id="sess-1")
    assert len(calls) == 1  # step-1 dispatched

    before = await store_sqlite.get_flow("flow-1")
    assert before is not None
    revision_before = before["expected_revision"]

    # When a dependent step is registered before step-1 is done
    out = await taskflow_run_task.coroutine(
        flow_id="flow-1",
        task="second",
        depends_on=["step-1"],
        session_id="sess-1",
    )

    # Then the step is stored blocked, nothing is spawned, revision bumps by 1
    assert "blocked" in out
    assert "pending=[step-1]" in out
    assert len(calls) == 1, "a blocked step must not spawn a child"

    after = await store_sqlite.get_flow("flow-1")
    assert after is not None
    assert after["expected_revision"] == revision_before + 1

    steps = after["state"]["steps"]
    assert steps[1]["step_id"] == "step-2"
    assert steps[1]["status"] == "blocked"
    assert steps[1]["depends_on"] == ["step-1"]
    assert "child_session_key" not in steps[1]


# ---------------------------------------------------------------------------
# (b) satisfied dependency: dispatched exactly once, child key + status recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_satisfied_dependency_dispatches_once_and_records_step(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow whose dependency step is already done
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch("child-2", calls))
    await taskflow_create.coroutine(
        flow_id="flow-1",
        description="satisfied probe",
        initial_state={
            "steps": [
                {
                    "step_id": "step-1",
                    "task": "first",
                    "depends_on": [],
                    "status": "done",
                }
            ]
        },
    )

    # When the dependent step is registered with its dependency satisfied
    out = await taskflow_run_task.coroutine(
        flow_id="flow-1",
        task="second",
        depends_on=["step-1"],
        label="lbl",
        session_id="sess-1",
    )

    # Then it dispatches exactly once and records the returned child key
    assert "dispatched" in out
    assert "child-2" in out
    assert len(calls) == 1
    assert calls[0] == ("second", "agent:main:session:sess-1", "lbl")

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    steps = flow["state"]["steps"]
    dependent = next(s for s in steps if s["step_id"] == "step-2")
    assert dependent["status"] == "dispatched"
    assert dependent["depends_on"] == ["step-1"]
    assert dependent["child_session_key"] == "child-2"
    assert dependent["dispatched_at"] is not None


# ---------------------------------------------------------------------------
# (c) unknown dependency id: Error text, no state change, zero spawns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_dependency_errors_without_state_change(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow with no steps
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch("child-x", calls))
    await taskflow_create.coroutine(flow_id="flow-1", description="ghost probe")
    before = await store_sqlite.get_flow("flow-1")
    assert before is not None
    revision_before = before["expected_revision"]

    # When a step depends on an id that does not exist
    out = await taskflow_run_task.coroutine(
        flow_id="flow-1",
        task="second",
        depends_on=["ghost"],
        session_id="sess-1",
    )

    # Then it is rejected with the unknown-id error and nothing changes
    assert "Error: unknown depends_on step id" in out
    assert "ghost" in out
    assert calls == []

    after = await store_sqlite.get_flow("flow-1")
    assert after is not None
    assert after["expected_revision"] == revision_before
    assert after["state"]["steps"] == []


# ---------------------------------------------------------------------------
# (d) dispatched path sets the flow-level child_session_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatched_path_sets_flow_level_child_session_key(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow and a satisfying dependency
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch("child-9", []))
    await taskflow_create.coroutine(
        flow_id="flow-1",
        description="flow key probe",
        initial_state={
            "steps": [
                {"step_id": "step-1", "task": "first", "depends_on": [], "status": "done"}
            ]
        },
    )

    # When a dependent step dispatches
    out = await taskflow_run_task.coroutine(
        flow_id="flow-1", task="second", depends_on=["step-1"], session_id="sess-1"
    )
    assert "child-9" in out

    # Then the flow-level child key is set (existing contract preserved)
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["child_session_key"] == "child-9"
    assert flow["state"]["steps"][1]["child_session_key"] == "child-9"
