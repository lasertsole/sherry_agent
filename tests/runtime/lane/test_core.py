"""Unit tests for runtime.lane.core.Lane — counters, queueing and loop rebinding."""

import asyncio
import time

import pytest
from loguru import logger

from runtime.lane.core import Lane, LaneType

pytestmark = [pytest.mark.unit]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


class TestLaneTypes:
    def test_lane_type_values(self):
        assert {lane_type.value for lane_type in LaneType} == {
            "main",
            "subagent",
            "nudge",
            "nested",
        }


class TestCounters:
    @pytest.mark.asyncio
    async def test_acquire_increments_and_release_decrements_active(self):
        lane = Lane("core", 2)
        assert (lane.active_count, lane.queued_count) == (0, 0)

        await lane.acquire()
        assert lane.active_count == 1

        lane.release()
        assert lane.active_count == 0

    @pytest.mark.asyncio
    async def test_queued_counts_waiters_only_while_waiting(self):
        lane = Lane("core", 1)
        await lane.acquire()
        release_waiter = asyncio.Event()

        async def waiter() -> None:
            await lane.acquire()
            await release_waiter.wait()
            lane.release()

        task = asyncio.create_task(waiter())
        await _wait_until(lambda: lane.queued_count == 1)
        assert lane.active_count == 1

        lane.release()
        await _wait_until(lambda: lane.active_count == 1)
        assert lane.queued_count == 0

        release_waiter.set()
        await task
        assert lane.active_count == 0

    def test_release_without_acquire_never_underflows(self):
        lane = Lane("core", 1)

        lane.release()

        assert lane.active_count == 0

    def test_constructor_rejects_zero_capacity(self):
        with pytest.raises(ValueError):
            Lane("core", 0)

    def test_snapshot_shape(self):
        lane = Lane("main", 3)

        assert lane.snapshot() == {
            "name": "main",
            "max_concurrent": 3,
            "active": 0,
            "queued": 0,
        }


class TestCapacity:
    @pytest.mark.asyncio
    async def test_lane_queues_after_capacity_is_full(self):
        lane = Lane("core", 2)
        await lane.acquire()
        await lane.acquire()

        waiter = asyncio.create_task(lane.acquire())
        await _wait_until(lambda: lane.queued_count == 1)
        assert not waiter.done()

        lane.release()
        await asyncio.wait_for(waiter, timeout=1.0)
        assert lane.active_count == 2

        lane.release()
        lane.release()
        assert lane.active_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_consume_or_return_a_permit(self):
        lane = Lane("core", 1)
        await lane.acquire()

        waiter = asyncio.create_task(lane.acquire())
        await _wait_until(lambda: lane.queued_count == 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert lane.queued_count == 0
        assert lane.active_count == 1

        lane.release()
        # Exactly one acquire fits now; a second one must queue — the cancelled
        # waiter neither consumed nor returned a permit.
        await lane.acquire()
        second = asyncio.create_task(lane.acquire())
        await _wait_until(lambda: lane.queued_count == 1)
        assert not second.done()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        lane.release()
        assert lane.active_count == 0


class TestCrossLoopRebind:
    def test_acquire_from_a_second_loop_rebinds_the_semaphore(self):
        lane = Lane("cross-loop", 2)

        loop_a = asyncio.new_event_loop()
        try:
            loop_a.run_until_complete(lane.acquire())
        finally:
            loop_a.close()
        assert lane.active_count == 1

        messages: list[str] = []
        sink_id = logger.add(
            lambda message: messages.append(message.record["message"]),
            level="WARNING",
        )
        try:
            asyncio.run(self._acquire_in_second_loop(lane))
        finally:
            logger.remove(sink_id)

        assert any("different event loop" in message for message in messages)
        assert lane.active_count == 0

    @staticmethod
    async def _acquire_in_second_loop(lane: Lane) -> None:
        await lane.acquire()
        assert lane.active_count == 2

        # The rebound semaphore only carried max(1, max - active) == 1 permit,
        # so a further acquire must queue (no double issue).
        waiter = asyncio.create_task(lane.acquire())
        await _wait_until(lambda: lane.queued_count == 1)
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        lane.release()
        lane.release()
