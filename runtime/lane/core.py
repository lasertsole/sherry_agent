"""Process-level multi-lane concurrency queue with FIFO ordering.

Four lanes gate concurrent work: MAIN agent turns, SUBAGENT executions,
NUDGE calls and serial NESTED reply turns. Each lane is an
``asyncio.Semaphore`` bound to the running event loop plus active/queued
counters.

The semaphore is created lazily on first ``acquire()`` so it binds to the
loop that actually uses the lane. If the lane is later acquired from a
different loop (misuse — production runs a single main loop), the lane logs
a warning and rebinds a fresh semaphore with the outstanding slots
deducted, so cross-loop misuse cannot silently double-issue permits.

``runtime/lane`` must not import ``agent``/``server``/``models``: the
optional drain-mode gate is injected through ``set_drain_check()``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum

from loguru import logger

from config.features import LANE_SYSTEM


class LaneType(StrEnum):
    """The four process-level work lanes."""

    MAIN = "main"
    SUBAGENT = "subagent"
    NUDGE = "nudge"
    NESTED = "nested"


_drain_check: Callable[[], bool] | None = None


def set_drain_check(fn: Callable[[], bool] | None) -> None:
    """Register (or clear) the drain-mode checker.

    The checker is consulted before every lane ``acquire()`` waits; while it
    reports draining, new acquires are refused without consuming a permit.
    The server wires the subagent registry's ``is_gateway_draining`` here so
    ``runtime/lane`` stays free of upward imports.
    """
    global _drain_check
    _drain_check = fn


class Lane:
    """Single concurrency lane: asyncio.Semaphore + counters.

    The Semaphore is lazily created on first acquire() to ensure it binds
    to the running event loop. If the loop changes (shouldn't happen in
    production — single main loop), ``_ensure_sem()`` rebinds it.
    """

    def __init__(self, name: str, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError(f"Lane {name!r} max_concurrent must be >= 1")
        self.name = name
        self._max = max_concurrent
        self._active = 0
        self._queued = 0
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_sem(self) -> asyncio.Semaphore:
        """Return the lane semaphore, creating or rebinding it for the running loop."""
        loop = asyncio.get_running_loop()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max)
            self._loop = loop
        elif self._loop is not loop:
            logger.warning(
                "Lane {} acquired from a different event loop; rebinding semaphore "
                "(active={}, max={})",
                self.name,
                self._active,
                self._max,
            )
            self._rebind(loop)
        return self._sem

    def _rebind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Replace the semaphore with a fresh one bound to ``loop``.

        The fresh semaphore starts with ``max(1, max_concurrent - active)``
        permits: slots already active keep running but are deducted so they
        cannot be issued twice, and the ``max(1, ...)`` floor keeps the lane
        able to make progress even when it was full.
        """
        permits = max(1, self._max - self._active)
        self._sem = asyncio.Semaphore(permits)
        self._loop = loop

    async def acquire(self, warn_ms: int = 5000) -> None:
        """Acquire one slot, queueing FIFO when the lane is full.

        Refuses (``RuntimeError``, no permit consumed) while a registered
        drain check reports draining. Cancellation while queueing propagates
        without consuming a permit.
        """
        check = _drain_check
        if check is not None and check():
            raise RuntimeError(f"Lane {self.name} acquire refused: gateway is draining")
        sem = self._ensure_sem()
        self._queued += 1
        wait_start = time.monotonic()
        try:
            await sem.acquire()
        except asyncio.CancelledError:
            self._queued -= 1
            raise  # never acquired — do not release
        self._queued -= 1
        waited_ms = (time.monotonic() - wait_start) * 1000
        if waited_ms > warn_ms:
            logger.warning(
                "Lane {} waited {:.0f}ms for a slot (active={}, queued={})",
                self.name,
                waited_ms,
                self._active,
                self._queued,
            )
        self._active += 1

    def release(self) -> None:
        """Return one slot to the lane. Never underflows."""
        if self._active <= 0:
            logger.warning("Lane {} release() called with no active slot", self.name)
            return
        self._active -= 1
        if self._sem is not None:
            self._sem.release()

    def set_max(self, max_concurrent: int) -> None:
        """Hot-update the limit; in-flight slots are unaffected.

        Existing slots keep their permit and the fresh semaphore is seeded
        with the remaining capacity, so only new acquires see the new limit.
        """
        if max_concurrent < 1:
            raise ValueError(f"Lane {self.name!r} max_concurrent must be >= 1")
        self._max = max_concurrent
        if self._sem is not None and self._loop is not None:
            self._rebind(self._loop)

    async def drain(self, timeout: float = 30.0) -> bool:
        """Wait for all active slots to release. Returns True if drained in time."""
        deadline = time.monotonic() + timeout
        while self._active > 0:
            if time.monotonic() >= deadline:
                logger.warning(
                    "Lane {} drain timed out: {} active slot(s) remain",
                    self.name,
                    self._active,
                )
                return False
            await asyncio.sleep(0.1)
        return True

    @property
    def active_count(self) -> int:
        """Number of slots currently held."""
        return self._active

    @property
    def queued_count(self) -> int:
        """Number of acquirers currently waiting for a slot."""
        return self._queued

    @property
    def max_concurrent(self) -> int:
        """Current slot limit (may change via ``set_max``)."""
        return self._max

    def snapshot(self) -> dict[str, int | str]:
        """Point-in-time lane counters for observability endpoints."""
        return {
            "name": self.name,
            "max_concurrent": self._max,
            "active": self._active,
            "queued": self._queued,
        }


