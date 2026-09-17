"""Nudge agents are gated by the process-wide NUDGE lane.

Covers plan Step 9's lane landing:

1. ``_nudge_memory`` concurrency is capped by the NUDGE lane (queue, never reject)
2. the plan-extraction nudge shares the same lane
3. the compression-time scheduler never dispatches without a running event
   loop, and ``ContextEngineHook`` no longer overrides the after-agent hooks —
   so no ``run_async()`` worker loop can ever acquire the loop-bound semaphore
4. acquiring the NUDGE lane across two loops does not raise (Wave 1 rebind)

Hermetic: the nudge agent builder is replaced with a gated fake (no LLM), and
each test gets a fresh ``LaneManager`` through the monkeypatched singleton.
"""

import asyncio
import time

import pytest

import runtime.lane.core as lane_core
from runtime.lane.core import LaneManager, LaneType, lane_slot
from agent.middlewares.context_engine import core as ce_core
from agent.middlewares.context_engine import nudge

pytestmark = [pytest.mark.unit]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


class NudgeTracker:
    """Shared state for fake nudge agents: gate + concurrency high-water mark."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[dict] = []
        self.gate = asyncio.Event()


class FakeNudgeAgent:
    def __init__(self, tracker: NudgeTracker) -> None:
        self._tracker = tracker

    async def ainvoke(self, input: dict) -> dict:
        tracker = self._tracker
        tracker.calls.append(input)
        tracker.active += 1
        tracker.max_active = max(tracker.max_active, tracker.active)
        try:
            await tracker.gate.wait()
        finally:
            tracker.active -= 1
        return {"messages": ["ok"]}


def _fake_builder(tracker: NudgeTracker):
    async def build(system_prompt, allowed_metadata_key=None, tools=None):
        return FakeNudgeAgent(tracker)

    return build


@pytest.fixture(autouse=True)
def _isolated_lanes(monkeypatch: pytest.MonkeyPatch) -> LaneManager:
    fresh = LaneManager()
    monkeypatch.setattr(lane_core, "_manager", fresh)
    lane_core.set_drain_check(None)
    yield fresh
    lane_core.set_drain_check(None)


class TestNudgeLaneCap:
    @pytest.mark.asyncio
    async def test_nudge_memory_concurrency_is_capped(
        self, _isolated_lanes: LaneManager, monkeypatch: pytest.MonkeyPatch
    ):
        _isolated_lanes.set_concurrency(LaneType.NUDGE, 2)
        lane = _isolated_lanes.get_lane(LaneType.NUDGE)
        tracker = NudgeTracker()
        monkeypatch.setattr(nudge, "_create_nudge_agent", _fake_builder(tracker))

        tasks = [asyncio.create_task(nudge._nudge_memory(f"s{i}", "sys", [])) for i in range(3)]

        await _wait_until(lambda: lane.active_count == 2)
        await _wait_until(lambda: lane.queued_count == 1)
        assert tracker.max_active == 2, "the third nudge must queue, not run"

        tracker.gate.set()
        await asyncio.gather(*tasks)

        assert tracker.max_active == 2
        assert len(tracker.calls) == 3
        assert lane.active_count == 0
        assert lane.queued_count == 0

    @pytest.mark.asyncio
    async def test_plan_extraction_shares_the_same_lane(
        self, _isolated_lanes: LaneManager, monkeypatch: pytest.MonkeyPatch
    ):
        _isolated_lanes.set_concurrency(LaneType.NUDGE, 1)
        lane = _isolated_lanes.get_lane(LaneType.NUDGE)
        tracker = NudgeTracker()
        monkeypatch.setattr(nudge, "_create_nudge_agent", _fake_builder(tracker))
        monkeypatch.setattr(nudge, "_build_plan_context", lambda session_id: {"plan": "x"})

        memory_task = asyncio.create_task(nudge._nudge_memory("s1", "sys", []))
        await _wait_until(lambda: lane.active_count == 1)

        plan_task = asyncio.create_task(nudge._nudge_plan_extraction("s2", "sys", []))
        await _wait_until(lambda: lane.queued_count == 1)
        assert tracker.max_active == 1

        tracker.gate.set()
        await asyncio.gather(memory_task, plan_task)

        assert tracker.max_active == 1
        assert lane.active_count == 0

    def test_sync_path_never_dispatches_or_acquires(
        self, _isolated_lanes: LaneManager, monkeypatch: pytest.MonkeyPatch
    ):
        from langchain.agents.middleware import AgentMiddleware

        assert ce_core.ContextEngineHook.after_agent is AgentMiddleware.after_agent
        assert ce_core.ContextEngineHook.aafter_agent is AgentMiddleware.aafter_agent

        calls: list[str] = []

        async def _memory(*args):
            calls.append("memory")

        async def _plan(*args):
            calls.append("plan")

        monkeypatch.setattr(nudge, "_nudge_memory", _memory)
        monkeypatch.setattr(nudge, "_nudge_plan_extraction", _plan)
        monkeypatch.setattr(nudge, "_detect_todo_all_complete", lambda session_id: True)

        scheduled = nudge.schedule_compression_nudges("sess-sync", [])

        assert scheduled is False, "no running loop: the scheduler must not dispatch"
        assert calls == [], "the sync path must not dispatch nudge agents"
        lane = _isolated_lanes.get_lane(LaneType.NUDGE)
        assert lane.active_count == 0
        assert lane.queued_count == 0

    def test_nudge_lane_acquire_across_two_loops_does_not_raise(self, _isolated_lanes: LaneManager):
        lane = _isolated_lanes.get_lane(LaneType.NUDGE)

        async def acquire_release() -> None:
            async with lane_slot(LaneType.NUDGE):
                assert lane.active_count == 1

        asyncio.run(acquire_release())
        asyncio.run(acquire_release())

        assert lane.active_count == 0
