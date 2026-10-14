"""SWE-bench_Lite instances: fetch, select, materialise, judge.

The official SWE-bench harness runs each instance inside its own Docker image.
This module builds the same three things on the host instead:

1. a checkout at the instance's **pinned base commit**,
2. a throwaway virtualenv with that checkout installed (`uv venv --python 3.11`
   and `uv pip install -e .`, the interpreter version the benchmark's images
   broadly use),
3. the instance's OWN verdict — every ``FAIL_TO_PASS`` test passes and every
   ``PASS_TO_PASS`` test still passes.

Faithfulness rules, which are what make the number comparable at all:

- the agent sees **only the problem statement**: the test patch is applied by the
  JUDGE, after the agent is done, exactly like the official harness — so the
  agent cannot read the expected behaviour off the new tests;
- the compared diff is taken against the pinned base commit, not a moving HEAD;
- an instance whose **gold patch** does not resolve it fails the harness check
  BEFORE any model call — the benchmark's own validation, reproduced locally;
- a submission is ``resolved`` only when all FAIL_TO_PASS and all PASS_TO_PASS
  pass; a test patch that does not apply (the agent edited tests) is unresolved.

The spike uses **sympy** instances: pure Python, a single runtime dependency
(mpmath), and its FAIL_TO_PASS names map onto pytest node ids of the file the
test patch touches. Django instances are deliberately not in this batch: their
ids are unittest labels routed through ``tests/runtests.py`` and their envs are
heavier, which is a second harness, not a first one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: SWE-bench_Lite test split, served by the HuggingFace datasets server
#: (public, no token; 300 rows, 100 per page).
DATASET_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Lite&config=default&split=test"
    "&offset={offset}&length=100"
)
DATASET_PAGES = (0, 100, 200)

#: The spike's instances (SWE-bench_Lite ids). Selection rule: pure-Python repo,
#: single dependency, one or two FAIL_TO_PASS tests, tests run in seconds.
SPIKE_INSTANCES: tuple[str, ...] = (
    "sympy__sympy-24909",
    "sympy__sympy-23117",
    "sympy__sympy-24213",
)

#: Interpreter for the instance environments (the benchmark's images pin old
#: Pythons; 3.11 keeps modern pytest working with these checkouts).
INSTANCE_PYTHON = "3.11"


@dataclass
class Instance:
    """One SWE-bench task, ready to run."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    gold_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    repo_dir: Path
    python_exe: Path = field(default=Path("python"))

    @property
    def repo_url(self) -> str:
        """The clone URL of the benchmark repository."""
        return f"https://github.com/{self.repo}.git"

    def node_ids(self) -> tuple[list[str], list[str]]:
        """``(fail_to_pass, pass_to_pass)`` as pytest node ids."""
        files = test_files_of(self.test_patch)
        return (
            _as_node_ids(self.fail_to_pass, files),
            _as_node_ids(self.pass_to_pass, files),
        )


def fetch_dataset(cache_path: Path) -> list[dict]:
    """Fetch (or reuse) the 300 raw SWE-bench_Lite rows."""
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(cached, list) and cached:
            return cached
    rows: list[dict] = []
    for offset in DATASET_PAGES:
        url = DATASET_ROWS_URL.format(offset=offset)
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = json.load(response)
        rows.extend(row["row"] for row in payload.get("rows", []))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def to_instance(row: dict, repo_dir: Path) -> Instance:
    """Turn one dataset row into an :class:`Instance` (no filesystem work yet)."""

    def _as_list(value: object) -> list[str]:
        if isinstance(value, str):
            decoded = json.loads(value)
            return [str(item) for item in decoded] if isinstance(decoded, list) else []
        return [str(item) for item in value] if isinstance(value, list) else []

    return Instance(
        instance_id=str(row["instance_id"]),
        repo=str(row["repo"]),
        base_commit=str(row["base_commit"]),
        problem_statement=str(row["problem_statement"]),
        test_patch=str(row.get("test_patch") or ""),
        gold_patch=str(row.get("patch") or ""),
        fail_to_pass=_as_list(row.get("FAIL_TO_PASS")),
        pass_to_pass=_as_list(row.get("PASS_TO_PASS")),
        repo_dir=repo_dir,
    )


