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
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

#: The HARD slice: same dataset, instances whose fixes need a real codebase and
#: whose regression surface is large (multi-test FAIL_TO_PASS sets and 100+
#: PASS_TO_PASS tests), one per repo so the number is not a single-repo artefact.
#: Versions are chosen for Python 3.11 compatibility (django 5.0, pytest 8.0,
#: pylint 2.15, sympy 1.8) — an old checkout whose suite fails for interpreter
#: reasons would measure the harness, not the agent.
HARD_INSTANCES: tuple[str, ...] = (
    "django__django-17051",
    "sympy__sympy-20639",
)

#: Two further repos were intended here and are NOT, each for a measured
#: environment reason (not a benchmark one — the gold-patch self-check catches
#: them before any model call, which is exactly its job):
#:
#: - ``pytest-dev__pytest-11148``: pytest's OWN suite owns the ``pytest``
#:   distribution, so installing it beside the checkout is a knife-edge; even
#:   with the editable checkout it leaves two PASS_TO_PASS tests failing in a
#:   hand-built venv (``test_usage_error_code`` and a conftest-load-failure case)
#:   — the official image pins that environment.
#: - ``pylint-dev__pylint-7080``: its 2.15 suite imports ``py._path.local``, and
#:   the ``py`` module a modern pytest leaves behind is a stub without ``py.io``.
#:
#: See ``evals/FINDINGS.md`` F7 for both.

#: Interpreter for the instance environments (the benchmark's images pin old
#: Pythons; 3.11 keeps modern pytest working with these checkouts).
INSTANCE_PYTHON = "3.11"

#: Repos whose test ids are unittest labels routed through their own runner
#: (``python tests/runtests.py module.Class.method``); everything else runs as
#: pytest node ids.
_DJANGO_LABEL_RE = re.compile(r"^(?P<name>[\w\[\].\-]+)\s+\((?P<module>[\w.]+)\)$")


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
    #: The library version this instance targets (SWE-bench's ``version``
    #: column). Used as the setuptools-scm pretend version so a SHALLOW clone —
    #: which cannot see the tag history — still reports a real version instead
    #: of ``0.1.dev1``: pytest's own suite refuses to start below its
    #: ``minversion`` otherwise.
    version: str = ""
    python_exe: Path = field(default=Path("python"))

    @property
    def repo_url(self) -> str:
        """The clone URL of the benchmark repository."""
        return f"https://github.com/{self.repo}.git"

    @property
    def judge_mode(self) -> str:
        """How this instance's tests are named and run."""
        return "django" if self.repo == "django/django" else "pytest"

    def node_ids(self) -> tuple[list[str], list[str]]:
        """``(fail_to_pass, pass_to_pass)`` as runnable test ids.

        pytest repos get ``file::name`` node ids; django's unittest labels
        (``test_x (module.Class)``) become ``module.Class.test_x`` for its own
        runner. A label that does not parse is dropped from the RUNNABLE set and
        reported (``unrunnable_ids``) rather than silently weakening the verdict.
        """
        if self.judge_mode == "django":
            return (
                [label for name in self.fail_to_pass if (label := _django_label(name))],
                [label for name in self.pass_to_pass if (label := _django_label(name))],
            )
        files = test_files_of(self.test_patch)
        return (
            _as_node_ids(self.fail_to_pass, files),
            _as_node_ids(self.pass_to_pass, files),
        )

    def unrunnable_ids(self) -> dict[str, list[str]]:
        """Benchmark test names this harness cannot schedule (reported, never hidden)."""
        if self.judge_mode != "django":
            return {}
        return {
            "fail_to_pass": [n for n in self.fail_to_pass if not _django_label(n)],
            "pass_to_pass": [n for n in self.pass_to_pass if not _django_label(n)],
        }


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
        version=str(row.get("version") or ""),
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


def _django_label(entry: str) -> str:
    """``test_x (module.Class)`` -> ``module.Class.test_x``; ``""`` when unrunnable.

    Some rows carry the FULL label inside the parentheses
    (``test_x (module.Class.test_x)``, which is how the benchmark's parser
    records an async or nested test). Appending the name there would produce
    ``module.Class.test_x.test_x`` — django's loader then resolves ``test_x`` as
    a function and every id errors out.
    """
    match = _DJANGO_LABEL_RE.match(entry.strip())
    if not match:
        return ""
    name, module = match.group("name"), match.group("module")
    if module.endswith(f".{name}"):
        return module
    return f"{module}.{name}"


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
    _install(repo_dir, python_exe, work_dir, scm_version=instance.version)
    instance.repo_dir = repo_dir
    instance.python_exe = python_exe
    return instance


