"""Regression: the eval continuation driver is SESSION-scoped.

Measured on the DeepSWE L1 run of 2026-11-25: task #2 (bandit) ended with
five open todos in the run-wide plan registry, and from then on every later
task's ``open_items`` was permanently non-empty — ``if not open_items: break``
could never fire, so a task that had closed its own plan still received
continuation turns (and a flow-less task could be told to wait for another
task's step children). These tests pin the scoping at both places that broke:
the registry reader and the devbench driver loop.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from evals.deepswe import suite
from evals.devbench import suite as devbench_suite

pytestmark = [pytest.mark.regression, pytest.mark.timeout(30)]


def _seed_registry(root: Path) -> None:
    """Two tasks in ONE run registry: ``sess-prev`` left its plan open."""
    stores = root / "sandbox" / "stores"
    stores.mkdir(parents=True)
    with sqlite3.connect(stores / "registry.db") as connection:
        connection.execute(
            "CREATE TABLE todos (session_id TEXT, todo_id INTEGER NOT NULL, "
            "content TEXT, status TEXT, PRIMARY KEY (session_id, todo_id))"
        )
        connection.execute(
            "CREATE TABLE task_flows (session_id TEXT, flow_id TEXT PRIMARY KEY, "
            "status TEXT, state_json TEXT)"
        )
        # The earlier task: still open, five items (the measured shape).
        for index, text in enumerate(
            (
                "prev: inspect the repo",
                "prev: implement the fix",
                "prev: run the tests",
                "prev: commit",
                "prev: acceptance review",
            ),
            start=1,
        ):
            connection.execute(
                "INSERT INTO todos VALUES ('sess-prev', ?, ?, 'pending')", (index, text)
            )
        connection.execute(
            "INSERT INTO task_flows VALUES ('sess-prev', 'prev-flow', 'running', ?)",
            ('{"steps": [{"step_id": "s1", "status": "dispatched"}]}',),
        )
        # The current task: finished everything.
        connection.execute("INSERT INTO todos VALUES ('sess-now', 1, 'now: the work', 'completed')")
        connection.execute(
            "INSERT INTO task_flows VALUES ('sess-now', 'now-flow', 'done', ?)",
            ('{"steps": [{"step_id": "s1", "status": "done"}]}',),
        )
        connection.commit()


def test_a_closed_plan_is_not_dragged_open_by_another_tasks_todos(tmp_path: Path):
    _seed_registry(tmp_path)

    assert suite._open_plan_items(tmp_path, "sess-now") == []
    assert suite._flow_in_flight(tmp_path, "sess-now") is False

    previous = suite._open_plan_items(tmp_path, "sess-prev")
    assert len(previous) == 6  # 5 todos + 1 flow
    assert suite._flow_in_flight(tmp_path, "sess-prev") is True


class _FakeHarness:
    """Records every driving prompt; one cheap turn per call."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def _drive(self, session_id: str, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"tool_calls": 1, "tool_names": {"todowrite": 1}, "text": "done"}


@pytest.mark.asyncio
async def test_the_driver_stops_once_this_tasks_plan_is_closed(tmp_path: Path):
    """The measured stall: a finished task kept being driven by a foreign plan."""
    _seed_registry(tmp_path)
    harness = _FakeHarness()

    drive = await devbench_suite._drive_with_continuations(
        "sess-now", "initial prompt", harness, 30.0, tmp_path
    )

    assert drive["tool_calls"] == 1
    assert len(harness.prompts) == 1, "a closed plan must not earn a continuation turn"


@pytest.mark.asyncio
async def test_the_continuation_prompt_names_only_this_tasks_items(tmp_path: Path):
    _seed_registry(tmp_path)
    # Make the CURRENT task's own plan open, with one item not present above.
    with sqlite3.connect(tmp_path / "sandbox" / "stores" / "registry.db") as connection:
        connection.execute("UPDATE todos SET status='pending' WHERE session_id='sess-now'")
        connection.execute(
            "UPDATE todos SET content='now: the leftover item' WHERE session_id='sess-now'"
        )
        connection.commit()
    harness = _FakeHarness()

    drive = await devbench_suite._drive_with_continuations(
        "sess-now", "initial prompt", harness, 30.0, tmp_path
    )

    # The fake never closes the plan, so the driver spends its whole budget.
    assert drive["tool_calls"] == 1 + devbench_suite._MAX_PLAN_CONTINUATIONS
    continuation = harness.prompts[1]
    assert "now: the leftover item" in continuation
    assert "prev:" not in continuation, "another task's items must never be listed"
    for prompt in harness.prompts[1:]:
        assert "prev:" not in prompt
