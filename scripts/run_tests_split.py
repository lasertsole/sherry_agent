#!/usr/bin/env python3
"""Process-isolated test runner: executes the pytest suite in two sequential,
separate OS processes so import-time ``sys.modules`` mutations can never leak
across suites.

Why this exists
---------------
``tests/unit/subagent/conftest.py`` installs stub callables into
process-global ``sys.modules`` at conftest import time. Under a single-process
full-suite run, pytest imports *all* conftests and test modules during
collection, before any test executes — so that pollution is live for every
test in the process, regardless of directory (see
``.omo/evidence/pre-existing-failures/REPORT.md`` §3). Running ``tests/unit``
in a different process from ``tests/integration + tests/system +
tests/module`` makes cross-suite pollution structurally impossible.

Groups (run SEQUENTIALLY, never in parallel — CPU/model resource contention):
  A  tests/unit
  B  tests/integration  tests/system  tests/module

Usage
-----
    uv run python scripts/run_tests_split.py
    uv run python scripts/run_tests_split.py --with-llm-e2e
    uv run python scripts/run_tests_split.py -- -k spawn -q

Exit codes: 0 = all groups passed; 1 = at least one group failed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

REPO_ROOT = Path(__file__).resolve().parent.parent

# (name, description, test paths)
GROUPS: list[tuple[str, str, list[str]]] = [
    ("A", "tests/unit", ["tests/unit"]),
    ("B", "integration + system + module", ["tests/integration", "tests/system", "tests/module"]),
]

# pytest exit codes (see `pytest --help` / _pytest.main.ExitCode)
RC_NAMES: dict[int, str] = {
    0: "all tests passed",
    1: "tests failed",
    2: "interrupted by user",
    3: "internal error",
    4: "pytest usage error",
    5: "no tests collected",
}


@dataclass(frozen=True)
class RunnerOptions:
    """Typed view of the runner's own flags."""

    with_llm_e2e: bool


def parse_args() -> tuple[RunnerOptions, list[str]]:
    """Parse runner flags; anything after ``--`` is forwarded verbatim to pytest."""
    argv: list[str] = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        passthrough = argv[split + 1 :]
        argv = argv[:split]

    parser = argparse.ArgumentParser(
        description=(
            "Run the test suite in two isolated pytest processes "
            "(unit | integration+system+module) so import-time sys.modules "
            "stubs cannot leak across suites."
        ),
        epilog="Extra args after '--' are forwarded to pytest, e.g. -- -k spawn -q",
    )
    _llm_flag = parser.add_argument(
        "--with-llm-e2e",
        action="store_true",
        help=(
            "Run ONLY the real-LLM e2e tests (`-m llm_e2e`, dedicated-job mode) "
            "instead of the default hermetic selection (`-m 'not llm_e2e'`). "
            "Those tests hit real LLM APIs, cost tokens and carry 300s "
            "pytest-timeout budgets."
        ),
    )
    ns = parser.parse_args(argv)
    options = RunnerOptions(with_llm_e2e=cast("bool", ns.with_llm_e2e))
    return options, passthrough


def build_group_cmd(group_paths: list[str], with_llm_e2e: bool, passthrough: list[str]) -> list[str]:
    """Build the pytest command line for one group."""
    cmd: list[str] = [sys.executable, "-m", "pytest", *group_paths, "-q"]
    # Marker selection. Passed on the CLI (after pyproject addopts) so the
    # script stays self-contained: the last -m wins over ini addopts.
    if with_llm_e2e:
        cmd += ["-m", "llm_e2e"]
    else:
        cmd += ["-m", "not llm_e2e"]
    cmd += passthrough
    return cmd


def child_env() -> dict[str, str]:
    """Environment for child pytest processes.

    PYTHONIOENCODING=utf-8: on Windows the console codepage is often GBK/cp936;
    forcing UTF-8 keeps pytest output intact, and our capture decodes with
    errors="replace" so stray bytes can never crash the runner.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Hermetic runs must not spawn the SkillSpector CLI (~120s subprocess during
    # collection: agent/core.py:40 build_skills_snapshot -> _scan_builtin_skills
    # -> scan_skill fires once per process) nor its LLM API round-trip; the
    # snapshot-path scan is fire-and-forget log-only and no test asserts it;
    # scanner unit tests patch module attributes and are immune (see
    # tests/unit/test_skill_scanner.py autouse fixture).
    env["SKILL_SCANNER_ENABLED"] = "0"
    return env


def run_group(
    name: str,
    desc: str,
    paths: list[str],
    with_llm_e2e: bool,
    passthrough: list[str],
) -> tuple[int, float]:
    """Run one group as a subprocess, streaming its output live.

    Returns (exit_code, elapsed_seconds). Output is captured with
    errors="replace" (UTF-8) and echoed, so GBK consoles can't corrupt it.
    """
    cmd = build_group_cmd(paths, with_llm_e2e, passthrough)
    print(f"\n{'=' * 70}\nGROUP {name}: {desc}\n  $ {' '.join(cmd)}\n{'=' * 70}", flush=True)

    start = time.monotonic()
    # stderr merged into stdout so interleaved output stays ordered.
    proc: subprocess.Popen[str] = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    stdout = cast("TextIO | None", proc.stdout)
    assert stdout is not None
    for line in stdout:
        _ = sys.stdout.write(line)
        _ = sys.stdout.flush()
    rc = proc.wait()
    elapsed = time.monotonic() - start
    return rc, elapsed


def main() -> int:
    options, passthrough = parse_args()

    # The runner's own stdout must survive GBK consoles / redirection too
    # (children get PYTHONIOENCODING=utf-8 via child_env()).
    import io

    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            _ = stream.reconfigure(encoding="utf-8", errors="replace")

    print("Process-isolated test runner (2 sequential pytest processes)")
    print(f"  repo root : {REPO_ROOT}")
    print(f"  llm_e2e   : {'SELECTED ONLY (dedicated job)' if options.with_llm_e2e else 'DESELECTED (default)'}")
    if passthrough:
        print(f"  passthrough: {passthrough}")

    results: list[tuple[str, str, int, float]] = []
    for name, desc, paths in GROUPS:
        rc, elapsed = run_group(name, desc, paths, options.with_llm_e2e, passthrough)
        results.append((name, desc, rc, elapsed))

    print(f"\n{'=' * 70}\nPER-GROUP SUMMARY\n{'=' * 70}")
    any_failed = False
    for name, desc, rc, elapsed in results:
        if rc == 0:
            status = "PASS"
        elif rc == 5:
            # Nothing collected (e.g. group A under --with-llm-e2e has no
            # llm_e2e-marked tests). Not a failure, but flagged loudly.
            status = "PASS (no tests collected)"
        else:
            status = "FAIL"
            any_failed = True
        print(f"  GROUP {name} ({desc}): {status}  [rc={rc} {RC_NAMES.get(rc, '?')}, {elapsed:.1f}s]")

    print("=" * 70)
    verdict = "FAIL - see failing group output above" if any_failed else "PASS - all groups green"
    print(f"FINAL VERDICT: {verdict}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    exit_code = main()
    raise SystemExit(exit_code)
