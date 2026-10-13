"""Module tests for server/trigger/http/lane.py — the lane status endpoint.

Drives the registered Robyn handler directly (no server started): Robyn's
route wrapper serializes the handler's dict into a JSON ``Response``, which
these tests parse back and assert on (same pattern as GET /model-config).
"""

import json

import pytest

import runtime.lane.core as lane_core
from runtime.lane.core import LaneManager, LaneType
from server.trigger.http import lane as lane_api

pytestmark = [pytest.mark.module]


@pytest.fixture
def lanes(monkeypatch: pytest.MonkeyPatch) -> LaneManager:
    fresh = LaneManager()
    monkeypatch.setattr(lane_core, "_manager", fresh)
    return fresh


async def _snapshot() -> dict:
    response = await lane_api.lane_status_handler(None)
    return json.loads(response.description)


class TestLaneStatusHandler:
    @pytest.mark.asyncio
    async def test_returns_snapshot_for_all_four_lanes(self, lanes: LaneManager):
        snapshot = await _snapshot()

        assert set(snapshot) == {"main", "subagent", "nudge", "nested"}
        for name, lane_snapshot in snapshot.items():
            assert lane_snapshot["name"] == name
            assert set(lane_snapshot) == {"name", "max_concurrent", "active", "queued"}
            assert lane_snapshot["max_concurrent"] >= 1

    @pytest.mark.asyncio
    async def test_active_slots_are_reflected(self, lanes: LaneManager):
        lane = lanes.get_lane(LaneType.MAIN)
        await lane.acquire()
        try:
            snapshot = await _snapshot()
            assert snapshot["main"]["active"] == 1
        finally:
            lane.release()

        snapshot = await _snapshot()
        assert snapshot["main"]["active"] == 0