class LaneManager:
    """Process-level singleton managing all lanes."""

    def __init__(self) -> None:
        self._lanes: dict[LaneType, Lane] = {
            LaneType.MAIN: Lane("main", LANE_SYSTEM["main_max_concurrent"]),
            LaneType.SUBAGENT: Lane("subagent", LANE_SYSTEM["subagent_max_concurrent"]),
            LaneType.NUDGE: Lane("nudge", LANE_SYSTEM["nudge_max_concurrent"]),
            LaneType.NESTED: Lane("nested", LANE_SYSTEM["nested_max_concurrent"]),
        }
        self._warn_ms = LANE_SYSTEM["lane_wait_warn_ms"]
        self._drain_timeout = LANE_SYSTEM["lane_drain_timeout_seconds"]

    def get_lane(self, lane_type: LaneType) -> Lane:
        """Return the lane registered for ``lane_type``."""
        return self._lanes[lane_type]

    def set_concurrency(self, lane_type: LaneType, max_concurrent: int) -> None:
        """Hot-update one lane's limit; in-flight slots are unaffected."""
        self._lanes[lane_type].set_max(max_concurrent)

    async def drain_all(self, timeout: float | None = None) -> dict[str, bool]:
        """Drain every lane for graceful shutdown."""
        t = self._drain_timeout if timeout is None else timeout
        results: dict[str, bool] = {}
        for lane_type, lane in self._lanes.items():
            results[lane_type.value] = await lane.drain(t)
        return results

    def snapshot(self) -> dict[str, dict[str, int | str]]:
        """Per-lane counters, keyed by lane name."""
        return {lane_type.value: lane.snapshot() for lane_type, lane in self._lanes.items()}


_manager: LaneManager | None = None


def get_lane_manager() -> LaneManager:
    """Return the process-level lane manager, creating it on first use."""
    global _manager
    if _manager is None:
        _manager = LaneManager()
    return _manager


@asynccontextmanager
async def lane_slot(lane_type: LaneType) -> AsyncIterator[None]:
    """Acquire a lane slot; waits in queue when full. Releases on exit/exception."""
    manager = get_lane_manager()
    lane = manager.get_lane(lane_type)
    await lane.acquire(warn_ms=manager._warn_ms)
    try:
        yield
    finally:
        lane.release()
