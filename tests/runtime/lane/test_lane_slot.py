"""Unit tests for runtime.lane.core.lane_slot — release paths and drain refusal."""

import asyncio
import time

import pytest

import runtime.lane.core as lane_core
from runtime.lane.core import LaneManager, LaneType, lane_slot

pytestmark = [pytest.mark.unit]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _reset_drain_check():
    lane_core.set_drain_check(None)
    yield
    lane_core.set_drain_check(None)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> LaneManager:
    fresh = LaneManager()
    monkeypatch.setattr(lane_core, "_manager", fresh)
    return fresh


class TestLaneSlot:
    @pytest.mark.asyncio
    async def test_releases_slot_on_normal_exit(self, manager: LaneManager):
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)

        async with lane_slot(LaneType.SUBAGENT):
            assert lane.active_count == 1

        assert lane.active_count == 0

    @pytest.mark.asyncio
    async def test_releases_slot_when_body_raises(self, manager: LaneManager):
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)

        with pytest.raises(RuntimeError, match="boom"):
            async with lane_slot(LaneType.SUBAGENT):
                raise RuntimeError("boom")

        assert lane.active_count == 0

    @pytest.mark.asyncio
    async def test_nested_same_lane_acquires_two_slots(self, manager: LaneManager):
        manager.set_concurrency(LaneType.NUDGE, 2)
        lane = manager.get_lane(LaneType.NUDGE)

        async with lane_slot(LaneType.NUDGE):
            assert lane.active_count == 1
            async with lane_slot(LaneType.NUDGE):
                assert lane.active_count == 2
            assert lane.active_count == 1

        assert lane.active_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_release_the_semaphore(self, manager: LaneManager):
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)

        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with lane_slot(LaneType.SUBAGENT):
                holder_entered.set()
                await release_holder.wait()

        holder_task = asyncio.create_task(holder())
        await asyncio.wait_for(holder_entered.wait(), timeout=1.0)
        assert lane.active_count == 1

        async def waiter() -> None:
            async with lane_slot(LaneType.SUBAGENT):
                pass

        waiter_task = asyncio.create_task(waiter())
        await _wait_until(lambda: lane.queued_count == 1)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

        assert lane.queued_count == 0
        assert lane.active_count == 1

        release_holder.set()
        await holder_task
        assert lane.active_count == 0

        # The cancelled waiter never acquired, so exactly one permit is free:
        # a second acquire must queue instead of slipping through an
        # over-released semaphore.
        await lane.acquire()
        second = asyncio.create_task(lane.acquire())
        await _wait_until(lambda: lane.queued_count == 1)
        assert not second.done()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        lane.release()
        assert lane.active_count == 0

    @pytest.mark.asyncio
    async def test_refuses_acquire_while_draining(self, manager: LaneManager):
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)
        lane_core.set_drain_check(lambda: True)

        with pytest.raises(RuntimeError, match="draining"):
            async with lane_slot(LaneType.SUBAGENT):
                pass

        assert lane.active_count == 0
        assert lane.queued_count == 0

        lane_core.set_drain_check(lambda: False)
        async with lane_slot(LaneType.SUBAGENT):
            assert lane.active_count == 1
        assert lane.active_count == 0
