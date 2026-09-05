"""Unit tests for heartbeat tick-loop backoff integration (Task 7, loop-detection-cron-breaker).

Import choice: ``skills.builtin.core.heartbeat.scripts.base`` is imported NORMALLY
(verified working under the repo test env; the module's import chain
``config``/``models`` resolves in ~2.6s and has no import-time side effects beyond
instantiating the module singleton, which these tests deliberately avoid).

Iron rules:
- Fresh ``HeartbeatService`` instances only — NEVER the module-level
  ``heartbeat_service`` singleton.
- No real threads: ``_run_loop`` is driven as a short-lived asyncio task with a
  ``wait_for`` timeout guard (or explicit cancel), never via ``start()``.
- All patching via ``monkeypatch`` (auto-restored, single-process safety).
- ``_decide`` (sync LLM) and the HEARTBEAT.md file read are stubbed per instance
  so ticks exercise only the loop/backoff logic.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from skills.builtin.core.heartbeat.scripts import base as hb_base

BASE_INTERVAL = 60  # seconds; small so backoff arithmetic assertions are exact


# --------------------------------------------------------------------------- helpers
def make_service(on_execute) -> hb_base.HeartbeatService:
    """Fresh service with the production default interval (30 min) *not* used —
    we pass BASE_INTERVAL so formula assertions are exact."""
    return hb_base.HeartbeatService(on_execute=on_execute, interval_s=BASE_INTERVAL)


def arm_tick(svc: hb_base.HeartbeatService, monkeypatch) -> None:
    """Stub the file read + LLM decision so ``_tick`` reaches ``on_execute``."""
    monkeypatch.setattr(svc, "_read_heartbeat_file", lambda: "# Heartbeat Tasks\n- [ ] t")
    monkeypatch.setattr(svc, "_decide", lambda content: ("run", "t"))


def mute_gate(monkeypatch) -> None:
    """Silence the notification gate: success-path ticks would otherwise hit the
    REAL ``evaluate_response`` LLM call (slow + network in a unit test)."""
    monkeypatch.setattr(hb_base, "evaluate_response", lambda response, tasks: False)


def flaky_execute(raises_times: int):
    """on_execute stub: raises the first N invocations, then succeeds."""
    state = {"n": 0}

    async def _exec(tasks: str) -> str:
        state["n"] += 1
        if state["n"] <= raises_times:
            raise RuntimeError(f"boom #{state['n']}")
        return "done"

    return _exec


# --------------------------------------------------------------------------- tests
@pytest.mark.asyncio
async def test_tick_failure_stretches_interval_exponentially(monkeypatch):
    """Hook 1: _tick's OWN except records the failure (interval = base * 2^n)."""
    svc = make_service(flaky_execute(raises_times=99))
    arm_tick(svc, monkeypatch)

    seen = []
    for _ in range(3):
        await svc._tick()
        seen.append(svc._backoff.current_interval)

    assert svc._backoff.consecutive_failures == 3
    assert seen == [BASE_INTERVAL * 2, BASE_INTERVAL * 4, BASE_INTERVAL * 8]
    assert svc._backoff.current_interval == pytest.approx(BASE_INTERVAL * 2**3)


@pytest.mark.asyncio
async def test_tick_success_resets_backoff(monkeypatch):
    """Hooks 1+2: failures accumulate, then a successful tick fully resets.
    Reset is observed through one ``_run_loop`` round (plan: "或经 _run_loop 单轮") —
    the reset lives inside ``_tick``'s success path, which the loop exercises."""
    mute_gate(monkeypatch)
    svc = make_service(flaky_execute(raises_times=2))
    arm_tick(svc, monkeypatch)

    await svc._tick()
    await svc._tick()
    assert svc._backoff.consecutive_failures == 2
    assert svc._backoff.current_interval == pytest.approx(BASE_INTERVAL * 4)

    # Fast-forward: the backoff has no clock of its own — shrink the pending
    # sleep to ms so the loop round runs immediately.
    svc._backoff.current_interval = 0.001
    svc._running = True
    task = asyncio.create_task(svc._run_loop())
    try:
        for _ in range(100):  # poll ≤ 0.5s for the success tick to land
            await asyncio.sleep(0.005)
            if svc._backoff.consecutive_failures == 0:
                break
        assert svc._backoff.consecutive_failures == 0
        assert svc._backoff.current_interval == pytest.approx(BASE_INTERVAL)
        assert not svc._backoff.is_exhausted()
    finally:
        task.cancel()
        await task  # _run_loop SWALLOWS CancelledError by design (stop() contract)
        assert task.done() and not task.cancelled()


@pytest.mark.asyncio
async def test_five_consecutive_failures_exhaust_backoff(monkeypatch):
    """5 consecutive failures → is_exhausted() True (service should pause)."""
    svc = make_service(flaky_execute(raises_times=99))
    arm_tick(svc, monkeypatch)

    for _ in range(5):
        await svc._tick()

    assert svc._backoff.consecutive_failures == 5
    assert svc._backoff.is_exhausted()


@pytest.mark.asyncio
async def test_exhausted_run_loop_exits_without_tick(monkeypatch):
    """Hook 4: exhausted check at top of each iteration → CRITICAL log, then
    ``_run_loop`` returns (service paused, process alive) before any new tick."""
    ticks: list[str] = []

    async def exec_ok(tasks: str) -> str:
        ticks.append(tasks)
        return "ok"

    svc = make_service(exec_ok)
    arm_tick(svc, monkeypatch)

    for _ in range(5):
        svc._backoff.record_failure("seed")
    assert svc._backoff.is_exhausted()
    svc._backoff.current_interval = 0.001  # ms-level per plan; loop must exit before sleeping
    svc._running = True  # start() minus the background task — loop state only

    criticals: list[str] = []
    handler_id = logger.add(lambda m: criticals.append(m.record["level"].name), level="CRITICAL")
    try:
        task = asyncio.create_task(svc._run_loop())
        await asyncio.wait_for(task, timeout=5.0)  # guard: loop must NOT hang
    finally:
        logger.remove(handler_id)

    assert task.done() and not task.cancelled() and task.exception() is None
    assert ticks == []  # no new tick execution after exhaustion
    assert criticals == ["CRITICAL"]  # exactly one CRITICAL log, then exit
    assert svc._running is True  # service paused (not stopped/crashed) — awaits manual recovery


@pytest.mark.asyncio
async def test_run_loop_sleeps_on_backoff_interval_and_records_success(monkeypatch):
    """Hooks 2+3: the loop sleeps on ``_backoff.current_interval`` (not the fixed
    ``interval_s``) and a clean tick fully resets the backoff."""
    mute_gate(monkeypatch)
    state = {"n": 0}

    async def exec_ok(tasks: str) -> str:
        state["n"] += 1
        return "ok"

    svc = make_service(exec_ok)
    arm_tick(svc, monkeypatch)

    svc._backoff.record_failure("seed")  # broken impl would sleep interval_s = 60s here
    svc._backoff.current_interval = 0.001  # backoff-driven sleep = 1ms
    svc._running = True

    task = asyncio.create_task(svc._run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    await task  # _run_loop SWALLOWS CancelledError by design (stop() contract)
    assert task.done() and not task.cancelled()

    # Exactly ONE tick: the pending sleep was current_interval (1ms, hook 3 — a
    # 60s interval_s sleep would give 0 ticks), then success RESET the interval
    # to base (60s → no second tick inside the 50ms window).
    assert state["n"] == 1
    assert svc._backoff.consecutive_failures == 0  # hook 2: record_success ran
    assert svc._backoff.current_interval == pytest.approx(BASE_INTERVAL)
