"""Hermetic tests for Terminal-Bench task discovery and validation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.terminal_bench import tasks as task_data

pytestmark = [pytest.mark.unit]

_TASK_YAML = """\
instruction: |-
  Create a file.
parser_name: pytest
max_agent_timeout_sec: 120.0
max_test_timeout_sec: 30.0
difficulty: easy
"""

_COMPOSE = """\
services:
  client:
    build:
      dockerfile: Dockerfile
    command: [ "sh", "-c", "sleep infinity" ]
    environment:
      - TEST_DIR=/tests
"""


def _make_task(root: Path, task_id: str = "hello-demo", *, solution_yaml: bool = False) -> Path:
    task = root / task_id
    (task / "tests").mkdir(parents=True)
    (task / "task.yaml").write_text(_TASK_YAML, encoding="utf-8")
    (task / "Dockerfile").write_text("FROM python:3.13-slim\nWORKDIR /app\n", encoding="utf-8")
    (task / "docker-compose.yaml").write_text(_COMPOSE, encoding="utf-8")
    (task / "run-tests.sh").write_text("uv run pytest /tests\n", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    if solution_yaml:
        (task / "solution.yaml").write_text(
            "- command: echo solved\n  block: true\n", encoding="utf-8"
        )
    else:
        (task / "solution.sh").write_text("echo solved\n", encoding="utf-8")
    return task


def test_parse_and_validate_task(tmp_path: Path):
    _make_task(tmp_path)

    task = task_data.parse_task(tmp_path / "hello-demo")
    task_data.validate(task)

    assert task.task_id == "hello-demo"
    assert task.parser == "pytest"
    assert task.agent_timeout_s == 120.0
    assert task.test_timeout_s == 30.0
    assert task.supported
    assert task.solution_sh
    assert task.can_self_check


def test_solution_yaml_commands_are_parsed(tmp_path: Path):
    _make_task(tmp_path, solution_yaml=True)

    task = task_data.parse_task(tmp_path / "hello-demo")
    task_data.validate(task)

    assert not task.solution_sh
    assert task.solution_yaml_commands == (("echo solved", True),)


def test_multi_service_compose_is_unsupported(tmp_path: Path):
    task_dir = _make_task(tmp_path)
    (task_dir / "docker-compose.yaml").write_text(
        _COMPOSE + "  db:\n    image: postgres\n", encoding="utf-8"
    )

    task = task_data.parse_task(task_dir)

    assert not task.supported
    assert "db" in task.unsupported_reason
    with pytest.raises(ValueError, match="extra compose"):
        task_data.validate(task)


def test_task_bind_mount_is_unsupported(tmp_path: Path):
    task_dir = _make_task(tmp_path)
    (task_dir / "docker-compose.yaml").write_text(
        _COMPOSE + "    volumes:\n      - ./data:/app/data\n", encoding="utf-8"
    )

    task = task_data.parse_task(task_dir)

    assert not task.supported
    assert "volume" in task.unsupported_reason


def test_missing_tests_directory_is_refused(tmp_path: Path):
    task_dir = _make_task(tmp_path)
    (task_dir / "tests" / "test_outputs.py").unlink()
    (task_dir / "tests").rmdir()

    task = task_data.parse_task(task_dir)

    with pytest.raises(ValueError, match="tests"):
        task_data.validate(task)


def test_non_pytest_parser_is_refused(tmp_path: Path):
    task_dir = _make_task(tmp_path)
    (task_dir / "task.yaml").write_text(
        _TASK_YAML.replace("parser_name: pytest", "parser_name: shell"), encoding="utf-8"
    )

    task = task_data.parse_task(task_dir)

    with pytest.raises(ValueError, match="parser_name"):
        task_data.validate(task)


def test_load_and_select_preserves_order(tmp_path: Path):
    _make_task(tmp_path, "alpha")
    _make_task(tmp_path, "beta")

    tasks = task_data.load_tasks(tmp_path)
    selected = task_data.select(tasks, ("beta", "alpha"))

    assert [task.task_id for task in selected] == ["beta", "alpha"]


def test_select_rejects_unknown_task(tmp_path: Path):
    _make_task(tmp_path)
    tasks = task_data.load_tasks(tmp_path)

    with pytest.raises(KeyError, match="unknown"):
        task_data.select(tasks, ("nope",))


def test_dry_run_cli_with_local_task_tree(tmp_path: Path):
    _make_task(tmp_path / "original-tasks")
    env = {
        **dict(os.environ),
        "TERMINAL_BENCH_DIR": str(tmp_path),
        "SHERRY_EVAL_TBENCH_CHILD": "1",
    }
    repo = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "evals/terminal_bench/suite.py", "--dry-run", "hello-demo"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "dry-run ok: hello-demo" in completed.stdout
