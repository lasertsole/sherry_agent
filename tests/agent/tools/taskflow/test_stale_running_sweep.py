"""Stale-running TaskFlow sweep (the 12h action tier).

The sweep fails a RUNNING/WAITING flow whose ``updated_at`` is older than
``stale_action_hours``, but never when a child run is still live or when the
liveness source cannot prove the child dead. It marks unresolved steps failed
so no ``dispatched`` ghost survives on a failed flow.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from agent.tools.subagent.registry import helpers as registry_helpers
from agent.tools.subagent.registry import queries as registry_queries
from agent.tools.subagent.registry import sweeper
from agent.tools.taskflow.config import StepStatus, TaskFlowStatus, TABLE_NAME
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]

SESSION = "sess-stale"


def _age_flow(db_path: Path, flow_id: str, *, hours: float) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET updated_at = ? WHERE flow_id = ?",
            (time.time() - hours * 3600, flow_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _flow(flow_id: str = "flow-1", *, status: str = TaskFlowStatus.RUNNING.value):
    return await store_sqlite.create_flow(
        flow_id,
        {
            "description": "stale probe",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "work",
                    "status": StepStatus.DISPATCHED.value,
                    "child_session_key": "child-1",
                }
            ],
            "results": [],
        },
        session_id=SESSION,
        status=status,
    )


def _liveness(monkeypatch: pytest.MonkeyPatch, *, live: bool) -> None:
    monkeypatch.setattr(registry_queries, "get_run_by_child_session_key", lambda _key: object())
    monkeypatch.setattr(registry_helpers, "is_live_unended_run", lambda _run: live)


@pytest.mark.asyncio
async def test_stale_running_flow_is_failed(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "stale_action_hours", 12)
    flow = await _flow()
    _age_flow(isolated_db, flow["flow_id"], hours=13)
    _liveness(monkeypatch, live=False)

    assert await sweeper._sweep_stale_running_taskflows() == 1

    fresh = await store_sqlite.get_flow(flow["flow_id"], SESSION)
    assert fresh is not None
    assert fresh["status"] == TaskFlowStatus.FAILED.value
    assert fresh["terminal_reason"] == "stale_swept"
    assert "stale" in fresh["state"]["failure_reason"]
    step = fresh["state"]["steps"][0]
    assert step["status"] == StepStatus.FAILED.value
    assert "stale" in step["fail_reason"]


@pytest.mark.asyncio
async def test_a_recent_flow_is_untouched(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "stale_action_hours", 12)
    flow = await _flow()
    _age_flow(isolated_db, flow["flow_id"], hours=1)
    _liveness(monkeypatch, live=False)

    assert await sweeper._sweep_stale_running_taskflows() == 0
    fresh = await store_sqlite.get_flow(flow["flow_id"], SESSION)
    assert fresh is not None and fresh["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_live_child_vetoes_the_stale_failure(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "stale_action_hours", 12)
    flow = await _flow()
    _age_flow(isolated_db, flow["flow_id"], hours=13)
    _liveness(monkeypatch, live=True)

    assert await sweeper._sweep_stale_running_taskflows() == 0
    fresh = await store_sqlite.get_flow(flow["flow_id"], SESSION)
    assert fresh is not None and fresh["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_zero_threshold_disables_the_pass(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "stale_action_hours", 0)
    flow = await _flow()
    _age_flow(isolated_db, flow["flow_id"], hours=999)
    _liveness(monkeypatch, live=False)

    assert await sweeper._sweep_stale_running_taskflows() == 0
    fresh = await store_sqlite.get_flow(flow["flow_id"], SESSION)
    assert fresh is not None and fresh["status"] == TaskFlowStatus.RUNNING.value
