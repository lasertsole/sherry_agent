"""Process-level multi-lane concurrency queue with FIFO ordering.

Four lanes gate concurrent work: MAIN agent turns, SUBAGENT executions,
BACKGROUND_REVIEW calls and serial NESTED reply turns. Each lane is an
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
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum

from loguru import logger

from config.features import LANE_SYSTEM


class LaneType(StrEnum):
    """The four process-level work lanes."""

    MAIN = "main"
    SUBAGENT = "subagent"
    BACKGROUND_REVIEW = "background_review"
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
    """Single concurrency lane: an explicit waiter queue with a live limit.

    The limit is read at every admission, so ``set_max`` applies IMMEDIATELY —
    including while the lane is saturated and waiters are parked (the old
    semaphore swap had to wait for ``_queued == 0``, so a full lane could keep
    running the stale limit indefinitely, and a lowered limit could even grant
    one extra slot through the ``max(1, ...)`` rebind floor).

    FIFO and cancellation-safe: a parked acquirer is one future in ``_waiters``;
    ``release`` hands the freed slot to the oldest live waiter, and a task
    cancelled after its future was granted returns that slot instead of
    leaking it. The loop binding is retained for the cross-loop misuse warning
    (production runs one loop); a loop change drops the old futures, which no
    live loop could ever wake.
    """

    def __init__(self, name: str, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError(f"Lane {name!r} max_concurrent must be >= 1")
        self.name = name
        self._max = max_concurrent
        self._active = 0
        self._queued = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bind_loop(self) -> None:
        """Bind on first use; a loop change drops waiters the new loop cannot wake."""
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            return
        if self._loop is loop:
            return
        logger.warning(
            "Lane {} acquired from a different event loop; dropping {} parked waiter(s) "
            "(active={}, max={})",
            self.name,
            len(self._waiters),
            self._active,
            self._max,
        )
        for future in self._waiters:
            if not future.done():
                future.cancel()
        self._waiters.clear()
        self._queued = 0
        self._loop = loop

    async def acquire(self, warn_ms: int = 5000) -> None:
        """Acquire one slot, queueing FIFO when the lane is full.

        Refuses (``RuntimeError``, no permit consumed) while a registered
        drain check reports draining. Cancellation while queueing propagates
        without consuming a permit; cancellation after the grant releases it.
        """
        check = _drain_check
        if check is not None and check():
            raise RuntimeError(f"Lane {self.name} acquire refused: gateway is draining")
        self._bind_loop()
        if self._active < self._max:
            self._active += 1
            logger.debug("Lane {} acquired (active={}, queued=0)", self.name, self._active)
            return

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        self._queued = len(self._waiters)
        wait_start = time.monotonic()
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled() and future.exception() is None:
                # The slot was granted before the cancellation landed.
                self.release()
            else:
                try:
                    self._waiters.remove(future)
                except ValueError:
                    pass
                self._queued = len(self._waiters)
            raise
        self._queued = len(self._waiters)
        waited_ms = (time.monotonic() - wait_start) * 1000
        if waited_ms > warn_ms:
            logger.warning(
                "Lane {} waited {:.0f}ms for a slot (active={}, queued={})",
                self.name,
                waited_ms,
                self._active,
                self._queued,
            )
        logger.debug(
            "Lane {} acquired (active={}, queued={}, waited={:.0f}ms)",
            self.name,
            self._active,
            self._queued,
            waited_ms,
        )

    def release(self) -> None:
        """Return one slot to the lane. Never underflows."""
        if self._active <= 0:
            logger.warning("Lane {} release() called with no active slot", self.name)
            return
        self._active -= 1
        self._wake_waiters()

    def _wake_waiters(self) -> None:
        """Grant slots to the oldest waiters while capacity remains."""
        while self._active < self._max and self._waiters:
            future = self._waiters.popleft()
            self._queued = len(self._waiters)
            if future.done():
                continue
            self._active += 1
            future.set_result(None)

    def set_max(self, max_concurrent: int) -> None:
        """Hot-update the limit; in-flight slots are unaffected.

        Raising admits parked waiters immediately; lowering stops new grants
        until releases bring ``_active`` back under the new limit.
        """
        if max_concurrent < 1:
            raise ValueError(f"Lane {self.name!r} max_concurrent must be >= 1")
        previous = self._max
        self._max = max_concurrent
        if previous != max_concurrent:
            logger.info(
                "Lane {}: max_concurrency {} -> {} (active={}, queued={})",
                self.name,
                previous,
                max_concurrent,
                self._active,
                self._queued,
            )
        self._wake_waiters()

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
            LaneType.BACKGROUND_REVIEW: Lane(
                "background_review", LANE_SYSTEM["background_review_max_concurrent"]
            ),
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
#: Guards the first construction (see get_lane_manager).
_MANAGER_LOCK = threading.Lock()


def get_lane_manager() -> LaneManager:
    """Return the process-level lane manager, creating it on first use.

    Double-checked under a module lock: two threads racing the first call must
    get the SAME manager (one shared set of lanes), not one each.
    """
    global _manager
    if _manager is None:
        with _MANAGER_LOCK:
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
