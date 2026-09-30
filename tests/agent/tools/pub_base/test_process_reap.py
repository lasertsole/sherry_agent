"""Bounded reap: a timeout must return, even against a child that will not die.

The two shapes that used to hang a tool call are both reproduced here with real
processes — a grandchild that keeps the pipe open after the parent exits, and a
child that ignores SIGTERM — because the failure mode is a *hang*, and only a
real process can show whether the bound holds.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest

from agent.tools.pub_base.process_reap import (
    ProcessWatchdog,
    areap_process,
    reap_process,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

FAST = {"grace": 0.5, "kill_grace": 0.5}


def _spawn(script: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def test_an_exited_child_reaps_as_exited():
    proc = _spawn("pass")
    proc.wait(timeout=30)

    assert reap_process(proc, **FAST) == "exited"
    assert proc.stdout is not None and proc.stdout.closed


def test_a_child_that_respects_sigterm_reaps_as_terminated():
    proc = _spawn("import time; time.sleep(60)")

    started = time.perf_counter()
    outcome = reap_process(proc, **FAST)

    assert outcome == "terminated"
    assert time.perf_counter() - started < 3.0


def test_a_child_that_ignores_sigterm_is_killed():
    """SIGTERM first, SIGKILL when it is ignored — in that order, both bounded.

    The child announces itself after installing the handler: without that
    handshake the SIGTERM can land during interpreter startup, where Python's
    default disposition ends the process and the test would assert the wrong
    escalation by accident.
    """
    proc = _spawn(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"

    started = time.perf_counter()
    outcome = reap_process(proc, grace=0.5, kill_grace=5.0)

    assert outcome == "killed"
    assert time.perf_counter() - started < 8.0


def test_a_grandchild_holding_stdout_does_not_hang_the_reap():
    """The real hazard: the child is killed, its own child keeps the pipe open.

    ``proc.communicate()`` after a kill waits for EOF on that pipe — i.e. for
    the grandchild — which is exactly the unbounded wait this replaces. The reap
    closes the pipes and waits only for the process it owns, so it returns while
    the grandchild is still sleeping.
    """
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                 stdout=sys.stdout)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = _spawn(script)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"

    started = time.perf_counter()
    outcome = reap_process(proc, **FAST)
    elapsed = time.perf_counter() - started

    assert outcome in ("terminated", "killed", "abandoned")
    assert elapsed < 3.0, f"reap blocked for {elapsed:.1f}s on a held pipe"


def test_an_async_child_reaps_too():
    async def run() -> str:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await areap_process(proc, **FAST)

    assert asyncio.run(run()) == "terminated"


def test_the_watchdog_fires_and_can_be_cancelled():
    fired: list[int] = []

    with ProcessWatchdog(0.05, lambda: fired.append(1)) as dog:
        time.sleep(0.2)

    assert dog.fired and fired == [1]

    quiet: list[int] = []
    with ProcessWatchdog(5.0, lambda: quiet.append(1)) as dog:
        pass
    assert not dog.fired and quiet == []


def test_a_failing_watchdog_callback_does_not_escape_its_thread():
    """The killer runs on a timer thread; an exception there must stay there."""
    with ProcessWatchdog(0.05, lambda: 1 / 0) as dog:
        time.sleep(0.2)

    assert dog.fired