def select(rows: list[dict], instance_ids: tuple[str, ...]) -> list[Instance]:
    """The requested instances, in the requested order."""
    ordered = []
    for wanted in instance_ids:
        match = next((row for row in rows if row.get("instance_id") == wanted), None)
        if match is None:
            raise SystemExit(f"instance {wanted!r} is not in SWE-bench_Lite")
        ordered.append(to_instance(match, Path(".")))
    return ordered


_PATCH_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def test_files_of(test_patch: str) -> list[str]:
    """The repository-relative paths a test patch touches."""
    return [match.strip() for match in _PATCH_FILE_RE.findall(test_patch)]


def _as_node_ids(names: list[str], files: list[str]) -> list[str]:
    """Map benchmark test names onto pytest node ids.

    sympy's own runner prints bare function names (``test_prefix_operations``),
    so the file comes from the test patch — every test in a SWE-bench test patch
    lives in exactly one file. Names that already carry ``::`` are kept as-is.
    """
    if not files:
        return list(names)
    default_file = files[0]
    return [name if "::" in name else f"{default_file}::{name}" for name in names]


# ── Materialising an instance ───────────────────────────────────────────────


def materialize(instance: Instance, work_dir: Path) -> Instance:
    """Clone at the base commit and install the checkout into a fresh venv."""
    repo_dir = work_dir / instance.instance_id
    venv_dir = work_dir / f"{instance.instance_id}-venv"
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=repo_dir)
    _run(["git", "remote", "add", "origin", instance.repo_url], cwd=repo_dir)
    _run(["git", "fetch", "-q", "--depth", "1", "origin", instance.base_commit], cwd=repo_dir)
    _run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=repo_dir)
    _run(["git", "config", "user.email", "evals@sherry.local"], cwd=repo_dir)
    _run(["git", "config", "user.name", "sherry-evals"], cwd=repo_dir)

    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    _run(["uv", "venv", "--python", INSTANCE_PYTHON, str(venv_dir)], cwd=work_dir)
    python_exe = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    _run(
        ["uv", "pip", "install", "--python", str(python_exe), "-q", "-e", str(repo_dir), "pytest"],
        cwd=work_dir,
    )
    instance.repo_dir = repo_dir
    instance.python_exe = python_exe
    return instance


def apply_patch(instance: Instance, patch: str) -> bool:
    """``git apply`` a patch into the checkout; ``False`` when it does not apply."""
    if not patch.strip():
        return False
    completed = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=str(instance.repo_dir),
        input=patch,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.returncode == 0


def revert(instance: Instance) -> None:
    """Drop every working-tree change, back to the pinned base."""
    _run(["git", "checkout", "--", "."], cwd=instance.repo_dir)
    _run(["git", "clean", "-qfd"], cwd=instance.repo_dir)


def run_tests(
    instance: Instance, node_ids: list[str], timeout_s: float = 900.0
) -> tuple[bool, str]:
    """Run the given node ids with the instance's own interpreter."""
    if not node_ids:
        return True, "(no tests)"
    completed = subprocess.run(
        [
            str(instance.python_exe),
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *node_ids,
        ],
        cwd=str(instance.repo_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    tail = (completed.stdout + completed.stderr).strip()[-3000:]
    return completed.returncode == 0, tail


def changed_paths(instance: Instance) -> list[str]:
    """Paths the agent changed, against the pinned base."""
    out = _run(["git", "diff", "--name-only"], cwd=instance.repo_dir)
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff(instance: Instance) -> str:
    """The agent's full diff against the pinned base commit."""
    return _run(["git", "diff"], cwd=instance.repo_dir)


def _run(argv: list[str], cwd: Path, timeout: float = 1800.0) -> str:
    completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{argv[0]} failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()[-800:]}"
        )
    return completed.stdout
