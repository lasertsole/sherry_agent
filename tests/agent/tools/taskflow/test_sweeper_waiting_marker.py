"""The stale-WAITING marker must not refresh the clock the sweeps read.

``updated_at`` is the inactivity clock the liveness sweep and the unreferenced-
flow GC read. The stale marker is a bookkeeping write made BY a sweep, so
stamping that column would keep the very flow the sweep just called stale
perpetually fresh — the flow would never age into collection.
"""

from __future__ import annotations

import time

import pytest

from agent.tools.taskflow.config import TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.subagent.registry import sweeper

pytestmark = [pytest.mark.unit]

_SESSION = "sess-waiting-marker"


@pytest.mark.asyncio
async def test_the_marker_stamps_the_wait_payload_but_not_updated_at(isolated_db):
    await store_sqlite.create_flow("flow-wait", {"steps": [], "results": []}, session_id=_SESSION)
    flow = await store_sqlite.get_flow("flow-wait", _SESSION)
    assert flow is not None
    # Waiting far longer than ``waiting_timeout_hours``, with no child session.
    await store_sqlite.update_flow(
        flow["flow_id"],
        flow["expected_revision"],
        session_id=_SESSION,
        wait={"reason": "child running", "set_at": time.time() - 30 * 3600},
        status=TaskFlowStatus.WAITING.value,
    )
    before = await store_sqlite.get_flow("flow-wait", _SESSION)
    assert before is not None

    stale = await sweeper._scan_stale_waiting_taskflows()

    after = await store_sqlite.get_flow("flow-wait", _SESSION)
    assert stale == 1
    assert after is not None
    assert after["wait"]["stale_detected_at"] is not None
    assert after["updated_at"] == before["updated_at"], (
        "the stale marker refreshed the inactivity clock the sweep reads"
    )
    # Optimistic locking is unchanged: the marker still bumps the revision.
    assert after["expected_revision"] == before["expected_revision"] + 1
