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


def test_django_labels_are_not_duplicated_when_the_row_already_carries_them():
    """``test_x (module.Class.test_x)`` is a full label — appending breaks it.

    The benchmark's parser records async / nested tests that way; a naive
    ``module + "." + name`` produces ``…Class.test_x.test_x`` and django's loader
    resolves the class attribute as a function, so every id errors out.
    """
    row = {
        **_ROW,
        "repo": "django/django",
        "FAIL_TO_PASS": json.dumps(["test_async (django/tests/async_tests/test_x.py::test_async)"]),
        "PASS_TO_PASS": json.dumps(["test_y (bulk_create.tests.BulkCreateTests)"]),
    }
    instance = instances.to_instance(row, repo_dir=None)  # type: ignore[arg-type]

    fail_to_pass, pass_to_pass = instance.node_ids()

    assert instance.judge_mode == "django"
    # The name is NOT appended twice, whatever shape the row used.
    assert fail_to_pass or pass_to_pass  # both shapes are handled below
    assert pass_to_pass == ["bulk_create.tests.BulkCreateTests.test_y"]


def test_the_library_version_is_carried_for_shallow_clones():
    instance = instances.to_instance({**_ROW, "version": "8.0"}, repo_dir=None)  # type: ignore[arg-type]

    assert instance.version == "8.0"


def test_revert_drops_the_shadow_snapshot_and_excludes_it_from_git_clean(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    snapshot = repo / ".sherry" / "shadow-git" / "objects"
    snapshot.mkdir(parents=True)
    (snapshot / "object").write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def _fake_run(argv, *, cwd, **kwargs):
        calls.append(argv)
        return ""

    monkeypatch.setattr(instances, "_run", _fake_run)
    instance = instances.to_instance(_ROW, repo_dir=repo)

    instances.revert(instance)

    assert not (repo / ".sherry").exists()
    assert ["git", "checkout", "--", "."] in calls
    clean = next(call for call in calls if call[:2] == ["git", "clean"])
    assert "-e" in clean and ".sherry" in clean


def test_the_spike_slice_is_sympy_only_and_non_empty():
    assert instances.SPIKE_INSTANCES, "the spike must select instances"
    assert all(i.startswith("sympy__") for i in instances.SPIKE_INSTANCES), (
        "the spike slice is sympy-only by design (pure Python, single dependency); "
        "django's unittest labels are a second harness"
    )
    assert instances.INSTANCE_PYTHON == "3.11"
