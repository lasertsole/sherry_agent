"""The coding-capability tasks must be solvable and tamper-checked.

Each task is a throwaway repository whose tests fail until the intended fix is
applied. That property is what makes the suite's verdicts meaningful: if a task
does not pass with its own reference fix, the harness reports a HARNESS failure
before spending a single model call, so "the model did not fix it" can never be
confused with "the task was broken".

These tests are the fast, hermetic half of the suite — no LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.coding_capability import tasks
from evals.coding_capability.tasks import TASKS

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_every_task_passes_with_its_reference_fix(task: tasks.Task, tmp_path: Path):
    """The task is solvable: the intended patch turns its suite green."""
    repo = tmp_path / task.id
    tasks.build_repo(task, repo)
    for relative, content in task.reference_fix.items():
        (repo / relative).write_text(content, encoding="utf-8")

    exit_code, output = tasks.run_tests(repo)

    assert exit_code == 0, output


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_every_task_starts_red(task: tasks.Task, tmp_path: Path):
    """And it is a real task: the shipped skeleton fails before any fix."""
    repo = tmp_path / task.id
    tasks.build_repo(task, repo)

    exit_code, _output = tasks.run_tests(repo)

    assert exit_code != 0, f"{task.id} passes without any change"


def test_the_repo_is_one_base_commit_with_a_clean_tree(tmp_path: Path):
    task = TASKS[0]
    repo = tmp_path / "repo"

    base = tasks.build_repo(task, repo)

    assert tasks.commit_count(repo) == 1
    assert len(base) == 40
    assert tasks.git_diff(repo, base) == ("", "")


def test_tampering_is_detected_by_protected_paths():
    assert tasks.tampered_paths(["tests/test_x.py"]) == ["tests/test_x.py"]
    assert tasks.tampered_paths(["conftest.py"]) == ["conftest.py"]
    assert tasks.tampered_paths(["src/module.py", "tests/test_x.py"]) == ["tests/test_x.py"]
    assert tasks.tampered_paths(["module.py", "pkg/other.py"]) == []


def test_every_task_has_a_multi_file_or_meaningful_shape():
    """Sanity pins on the task SET itself (a task with no tests proves nothing)."""
    ids = {task.id for task in TASKS}
    assert len(ids) == len(TASKS), "task ids must be unique"
    for task in TASKS:
        test_files = [path for path in task.files if path.startswith("tests/")]
        assert test_files, f"{task.id} ships no tests"
        assert task.reference_fix, f"{task.id} has no reference fix"
        assert task.prompt.strip(), f"{task.id} has no prompt"
