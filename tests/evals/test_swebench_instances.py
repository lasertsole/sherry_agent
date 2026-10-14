"""SWE-bench instance plumbing: parsing and selection, no network, no clone.

The suite's verdicts are only as good as this layer: the dataset's test names
must land on the right pytest node ids (sympy's runner prints bare function
names, so the file comes from the test patch), and an instance that is missing
from the dataset must fail loudly rather than silently shrink the slice.
"""

from __future__ import annotations

import json

import pytest

from evals.swebench import instances

pytestmark = [pytest.mark.unit]

_TEST_PATCH = """diff --git a/sympy/physics/units/tests/test_prefixes.py b/sympy/physics/units/tests/test_prefixes.py
--- a/sympy/physics/units/tests/test_prefixes.py
+++ b/sympy/physics/units/tests/test_prefixes.py
@@ -1,3 +1,4 @@
+def test_prefix_operations():
+    assert True
"""

_ROW = {
    "instance_id": "sympy__sympy-0001",
    "repo": "sympy/sympy",
    "base_commit": "0" * 40,
    "problem_statement": "a problem",
    "test_patch": _TEST_PATCH,
    "patch": "gold",
    "FAIL_TO_PASS": json.dumps(["test_prefix_operations"]),
    "PASS_TO_PASS": json.dumps(["test_bases", "other/test_file.py::test_kept"]),
}


def test_test_files_come_from_the_test_patch():
    assert instances.test_files_of(_TEST_PATCH) == ["sympy/physics/units/tests/test_prefixes.py"]
    assert instances.test_files_of("") == []


def test_bare_names_become_node_ids_in_the_patched_file():
    instance = instances.to_instance(_ROW, repo_dir=None)  # type: ignore[arg-type]

    fail_to_pass, pass_to_pass = instance.node_ids()

    assert fail_to_pass == ["sympy/physics/units/tests/test_prefixes.py::test_prefix_operations"]
    # A name that already carries a node id is left alone.
    assert pass_to_pass == [
        "sympy/physics/units/tests/test_prefixes.py::test_bases",
        "other/test_file.py::test_kept",
    ]


def test_lists_are_accepted_as_json_strings_or_lists():
    row = {**_ROW, "FAIL_TO_PASS": ["a_name"], "PASS_TO_PASS": []}
    instance = instances.to_instance(row, repo_dir=None)  # type: ignore[arg-type]

    assert instance.fail_to_pass == ["a_name"]
    assert instance.pass_to_pass == []


def test_selection_is_ordered_and_loud_about_a_missing_instance():
    rows = [_ROW, {**_ROW, "instance_id": "sympy__sympy-0002"}]

    picked = instances.select(rows, ("sympy__sympy-0002", "sympy__sympy-0001"))
    assert [i.instance_id for i in picked] == ["sympy__sympy-0002", "sympy__sympy-0001"]

    with pytest.raises(SystemExit, match="not in SWE-bench_Lite"):
        instances.select(rows, ("nope__nope-1",))


def test_the_spike_slice_is_sympy_only_and_non_empty():
    assert instances.SPIKE_INSTANCES, "the spike must select instances"
    assert all(i.startswith("sympy__") for i in instances.SPIKE_INSTANCES), (
        "the spike slice is sympy-only by design (pure Python, single dependency); "
        "django's unittest labels are a second harness"
    )
    assert instances.INSTANCE_PYTHON == "3.11"
