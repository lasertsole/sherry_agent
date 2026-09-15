"""Unit tests for Lane.drain / LaneManager.drain_all."""

import asyncio
import time

import pytest

from runtime.lane.core import Lane, LaneManager, LaneType

pytestmark = [pytest.mark.unit]


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_with_no_active_slots_returns_immediately(self):
        lane = Lane("drain", 1)

        assert await asyncio.wait_for(lane.drain(timeout=0), timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_drain_times_out_while_a_slot_is_active(self):
        lane = Lane("drain", 1)
        await lane.acquire()

        started = time.monotonic()
        assert await lane.drain(timeout=0.2) is False
        assert time.monotonic() - started >= 0.2
        assert lane.active_count == 1

        lane.release()

    @pytest.mark.asyncio
    async def test_drain_returns_true_once_every_slot_is_released(self):
        lane = Lane("drain", 1)
        await lane.acquire()

        async def release_soon() -> None:
            await asyncio.sleep(0.05)
            lane.release()

        task = asyncio.create_task(release_soon())
        assert await lane.drain(timeout=2.0) is True
        await task
        assert lane.active_count == 0


class TestDrainAll:
    @pytest.mark.asyncio
    async def test_drain_all_reports_every_lane(self):
        manager = LaneManager()
        nested_lane = manager.get_lane(LaneType.NESTED)
        await nested_lane.acquire()

        results = await manager.drain_all(timeout=0.2)

        assert set(results) == {"main", "subagent", "nudge", "nested"}
        assert results["nested"] is False
        assert results["main"] is True
        assert results["subagent"] is True
        assert results["nudge"] is True

        nested_lane.release()
        assert all((await manager.drain_all(timeout=0.5)).values())
