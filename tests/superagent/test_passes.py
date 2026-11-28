"""Pass scheduler tests."""

from __future__ import annotations

import pytest

from superagent.app.passes import Passes


class StubEffects:
    """Record Effect pass calls."""

    def __init__(self) -> None:
        self._handlers = {"notice": object(), "reply": object()}
        self.calls: list[str] = []

    async def run_once(self, kind: str) -> int:
        self.calls.append(kind)
        return 0


class StubRuntime:
    """Record Engine passes."""

    def __init__(self) -> None:
        self.effects = StubEffects()
        self.evaluated = 0

    def evaluate_due(self) -> None:
        self.evaluated += 1


@pytest.mark.asyncio
async def test_request_sets_wake_flag() -> None:
    runtime = StubRuntime()
    passes = Passes(runtime)
    assert passes._wake.is_set() is False
    passes.request()
    assert passes._wake.is_set() is True
    passes._wake.clear()


@pytest.mark.asyncio
async def test_run_once_effects_then_engine() -> None:
    runtime = StubRuntime()
    passes = Passes(runtime)
    await passes.run_once()
    assert runtime.effects.calls == ["notice", "reply"]
    assert runtime.evaluated == 1
    assert passes.count == 1
