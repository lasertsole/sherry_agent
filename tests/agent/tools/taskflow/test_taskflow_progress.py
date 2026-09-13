"""Behavior tests for taskflow_progress (GAP-6).

The tool is a read-only human-readable companion to taskflow_summary: it
renders completion percentage, status breakdown, the first three actionable
steps, an estimated remaining time when at least two done steps carry a
``dispatched_at`` timestamp, the wait reason, and the injected-result count.

Every test seeds a flow directly in the store (no dispatch pipeline), so the
report formatter is exercised in isolation.
"""

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_progress import taskflow_progress

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _step(
    step_id: str,
    task: str,
    status: str,
    *,
    dispatched_at: float | None = None,
    depends_on: list[str] | None = None,
) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": status,
    }
    if dispatched_at is not None:
        step["dispatched_at"] = dispatched_at
    return step


async def test_progress_empty_flow(isolated_db):
    await store_sqlite.create_flow("flow-empty", {"description": "nothing yet", "steps": []})

    out = await taskflow_progress.coroutine(flow_id="flow-empty")

    assert "No steps registered yet" in out
    assert "flow-empty" in out


async def test_progress_all_done(isolated_db):
    await store_sqlite.create_flow(
        "flow-done",
        {
            "description": "all complete",
            "steps": [
                _step("step-1", "first", "done", dispatched_at=1000),
                _step("step-2", "second", "done", dispatched_at=2000),
                _step("step-3", "third", "done", dispatched_at=3000),
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-done")

    assert "Completion: 3/3 steps (100%)" in out
    assert "Next steps:" not in out


async def test_progress_partial(isolated_db):
    await store_sqlite.create_flow(
        "flow-partial",
        {
            "description": "deploy to staging",
            "steps": [
                _step("step-1", "first", "done", dispatched_at=1000),
                _step("step-2", "second", "done", dispatched_at=2000),
                _step("step-3", "third", "ready"),
                _step("step-4", "fourth", "ready"),
                _step("step-5", "fifth", "ready"),
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-partial")

    assert "Completion: 2/5 steps (40%)" in out
    assert "Next steps:" in out
    assert "○ [step-3] third" in out


async def test_progress_with_blocked(isolated_db):
    await store_sqlite.create_flow(
        "flow-blocked",
        {
            "description": "blocked probe",
            "steps": [
                _step("step-1", "first", "done", dispatched_at=1000),
                _step("step-2", "second", "blocked", depends_on=["step-1"]),
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-blocked")

    assert "blocked=1" in out
    assert "⊘ [step-2] second" in out


async def test_progress_next_steps_max_three(isolated_db):
    await store_sqlite.create_flow(
        "flow-many",
        {
            "description": "many actionable",
            "steps": [
                _step("step-1", "a", "ready"),
                _step("step-2", "b", "ready"),
                _step("step-3", "c", "ready"),
                _step("step-4", "d", "ready"),
                _step("step-5", "e", "ready"),
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-many")

    assert "[step-1]" in out
    assert "[step-2]" in out
    assert "[step-3]" in out
    assert "[step-4]" not in out
    assert "[step-5]" not in out


async def test_progress_est_remaining(isolated_db):
    await store_sqlite.create_flow(
        "flow-est",
        {
            "description": "estimate probe",
            "steps": [
                _step("step-1", "first", "done", dispatched_at=1000),
                _step("step-2", "second", "done", dispatched_at=2000),
                _step("step-3", "third", "ready"),
                _step("step-4", "fourth", "ready"),
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-est")

    assert "Est. remaining" in out


async def test_progress_no_est_when_one_done(isolated_db):
    await store_sqlite.create_flow(
        "flow-one-done",
        {
            "description": "single done",
            "steps": [
                _step("step-1", "first", "done", dispatched_at=1000),
                _step("step-2", "second", "ready"),
                _step("step-3", "third", "ready"),
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-one-done")

    assert "Est. remaining" not in out


async def test_progress_waiting_flow(isolated_db):
    await store_sqlite.create_flow(
        "flow-waiting",
        {
            "description": "waiting probe",
            "steps": [_step("step-1", "first", "dispatched")],
        },
    )
    await store_sqlite.update_flow("flow-waiting", 1, wait={"reason": "awaiting child result"})

    out = await taskflow_progress.coroutine(flow_id="flow-waiting")

    assert "Waiting on: awaiting child result" in out


async def test_progress_not_found(isolated_db):
    out = await taskflow_progress.coroutine(flow_id="ghost")

    assert out == "Error: TaskFlow 'ghost' not found"


async def test_progress_results_count(isolated_db):
    await store_sqlite.create_flow(
        "flow-results",
        {
            "description": "results probe",
            "steps": [
                _step("step-1", "first", "done", dispatched_at=1000),
                _step("step-2", "second", "done", dispatched_at=2000),
            ],
            "results": [
                {"child_session_key": "child-1", "result": "R1"},
                {"child_session_key": "child-2", "result": "R2"},
            ],
        },
    )

    out = await taskflow_progress.coroutine(flow_id="flow-results")

    assert "Results injected: 2" in out
