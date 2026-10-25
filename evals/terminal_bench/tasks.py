"""Terminal-Bench task discovery and validation.

Terminal-Bench's tasks live under ``original-tasks/<id>/``:

- ``task.yaml`` — instruction, pytest parser, agent/test timeouts, difficulty;
- ``Dockerfile`` + ``docker-compose.yaml`` — the task environment;
- ``run-tests.sh`` + ``tests/`` — the verifier, run from ``/tests`` with
  ``TEST_DIR=/tests``;
- ``solution.sh`` / ``solution.yaml`` — the oracle, used only for the harness's
  optional self-check.

A task whose compose file defines extra services needs the official compose
orchestrator; those tasks are marked unsupported here instead of being run with
a half-built environment.

The task tree is cloned at the pinned commit into ``~/.cache/sherry_evals`` (or
a checkout named by ``TERMINAL_BENCH_DIR``); ``--dry-run`` validates selections
without Docker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evals import external_bench

TERMINAL_BENCH_REPO = "https://github.com/laude-institute/terminal-bench.git"
#: The repository's HEAD when this suite was written (Terminal-Bench 2.0 era).
TERMINAL_BENCH_PIN = "d28711d0da2675d0bb1d56de45ae5df6082438a3"

#: Two small, single-service tasks: they exercise the full build → agent →
#: pytest path without a multi-minute environment.
DEFAULT_SLICE: tuple[str, ...] = ("hello-world", "fix-permissions")


@dataclass(frozen=True)
class Task:
    """One Terminal-Bench task's validated metadata."""

    task_id: str
    task_dir: Path
    instruction: str
    agent_timeout_s: float
    test_timeout_s: float
    parser: str
    supported: bool
    unsupported_reason: str
    solution_sh: bool
    solution_yaml_commands: tuple[tuple[str, bool], ...]

    @property
    def can_self_check(self) -> bool:
        """Whether an oracle script exists for the harness self-check."""
        return self.solution_sh or bool(self.solution_yaml_commands)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compose_support(task_dir: Path) -> tuple[bool, str]:
    """Whether the task fits the single-client-container runner this suite has."""
    compose = task_dir / "docker-compose.yaml"
    if not compose.is_file():
        return True, ""
    try:
        data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return False, f"docker-compose.yaml is unreadable: {exc}"
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return True, ""
    extras = sorted(set(services) - {"client"})
    if extras:
        return False, (
            "the task needs extra compose service(s) "
            f"{extras}; this suite runs the single client container only"
        )

    client = services.get("client")
    if not isinstance(client, dict):
        return True, ""
    environment = client.get("environment") or []
    if isinstance(environment, dict):
        env_keys = {str(key) for key in environment}
    else:
        env_keys = {
            str(entry).split("=", 1)[0]
            for entry in environment
            if isinstance(entry, str) and "=" in entry
        }
    extra_env = sorted(env_keys - {"TEST_DIR"})
    if extra_env:
        return False, f"the task needs runtime environment {extra_env}"

    if client.get("extra_hosts"):
        return False, "the task needs extra /etc/hosts entries"
    if client.get("dns") or client.get("cap_add") or client.get("privileged"):
        return False, "the task needs container network/kernel options this runner omits"
    for volume in client.get("volumes") or []:
        if not isinstance(volume, str):
            return False, f"unsupported compose volume entry: {volume!r}"
        source = volume.split(":", 1)[0]
        # The harness's own log mounts carry no task state; anything else (a
        # bind mount, a named volume) changes the task's filesystem and needs
        # the official compose orchestrator.
        if "${T_BENCH_" not in source:
            return False, f"the task needs the compose volume {volume}"
    build_args = (client.get("build") or {}).get("args")
    if build_args:
        return False, "the task needs Docker build arguments"
    return True, ""


