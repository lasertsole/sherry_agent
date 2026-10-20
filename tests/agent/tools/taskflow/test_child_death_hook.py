"""Child-death → TaskFlow step finalization.

Sherry's subagents run as async tasks in the parent process (no child PID), so
the plan's PID-liveness probe is replaced by the registry's own terminal
signal: ``_finalize_cleanup`` calls ``fail_step_for_child``. This test pins the
store contract directly (the lifecycle hook is a thin caller).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow.config import StepStatus, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]

SESSION = "sess-child-death"


async def _flow() -> dict:
    return await store_sqlite.create_flow(
        "flow-death",
        {
            "description": "child death",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "running",
                    "status": StepStatus.DISPATCHED.value,
                    "child_session_key": "child-dead",
                },
                {
                    "step_id": "s2",
                    "task": "done",
                    "status": StepStatus.DONE.value,
                    "child_session_key": "child-done",
                },
            ],
            "results": [{"child_session_key": "child-done", "result": "ok"}],
        },
        session_id=SESSION,
    )


@pytest.mark.asyncio
async def test_a_dead_child_fails_only_its_dispatched_step(isolated_db: Path):
    await _flow()

    flow_id = await store_sqlite.fail_step_for_child(
        "child-dead", reason="subagent ended before resuming the flow"
    )

    assert flow_id == "flow-death"
    flow = await store_sqlite.get_flow("flow-death", SESSION)
    assert flow is not None
    # The flow stays open for the AI/deadline backstops; only the step settles.
    assert flow["status"] == TaskFlowStatus.RUNNING.value
    steps = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert steps["s1"]["status"] == StepStatus.FAILED.value
    assert "subagent ended" in steps["s1"]["fail_reason"]
    assert steps["s2"]["status"] == StepStatus.DONE.value


@pytest.mark.asyncio
async def test_the_child_failure_is_idempotent(isolated_db: Path):
    await _flow()
    assert await store_sqlite.fail_step_for_child("child-dead", reason="first") == "flow-death"

    assert await store_sqlite.fail_step_for_child("child-dead", reason="second") is None
    flow = await store_sqlite.get_flow("flow-death", SESSION)
    assert flow is not None
    assert flow["state"]["steps"][0]["fail_reason"] == "first"


@pytest.mark.asyncio
async def test_unknown_child_is_a_no_op(isolated_db: Path):
    await _flow()
    assert await store_sqlite.fail_step_for_child("not-ours", reason="x") is None
