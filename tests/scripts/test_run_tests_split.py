"""The split runner must know a group finished from the *process*, not the pipe.

The failure these tests pin down: a grandchild that inherited stdout (a test
leaking ``sh -c sleep …``) keeps the pipe's write end open after pytest exited,
so a runner that streams ``for line in stdout`` blocks forever — CI burned its
whole 30-minute job on exactly that, with group A's summary as the last line.
The runner therefore treats "child exited" as the end, kills the leftover
session when the pipe stays open, and bounds every group with an idle budget
and a total budget that abort with a stack dump instead of a silent stall.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "run_tests_split.py"


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    """Load the runner from its file path (``tests/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("run_tests_split", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass annotation resolution finds the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_normal_group_returns_its_exit_code(runner, capsys):
    rc = runner._run_group_process(
        [sys.executable, "-c", "print('hello from the group', flush=True)"]
    )

    assert rc == 0
    assert "hello from the group" in capsys.readouterr().out


def test_a_leaked_grandchild_holding_the_pipe_does_not_stall_the_group(runner, capsys):
    """The child exits at once; its grandchild keeps stdout open for 120s.

    A pipe-EOF-bound runner would block the full two minutes (and in CI, until
    the job's timeout). The runner must instead notice the child is gone, kill
    the leftover session, and move on to the next group.
    """
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print('child done', flush=True)\n"
    )

    start = time.monotonic()
    rc = runner._run_group_process([sys.executable, "-c", script])
    elapsed = time.monotonic() - start

    assert rc == 0
    assert elapsed < 60, f"the group should not wait on the leaked holder ({elapsed:.1f}s)"
    out = capsys.readouterr().out
    assert "child done" in out
    assert "leaked process" in out, "the leftover holder must be reported and reaped"


def test_a_stalled_group_is_aborted_within_the_idle_budget(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "GROUP_IDLE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(runner, "GROUP_TOTAL_TIMEOUT_SECONDS", 60.0)

    start = time.monotonic()
    rc = runner._run_group_process([sys.executable, "-c", "import time; time.sleep(300)"])
    elapsed = time.monotonic() - start

    assert rc in (-6, -9), f"a killed group reports its signal, got {rc}"
    assert elapsed < 30, f"the abort must be bounded ({elapsed:.1f}s)"
    out = capsys.readouterr().out
    assert "GROUP ABORT: no output for" in out


def test_the_total_budget_aborts_even_while_output_flows(runner, monkeypatch, capsys):
    """A chatty child defeats the idle budget; the total budget still ends it."""
    monkeypatch.setattr(runner, "GROUP_IDLE_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(runner, "GROUP_TOTAL_TIMEOUT_SECONDS", 1.5)
    script = "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.2)\n"

    start = time.monotonic()
    rc = runner._run_group_process([sys.executable, "-u", "-c", script])
    elapsed = time.monotonic() - start

    assert rc in (-6, -9), rc
    assert elapsed < 30, f"the total budget must be bounded ({elapsed:.1f}s)"
    assert "GROUP ABORT" in capsys.readouterr().out


def test_the_runner_carries_names_for_its_own_kill_codes(runner):
    """The summary prints rc names; a killed group must not read as ``rc=?, ?``."""
    assert -9 in runner.RC_NAMES
    assert -6 in runner.RC_NAMES
