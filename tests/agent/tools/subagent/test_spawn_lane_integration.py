"""SUBAGENT lane integration: over-limit spawns queue (PENDING) instead of being rejected."""

import asyncio
import time

import pytest

import runtime.lane.core as lane_core
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.registry.memory import get as get_run
from agent.tools.subagent.spawn.core import spawn_subagent_direct
from agent.tools.subagent.types.registry import ExecutionStatus
from runtime.lane import LaneManager, LaneType

pytestmark = [pytest.mark.integration]


class _BlockingExecutor:
    """Fake ``_execute_subagent``: records start order, blocks until released."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self._release = asyncio.Event()

    async def __call__(self, run, **kwargs) -> None:
        self.started.append(run.run_id)
        await self._release.wait()

    def release(self) -> None:
        self._release.set()


@pytest.fixture(autouse=True)
def _fresh_lane_manager(monkeypatch: pytest.MonkeyPatch) -> LaneManager:
    """Per-test LaneManager so lane counters never leak across tests."""
    fresh = LaneManager()
    monkeypatch.setattr(lane_core, "_manager", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> _BlockingExecutor:
    fake = _BlockingExecutor()
    monkeypatch.setattr("agent.tools.subagent.spawn.core._execute_subagent", fake)
    return fake


async def _wait_for_status(run_id: str, status: ExecutionStatus, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = get_run(run_id)
        if run is not None and run.execution.status == status:
            return run
        await asyncio.sleep(0.005)
    run = get_run(run_id)
    current = run.execution.status if run is not None else None
    raise AssertionError(f"run {run_id} did not reach {status}: {current}")


async def _wait_for_lane_drain(manager: LaneManager, timeout: float = 2.0) -> None:
    lane = manager.get_lane(LaneType.SUBAGENT)
    deadline = time.monotonic() + timeout
    while lane.active_count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert lane.active_count == 0


class TestSpawnLaneQueueing:
    @pytest.mark.asyncio
    async def test_full_lane_queues_instead_of_rejecting(
        self, _fresh_lane_manager: LaneManager, executor: _BlockingExecutor
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.SUBAGENT, 1)

        first = await spawn_subagent_direct(
            task="first", requester_session_key="agent:main:session:lane_a"
        )
        assert first.status == "accepted"
        await _wait_for_status(first.run_id, ExecutionStatus.RUNNING)

        second = await spawn_subagent_direct(
            task="second", requester_session_key="agent:main:session:lane_a"
        )
        assert second.status == "accepted"
        assert second.error is None

        queued = get_run(second.run_id)
        assert queued is not None
        assert queued.execution.status == ExecutionStatus.PENDING
        assert queued.execution.started_at is None
        assert executor.started == [first.run_id]

        executor.release()
        await _wait_for_status(second.run_id, ExecutionStatus.RUNNING)
        await _wait_for_lane_drain(manager)

    @pytest.mark.asyncio
    async def test_pending_promotes_to_running_with_started_at(
        self, _fresh_lane_manager: LaneManager, executor: _BlockingExecutor
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.SUBAGENT, 1)

        first = await spawn_subagent_direct(
            task="first", requester_session_key="agent:main:session:lane_b"
        )
        await _wait_for_status(first.run_id, ExecutionStatus.RUNNING)
        second = await spawn_subagent_direct(
            task="second", requester_session_key="agent:main:session:lane_b"
        )
        assert get_run(second.run_id).execution.status == ExecutionStatus.PENDING

        executor.release()
        promoted = await _wait_for_status(second.run_id, ExecutionStatus.RUNNING)
        assert promoted.execution.started_at is not None
        assert second.run_id in executor.started
        await _wait_for_lane_drain(manager)

    @pytest.mark.asyncio
    async def test_fifo_start_order_under_contention(
        self, _fresh_lane_manager: LaneManager, executor: _BlockingExecutor
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.SUBAGENT, 1)

        results = [
            await spawn_subagent_direct(
                task=f"task {i}", requester_session_key="agent:main:session:lane_c"
            )
            for i in range(3)
        ]
        run_ids = [r.run_id for r in results]
        assert all(r.status == "accepted" for r in results)

        await _wait_for_status(run_ids[0], ExecutionStatus.RUNNING)
        await asyncio.sleep(0.05)  # queued runs get a chance to (wrongly) jump ahead
        assert executor.started == [run_ids[0]]
        assert get_run(run_ids[1]).execution.status == ExecutionStatus.PENDING
        assert get_run(run_ids[2]).execution.status == ExecutionStatus.PENDING

        executor.release()
        for run_id in run_ids:
            await _wait_for_status(run_id, ExecutionStatus.RUNNING)
        await _wait_for_lane_drain(manager)

        assert executor.started == run_ids


class TestMainLaneIndependence:
    """§18: a MAIN-slot holder waiting on a child cannot starve that child."""

    @pytest.mark.asyncio
    async def test_subagent_runs_while_main_lane_slot_is_held(
        self, _fresh_lane_manager: LaneManager, executor: _BlockingExecutor
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.MAIN, 1)
        manager.set_concurrency(LaneType.SUBAGENT, 1)

        main_lane = manager.get_lane(LaneType.MAIN)
        await main_lane.acquire()  # the only MAIN slot: an in-flight main turn
        try:
            spawned = await spawn_subagent_direct(
                task="child", requester_session_key="agent:main:session:lane_independence"
            )
            assert spawned.status == "accepted"

            # The child reaches RUNNING on the independent SUBAGENT lane while the
            # MAIN slot is still held, so "hold MAIN, wait for a child" cannot form
            # a cross-lane wait cycle.
            await _wait_for_status(spawned.run_id, ExecutionStatus.RUNNING)
            assert executor.started == [spawned.run_id]
            assert main_lane.active_count == 1
        finally:
            executor.release()
            await _wait_for_lane_drain(manager)
            main_lane.release()
