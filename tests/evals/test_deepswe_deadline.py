"""`--timeout` is the budget of the WHOLE drive, not of every turn.

Measured on the DeepSWE L1 run of 2026-11-25: the flag (3600 s by default) was
handed to each of the up-to-4 ``asyncio.wait_for`` calls, so arktype ran 9434 s
with ``continuations=3`` and no signal in the report. Both suites now run every
driven turn through ``_next_drive_turn`` on ONE deadline.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from evals.deepswe import suite
from evals.devbench import suite as devbench_suite

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeClock:
    """A monotonic clock the fake drive advances (no real sleeping)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeHarness:
    """One cheap turn per call; advances the fake clock by `turn_cost`."""

    def __init__(self, clock: _FakeClock, turn_cost: float) -> None:
        self.clock = clock
        self.turn_cost = turn_cost
        self.prompts: list[str] = []

    async def _drive(self, session_id: str, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.clock.advance(self.turn_cost)
        return {"tool_calls": 1, "tool_names": {"todowrite": 1}, "text": "ok"}


def _open_plan(root) -> None:
    import sqlite3

    stores = root / "sandbox" / "stores"
    stores.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(stores / "registry.db") as connection:
        connection.execute(
            "CREATE TABLE todos (session_id TEXT, todo_id INTEGER NOT NULL, "
            "content TEXT, status TEXT, PRIMARY KEY (session_id, todo_id))"
        )
        connection.execute(
            "CREATE TABLE task_flows (session_id TEXT, flow_id TEXT PRIMARY KEY, status TEXT)"
        )
        connection.execute("INSERT INTO todos VALUES ('sess-a', 1, 'open item', 'pending')")
        connection.commit()


@pytest.mark.asyncio
async def test_a_spent_budget_stops_before_another_turn(monkeypatch, tmp_path):
    clock = _FakeClock()
    monkeypatch.setattr(suite, "time", clock)  # only the suite's clock reads
    _open_plan(tmp_path)
    harness = _FakeHarness(clock, turn_cost=9.5)

    drive = await devbench_suite._drive_with_continuations(
        "sess-a", "initial prompt", harness, 10.0, tmp_path
    )

    assert drive["tool_calls"] == 1, "only the first turn fit in the budget"
    assert drive["deadline_hit"] is True
    assert drive["timeout"] is True
    assert clock.now - 1000.0 <= 10.0


@pytest.mark.asyncio
async def test_a_closed_plan_finishes_without_touching_the_deadline(monkeypatch, tmp_path):
    clock = _FakeClock()
    monkeypatch.setattr(suite, "time", clock)
    harness = _FakeHarness(clock, turn_cost=0.5)

    drive = await devbench_suite._drive_with_continuations(
        "sess-a", "initial prompt", harness, 30.0, tmp_path
    )

    assert drive["tool_calls"] == 1
    assert drive["deadline_hit"] is False
    assert drive["timeout"] is False


@pytest.mark.asyncio
async def test_a_turn_that_overruns_the_deadline_is_cancelled():
    """The real clock case: wait_for gets only the remaining budget."""
    clock = _FakeClock()

    class _SlowHarness:
        async def _drive(self, session_id: str, prompt: str) -> dict[str, Any]:
            await _sleep(3.0)
            return {"tool_calls": 1, "tool_names": {}, "text": ""}

    deadline = time.monotonic() + 1.2  # above the 1 s floor, short enough to test
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await suite._next_drive_turn(_SlowHarness(), "sess-a", "p", deadline)

    assert time.monotonic() - started < 2.5, "the turn was cancelled at the deadline"
    assert clock.now == 1000.0  # the fake clock was never involved


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
