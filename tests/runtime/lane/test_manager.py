"""Unit tests for runtime.lane.core.LaneManager — hot updates, snapshots, isolation."""

import asyncio
import time

import pytest

from runtime.lane.core import Lane, LaneManager, LaneType

pytestmark = [pytest.mark.unit]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


class TestRegistration:
    def test_get_lane_returns_every_registered_lane(self):
        manager = LaneManager()

        for lane_type in LaneType:
            lane = manager.get_lane(lane_type)
            assert isinstance(lane, Lane)
            assert lane.name == lane_type.value

    def test_snapshot_groups_all_four_lanes_by_name(self):
        manager = LaneManager()

        snapshot = manager.snapshot()

        assert set(snapshot) == {"main", "subagent", "nudge", "nested"}
        for name, lane_snapshot in snapshot.items():
            assert lane_snapshot["name"] == name
            assert set(lane_snapshot) == {"name", "max_concurrent", "active", "queued"}


class TestHotUpdate:
    @pytest.mark.asyncio
    async def test_set_concurrency_only_affects_new_acquires(self):
        manager = LaneManager()
        manager.set_concurrency(LaneType.SUBAGENT, 2)
        lane = manager.get_lane(LaneType.SUBAGENT)

        await lane.acquire()
        await lane.acquire()
        assert lane.active_count == 2  # in-flight slots are unaffected

        manager.set_concurrency(LaneType.SUBAGENT, 4)
        assert lane.max_concurrent == 4
        await lane.acquire()
        await lane.acquire()
        assert lane.active_count == 4

        extra = asyncio.create_task(lane.acquire())
        await _wait_until(lambda: lane.queued_count == 1)
        assert not extra.done()
        extra.cancel()
        with pytest.raises(asyncio.CancelledError):
            await extra

        for _ in range(4):
            lane.release()
        assert lane.active_count == 0

    def test_set_concurrency_rejects_zero(self):
        manager = LaneManager()

        with pytest.raises(ValueError):
            manager.set_concurrency(LaneType.NUDGE, 0)


class TestIsolation:
    @pytest.mark.asyncio
    async def test_filling_main_does_not_block_subagent(self):
        manager = LaneManager()
        manager.set_concurrency(LaneType.MAIN, 1)
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        main_lane = manager.get_lane(LaneType.MAIN)
        subagent_lane = manager.get_lane(LaneType.SUBAGENT)

        await main_lane.acquire()
        assert main_lane.active_count == 1

        await asyncio.wait_for(subagent_lane.acquire(), timeout=1.0)
        assert subagent_lane.active_count == 1

        subagent_lane.release()
        main_lane.release()
        assert (main_lane.active_count, subagent_lane.active_count) == (0, 0)