def _install(repo_dir: Path, python_exe: Path, work_dir: Path, *, scm_version: str = "") -> None:
    """Install the checkout plus whatever its own test suite needs.

    A repo that declares an optional dependency group for testing
    (``[testing]`` / ``[test]``) gets it; a repo whose tests are driven by
    ``test_requirements.txt`` gets that file. Both mirror how the official
    images prepare their environments — nothing invented here.
    """
    base = ["uv", "pip", "install", "--python", str(python_exe), "-q"]
    # setuptools-scm BAKES the version at build time (pytest writes it into
    # ``src/_pytest/_version.py``), so a shallow clone needs the pretend version
    # before the editable install — not only when the tests run.
    env = dict(os.environ)
    if scm_version:
        env["SETUPTOOLS_SCM_PRETEND_VERSION"] = scm_version
    # A repo that IS pytest provides the ``pytest`` distribution itself: asking
    # for PyPI's pytest beside the editable checkout would fight it.
    extra_pytest = [] if _distribution_name(repo_dir) == "pytest" else ["pytest"]
    installed = False
    for extra in _test_extras(repo_dir):
        completed = subprocess.run(
            [*base, "-e", f"{repo_dir}[{extra}]", *extra_pytest],
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if completed.returncode == 0:
            installed = True
            break
    if not installed:
        _run([*base, "-e", str(repo_dir), *extra_pytest], cwd=work_dir, env=env)
    # The repo's own test pins matter: pylint 2.15's suite imports ``py``, which
    # only an old pytest (or the ``py`` package) provides.
    for candidate in (
        "test_requirements.txt",
        "requirements_test.txt",
        "requirements-test.txt",
        "requirements_dev.txt",
    ):
        requirements = repo_dir / candidate
        if requirements.is_file():
            # cwd = the repo: these files carry relative entries (`-e .`), and uv
            # resolves them against the working directory.
            _run([*base, "-r", str(requirements)], cwd=repo_dir, env=env)
            break


def _distribution_name(repo_dir: Path) -> str:
    """The package name a checkout installs as (``[project].name``)."""
    pyproject = repo_dir / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    text = pyproject.read_text(encoding="utf-8", errors="replace")
    section = re.search(r"^\[project\]$(.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not section:
        return ""
    name = re.search(r'^\s*name\s*=\s*"([^"]+)"', section.group(1), re.M)
    return name.group(1) if name else ""


def _test_extras(repo_dir: Path) -> list[str]:
    """Optional-dependency group names that look like the repo's test deps."""
    pyproject = repo_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []
    text = pyproject.read_text(encoding="utf-8", errors="replace")
    section = re.search(r"^\[project\.optional-dependencies\]$(.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not section:
        return []
    names = re.findall(r"^\s*([\w.\-]+)\s*=", section.group(1), re.M)
    return [name for name in names if name.lower().startswith("test")]


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


# ---------------------------------------------------------------------------
# Per-test outcome extraction — report observability, never the verdict
# ---------------------------------------------------------------------------

#: pytest failure-section header (``______ test_name ______``).
_PYTEST_SECTION_RE = re.compile(r"^_{3,}\s*(?P<name>\S+)\s*_{3,}$")

#: pytest short-summary lines (``FAILED path::name - AssertionError: ...``).
_PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<node>\S+)(?:\s+-\s+(?P<note>.*))?$", re.M)

#: django unittest block headers (``FAIL: name (dotted.path)``).
_DJANGO_FAILED_RE = re.compile(r"^(?:FAIL|ERROR):\s+(?P<name>\S+?)(?:\s+\((?P<path>[^)]+)\))?$")


def _pytest_outcomes(output: str, node_ids: list[str]) -> list[dict[str, Any]]:
    """Per-test outcomes for a pytest run (failures from the short summary)."""
    failed: set[str] = set()
    notes: dict[str, str] = {}
    for match in _PYTEST_FAILED_RE.finditer(output):
        node = match.group("node")
        failed.add(node)
        note = (match.group("note") or "").strip()
        if note:
            notes.setdefault(node, note[:200])

    # First ``E ... assert ...`` line of each failure section, keyed by header.
    assert_lines: dict[str, str] = {}
    current: str | None = None
    for line in output.splitlines():
        header = _PYTEST_SECTION_RE.match(line)
        if header:
            current = header.group("name")
            continue
        if current and line.startswith("E ") and "assert" in line:
            assert_lines.setdefault(current, line[2:].strip()[:200])

    outcomes: list[dict[str, Any]] = []
    for node in node_ids:
        if node in failed:
            # The failure section's first ``E assert`` line is richer than the
            # short-summary note (which pytest truncates); fall back to the note.
            detail = (
                assert_lines.get(node)
                or next(
                    (v for k, v in assert_lines.items() if node.endswith(k) or k.endswith(node)),
                    None,
                )
                or notes.get(node)
            )
            outcomes.append({"id": node, "outcome": "fail", "first_assert": detail})
        else:
            outcomes.append({"id": node, "outcome": "pass", "first_assert": None})
    return outcomes


def _django_outcomes(output: str, node_ids: list[str]) -> list[dict[str, Any]]:
    """Per-test outcomes for a django runner run (failures from FAIL/ERROR blocks)."""
    failed: dict[str, str | None] = {}
    current: str = ""
    for line in output.splitlines():
        match = _DJANGO_FAILED_RE.match(line)
        if match:
            path = match.group("path") or ""
            name = match.group("name") or ""
            current = f"{path}.{name}" if path and not path.endswith(f".{name}") else (path or name)
            failed.setdefault(current, None)
            continue
        if current and failed.get(current) is None and "AssertionError" in line:
            failed[current] = line.strip()[:200]

    outcomes: list[dict[str, Any]] = []
    for node in node_ids:
        if node in failed:
            outcomes.append({"id": node, "outcome": "fail", "first_assert": failed[node]})
        else:
            outcomes.append({"id": node, "outcome": "pass", "first_assert": None})
    return outcomes


def parse_test_outcomes(
    instance: Instance, output: str, node_ids: list[str]
) -> list[dict[str, Any]]:
    """Structured per-test results for the report (``judge_tests``).

    A test absent from the failure summary counts as passed — the VERDICT comes
    from the runner's exit code, this field only makes near-misses visible.
    """
    if instance.judge_mode == "django":
        return _django_outcomes(output, node_ids)
    return _pytest_outcomes(output, node_ids)


def run_tests(
    instance: Instance, node_ids: list[str], timeout_s: float = 1800.0
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Run the given test ids with the instance's own interpreter and runner.

    Returns ``(ok, tail, outcomes)``: the exit-code verdict, the bounded output
    tail (``judge_detail``), and the structured per-test outcomes
    (``judge_tests``) parsed from the full output.
    """
    if not node_ids:
        return True, "(no tests)", []
    if instance.judge_mode == "django":
        argv = [str(instance.python_exe), "tests/runtests.py", "--verbosity", "1", *node_ids]
    else:
        argv = [
            str(instance.python_exe),
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *node_ids,
        ]
    env = dict(os.environ)
    if instance.version:
        # See Instance.version: a shallow clone cannot report the real version.
        env["SETUPTOOLS_SCM_PRETEND_VERSION"] = instance.version
    completed = subprocess.run(
        argv,
        cwd=str(instance.repo_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    output = completed.stdout + completed.stderr
    tail = output.strip()[-3000:]
    outcomes = parse_test_outcomes(instance, output, node_ids)
    return completed.returncode == 0, tail, outcomes


def changed_paths(instance: Instance) -> list[str]:
    """Paths the agent changed, against the pinned base."""
    out = _run(["git", "diff", "--name-only"], cwd=instance.repo_dir)
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff(instance: Instance) -> str:
    """The agent's full diff against the pinned base commit."""
    return _run(["git", "diff"], cwd=instance.repo_dir)


def _run(
    argv: list[str], cwd: Path, timeout: float = 1800.0, env: dict[str, str] | None = None
) -> str:
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{argv[0]} failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()[-800:]}"
        )
    return completed.stdout
