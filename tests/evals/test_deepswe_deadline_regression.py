"""Regression: one 3600 s budget, not four.

Measured on the DeepSWE L1 run of 2026-11-25: arktype took 9434 s with
``continuations=3`` because ``--timeout`` was re-handed to every ``wait_for``.
The correct shape: turns keep starting while the budget lasts, the turn that
does not fit never starts, and the drive reports the deadline instead of
raising.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import pytest

from evals.deepswe import suite
from evals.devbench import suite as devbench_suite

pytestmark = [pytest.mark.regression, pytest.mark.timeout(60)]


class _Clock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _open_plan(root) -> None:
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


class _Harness:
    def __init__(self, clock: _Clock, turn_cost: float) -> None:
        self.clock = clock
        self.turn_cost = turn_cost
        self.prompts: list[str] = []

    async def _drive(self, session_id: str, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.clock.advance(self.turn_cost)
        return {"tool_calls": 1, "tool_names": {"todowrite": 1}, "text": "ok"}


@pytest.mark.asyncio
async def test_four_turns_cannot_outrun_one_budget(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(suite, "time", clock)
    _open_plan(tmp_path)
    harness = _Harness(clock, turn_cost=1200.0)

    drive = await devbench_suite._drive_with_continuations(
        "sess-a", "initial prompt", harness, 3600.0, tmp_path
    )

    assert drive["tool_calls"] == 3, "the 4th turn would start past the budget"
    assert drive["deadline_hit"] is True
    assert clock.now - 5000.0 == 3600.0, "exactly one budget was spent"


@pytest.mark.asyncio
async def test_an_overrunning_turn_is_a_timeout_not_a_harness_error(tmp_path):
    """The old devbench path let TimeoutError escape into HARNESS ERROR."""

    class _SlowHarness:
        async def _drive(self, session_id: str, prompt: str) -> dict[str, Any]:
            await _sleep(3.0)
            return {"tool_calls": 1, "tool_names": {}, "text": ""}

    started = time.monotonic()
    drive = await devbench_suite._drive_with_continuations(
        "sess-a", "p", _SlowHarness(), 1.2, tmp_path
    )

    assert drive["timeout"] is True
    assert drive["deadline_hit"] is True
    assert drive["tool_calls"] == 0
    assert time.monotonic() - started < 2.5


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
