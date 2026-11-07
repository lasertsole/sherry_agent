"""Background-review agents are gated by the process-wide BACKGROUND_REVIEW lane.

Covers plan Step 9's lane landing:

1. ``_execute_memory_review`` concurrency is capped by the BACKGROUND_REVIEW lane (queue, never reject)
2. the skill-review pass shares the same lane
3. the compression-time scheduler never dispatches without a running event
   loop, and the ``system_prompt_injection`` middleware does not override the
   after-agent hooks — so no ``run_async()`` worker loop can ever acquire the
   loop-bound semaphore
4. acquiring the BACKGROUND_REVIEW lane across two loops does not raise (Wave 1 rebind)

Hermetic: the background-review agent builder is replaced with a gated fake (no LLM), and
each test gets a fresh ``LaneManager`` through the monkeypatched singleton.
"""

import asyncio
import time

import pytest

import runtime.lane.core as lane_core
from runtime.lane.core import LaneManager, LaneType, lane_slot
from agent.middlewares.system_prompt import core as ce_core
from agent.middlewares.summarization import background_reviews as review_mod

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _passing_skill_judge(monkeypatch: pytest.MonkeyPatch):
    class _Judgement:
        passed = True
        verdict = "pass"
        grounded_in_run = True
        reusable = True
        trigger_quality = 8
        quality = 8
        no_op_justified = False
        feedback = ""

    async def _pass(*_args, **_kwargs):
        return _Judgement()

    monkeypatch.setattr(review_mod, "judge_skill_update", _pass)


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


class ReviewTracker:
    """Shared state for fake background-review agents: gate + concurrency high-water mark."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[dict] = []
        self.gate = asyncio.Event()


class FakeReviewAgent:
    def __init__(self, tracker: ReviewTracker) -> None:
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


def _fake_builder(tracker: ReviewTracker):
    """Tolerant of both factory signatures (memory review / skill review)."""

    async def build(system_prompt, *args, **kwargs):
        return FakeReviewAgent(tracker)

    return build


@pytest.fixture(autouse=True)
def _isolated_lanes(monkeypatch: pytest.MonkeyPatch) -> LaneManager:
    fresh = LaneManager()
    monkeypatch.setattr(lane_core, "_manager", fresh)
    lane_core.set_drain_check(None)
    yield fresh
    lane_core.set_drain_check(None)


class TestBackgroundReviewLaneCap:
    @pytest.mark.asyncio
    async def test_execute_memory_review_concurrency_is_capped(
        self, _isolated_lanes: LaneManager, monkeypatch: pytest.MonkeyPatch
    ):
        _isolated_lanes.set_concurrency(LaneType.BACKGROUND_REVIEW, 2)
        lane = _isolated_lanes.get_lane(LaneType.BACKGROUND_REVIEW)
        tracker = ReviewTracker()
        monkeypatch.setattr(review_mod, "_create_memory_review_agent", _fake_builder(tracker))

        tasks = [
            asyncio.create_task(review_mod._execute_memory_review(f"s{i}", "sys", []))
            for i in range(3)
        ]

        await _wait_until(lambda: lane.active_count == 2)
        await _wait_until(lambda: lane.queued_count == 1)
        assert tracker.max_active == 2, "the third review must queue, not run"

        tracker.gate.set()
        await asyncio.gather(*tasks)

        assert tracker.max_active == 2
        assert len(tracker.calls) == 3
        assert lane.active_count == 0
        assert lane.queued_count == 0

    @pytest.mark.asyncio
    async def test_skill_review_shares_the_same_lane(
        self, _isolated_lanes: LaneManager, monkeypatch: pytest.MonkeyPatch
    ):
        _isolated_lanes.set_concurrency(LaneType.BACKGROUND_REVIEW, 1)
        lane = _isolated_lanes.get_lane(LaneType.BACKGROUND_REVIEW)
        tracker = ReviewTracker()
        monkeypatch.setattr(review_mod, "_create_memory_review_agent", _fake_builder(tracker))
        monkeypatch.setattr(review_mod, "_create_skill_review_agent", _fake_builder(tracker))
        monkeypatch.setattr(review_mod, "_build_plan_context", lambda session_id: {"plan": "x"})

        memory_task = asyncio.create_task(review_mod._execute_memory_review("s1", "sys", []))
        await _wait_until(lambda: lane.active_count == 1)

        plan_task = asyncio.create_task(review_mod._run_skill_review("s2"))
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

        assert type(ce_core.system_prompt_injection).after_agent is AgentMiddleware.after_agent
        assert type(ce_core.system_prompt_injection).aafter_agent is AgentMiddleware.aafter_agent

        calls: list[str] = []

        async def _memory(*args):
            calls.append("memory")

        async def _plan(*args):
            calls.append("plan")

        monkeypatch.setattr(review_mod, "_execute_memory_review", _memory)
        monkeypatch.setattr(review_mod, "_run_skill_review", _plan)

        scheduled = review_mod.schedule_memory_review("sess-sync", [])

        assert scheduled is False, "no running loop: the scheduler must not dispatch"
        assert calls == [], "the sync path must not dispatch background-review agents"
        lane = _isolated_lanes.get_lane(LaneType.BACKGROUND_REVIEW)
        assert lane.active_count == 0
        assert lane.queued_count == 0

    def test_background_review_lane_acquire_across_two_loops_does_not_raise(
        self, _isolated_lanes: LaneManager
    ):
        lane = _isolated_lanes.get_lane(LaneType.BACKGROUND_REVIEW)

        async def acquire_release() -> None:
            async with lane_slot(LaneType.BACKGROUND_REVIEW):
                assert lane.active_count == 1

        asyncio.run(acquire_release())
        asyncio.run(acquire_release())

        assert lane.active_count == 0
