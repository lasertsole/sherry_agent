"""The eval report must describe the run that happened, not a friendlier one.

Three measured distortions on the DeepSWE L1 run of 2026-11-25:

- ``changed_paths`` came from ``+++ b/`` lines, so the 11.5 MB rebuilt binary in
  the abs patch was invisible (the report said three Go files);
- ``resolve_rate`` used the valid-run denominator while nothing reported how
  many tasks never ran (0.5 for 4/12);
- a task aborted by the provider was listed as an orchestration VIOLATION.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.deepswe import suite

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_BINARY_PATCH = """diff --git a/abs b/abs
new file mode 100755
index 0000000000000000000000000000000000000000..c56ba063f2630c40dd7527cc0f5a9c135e858e82
GIT binary patch
literal 11526548
zcmeFadwdjCwl7|tq?6_mcOxJ{Kswku$sCacA$E(A4m7wLDi|bc6d2<x#wQWFA@a~
"""

_TEXT_PATCH = """diff --git a/evaluator/modules.go b/evaluator/modules.go
index 111..222 100644
--- a/evaluator/modules.go
+++ b/evaluator/modules.go
@@ -1,3 +1,3 @@
-old
+new
diff --git a/old-name.go b/new-name.go
similarity index 90%
rename from old-name.go
rename to new-name.go
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def _result(task_id: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": task_id,
        "resolved": False,
        "invalid_run": False,
        "orchestration_compliant": True,
        "experience_extraction": {},
    }
    base.update(fields)
    return base


def test_changed_paths_include_a_binary_file():
    """The abs patch: one 11.5 MB new binary + one text file."""
    paths = suite._changed_paths(_BINARY_PATCH + _TEXT_PATCH)

    assert "abs" in paths, "a GIT binary patch carries no +++/--- pair"
    assert "evaluator/modules.go" in paths


def test_changed_paths_skip_a_deletion_and_keep_the_new_rename_side():
    paths = suite._changed_paths(_TEXT_PATCH)

    assert "gone.py" not in paths, "a deleted path does not exist after the patch"
    assert "new-name.go" in paths
    assert "old-name.go" not in paths


def test_a_large_patch_section_is_surfaced():
    # The shipped fixture is small; the threshold is what matters here.
    large = suite._large_patch_files(_BINARY_PATCH + _TEXT_PATCH, limit=64)

    assert "abs" in [entry["path"] for entry in large]
    assert suite._PATCH_FILE_WARN_BYTES == 1_000_000
    assert suite._PATCH_TOTAL_WARN_BYTES == 5_000_000


def test_aggregate_reports_the_never_run_tasks_and_both_rates():
    results = [_result(f"task-{index}") for index in range(12)]
    for index in range(4):
        results[index]["resolved"] = True
    # The four provider aborts: invalid AND not run.
    for index in range(8, 12):
        results[index]["invalid_run"] = True
        results[index]["orchestration_compliant"] = None

    aggregate = suite._aggregate(results, started=0.0)

    assert aggregate["tasks_total"] == 12
    assert aggregate["attempted"] == 8
    assert aggregate["resolved"] == 4
    assert aggregate["not_run"] == [f"task-{index}" for index in range(8, 12)]
    assert aggregate["invalid_runs"] == [f"task-{index}" for index in range(8, 12)]
    assert aggregate["resolve_rate"] == 0.5
    assert aggregate["resolve_rate_all"] == 0.3333


def test_a_suite_level_abort_counts_as_not_run_too():
    results = [_result("ran"), _result("aborted", not_run=True)]

    aggregate = suite._aggregate(results, started=0.0)

    assert aggregate["attempted"] == 1
    assert aggregate["not_run"] == ["aborted"]


def test_an_unjudged_task_is_neither_compliant_nor_violating():
    results = [
        _result("planned"),
        _result("unplanned", orchestration_compliant=False),
        _result("aborted", invalid_run=True, orchestration_compliant=None),
    ]

    lists = suite._orchestration_task_lists(results)

    assert lists["compliant_tasks"] == ["planned"]
    assert lists["violating_tasks"] == ["unplanned"]
    assert lists["not_run_tasks"] == ["aborted"]


def test_a_forced_extraction_is_attempted_but_only_production_is_produced():
    results = [
        _result(
            "forced-no-output",
            experience_extraction={"enabled": True, "attempted": True, "fired": True},
        ),
        _result(
            "forced-produced",
            experience_extraction={
                "enabled": True,
                "attempted": True,
                "fired": True,
                "snapshot_hash": "abc123",
            },
        ),
        _result("aborted", experience_extraction={"enabled": True}),
    ]

    summary = suite._experience_extraction_summary(results)

    assert summary["enabled_tasks"] == ["forced-no-output", "forced-produced", "aborted"]
    assert summary["attempted_tasks"] == ["forced-no-output", "forced-produced"]
    assert summary["fired_tasks"] == ["forced-no-output", "forced-produced"]
    assert summary["produced_tasks"] == ["forced-produced"]


def test_the_compliance_label_is_explicit_about_unjudged_runs():
    assert suite._compliance_label(True) == "ok"
    assert suite._compliance_label(False) == "VIOLATED"
    assert suite._compliance_label(None) == "n/a"