def _solution_yaml_commands(task_dir: Path) -> tuple[tuple[str, bool], ...]:
    path = task_dir / "solution.yaml"
    if not path.is_file():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return ()
    if not isinstance(data, list):
        return ()
    commands: list[tuple[str, bool]] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("command"), str):
            commands.append((entry["command"], bool(entry.get("block", True))))
    return tuple(commands)


def parse_task(task_dir: Path) -> Task:
    """Parse one task directory; raise ``ValueError`` on a malformed task."""
    config_path = task_dir / "task.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{task_dir.name}: cannot read task.yaml ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{task_dir.name}: task.yaml is not a mapping")

    instruction = str(data.get("instruction") or "").strip()
    parser = str(data.get("parser_name") or "pytest")
    supported, reason = _compose_support(task_dir)
    solution_sh = (task_dir / "solution.sh").is_file()
    solution_yaml_commands = _solution_yaml_commands(task_dir)
    if supported:
        missing = [
            name
            for name in ("Dockerfile", "run-tests.sh", "tests")
            if not (task_dir / name).exists()
        ]
        if missing:
            supported, reason = False, f"missing {', '.join(missing)}"
    if supported and parser != "pytest":
        supported, reason = False, f"parser_name={parser!r} (only pytest is judged)"
    if supported and not (solution_sh or solution_yaml_commands):
        supported, reason = False, "no solution.sh/solution.yaml for the self-check"
    return Task(
        task_id=task_dir.name,
        task_dir=task_dir,
        instruction=instruction,
        agent_timeout_s=_as_float(data.get("max_agent_timeout_sec"), 900.0),
        test_timeout_s=_as_float(data.get("max_test_timeout_sec"), 300.0),
        parser=parser,
        supported=supported,
        unsupported_reason=reason,
        solution_sh=solution_sh,
        solution_yaml_commands=solution_yaml_commands,
    )


def validate(task: Task) -> None:
    """Refuse a task whose harness inputs are not all present."""
    if not task.supported:
        raise ValueError(f"{task.task_id}: {task.unsupported_reason}")
    missing = [
        name
        for name in ("Dockerfile", "run-tests.sh", "tests")
        if not (task.task_dir / name).exists()
    ]
    if missing:
        raise ValueError(f"{task.task_id}: missing {', '.join(missing)}")
    if not task.instruction:
        raise ValueError(f"{task.task_id}: task.yaml has no instruction")
    if task.parser != "pytest":
        raise ValueError(
            f"{task.task_id}: parser_name={task.parser!r}; this suite judges pytest output"
        )
    if not task.can_self_check:
        raise ValueError(f"{task.task_id}: no solution.sh/solution.yaml for the self-check")


def load_tasks(root: Path) -> dict[str, Task]:
    """Load every task directory under *root*."""
    tasks: dict[str, Task] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not (entry / "task.yaml").is_file():
            continue
        task = parse_task(entry)
        tasks[task.task_id] = task
    if not tasks:
        raise ValueError(f"no Terminal-Bench tasks found under {root}")
    return tasks


def select(tasks: dict[str, Task], wanted: tuple[str, ...]) -> list[Task]:
    """Select tasks by id, preserving the caller's order."""
    unknown = [task_id for task_id in wanted if task_id not in tasks]
    if unknown:
        raise KeyError(
            f"unknown Terminal-Bench task(s): {unknown}; available: {sorted(tasks)[:10]} …"
        )
    selected = [tasks[task_id] for task_id in wanted]
    for task in selected:
        validate(task)
    return selected


def fetch_tasks() -> tuple[Path, str]:
    """Resolve the task tree (cloned into the eval cache unless overridden)."""
    pin = os.environ.get("TERMINAL_BENCH_REF", "").strip() or TERMINAL_BENCH_PIN
    return external_bench.local_or_cloned(
        "TERMINAL_BENCH_DIR",
        external_bench.CACHE_ROOT / "terminal-bench",
        TERMINAL_BENCH_REPO,
        pin,
        label="Terminal-Bench",
        subdir="original-tasks",
    )
