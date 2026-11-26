"""Regression: a terminal provider error stops the suite after two tasks.

Measured on the DeepSWE L1 run 20261125_222727: the balance was gone at task 9
and tasks 10–12 still ran the full pipeline, each recorded as failed work. The
correct sequence stops before starting the next container and reports the rest
as NOT RUN — which the aggregate must then reflect (attempted, not_run, and both
rates).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.deepswe import suite

pytestmark = [pytest.mark.regression, pytest.mark.timeout(30)]

_BALANCE = "Error code: 402 - insufficient_balance_error"


def _ok(task_id: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "resolved": True,
        "invalid_run": False,
        "orchestration_compliant": True,
        "error": "",
        "experience_extraction": {"enabled": True, "attempted": True},
    }


def _terminal(task_id: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "resolved": False,
        "invalid_run": True,
        "orchestration_compliant": None,
        "error": _BALANCE,
        "experience_extraction": {"enabled": True},
    }


def _task(task_id: str) -> suite.Task:
    return suite.Task(
        task_id=task_id,
        task_dir=Path("/nonexistent"),
        repo="owner/repo",
        base_commit="0" * 40,
        language="python",
        image="image:tag",
        verifier_timeout_s=60.0,
        verifier_network="none",
        build_timeout_s=60.0,
        instruction="do the thing",
    )


def test_the_suite_stops_before_the_third_terminal_task():
    abort = suite._ProviderAbort()
    results = [_ok(f"ok-{index}") for index in range(9)]

    # Tasks 10 and 11 die on the dead provider; each observation is what the
    # run loop does right after appending the task's row.
    results.append(_terminal("dead-9"))
    abort.observe(results[-1])
    assert abort.tripped is False, "one terminal failure may still be a blip"

    results.append(_terminal("dead-10"))
    abort.observe(results[-1])
    assert abort.tripped is True

    # Task 12 is never started: it gets the not-run row instead.
    results.append(
        suite._not_run_result(_task("dead-11"), experience_extraction=True, gold_checked=True)
    )

    aggregate = suite._aggregate(results, started=0.0)
    assert aggregate["tasks_total"] == 12
    assert aggregate["attempted"] == 9
    assert aggregate["resolved"] == 9
    assert aggregate["not_run"] == ["dead-9", "dead-10", "dead-11"]
    assert aggregate["invalid_runs"] == ["dead-9", "dead-10", "dead-11"]
    assert aggregate["resolve_rate"] == 1.0, "valid runs all resolved"
    assert aggregate["resolve_rate_all"] == 0.75, "9/12 including the unrun task"


def test_the_aborted_task_is_not_a_violation_and_not_a_compliance_pass():
    results = [
        _ok("done"),
        _terminal("dead-9"),
        suite._not_run_result(_task("dead-10"), experience_extraction=False, gold_checked=False),
    ]

    lists = suite._orchestration_task_lists(results)

    assert lists["compliant_tasks"] == ["done"]
    assert lists["violating_tasks"] == []
    assert lists["not_run_tasks"] == ["dead-9", "dead-10"]
    assert suite._compliance_label(None) == "n/a"
