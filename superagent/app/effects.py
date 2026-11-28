"""Durable side-effect queue (prototype ``src/app/effects.ts``)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any

from superagent.app.store import Store
from superagent.core.model import Effect
from superagent.infra.health import HealthBoard
from superagent.limits import EFFECT_RETRY_DEFAULT


class Later(Exception):  # noqa: N818 - protocol name from the prototype
    """Reschedule without counting a failure."""


EffectRun = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class EffectHandler:
    """One registered Effect kind."""

    run: EffectRun
    concurrency: int
    retry: tuple[int, int]


def _now() -> datetime:
    return datetime.now(UTC)


def _at(seconds: float) -> str:
    return (_now() + timedelta(seconds=seconds)).isoformat()


def backoff(attempts: int, retry: tuple[int, int]) -> int:
    """Return an exponential delay clamped to the handler's range."""
    low, high = retry
    return min(max(low * 2 ** max(0, attempts - 1), low), high)


class Effects:
    """Coalescing, retrying, commit-driven Effect processor."""

    def __init__(self, store: Store, board: HealthBoard) -> None:
        self.store = store
        self.board = board
        self._handlers: dict[str, EffectHandler] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        """Upsert a pending Effect; coalescing keeps seq and attempts."""
        previous = self.store.effects.get(kind, key)
        effect = Effect(
            kind=kind,
            key=key,
            payload=payload,
            seq=previous.seq if previous else None,
            status="pending",
            attempts=previous.attempts if previous else 0,
            last_error=None,
            due_at="",
        )
        self.store.effects.save(effect)
        self._wake.set()

    def handle(
        self,
        kind: str,
        run: EffectRun,
        *,
        concurrency: int = 1,
        retry: tuple[int, int] = EFFECT_RETRY_DEFAULT,
    ) -> None:
        """Register one handler per kind."""
        self.board.claim(kind, kind)
        self._handlers[kind] = EffectHandler(run=run, concurrency=concurrency, retry=retry)

    async def run_once(self, kind: str, now: str | None = None) -> int:
        """Process all due rows of one kind and return the number attempted."""
        handler = self._handlers.get(kind)
        if handler is None:
            return 0
        rows = self.store.effects.due_rows(now or _now().isoformat())
        due = [row for row in rows if row.kind == kind]
        if not due:
            return 0
        semaphore = asyncio.Semaphore(handler.concurrency)

        async def run_one(row: Effect) -> None:
            async with semaphore:
                try:
                    await handler.run(row.payload)
                except Later:
                    self.store.effects.reschedule(row.kind, row.key, _at(2))
                    return
                except Exception as exc:
                    attempts = self.store.effects.bump_attempts(row.kind, row.key, str(exc))
                    self.board.report(
                        kind,
                        {"state": "error", "detail": str(exc), "at": _now().isoformat()},
                    )
                    self.store.effects.reschedule(
                        row.kind, row.key, _at(backoff(attempts, handler.retry))
                    )
                    return
                self.store.effects.set_done(row.kind, row.key)
                self.board.clear(kind)

        await asyncio.gather(*(run_one(row) for row in due))
        return len(due)

    async def start(self) -> None:
        """Start the background Effect loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def drain(self) -> None:
        """Stop processing and keep pending rows for the next process."""
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def wakeup(self) -> None:
        """Request an immediate pass."""
        self._wake.set()

    def pending(self) -> int:
        """Return the number of pending Effects."""
        return sum(1 for row in self.store.effects.list() if row.status == "pending")

    def idle(self) -> bool:
        """Return whether no Effects are pending."""
        return self.pending() == 0

    def count(self, kind: str) -> int:
        """Return the number of non-done Effects of one kind."""
        return sum(
            1 for row in self.store.effects.list() if row.kind == kind and row.status != "done"
        )

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            for kind in self._handlers:
                await self.run_once(kind)
            await asyncio.sleep(0)


__all__ = ["EffectHandler", "Effects", "Later", "backoff"]
