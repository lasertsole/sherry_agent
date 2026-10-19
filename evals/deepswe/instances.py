"""DeepSWE task discovery and validation.

DeepSWE (Datacurve) ships 113 tasks in the Harbor task format. Each task is a
directory with:

- ``task.toml`` — repo, pinned base commit, language, the prebuilt task image,
  the verifier's timeout and network mode;
- ``instruction.md`` — the prompt the agent sees;
- ``environment/Dockerfile`` — how the task image was built;
- ``tests/Dockerfile`` + ``tests/test.sh`` + ``tests/grader.py`` + ``tests/config.json``
  — the held-out verifier, built as a separate image from the task image;
- ``solution/solution.patch`` — the reference fix, used ONLY for the harness's
  gold self-check (never shown to the agent).

The task data is NOT vendored into this repository: it is cloned at the pinned
commit into ``~/.cache/sherry_evals`` (or a checkout named by
``DEEPSWE_TASKS_DIR``). ``--dry-run`` exercises this module without Docker.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import external_bench

#: DeepSWE v1.1 task tree (the repository's HEAD when this suite was written).
#: Pinning is what makes a later run comparable; override with
#: ``DEEPSWE_REF=<commit>`` if a newer dataset is intentional.
DEEPSWE_REPO = "https://github.com/datacurve-ai/deep-swe.git"
DEEPSWE_PIN = "0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea"

#: Two pure-Python tasks: the default slice stays small enough to finish, and a
#: host-side language toolchain is not needed (commands run in the image).
DEFAULT_SLICE: tuple[str, ...] = (
    "httpx-streaming-json-iteration",
    "cattrs-partial-structuring-recovery",
)

_REQUIRED_FILES = (
    "instruction.md",
    "tests/Dockerfile",
    "tests/test.sh",
    "tests/grader.py",
    "tests/config.json",
    "solution/solution.patch",
)


@dataclass(frozen=True)
class Task:
    """One DeepSWE task's validated metadata."""

    task_id: str
    task_dir: Path
    repo: str
    base_commit: str
    language: str
    image: str
    verifier_timeout_s: float
    verifier_network: str
    build_timeout_s: float
    instruction: str

    @property
    def gold_patch(self) -> str:
        """The reference fix, used only by the harness's gold self-check."""
        return (self.task_dir / "solution" / "solution.patch").read_text(encoding="utf-8")


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def parse_task(task_dir: Path) -> Task:
    """Parse one task directory; raise ``ValueError`` on a malformed task."""
    try:
        with (task_dir / "task.toml").open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{task_dir.name}: cannot read task.toml ({exc})") from exc

    metadata = _table(data, "metadata")
    environment = _table(data, "environment")
    verifier = _table(data, "verifier")

    task_id = str(metadata.get("task_id") or task_dir.name)
    repo = str(metadata.get("repository_url") or "")
    base_commit = str(metadata.get("base_commit_hash") or "")
    language = str(metadata.get("language") or "")
    image = str(environment.get("docker_image") or "")
    instruction_path = task_dir / "instruction.md"
    instruction = instruction_path.read_text(encoding="utf-8") if instruction_path.is_file() else ""

    try:
        verifier_timeout = float(verifier.get("timeout_sec") or 1800.0)
        build_timeout = float(_table(verifier, "environment").get("build_timeout_sec") or 1800.0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{task_id}: non-numeric timeout") from exc

    return Task(
        task_id=task_id,
        task_dir=task_dir,
        repo=repo,
        base_commit=base_commit,
        language=language,
        image=image,
        verifier_timeout_s=verifier_timeout,
        verifier_network=str(verifier.get("network_mode") or "no-network"),
        build_timeout_s=build_timeout,
        instruction=instruction.strip(),
    )


def validate(task: Task) -> None:
    """Refuse a task whose harness inputs are not all present."""
    missing = [name for name in _REQUIRED_FILES if not (task.task_dir / name).is_file()]
    if missing:
        raise ValueError(f"{task.task_id}: missing {', '.join(missing)}")
    if not task.repo:
        raise ValueError(f"{task.task_id}: metadata.repository_url is empty")
    if not task.base_commit:
        raise ValueError(f"{task.task_id}: metadata.base_commit_hash is empty")
    if not task.image:
        raise ValueError(f"{task.task_id}: environment.docker_image is empty")
    if not task.instruction:
        raise ValueError(f"{task.task_id}: instruction.md is empty")
    if task.task_id != task.task_dir.name:
        raise ValueError(
            f"{task.task_dir.name}: metadata.task_id is {task.task_id!r}; "
            "the directory name is the task's identity"
        )


def load_tasks(root: Path) -> dict[str, Task]:
    """Load every task directory under *root* (manifest files are skipped)."""
    tasks: dict[str, Task] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not (entry / "task.toml").is_file():
            continue
        task = parse_task(entry)
        tasks[task.task_id] = task
    if not tasks:
        raise ValueError(f"no DeepSWE tasks found under {root}")
    return tasks


def select(tasks: dict[str, Task], wanted: tuple[str, ...]) -> list[Task]:
    """Select tasks by id, preserving the caller's order."""
    unknown = [task_id for task_id in wanted if task_id not in tasks]
    if unknown:
        raise KeyError(f"unknown DeepSWE task(s): {unknown}; available: {sorted(tasks)[:10]} …")
    selected = [tasks[task_id] for task_id in wanted]
    for task in selected:
        validate(task)
    return selected


def fetch_tasks() -> tuple[Path, str]:
    """Resolve the task tree (cloned into the eval cache unless overridden)."""
    pin = os.environ.get("DEEPSWE_REF", "").strip() or DEEPSWE_PIN
    return external_bench.local_or_cloned(
        "DEEPSWE_TASKS_DIR",
        external_bench.CACHE_ROOT / "deep-swe",
        DEEPSWE_REPO,
        pin,
        label="DeepSWE",
        subdir="tasks",
    )
