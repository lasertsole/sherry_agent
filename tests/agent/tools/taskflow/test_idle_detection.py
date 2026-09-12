"""Unit tests for GAP-11 idle TaskFlow detection.

Covers two seams:

1. ``taskflow_summary`` rendering the WAITING timeout status derived from
   ``wait.set_at`` (active / STALE / no wait payload);
2. the sweeper's ``_scan_stale_waiting_taskflows`` detecting WAITING flows
   past the timeout whose child session is no longer live, and stamping a
   ``stale_detected_at`` marker into the wait payload (without changing the
   flow status).

The store is isolated per test through the ``isolated_db`` fixture; the real
``taskflow_registry.db`` is never touched. Timestamps are always placed hours
in the past rather than slept on, so no test depends on wall-clock advances.
Child liveness is injected by monkeypatching the call-time-imported registry
queries/helpers, so the real registry memory is never required.
"""

import time
from pathlib import Path

import pytest

from agent.tools.subagent.registry import helpers as registry_helpers
from agent.tools.subagent.registry import queries as registry_queries
from agent.tools.subagent.registry import sweeper
from agent.tools.taskflow.config import TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary

pytestmark = [pytest.mark.unit]

_STALE_HOURS = 25.0
_RECENT_HOURS = 1.0


def _make_state(description: str = "demo flow") -> dict:
    return {"description": description, "steps": [], "results": []}


async def _create_waiting(
    flow_id: str,
    *,
    hours_ago: float,
    child_session_key: str | None = None,
) -> dict:
    """Create a flow and park it in WAITING with set_at ``hours_ago`` in the past."""
    flow = await store_sqlite.create_flow(
        flow_id,
        _make_state(flow_id),
        child_session_key=child_session_key,
    )
    return await store_sqlite.update_flow(
        flow_id,
        flow["expected_revision"],
        wait={"reason": "awaiting child result", "set_at": time.time() - hours_ago * 3600},
        status=TaskFlowStatus.WAITING.value,
    )


def _patch_child_liveness(
    monkeypatch: pytest.MonkeyPatch, *, run: object | None, live: bool
) -> None:
    """Inject the call-time registry imports used by the sweeper's liveness check."""
    monkeypatch.setattr(registry_queries, "get_run_by_child_session_key", lambda key: run)
    monkeypatch.setattr(registry_helpers, "is_live_unended_run", lambda r: live)


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_shows_active_waiting(isolated_db: Path):
    """A WAITING flow inside the timeout renders an active wait_status line."""
    await _create_waiting("flow-1", hours_ago=_RECENT_HOURS)

    out = await taskflow_summary.coroutine(flow_id="flow-1")

    assert "wait_status: active" in out
    assert "to timeout" in out
    assert "STALE" not in out


@pytest.mark.asyncio
async def test_summary_shows_stale(isolated_db: Path):
    """A WAITING flow past the timeout renders the STALE wait_status line."""
    await _create_waiting("flow-1", hours_ago=_STALE_HOURS)

    out = await taskflow_summary.coroutine(flow_id="flow-1")

    assert "wait_status: STALE" in out
    assert "timeout=24h" in out
    assert "consider taskflow_resume" in out


@pytest.mark.asyncio
async def test_summary_no_wait_payload(isolated_db: Path):
    """A flow without a wait payload renders no wait_status line at all."""
    await store_sqlite.create_flow("flow-1", _make_state("plain"))

    out = await taskflow_summary.coroutine(flow_id="flow-1")

    assert "wait: (none)" in out
    assert "wait_status" not in out


# ---------------------------------------------------------------------------
# Sweeper scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweeper_detects_stale(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """A stale WAITING flow with an inactive child is counted once."""
    await _create_waiting(
        "flow-1", hours_ago=_STALE_HOURS, child_session_key="agent:main:subagent:dead"
    )
    _patch_child_liveness(monkeypatch, run=None, live=False)

    stale = await sweeper._scan_stale_waiting_taskflows()

    assert stale == 1


@pytest.mark.asyncio
async def test_sweeper_skips_active_child(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """A stale WAITING flow whose child is still live is not marked."""
    await _create_waiting(
        "flow-1", hours_ago=_STALE_HOURS, child_session_key="agent:main:subagent:live"
    )
    _patch_child_liveness(monkeypatch, run=object(), live=True)

    stale = await sweeper._scan_stale_waiting_taskflows()

    assert stale == 0
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert "stale_detected_at" not in flow["wait"]


@pytest.mark.asyncio
async def test_sweeper_marks_dead_child(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """The stale mark records both the detection time and the dead child key."""
    child_key = "agent:main:subagent:dead"
    await _create_waiting("flow-1", hours_ago=_STALE_HOURS, child_session_key=child_key)
    _patch_child_liveness(monkeypatch, run=None, live=False)

    stale = await sweeper._scan_stale_waiting_taskflows()

    assert stale == 1
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["wait"]["stale_detected_at"] is not None
    assert flow["wait"]["stale_child_session_key"] == child_key
    assert flow["status"] == TaskFlowStatus.WAITING.value  # marked, never auto-failed


@pytest.mark.asyncio
async def test_sweeper_skips_recent_waiting(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """A WAITING flow inside the timeout is skipped even with a dead child."""
    await _create_waiting(
        "flow-1", hours_ago=_RECENT_HOURS, child_session_key="agent:main:subagent:dead"
    )
    _patch_child_liveness(monkeypatch, run=None, live=False)

    stale = await sweeper._scan_stale_waiting_taskflows()

    assert stale == 0
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert "stale_detected_at" not in flow["wait"]


@pytest.mark.asyncio
async def test_sweeper_no_waiting_flows(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """A registry with no WAITING flows yields a zero stale count."""
    await store_sqlite.create_flow("flow-1", _make_state("running"))
    _patch_child_liveness(monkeypatch, run=None, live=False)

    stale = await sweeper._scan_stale_waiting_taskflows()

    assert stale == 0


@pytest.mark.asyncio
async def test_stale_marker_in_wait_payload(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """After the scan the wait payload carries a fresh stale_detected_at timestamp."""
    before = time.time()
    await _create_waiting("flow-1", hours_ago=_STALE_HOURS)
    _patch_child_liveness(monkeypatch, run=None, live=False)

    await sweeper._scan_stale_waiting_taskflows()

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    marker = flow["wait"]["stale_detected_at"]
    assert before <= marker <= time.time()
    assert flow["wait"]["stale_child_session_key"] is None
