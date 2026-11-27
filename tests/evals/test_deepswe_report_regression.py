"""Regression: the report of the measured L1 run of 2026-11-25.

That run selected 12 tasks, four of them died on a provider 402 before any tool
call, four resolved, and one patch carried a rebuilt 11.5 MB binary. The report
said ``resolve_rate: 0.5`` (no "not run" count), listed the four aborted tasks as
orchestration violations, and omitted the binary from ``changed_paths``. These
tests pin the corrected shape of exactly that report.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.deepswe import suite

pytestmark = [pytest.mark.regression, pytest.mark.timeout(30)]

_RESOLVED = (
    "actionlint-action-pinning-lint",
    "aiomonitor-task-snapshots-diff",
    "koota-entity-snapshot-rollback",
    "returns-validated-error-accumulation",
)
_ABORTED = (
    "langchain-request-coalescing",
    "happy-dom-abort-pending-body-reads",
    "csstree-shorthand-expansion-compression",
    "pest-character-class-coalescing",
)


def _measured_results() -> list[dict[str, Any]]:
    resolved = {0: _RESOLVED[0], 3: _RESOLVED[1], 6: _RESOLVED[2], 7: _RESOLVED[3]}
    results: list[dict[str, Any]] = []
    for index in range(8):
        results.append(
            {
                "id": resolved.get(index, f"failed-task-{index}"),
                "resolved": index in resolved,
                "invalid_run": False,
                # bandit (index 1) ran 47 tool calls with taskflow_calls=0.
                "orchestration_compliant": index != 1,
            }
        )
    for task_id in _ABORTED:
        results.append(
            {
                "id": task_id,
                "resolved": False,
                "invalid_run": True,
                "orchestration_compliant": None,
            }
        )
    return results


def test_the_measured_run_reports_both_rates_and_its_unrun_tasks():
    results = _measured_results()

    aggregate = suite._aggregate(results, started=0.0)

    assert aggregate["tasks_total"] == 12
    assert aggregate["attempted"] == 8
    assert aggregate["resolved"] == 4
    assert aggregate["resolve_rate"] == 0.5, "valid-run denominator is unchanged"
    assert aggregate["resolve_rate_all"] == 0.3333, "the honest 4/12"
    assert aggregate["not_run"] == list(_ABORTED)


def test_the_aborted_tasks_are_not_orchestration_violations():
    lists = suite._orchestration_task_lists(_measured_results())

    assert lists["not_run_tasks"] == list(_ABORTED)
    assert all(task_id not in lists["violating_tasks"] for task_id in _ABORTED)
    assert "failed-task-1" in lists["violating_tasks"], "the one real violation stays reported"


def test_the_abs_patch_binary_is_reported_and_flagged():
    """8.3 MB patch: three Go files plus a rebuilt 11.5 MB binary."""
    padding = "z" + "A" * 78 + "\n"
    binary = (
        "diff --git a/abs b/abs\n"
        "new file mode 100755\n"
        "index 0000000..c56ba06\n"
        "GIT binary patch\n"
        "literal 11526548\n" + padding * 14_000
    )
    text = "diff --git a/evaluator/modules.go b/evaluator/modules.go\n" + padding * 10
    patch = binary + text

    assert suite._changed_paths(patch) == ["abs", "evaluator/modules.go"]
    flagged = suite._large_patch_files(patch)
    assert [entry["path"] for entry in flagged] == ["abs"]
    assert flagged[0]["bytes"] > suite._PATCH_FILE_WARN_BYTES
