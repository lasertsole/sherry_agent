"""Hermetic tests for DeepSWE task discovery, validation, and patch capture."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.deepswe import instances

pytestmark = [pytest.mark.unit]

_TOML = """\
schema_version = "1.3"
[metadata]
task_id = "{task_id}"
repository_url = "https://example.test/repo"
base_commit_hash = "{base}"
language = "python"
[verifier]
timeout_sec = 60.0
network_mode = "no-network"
[verifier.environment]
build_timeout_sec = 120.0
[environment]
docker_image = "example/image:tag"
"""


def _make_task(root: Path, task_id: str = "demo-task", base: str = "abc123") -> Path:
    task = root / task_id
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "task.toml").write_text(_TOML.format(task_id=task_id, base=base), encoding="utf-8")
    (task / "instruction.md").write_text("Fix the demo bug.", encoding="utf-8")
    for name in ("Dockerfile", "test.sh", "grader.py", "config.json"):
        (task / "tests" / name).write_text("{}", encoding="utf-8")
    (task / "solution" / "solution.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    return task


def test_parse_and_validate_task(tmp_path: Path):
    _make_task(tmp_path)

    task = instances.parse_task(tmp_path / "demo-task")
    instances.validate(task)

    assert task.task_id == "demo-task"
    assert task.repo == "https://example.test/repo"
    assert task.base_commit == "abc123"
    assert task.language == "python"
    assert task.image == "example/image:tag"
    assert task.verifier_network == "no-network"
    assert task.verifier_timeout_s == 60.0
    assert task.build_timeout_s == 120.0
    assert task.instruction == "Fix the demo bug."
    assert task.gold_patch.startswith("diff --git")


def test_validate_refuses_missing_harness_inputs(tmp_path: Path):
    task_dir = _make_task(tmp_path)
    (task_dir / "tests" / "grader.py").unlink()

    task = instances.parse_task(task_dir)

    with pytest.raises(ValueError, match="grader.py"):
        instances.validate(task)


def test_load_and_select_preserves_order(tmp_path: Path):
    _make_task(tmp_path, "alpha")
    _make_task(tmp_path, "beta")

    tasks = instances.load_tasks(tmp_path)
    selected = instances.select(tasks, ("beta", "alpha"))

    assert [task.task_id for task in selected] == ["beta", "alpha"]


def test_select_rejects_unknown_task(tmp_path: Path):
    _make_task(tmp_path)
    tasks = instances.load_tasks(tmp_path)

    with pytest.raises(KeyError, match="unknown"):
        instances.select(tasks, ("nope",))


def test_agent_patch_covers_tracked_and_untracked(tmp_path: Path):
    from evals.deepswe.suite import _agent_patch

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "eval@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Eval"], cwd=repo, check=True)
    tracked = repo / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    tracked.write_text("value = 2\n", encoding="utf-8")
    (repo / "new_file.py").write_text("added = True\n", encoding="utf-8")

    patch = _agent_patch(repo, base)

    assert "tracked.py" in patch and "new_file.py" in patch
    assert "-value = 1" in patch and "+value = 2" in patch


def test_dry_run_cli_with_local_task_tree(tmp_path: Path):
    _make_task(tmp_path / "tasks")
    env = {
        **dict(os.environ),
        "DEEPSWE_TASKS_DIR": str(tmp_path),
        "SHERRY_EVAL_DEEPSWE_CHILD": "1",
    }
    repo = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "evals/deepswe/suite.py", "--dry-run", "demo-task"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "dry-run ok: demo-task" in completed.stdout


def test_runner_registers_both_suites():
    from evals.evals import SUITES

    assert SUITES["deepswe"] == "evals.deepswe.suite"
    assert SUITES["terminal_bench"] == "evals.terminal_bench.suite"
