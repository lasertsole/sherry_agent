"""Unit tests: the subagent sweeper is event-driven with a fallback cadence.

M7 de-polling contract:
- a run reaching TERMINAL publishes a notification that wakes the sweep loop
  immediately (instead of waiting out ``sweeper_interval_seconds``);
- the periodic interval is KEPT as the missed-notification / orphan / expiry
  safety net, so no event still means a sweep;
- the loop releases its subscription on every exit path.
"""

import asyncio
import time

import pytest

from agent.tools.subagent.registry import completion_events, sweeper
from runtime.process.periodic_backoff import PeriodicBackoff

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_sweeper_state():
    """Isolate sweeper module globals per test so nothing leaks across tests."""
    saved = (sweeper._backoff, sweeper._running, sweeper._sweeper_task)
    sweeper._backoff = None
    sweeper._running = False
    sweeper._sweeper_task = None
    yield
    sweeper._backoff, sweeper._running, sweeper._sweeper_task = saved


async def _wait_for_subscription(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while sweeper._sweep_subscription is None:
        assert time.monotonic() < deadline, "sweeper never subscribed to terminal events"
        await asyncio.sleep(0.005)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_terminal_notification_wakes_sweeper_before_the_interval(monkeypatch):
    # Given a loop whose fallback interval is far away (60 s)
    sweeper._backoff = PeriodicBackoff(base_interval=60.0)
    monkeypatch.setattr(sweeper, "_EVENT_SWEEP_MIN_SPACING_SECONDS", 0.0)
    swept = asyncio.Event()

    async def _sweep() -> None:
        swept.set()

    monkeypatch.setattr(sweeper, "_do_sweep", _sweep)
    task = asyncio.create_task(sweeper._sweep_loop())
    try:
        await _wait_for_subscription()

        # When a child run turns terminal
        started_at = time.monotonic()
        completion_events.notify_run_terminal("child-event", run_id="run-event")

        # Then the sweep runs immediately, not after the 60 s fallback.
        await asyncio.wait_for(swept.wait(), timeout=2.0)
        assert time.monotonic() - started_at < 1.0
    finally:
        await _cancel(task)
    assert sweeper._sweep_subscription is None


@pytest.mark.asyncio
async def test_fallback_interval_still_sweeps_without_a_notification(monkeypatch):
    # Given a loop nobody notifies (a missed event / orphan / expiry case)
    sweeper._backoff = PeriodicBackoff(base_interval=0.05)
    monkeypatch.setattr(sweeper, "_EVENT_SWEEP_MIN_SPACING_SECONDS", 0.0)
    swept = asyncio.Event()

    async def _sweep() -> None:
        swept.set()

    monkeypatch.setattr(sweeper, "_do_sweep", _sweep)
    task = asyncio.create_task(sweeper._sweep_loop())
    try:
        # Then the fallback interval alone still drives a sweep.
        await asyncio.wait_for(swept.wait(), timeout=2.0)
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_loop_exit_releases_the_subscription(monkeypatch):
    # Given a loop whose backoff is exhausted on the first failure
    sweeper._backoff = PeriodicBackoff(base_interval=0.01, max_consecutive_failures=1)
    monkeypatch.setattr(sweeper, "_EVENT_SWEEP_MIN_SPACING_SECONDS", 0.0)

    async def _failing_sweep() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sweeper, "_do_sweep", _failing_sweep)

    # When the loop runs to its natural exit
    await sweeper._sweep_loop()

    # Then it is stopped and its subscription is gone.
    assert sweeper._running is False
    assert sweeper._sweep_subscription is None
