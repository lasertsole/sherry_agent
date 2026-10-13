"""Steer integration with the SUBAGENT lane: PENDING rejected; restarts queue as PENDING."""

import asyncio
import time
import uuid

import pytest

import runtime.lane.core as lane_core
from agent.tools.subagent.control.steer import steer_subagent_run
from agent.tools.subagent.registry import (
    clear as clear_registry,
    get_run,
    get_task,
    register_run,
)
from agent.tools.subagent.types.registry import ExecutionStatus
from runtime.lane import LaneManager, LaneType

pytestmark = [pytest.mark.integration]


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


@pytest.fixture(autouse=True)
def _steer_deps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the child-agent build and record steered-execution invocations."""
    from unittest.mock import AsyncMock

    calls: list[str] = []

    async def fake_steered(run, child_agent, steer_message, timeout_seconds) -> None:
        calls.append(run.run_id)

    monkeypatch.setattr(
        "agent.tools.subagent.spawn.core._build_child_agent",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "agent.tools.subagent.control.steer._execute_steered_subagent", fake_steered
    )
    return calls


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert predicate()


async def _wait_for_lane_drain(manager: LaneManager, timeout: float = 2.0) -> None:
    lane = manager.get_lane(LaneType.SUBAGENT)
    deadline = time.monotonic() + timeout
    while lane.active_count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert lane.active_count == 0


class TestSteerLane:
    @pytest.mark.asyncio
    async def test_pending_run_is_not_steerable(self):
        run = register_run(
            child_session_key=f"agent:main:subagent:{uuid.uuid4()}",
            requester_session_key="agent:main:session:steer_pending",
            task="queued task",
            depth=1,
        )
        assert run.execution.status == ExecutionStatus.PENDING

        result = await steer_subagent_run(run.run_id, new_instructions="redirect")
        assert result is None
        assert get_run(run.run_id).execution.status == ExecutionStatus.PENDING

    @pytest.mark.asyncio
    async def test_steer_restart_queues_as_pending_then_promotes(
        self, _fresh_lane_manager: LaneManager, _steer_deps: list[str]
    ):
        manager = _fresh_lane_manager
        manager.set_concurrency(LaneType.SUBAGENT, 1)
        lane = manager.get_lane(LaneType.SUBAGENT)
        await lane.acquire()  # hold the only slot so the restart must queue

        run = register_run(
            child_session_key=f"agent:main:subagent:{uuid.uuid4()}",
            requester_session_key="agent:main:session:steer_lane",
            task="initial task",
            depth=1,
        )
        run.execution.status = ExecutionStatus.INTERRUPTED

        result = await steer_subagent_run(run.run_id, new_task="redirected task")
        assert result is not None
        assert result.execution.status == ExecutionStatus.PENDING
        assert result.execution.started_at is None
        assert _steer_deps == []

        restarted_task = get_task(run.run_id)
        assert restarted_task is not None
        assert not restarted_task.done()

        lane.release()
        await _wait_until(lambda: get_run(run.run_id).execution.status == ExecutionStatus.RUNNING)
        await _wait_until(lambda: _steer_deps == [run.run_id])

        promoted = get_run(run.run_id)
        assert promoted.execution.started_at is not None
        await _wait_for_lane_drain(manager)
