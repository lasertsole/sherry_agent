"""Deferred-cleanup shutdown tests (audit #45).

Locks the contract of ``shutdown_deferred_cleanup``: every pending timer is
cancelled and awaited, the registry is drained, repeat calls are no-ops, and
``stop_sweeper`` (the lifecycle seam) triggers the same cancellation.
"""

import pytest

from agent.tools.subagent.registry import lifecycle, sweeper
from agent.tools.subagent.types.registry import SubagentRunRecord

pytestmark = [pytest.mark.unit]


def _make_run(run_id: str) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=f"agent:main:subagent:{run_id}",
        requester_session_key="agent:main:session:parent",
        task="t",
    )


@pytest.fixture(autouse=True)
def _drain_deferred_timers():
    yield
    for timer in lifecycle._deferred_cleanup_timers.values():
        timer.cancel()
    lifecycle._deferred_cleanup_timers.clear()


@pytest.fixture
def _isolated_sweeper_state():
    saved = (sweeper._backoff, sweeper._running, sweeper._sweeper_task)
    sweeper._backoff = None
    sweeper._running = False
    sweeper._sweeper_task = None
    yield
    sweeper._backoff, sweeper._running, sweeper._sweeper_task = saved


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_all_pending_timers():
    for index in range(3):
        lifecycle._schedule_deferred_cleanup_resume(_make_run(f"r{index}"), delay_seconds=60.0)
    timers = list(lifecycle._deferred_cleanup_timers.values())
    assert len(timers) == 3

    cancelled = await lifecycle.shutdown_deferred_cleanup()

    assert cancelled == 3
    assert lifecycle._deferred_cleanup_timers == {}
    assert all(timer.cancelled() for timer in timers)


@pytest.mark.asyncio
async def test_shutdown_is_idempotent():
    lifecycle._schedule_deferred_cleanup_resume(_make_run("r1"), delay_seconds=60.0)

    assert await lifecycle.shutdown_deferred_cleanup() == 1
    assert await lifecycle.shutdown_deferred_cleanup() == 0
    assert lifecycle._deferred_cleanup_timers == {}


@pytest.mark.asyncio
async def test_stop_sweeper_cancels_deferred_timers(_isolated_sweeper_state):
    lifecycle._schedule_deferred_cleanup_resume(_make_run("r1"), delay_seconds=60.0)
    timer = next(iter(lifecycle._deferred_cleanup_timers.values()))

    await sweeper.stop_sweeper()

    assert lifecycle._deferred_cleanup_timers == {}
    assert timer.cancelled()
