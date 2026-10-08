"""Kill-while-PENDING: cancellation unwinds the lane waiter without corrupting lane state."""

import asyncio
import time

import pytest

import runtime.lane.core as lane_core
from agent.tools.subagent.control.kill import kill_subagent_run, list_killable_children
from agent.tools.subagent.registry import (
    clear as clear_registry,
    complete_run,
    get_run,
    get_task,
    register_run,
)
from agent.tools.subagent.spawn.core import spawn_subagent_direct
from agent.tools.subagent.types.registry import (
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
)
from runtime.lane import LaneManager, LaneType

pytestmark = [pytest.mark.integration]


class _BlockingExecutor:
    """Fake ``_execute_subagent`` that blocks until released."""

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


async def _wait_task_done(task: asyncio.Task, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert task.done()


async def _wait_for_lane_drain(manager: LaneManager, timeout: float = 2.0) -> None:
    lane = manager.get_lane(LaneType.SUBAGENT)
    deadline = time.monotonic() + timeout
    while lane.active_count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert lane.active_count == 0


class TestListKillablePending:
    def test_pending_run_is_killable(self):
        run = register_run(
            child_session_key="agent:main:subagent:kill_list",
            requester_session_key="agent:main:session:kill_list",
            task="queued task",
            depth=1,
        )
        assert run.execution.status == ExecutionStatus.PENDING

        killable = list_killable_children("agent:main:session:kill_list")
        assert [r.run_id for r in killable] == [run.run_id]

    def test_terminal_run_is_not_killable(self):
        run = register_run(
            child_session_key="agent:main:subagent:kill_list_done",
            requester_session_key="agent:main:session:kill_list",
            task="done task",
        )
        complete_run(run.run_id, RunOutcome(status=RunOutcomeStatus.OK))

        assert list_killable_children("agent:main:session:kill_list") == []


class TestKillPendingRun:
    @pytest.mark.asyncio
    async def test_kill_pending_cancels_lane_waiter(
        self, _fresh_lane_manager: LaneManager, executor: _BlockingExecutor
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)

        first = await spawn_subagent_direct(
            task="first", requester_session_key="agent:main:session:kill_a"
        )
        await _wait_for_status(first.run_id, ExecutionStatus.RUNNING)

        second = await spawn_subagent_direct(
            task="second", requester_session_key="agent:main:session:kill_a"
        )
        assert get_run(second.run_id).execution.status == ExecutionStatus.PENDING
        queued_task = get_task(second.run_id)
        assert queued_task is not None

        killed = await kill_subagent_run(second.run_id, reason="test kill")
        assert killed is not None
        assert killed.execution.status == ExecutionStatus.TERMINAL
        assert killed.execution.outcome.status == RunOutcomeStatus.KILLED
        assert killed.aborted_last_run is True

        # CancelledError propagated out of lane.acquire(): the waiter task ends cancelled.
        await _wait_task_done(queued_task)
        assert queued_task.cancelled()

        # The killed waiter never held a slot, so the only active slot is the first run.
        assert lane.active_count == 1

        executor.release()
        await _wait_for_status(first.run_id, ExecutionStatus.RUNNING)
        await _wait_for_lane_drain(manager)

    @pytest.mark.asyncio
    async def test_killed_waiter_does_not_over_release_lane(
        self, _fresh_lane_manager: LaneManager, executor: _BlockingExecutor
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)

        first = await spawn_subagent_direct(
            task="first", requester_session_key="agent:main:session:kill_b"
        )
        await _wait_for_status(first.run_id, ExecutionStatus.RUNNING)

        second = await spawn_subagent_direct(
            task="second", requester_session_key="agent:main:session:kill_b"
        )
        assert get_run(second.run_id).execution.status == ExecutionStatus.PENDING
        await kill_subagent_run(second.run_id, reason="test kill")

        third = await spawn_subagent_direct(
            task="third", requester_session_key="agent:main:session:kill_b"
        )
        assert third.status == "accepted"
        await asyncio.sleep(0.05)
        # If killing the PENDING waiter had released a permit it never took, this
        # queued spawn would slip into the lane while the first run still holds it.
        assert get_run(third.run_id).execution.status == ExecutionStatus.PENDING
        assert lane.active_count == 1

        executor.release()
        await _wait_for_status(third.run_id, ExecutionStatus.RUNNING)
        await _wait_for_lane_drain(manager)
        assert executor.started == [first.run_id, third.run_id]
