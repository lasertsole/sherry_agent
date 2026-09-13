"""Unit tests for GAP-4 task deadlines.

Covers four seams:

1. ``taskflow_create(deadline_hours=...)`` computing and persisting
   ``deadline_ts`` (and omitting it when not requested);
2. ``taskflow_summary`` rendering the deadline line (remaining / EXCEEDED /
   absent when no deadline is set);
3. the store query ``get_overdue_flows`` + the sweeper's
   ``_expire_overdue_taskflows`` marking overdue non-terminal flows failed;
4. the terminal / no-deadline skip paths and the failure_reason payload.

The store is isolated per test through the ``isolated_db`` fixture: the real
``taskflow_registry.db`` is never touched. Deadlines are always placed in the
past (``time.time() - 60``) rather than slept on, so no test depends on
wall-clock advances.
"""

import time
from pathlib import Path

import pytest

from agent.tools.taskflow.config import INITIAL_REVISION, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary
from agent.tools.subagent.registry import sweeper

pytestmark = [pytest.mark.unit]


def _make_state(description: str = "demo flow") -> dict:
    return {"description": description, "steps": [], "results": []}


async def _create_overdue(flow_id: str, *, seconds_ago: float = 60.0) -> dict:
    return await store_sqlite.create_flow(
        flow_id,
        _make_state(flow_id),
        deadline_ts=time.time() - seconds_ago,
    )


# ---------------------------------------------------------------------------
# Create: deadline_hours
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_with_deadline(isolated_db: Path):
    """deadline_hours=24 persists deadline_ts within the [before, after]+24h window."""
    before = time.time()
    out = await taskflow_create.coroutine(
        flow_id="flow-1", description="urgent", deadline_hours=24.0
    )
    after = time.time()

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    deadline_ts = flow["deadline_ts"]
    assert deadline_ts is not None
    assert before + 24 * 3600 <= deadline_ts <= after + 24 * 3600
    assert "deadline=" in out


@pytest.mark.asyncio
async def test_create_without_deadline(isolated_db: Path):
    """deadline_hours=None leaves deadline_ts NULL and appends no deadline text."""
    out = await taskflow_create.coroutine(flow_id="flow-1", description="no deadline")

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["deadline_ts"] is None
    assert "deadline=" not in out


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_shows_deadline(isolated_db: Path):
    """A live deadline renders a 'deadline:' line with the remaining hours."""
    await taskflow_create.coroutine(flow_id="flow-1", description="live", deadline_hours=5.0)

    out = await taskflow_summary.coroutine(flow_id="flow-1")

    assert "deadline:" in out
    assert "h remaining" in out


@pytest.mark.asyncio
async def test_summary_shows_exceeded(isolated_db: Path):
    """A passed deadline on a non-terminal flow renders EXCEEDED."""
    await _create_overdue("flow-1")

    out = await taskflow_summary.coroutine(flow_id="flow-1")

    assert "EXCEEDED" in out


@pytest.mark.asyncio
async def test_summary_no_deadline(isolated_db: Path):
    """No deadline set -> the summary contains no deadline line at all."""
    await taskflow_create.coroutine(flow_id="flow-1", description="plain")

    out = await taskflow_summary.coroutine(flow_id="flow-1")

    assert "deadline:" not in out


# ---------------------------------------------------------------------------
# Sweeper expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweeper_expires_overdue(isolated_db: Path):
    """An overdue non-terminal flow is marked failed by the sweeper scan."""
    await _create_overdue("flow-1")

    expired = await sweeper._expire_overdue_taskflows()

    assert expired == 1
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.FAILED.value


@pytest.mark.asyncio
async def test_sweeper_skips_terminal(isolated_db: Path):
    """A terminal flow is not reprocessed even with a passed deadline."""
    await _create_overdue("flow-1")
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, status=TaskFlowStatus.DONE.value)

    expired = await sweeper._expire_overdue_taskflows()

    assert expired == 0
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.DONE.value
    assert flow["expected_revision"] == INITIAL_REVISION + 1
    assert "failure_reason" not in flow["state"]


@pytest.mark.asyncio
async def test_sweeper_skips_no_deadline(isolated_db: Path):
    """A flow without a deadline is never touched by the sweeper scan."""
    await store_sqlite.create_flow("flow-1", _make_state("no deadline"))

    expired = await sweeper._expire_overdue_taskflows()

    assert expired == 0
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.RUNNING.value
    assert flow["expected_revision"] == INITIAL_REVISION


@pytest.mark.asyncio
async def test_expired_flow_has_failure_reason(isolated_db: Path):
    """The sweeper's mark carries the 'Deadline exceeded' failure reason in state."""
    await _create_overdue("flow-1")

    await sweeper._expire_overdue_taskflows()

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert "Deadline exceeded" in flow["state"]["failure_reason"]
