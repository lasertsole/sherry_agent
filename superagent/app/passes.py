"""Two-second pass scheduler (prototype ``src/app/passes.ts``)."""

from __future__ import annotations

import asyncio
from typing import Protocol

from superagent.app.effects import Effects
from superagent.limits import DEFAULT_PASS_INTERVAL_S


class PassRuntime(Protocol):
    """Runtime surface required by the pass scheduler."""

    effects: Effects

    def evaluate_due(self) -> None: ...


class Passes:
    """Run due Effects then Engine.evaluate_due, one pass at a time."""

    def __init__(self, runtime: PassRuntime, interval_s: float = DEFAULT_PASS_INTERVAL_S) -> None:
        self._runtime = runtime
        self._interval = interval_s
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.count = 0

    def request(self) -> None:
        """Request an immediate pass."""
        self._wake.set()

    async def start(self) -> None:
        """Start the pass loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the pass loop."""
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run_once(self) -> None:
        """Execute one pass."""
        for kind in list(self._runtime.effects._handlers):
            await self._runtime.effects.run_once(kind)
        self._runtime.evaluate_due()
        self.count += 1

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stop.is_set():
                return
            await self.run_once()


__all__ = ["Passes", "PassRuntime"]
