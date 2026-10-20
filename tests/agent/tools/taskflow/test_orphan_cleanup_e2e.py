"""End-to-end TaskFlow orphan lifecycle through the real tool façade.

No mocked store: create → simulate a dispatched child → child death → finish
rejection → explicit fail, then an owner-missing flow reconciled by the
proof-based reconciler. This is the "does the whole chain behave" test rather
than a single-unit seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.reconcile import reconcile_orphaned_flows

pytestmark = [pytest.mark.module]

SESSION = "sess-e2e-orphan"


def _tools() -> dict:
    return {tool.name: tool for tool in build_taskflow_tools()}


@pytest.mark.asyncio
async def test_child_death_to_terminal_failure(isolated_db: Path):
    tools = _tools()
    initial = {
        "steps": [
            {
                "step_id": "s1",
                "task": "the work",
                "depends_on": [],
                "status": StepStatus.DISPATCHED.value,
                "child_session_key": "agent:main:subagent:dead",
            }
        ],
        "results": [],
    }
    out = await tools["taskflow_create"].coroutine(
        session_id=SESSION, flow_id="flow-e2e", description="ship it", initial_state=initial
    )
    assert "TaskFlow created" in out

    # The subagent ended without resuming the flow.
    assert (
        await store_sqlite.fail_step_for_child(
            "agent:main:subagent:dead", reason="subagent ended before resuming the flow"
        )
        == "flow-e2e"
    )

    rejected = await tools["taskflow_finish"].coroutine(session_id=SESSION, flow_id="flow-e2e")
    assert rejected.startswith("Error: Cannot finish")
    assert "s1=failed" in rejected

    failed = await tools["taskflow_fail"].coroutine(
        session_id=SESSION, flow_id="flow-e2e", reason="worker died"
    )
    assert "status=failed" in failed
    flow = await store_sqlite.get_flow("flow-e2e", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.FAILED.value
    assert flow["terminal_reason"] == "failed"


@pytest.mark.asyncio
async def test_owner_missing_e2e_cancels_and_leaves_a_live_flow(isolated_db: Path):
    from runtime import hooks

    await store_sqlite.create_flow(
        "flow-gone", {"steps": [], "results": []}, session_id="sess-gone"
    )
    await store_sqlite.create_flow(
        "flow-live", {"steps": [], "results": []}, session_id="sess-live"
    )
    hooks.register(hooks.SESSION_EXISTS, lambda sid: sid == "sess-live")
    try:
        finalized = await reconcile_orphaned_flows(
            session_exists=lambda sid: sid == "sess-live",
            child_is_live=lambda _key: False,
            grace_seconds=0.0,
        )
    finally:
        hooks.unregister(hooks.SESSION_EXISTS)

    assert finalized == ["flow-gone"]
    gone = await store_sqlite.get_flow("flow-gone", "sess-gone")
    live = await store_sqlite.get_flow("flow-live", "sess-live")
    assert gone is not None and gone["status"] == TaskFlowStatus.CANCELLED.value
    assert gone["terminal_reason"] == "orphaned"
    assert live is not None and live["status"] == TaskFlowStatus.RUNNING.value
