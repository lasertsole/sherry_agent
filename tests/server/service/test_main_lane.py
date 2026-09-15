"""Lane-gated main turns: queueing, per-session serialization, and the global cap.

Covers plan Step 11 (MAIN lane integration):

1. a full MAIN lane queues a dispatched turn until a slot frees (no rejection)
2. per-session serialization is unchanged — a second submit for the same
   session is QUEUED by the pre-existing busy check, never reaching the lane
3. cross-session concurrency is capped by the MAIN lane
4. a drain-mode refusal propagates without consuming a permit

Hermetic: fresh ``LaneManager`` per test (monkeypatched singleton), fake idle
``detect_state``, real ``UserInputQueue`` on a tmp SQLite file, a gated fake
executor injected through a real ``TurnExecutorRegistry``.
"""

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

import runtime.lane.core as lane_core
from runtime.lane.core import LaneManager, LaneType
from agent.tools.subagent.registry.session_state import SessionState
from server.queue.user_input_queue import UserInputQueue
from server.service import input_queue_service as iqs
from server.service.input_queue_service import (
    SubmitStatus,
    TurnExecutorRegistry,
    submit_user_input,
)

pytestmark = [pytest.mark.unit]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


class GatedExecutor(iqs.BatchTurnExecutor):
    """Batches record their start and block until released, exposing lane admission."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.gate = asyncio.Event()

    async def execute_batch(
        self, session_id: str, batch: Sequence[iqs.TurnInput], reply_target: str | None
    ) -> None:
        self.started.append(session_id)
        await self.gate.wait()


@pytest.fixture(autouse=True)
def _isolated_lanes(monkeypatch: pytest.MonkeyPatch) -> LaneManager:
    """Fresh lane manager (and cleared drain gate) for every test."""
    fresh = LaneManager()
    monkeypatch.setattr(lane_core, "_manager", fresh)
    lane_core.set_drain_check(None)
    yield fresh
    lane_core.set_drain_check(None)


@pytest.fixture(autouse=True)
def _clean_session_locks():
    """Keep the module-level per-session lock table small around every test."""
    iqs._SESSION_LOCKS.clear()
    yield
    iqs._SESSION_LOCKS.clear()


@pytest.fixture(autouse=True)
def idle_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every session looks idle; busy-ness comes only from live CLAIMED rows."""

    def detect(session_key: str) -> SessionState:
        return SessionState(session_id=session_key, busy=False, reason="idle")

    monkeypatch.setattr(iqs, "detect_state", detect)


@pytest.fixture
def store(tmp_path: Path) -> UserInputQueue:
    """Real store on a hermetic tmp SQLite file."""
    return UserInputQueue(db_path=tmp_path / "subagent_registry.db")


@pytest.fixture
def executor() -> GatedExecutor:
    return GatedExecutor()


@pytest.fixture
def registry(executor: GatedExecutor) -> TurnExecutorRegistry:
    reg = TurnExecutorRegistry()
    reg.register("ws", executor)
    return reg


class TestMainLaneAdmission:
    @pytest.mark.asyncio
    async def test_full_lane_queues_a_dispatched_turn_until_release(
        self,
        _isolated_lanes: LaneManager,
        store: UserInputQueue,
        registry: TurnExecutorRegistry,
        executor: GatedExecutor,
    ):
        _isolated_lanes.set_concurrency(LaneType.MAIN, 1)
        lane = _isolated_lanes.get_lane(LaneType.MAIN)
        await lane.acquire()  # a pre-existing turn occupies the only slot

        result = await submit_user_input(
            "s1", "hello", "user", queue=store, executor_registry=registry
        )

        assert result.status is SubmitStatus.STARTED
        await _wait_until(lambda: lane.queued_count == 1)
        assert executor.started == [], "the turn must not run while the lane is full"

        lane.release()
        await _wait_until(lambda: executor.started == ["s1"])
        assert lane.active_count == 1

        executor.gate.set()
        await _wait_until(lambda: lane.active_count == 0)

    @pytest.mark.asyncio
    async def test_second_submit_same_session_stays_per_session_serialized(
        self,
        _isolated_lanes: LaneManager,
        store: UserInputQueue,
        registry: TurnExecutorRegistry,
        executor: GatedExecutor,
    ):
        _isolated_lanes.set_concurrency(LaneType.MAIN, 2)

        first = await submit_user_input(
            "s1", "one", "user", queue=store, executor_registry=registry
        )
        assert first.status is SubmitStatus.STARTED
        await _wait_until(lambda: executor.started == ["s1"])

        second = await submit_user_input(
            "s1", "two", "user", queue=store, executor_registry=registry
        )

        assert second.status is SubmitStatus.QUEUED
        assert _isolated_lanes.get_lane(LaneType.MAIN).queued_count == 0, (
            "per-session serialization short-circuits before the lane"
        )
        assert executor.started == ["s1"]

        executor.gate.set()
        await _wait_until(lambda: _isolated_lanes.get_lane(LaneType.MAIN).active_count == 0)

    @pytest.mark.asyncio
    async def test_cross_session_concurrency_is_capped_by_the_lane(
        self,
        _isolated_lanes: LaneManager,
        store: UserInputQueue,
        registry: TurnExecutorRegistry,
        executor: GatedExecutor,
    ):
        _isolated_lanes.set_concurrency(LaneType.MAIN, 2)
        lane = _isolated_lanes.get_lane(LaneType.MAIN)

        for session in ("s1", "s2", "s3"):
            result = await submit_user_input(
                session, "hi", "user", queue=store, executor_registry=registry
            )
            assert result.status is SubmitStatus.STARTED

        await _wait_until(lambda: len(executor.started) == 2)
        await _wait_until(lambda: lane.queued_count == 1)
        assert set(executor.started) == {"s1", "s2"}

        executor.gate.set()
        await _wait_until(lambda: len(executor.started) == 3)
        await _wait_until(lambda: lane.active_count == 0)

    @pytest.mark.asyncio
    async def test_drain_gate_refuses_the_turn_without_consuming_a_permit(
        self, _isolated_lanes: LaneManager, executor: GatedExecutor
    ):
        lane_core.set_drain_check(lambda: True)

        with pytest.raises(RuntimeError, match="draining"):
            await iqs._run_executor(executor, "s1", "hello", "user", None)

        assert executor.started == []
        assert _isolated_lanes.get_lane(LaneType.MAIN).active_count == 0
        assert _isolated_lanes.get_lane(LaneType.MAIN).queued_count == 0
