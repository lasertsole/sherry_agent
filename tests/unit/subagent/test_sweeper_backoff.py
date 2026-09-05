"""Unit tests: PeriodicBackoff integration in the subagent sweeper loop (plan task 8).

Covers: failure backoff growth, success reset, exhaustion stop semantics,
stop_sweeper backoff reset with a fresh rebuild on restart, and lazy init
from config.
"""

import asyncio

import pytest
from runtime.periodic_backoff import PeriodicBackoff

from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import sweeper


@pytest.fixture(autouse=True)
def _restore_sweeper_state():
    """Isolate sweeper module globals per test so nothing leaks across tests."""
    saved_backoff = sweeper._backoff
    saved_running = sweeper._running
    saved_task = sweeper._sweeper_task
    sweeper._backoff = None
    sweeper._running = False
    sweeper._sweeper_task = None
    yield
    sweeper._backoff = saved_backoff
    sweeper._running = saved_running
    sweeper._sweeper_task = saved_task


def _limit_iterations(limit: int, sleep_calls: list[float]):
    """Build a fake asyncio.sleep that records delays and stops the loop
    (via the module's own _running flag) after ``limit`` iterations."""

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) >= limit:
            sweeper._running = False

    return fake_sleep


@pytest.mark.asyncio
async def test_sweep_failures_record_and_backoff_grows(monkeypatch):
    """_do_sweep raising repeatedly must record failures and grow the interval."""
    sweeper._backoff = PeriodicBackoff(base_interval=0.02, max_consecutive_failures=10)
    sleep_calls: list[float] = []

    async def failing_sweep() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(asyncio, "sleep", _limit_iterations(3, sleep_calls))
    monkeypatch.setattr(sweeper, "_do_sweep", failing_sweep)

    await sweeper._sweep_loop()

    backoff = sweeper._get_backoff()
    assert backoff.consecutive_failures == 3
    assert backoff.current_interval == pytest.approx(0.02 * 2**3)
    # sleep interval per iteration: base, base*2, base*4 (before each failing sweep)
    assert sleep_calls == pytest.approx([0.02, 0.04, 0.08])


@pytest.mark.asyncio
async def test_sweep_success_resets_backoff(monkeypatch):
    """A successful sweep must fully reset failures/interval/exhaustion."""
    sweeper._backoff = PeriodicBackoff(base_interval=0.02, max_consecutive_failures=10)
    sweeper._backoff.record_failure("simulated-1")
    sweeper._backoff.record_failure("simulated-2")
    assert sweeper._backoff.consecutive_failures == 2  # precondition

    iterations = 0

    async def ok_sweep() -> None:
        nonlocal iterations
        iterations += 1

    monkeypatch.setattr(asyncio, "sleep", _limit_iterations(1, []))
    monkeypatch.setattr(sweeper, "_do_sweep", ok_sweep)

    await sweeper._sweep_loop()

    backoff = sweeper._get_backoff()
    assert iterations == 1
    assert backoff.consecutive_failures == 0
    assert backoff.current_interval == backoff.base_interval
    assert backoff.is_exhausted() is False


@pytest.mark.asyncio
async def test_sweep_exhaustion_stops_loop(monkeypatch):
    """Exhausted backoff must flip _running False and exit instead of re-sweeping."""
    sweeper._backoff = PeriodicBackoff(base_interval=0.01, max_consecutive_failures=1)
    sleep_calls: list[float] = []
    sweeps = 0

    async def failing_sweep() -> None:
        nonlocal sweeps
        sweeps += 1
        raise RuntimeError("dead")

    async def noop_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", noop_sleep)
    monkeypatch.setattr(sweeper, "_do_sweep", failing_sweep)

    await sweeper._sweep_loop()

    assert sweeper._running is False
    assert sweeps == 1  # loop exited instead of sweeping again
    assert len(sleep_calls) == 1
    assert sweeper._get_backoff().is_exhausted() is True


@pytest.mark.asyncio
async def test_stop_sweeper_resets_backoff_and_restart_rebuilds_fresh():
    """stop_sweeper() drops the backoff; start_sweeper() rebuilds it at base state."""
    sweeper._backoff = PeriodicBackoff(base_interval=0.01)
    sweeper._backoff.record_failure("simulated")
    sweeper._sweeper_task = None  # no live task; stop_sweeper still resets state

    await sweeper.stop_sweeper()
    assert sweeper._backoff is None

    await sweeper.start_sweeper()
    backoff = sweeper._get_backoff()
    assert backoff is not None
    assert backoff.consecutive_failures == 0
    assert backoff.current_interval == get_config().sweeper_interval_seconds

    await sweeper.stop_sweeper()  # cleanup: cancel the live task


def test_get_backoff_lazy_init_from_config():
    """_get_backoff() lazily builds from sweeper_interval_seconds and is idempotent."""
    assert sweeper._backoff is None
    backoff = sweeper._get_backoff()
    assert isinstance(backoff, PeriodicBackoff)
    assert backoff.base_interval == get_config().sweeper_interval_seconds
    assert sweeper._get_backoff() is backoff
