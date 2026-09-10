"""DAG-rendering tests for taskflow_summary.

Covers todo 6 of taskflow-dag-phase1: each step line renders
``[step_id] status task -> child_key depends_on=[...]`` plus a status-count
line, and legacy steps without a ``status`` field still render a derived
status instead of raising.
"""

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]


def _summary_coroutine():
    tools = {t.name: t for t in build_taskflow_tools()}
    return tools["taskflow_summary"].coroutine


@pytest.mark.asyncio
async def test_summary_renders_blocked_and_done_statuses_with_counts(isolated_db):
    await store_sqlite.create_flow(
        "flow-dag",
        {
            "description": "dag probe",
            "steps": [
                {
                    "step_id": "step-1",
                    "task": "wait for dep",
                    "depends_on": ["step-2"],
                    "status": "blocked",
                },
                {
                    "step_id": "step-2",
                    "task": "first",
                    "depends_on": [],
                    "status": "done",
                    "child_session_key": "child-a",
                },
            ],
            "results": [],
        },
    )

    out = await _summary_coroutine()(flow_id="flow-dag")
    lines = out.splitlines()

    assert "  - [step-1] blocked wait for dep -> None depends_on=[step-2]" in lines
    assert "  - [step-2] done first -> child-a depends_on=[]" in lines
    assert "step statuses: blocked=1 ready=0 dispatched=0 done=1" in lines


@pytest.mark.asyncio
async def test_summary_derives_status_for_legacy_step(isolated_db):
    # Legacy step: no ``status`` key, but it already has a child session key.
    await store_sqlite.create_flow(
        "flow-legacy",
        {
            "description": "legacy probe",
            "steps": [
                {
                    "step_id": "step-1",
                    "task": "legacy task",
                    "child_session_key": "child-legacy",
                },
            ],
            "results": [],
        },
    )

    out = await _summary_coroutine()(flow_id="flow-legacy")
    lines = out.splitlines()

    assert "Error" not in out
    assert "  - [step-1] dispatched legacy task -> child-legacy depends_on=[]" in lines
    assert "step statuses: blocked=0 ready=0 dispatched=1 done=0" in lines
