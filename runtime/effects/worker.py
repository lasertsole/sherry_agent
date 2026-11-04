"""The effect worker: claim due intents, run a handler, record one outcome.

The worker is opt-in (``effects.worker_enabled`` + master switch + not shadow
mode). It claims with a generation CAS, so an intent re-enqueued while a slow
handler was running is left pending for the next attempt instead of being
marked done by the stale result.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from . import intent_log
from .config import max_attempts, poll_interval_s, retry_initial_s, retry_max_s

__all__ = ["EffectResult", "EffectWorker", "get_worker"]


@dataclass
class EffectResult:
    """One handler outcome; exactly one of the terminal/retry fields applies."""

    done: bool = False
    retry_after_s: float | None = None
    abandon: bool = False


Handler = Callable[[dict], Awaitable[EffectResult]]


class EffectWorker:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._task: asyncio.Task | None = None
        self._stop = False

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop(), name="effect-worker")
        logger.info("effects: worker started ({} handler(s))", len(self._handlers))

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must not kill the worker
                logger.exception("effects: worker tick failed")
            await asyncio.sleep(max(1, poll_interval_s()))

    async def _tick(self) -> None:
        for row in intent_log.fetch_pending(limit=50):
            handler = self._handlers.get(row.kind)
            if handler is None:
                # A handler may be registered later (boot ordering); leave the
                # row pending rather than burning it.
                continue
            if not intent_log.mark_in_flight(row.id, row.generation):
                continue  # another worker claimed it, or a new generation exists
            if row.attempts >= max_attempts():
                intent_log.mark_abandoned(row.id, row.generation)
                logger.warning(
                    "effects: abandoned {}:{} after {} attempts",
                    row.kind,
                    row.key,
                    row.attempts,
                )
                continue
            try:
                result = await handler(row.payload)
            except Exception:  # noqa: BLE001 - a handler failure is a retry
                logger.exception(
                    "effects: handler failed for {} {} (attempt {})",
                    row.kind,
                    row.key,
                    row.attempts + 1,
                )
                self._retry(row, None)
                continue

            if result.done:
                intent_log.mark_done(row.id, row.generation)
            elif result.abandon:
                intent_log.mark_abandoned(row.id, row.generation)
            else:
                self._retry(row, result.retry_after_s)

    @staticmethod
    def _retry(row: intent_log.IntentRow, retry_after_s: float | None) -> None:
        delay = retry_after_s
        if delay is None:
            delay = retry_initial_s() * (2**row.attempts)
        delay = min(float(delay), float(retry_max_s()))
        intent_log.mark_retry(row.id, row.generation, time.time() + max(0.0, delay))


_worker: EffectWorker | None = None


def get_worker() -> EffectWorker:
    global _worker
    if _worker is None:
        _worker = EffectWorker()
    return _worker
