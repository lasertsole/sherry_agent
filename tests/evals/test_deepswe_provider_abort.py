"""The harness must stop spending when the provider is terminally down.

Measured on the DeepSWE L1 run of 2026-11-25: the balance was exhausted
(``insufficient_balance``) and four more tasks each ran the whole
extract → container → bind → verifier pipeline before being recorded as failed
work. A terminal provider error is not retryable, while a 429 or a timeout is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.deepswe import suite

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _result(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "task",
        "invalid_run": False,
        "error": "",
        "resolved": False,
        "orchestration_compliant": True,
    }
    base.update(fields)
    return base


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


def test_two_consecutive_terminal_errors_trip_the_abort():
    abort = suite._ProviderAbort()

    abort.observe(
        _result(invalid_run=True, error="APIStatusError: Error code: 402 - insufficient_balance")
    )
    assert abort.tripped is False
    abort.observe(_result(invalid_run=True, error="Error code: 401 - invalid_api_key"))

    assert abort.tripped is True
    assert abort.count == 2


def test_a_recovered_run_resets_the_streak():
    abort = suite._ProviderAbort()
    abort.observe(_result(invalid_run=True, error="error code: 402 insufficient balance"))

    abort.observe(_result(resolved=True))  # the next task worked

    assert abort.count == 0
    assert abort.tripped is False


def test_rate_limits_and_timeouts_are_not_terminal():
    abort = suite._ProviderAbort()

    for error in (
        "RateLimitError: 429 too many requests",
        "ReadTimeout: request timed out",
        "APIConnectionError: connection reset",
        "Stream stalled",
    ):
        abort.observe(_result(invalid_run=True, error=error))

    assert abort.count == 0, "transient provider errors must keep the suite running"
    assert abort.tripped is False


def test_a_terminal_error_without_invalid_run_is_not_counted():
    """A resolved task whose skill review hit a 402 is still a measured task."""
    abort = suite._ProviderAbort()
    abort.observe(_result(invalid_run=False, resolved=True, error="error code: 402"))

    assert abort.count == 0


def test_the_not_run_row_has_the_full_report_shape():
    row = suite._not_run_result(_task("koota"), experience_extraction=True, gold_checked=True)

    assert row["id"] == "koota"
    assert row["not_run"] is True
    assert row["invalid_run"] is True
    assert row["failure_kind"] == "provider"
    assert row["orchestration_compliant"] is None
    assert row["changed_paths"] == []
    assert row["experience_extraction"]["enabled"] is True
    assert row["experience_extraction"]["attempted"] is False
    assert row["gold_self_check"] == "checked"
    # The aggregate still needs to agree with the row.
    aggregate = suite._aggregate([row], started=0.0)
    assert aggregate["attempted"] == 0
    assert aggregate["not_run"] == ["koota"]
    assert aggregate["resolve_rate_all"] == 0.0
