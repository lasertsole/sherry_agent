#!/usr/bin/env python3
"""Process-isolated test runner: executes the pytest suite in two sequential,
separate OS processes so import-time ``sys.modules`` mutations can never leak
across suites.

Why this exists
---------------
``tests/agent/tools/subagent/conftest.py`` installs stub callables into
process-global ``sys.modules`` at conftest import time. Under a single-process
full-suite run, pytest imports *all* conftests and test modules during
collection, before any test executes — so that pollution is live for every
test in the process, regardless of directory. Running the subagent
tree in a different process from the other test groups makes cross-suite
pollution structurally impossible.

Groups (run SEQUENTIALLY, never in parallel — CPU/model resource contention):
  A  marker: unit
  B  marker: integration or module or system
  C  marker: regression

Every group is bounded
-----------------------
Streaming a child's pipe and waiting for EOF is not enough to know a group is
done: a grandchild that inherited stdout (a test that leaked ``sh -c sleep …``)
keeps the write end open after pytest exited, and the runner then blocks on a
pipe nothing will close. Each group therefore runs in its own session, its
output is drained by a daemon reader, and the group is considered finished when
the *process* exits — a still-open pipe is treated as a leaked process and the
leftover session is killed. Two budgets back that up: no output for
``GROUP_IDLE_TIMEOUT_SECONDS`` and a total of ``GROUP_TOTAL_TIMEOUT_SECONDS``
both abort the group with a stack dump (SIGABRT + faulthandler) instead of
burning the CI job's whole budget on a silent stall.

Usage
-----
    uv run python tests/run_tests_split.py
    uv run python tests/run_tests_split.py --with-llm-e2e
    uv run python tests/run_tests_split.py -- -k spawn -q

Exit codes: 0 = all groups passed; 1 = at least one group failed.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

REPO_ROOT = Path(__file__).resolve().parent.parent

# (name, description, marker expression evaluated against the whole tests/ tree)
# Grouping is BY MARKER, not by directory — test files mirror the source tree
# (tests/agent/..., tests/server/...) and carry a module-level pytestmark.
GROUPS: list[tuple[str, str, str]] = [
    ("A", "unit", "unit"),
    ("B", "integration + module + system", "integration or module or system"),
    ("C", "regression", "regression"),
]

# pytest exit codes (see `pytest --help` / _pytest.main.ExitCode)
RC_NAMES: dict[int, str] = {
    0: "all tests passed",
    1: "tests failed",
    2: "interrupted by user",
    3: "internal error",
    4: "pytest usage error",
    5: "no tests collected",
    -9: "killed by the runner (stalled or over budget — see the group's abort dump above)",
    -6: "aborted (SIGABRT stack dump from the runner's stall detection)",
}

#: A group that prints nothing for this long is hung (the slowest hermetic
#: single test stays under 120s; llm_e2e groups are not run here).
GROUP_IDLE_TIMEOUT_SECONDS = 300.0

#: Hard wall clock per group. Group A (unit) is the slowest at ~2 min; the
#: budget exists so a pathological group fails the job with a summary instead
#: of hitting the job's own (much longer) timeout with no diagnostics.
GROUP_TOTAL_TIMEOUT_SECONDS = 900.0

#: Grace for draining an exited child's buffered output before a still-open pipe
#: is declared leaked and its session killed.
_EXIT_DRAIN_GRACE_SECONDS = 5.0

#: Group id of the group currently running — groups run sequentially, so one
#: slot is enough. A cancelled CI job (SIGTERM) has to take the child down with
#: the runner: the child owns its own session, so it would otherwise outlive
#: the runner and keep writing to a pipe nobody reads.
_ACTIVE_PGID: int | None = None


def _on_terminate(signum: int, _frame: object) -> None:  # pragma: no cover - signal path
    if _ACTIVE_PGID is not None:
        try:
            os.killpg(_ACTIVE_PGID, signal.SIGKILL)
        except OSError:
            pass
    raise SystemExit(128 + signum)


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
            "Run the test suite in three isolated pytest processes "
            "(unit | integration+module+system | regression) so import-time "
            "sys.modules stubs cannot leak across suites."
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


def build_group_cmd(group_expr: str, with_llm_e2e: bool, passthrough: list[str]) -> list[str]:
    """Build the pytest command line for one group."""
    cmd: list[str] = [sys.executable, "-m", "pytest", "tests/", "--ignore=tests/full", "-q"]
    # Marker selection. The group expression is composed with the llm_e2e
    # policy on the CLI (after pyproject addopts) so the script stays
    # self-contained: the last -m wins over ini addopts.
    llm = "llm_e2e" if with_llm_e2e else "not llm_e2e"
    cmd += ["-m", f"({group_expr}) and {llm}"]
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
    # Defense-in-depth: hermetic runs must never spawn the SkillSpector CLI
    # (~120s subprocess) nor its LLM API round-trip. The historical trigger —
    # agent.core import-time build_skills_snapshot -> _scan_builtin_skills ->
    # scan_skill once per process during collection — is gone (moved into the
    # explicit agent.core.init()), but any residual
    # snapshot/scanner path stays fire-and-forget log-only and no test
    # asserts it; scanner unit tests patch module attributes and are immune
    # (see tests/server/service/test_skill_scanner.py autouse fixture).
    env["SKILL_SCANNER_ENABLED"] = "0"
    return env


class _OutputPump:
    """Forward a child's merged output, remembering when it last arrived.

    Runs in a daemon thread because the pipe's EOF is NOT a reliable
    "group done" signal — see the module docstring.
    """

    def __init__(self, stdout: TextIO) -> None:
        self._stdout = stdout
        self._sink: TextIO = sys.stdout
        self.last_output = time.monotonic()
        self._thread = threading.Thread(target=self._pump, name="group-output", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in self._stdout:
                self.last_output = time.monotonic()
                self._sink.write(line)
                self._sink.flush()
        except (OSError, ValueError):
            # The pipe was closed under us (session killed, or the runner gave
            # up on a wedged holder). Nothing left to read.
            return

    def join(self, timeout: float) -> bool:
        """Wait for the pipe to reach EOF. True when the reader finished."""
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()


def _signal_group(proc: subprocess.Popen[str], pgid: int | None, sig: int) -> None:
    """Signal the child and everything it spawned.

    ``pgid`` is read at spawn time on purpose: once the direct child has exited
    and been reaped, ``os.getpgid(proc.pid)`` fails, and a group kill built on
    it would silently miss the leaked grandchild — the exact process that holds
    the pipe. The group itself survives as long as any member does.
    """
    if pgid is not None and pgid != os.getpgid(0):
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass
    try:
        proc.send_signal(sig)
    except (OSError, ValueError):
        pass


def _abort_group(proc: subprocess.Popen[str], pgid: int | None, reason: str) -> None:
    """Dump every thread stack in the child, then kill its session.

    SIGABRT reaches pytest's faulthandler (the plugin enables it), so the CI
    log carries *where* the group was stuck instead of only that it was.
    """
    print(
        f"\n{'!' * 70}\nGROUP ABORT: {reason}\n"
        "Sending SIGABRT for a full stack dump, then SIGKILL.\n"
        f"{'!' * 70}",
        flush=True,
    )
    _signal_group(proc, pgid, signal.SIGABRT)
    time.sleep(2.0)  # let the dump stream through before the kill
    _signal_group(proc, pgid, signal.SIGKILL)


def _run_group_process(cmd: list[str]) -> int:
    """Run one pytest command streamed, bounded, and never waiting on EOF.

    Returns the child's exit code (negative when it died from a signal).
    """
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
        # Its own session: the runner can reap pytest and every process a test
        # leaked with a single killpg, so no group can outlive this script.
        start_new_session=(os.name == "posix"),
    )
    # The group id while the leader is alive: with start_new_session this IS
    # the child's pid, but reading it now keeps the fallback honest if the
    # platform ignored the flag. Recorded here because it is unobtainable once
    # the child has exited and been reaped.
    pgid: int | None = None
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None

    stdout = cast("TextIO | None", proc.stdout)
    assert stdout is not None
    pump = _OutputPump(stdout)
    pump.start()

    global _ACTIVE_PGID
    _ACTIVE_PGID = pgid
    start = time.monotonic()
    rc: int | None = None
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            now = time.monotonic()
            if now - pump.last_output > GROUP_IDLE_TIMEOUT_SECONDS:
                _abort_group(proc, pgid, f"no output for {GROUP_IDLE_TIMEOUT_SECONDS:.0f}s")
                rc = None
                break
            if now - start > GROUP_TOTAL_TIMEOUT_SECONDS:
                _abort_group(
                    proc, pgid, f"total budget {GROUP_TOTAL_TIMEOUT_SECONDS:.0f}s exceeded"
                )
                rc = None
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\ninterrupted — killing the group's session", flush=True)
        _signal_group(proc, pgid, signal.SIGKILL)
        raise

    if rc is None:
        # Aborted above (or a signal race): bound the wait, never block.
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            rc = -9

    if not pump.join(timeout=_EXIT_DRAIN_GRACE_SECONDS):
        # The child is gone but its stdout is still open: something it spawned
        # inherited the pipe. Not a reason to wait — reap the session.
        print(
            f"\n{'!' * 70}\nGROUP NOTE: the child exited but a leaked process still holds its "
            "output pipe; killing the leftover session.\n" + "!" * 70,
            flush=True,
        )
        _signal_group(proc, pgid, signal.SIGKILL)
        _ = pump.join(timeout=_EXIT_DRAIN_GRACE_SECONDS)

    # Deliberately NOT closing the pipe: the daemon pump may still hold its
    # read lock, and close() would then block on a holder that SIGKILL cannot
    # reach (uninterruptible I/O) — the very hang this function exists to
    # prevent. The fd dies with the runner.
    _ACTIVE_PGID = None
    return rc


def run_group(
    name: str,
    desc: str,
    expr: str,
    with_llm_e2e: bool,
    passthrough: list[str],
) -> tuple[int, float]:
    """Run one group as a subprocess, streaming its output live.

    Returns (exit_code, elapsed_seconds).
    """
    cmd = build_group_cmd(expr, with_llm_e2e, passthrough)
    print(f"\n{'=' * 70}\nGROUP {name}: {desc}\n  $ {' '.join(cmd)}\n{'=' * 70}", flush=True)

    start = time.monotonic()
    rc = _run_group_process(cmd)
    elapsed = time.monotonic() - start
    return rc, elapsed


def main() -> int:
    options, passthrough = parse_args()

    # A cancelled job / `timeout` must not orphan a running group: SIGTERM
    # takes the current session down with the runner (SIGINT already surfaces
    # as KeyboardInterrupt inside the group loop).
    try:
        _ = signal.signal(signal.SIGTERM, _on_terminate)
    except (ValueError, OSError):  # pragma: no cover - non-main thread / platform
        pass

    # The runner's own stdout must survive GBK consoles / redirection too
    # (children get PYTHONIOENCODING=utf-8 via child_env()).
    import io

    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            _ = stream.reconfigure(encoding="utf-8", errors="replace")

    print("Process-isolated test runner (3 sequential pytest processes)")
    print(f"  repo root : {REPO_ROOT}")
    print(
        f"  llm_e2e   : {'SELECTED ONLY (dedicated job)' if options.with_llm_e2e else 'DESELECTED (default)'}"
    )
    if passthrough:
        print(f"  passthrough: {passthrough}")

    results: list[tuple[str, str, int, float]] = []
    for name, desc, expr in GROUPS:
        rc, elapsed = run_group(name, desc, expr, options.with_llm_e2e, passthrough)
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
        print(
            f"  GROUP {name} ({desc}): {status}  [rc={rc} {RC_NAMES.get(rc, '?')}, {elapsed:.1f}s]"
        )

    print("=" * 70)
    verdict = "FAIL - see failing group output above" if any_failed else "PASS - all groups green"
    print(f"FINAL VERDICT: {verdict}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    exit_code = main()
    raise SystemExit(exit_code)
