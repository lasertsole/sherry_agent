"""Bounded process cleanup for every spawn point that runs a child with a deadline.

The hazard this exists for: killing a child and then waiting for it without a
bound can wait forever. Two shapes of it are real here —

* the child forked a grandchild that inherited stdout/stderr, so the pipe never
  reaches EOF and a post-kill ``communicate()`` blocks until that grandchild
  exits (``terminal.py`` did exactly this);
* the child is wedged in uninterruptible I/O (a dead network mount), where even
  ``SIGKILL`` is not delivered until the I/O returns.

Both leave a tool call hanging past its deadline, which is the one thing a
timeout must never do. The escalation is therefore bounded at every step:
``terminate`` → wait ``grace`` → ``kill`` → wait ``kill_grace`` → **abandon the
handle** with a warning. Ported from DeepAgents' ``_reap_ripgrep`` (see
a sibling agent framework's filesystem backend), whose comment on the
uninterruptible case is the reason the second wait is bounded too.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from types import TracebackType
from typing import Any, Literal, Protocol

from loguru import logger

__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_KILL_GRACE_SECONDS",
    "ProcessWatchdog",
    "akill_process_tree",
    "areap_process",
    "kill_process_tree",
    "reap_process",
]

#: Seconds a child gets to exit after SIGTERM, and again after SIGKILL.
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_KILL_GRACE_SECONDS = 5.0

type ReapOutcome = Literal["exited", "terminated", "killed", "abandoned"]


class _SyncProcess(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _close_streams(proc: Any) -> None:
    """Close the child's pipes so a stray reader cannot keep the fds alive."""
    for stream in (
        getattr(proc, "stdout", None),
        getattr(proc, "stderr", None),
        getattr(proc, "stdin", None),
    ):
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("closing a process stream failed", exc_info=True)


def reap_process(
    proc: _SyncProcess,
    *,
    grace: float = DEFAULT_GRACE_SECONDS,
    kill_grace: float = DEFAULT_KILL_GRACE_SECONDS,
) -> ReapOutcome:
    """Make sure ``proc`` is gone, or give up on it — never both unbounded.

    Returns ``"exited"`` when it had already finished, ``"terminated"`` when
    SIGTERM sufficed, ``"killed"`` when SIGKILL was needed, and ``"abandoned"``
    when it survived even that (logged).
    """
    _close_streams(proc)
    try:
        if proc.poll() is not None:
            return "exited"
    except Exception:  # noqa: BLE001 - a broken handle is already gone for our purposes
        return "abandoned"

    for step, outcome in (
        (proc.terminate, "terminated"),
        (proc.kill, "killed"),
    ):
        try:
            step()
        except Exception:  # noqa: BLE001 - ProcessLookupError means it exited between checks
            return "exited"
        try:
            proc.wait(timeout=grace if outcome == "terminated" else kill_grace)
            return outcome
        except subprocess.TimeoutExpired:
            continue

    logger.warning(
        "process did not exit after SIGKILL; abandoning handle (pid={})",
        getattr(proc, "pid", "?"),
    )
    return "abandoned"


async def areap_process(
    proc: Any,
    *,
    grace: float = DEFAULT_GRACE_SECONDS,
    kill_grace: float = DEFAULT_KILL_GRACE_SECONDS,
) -> ReapOutcome:
    """``reap_process`` for ``asyncio.subprocess`` handles."""
    _close_streams(proc)
    if proc.returncode is not None:
        return "exited"

    for step, outcome, budget in (
        (proc.terminate, "terminated", grace),
        (proc.kill, "killed", kill_grace),
    ):
        try:
            step()
        except Exception:  # noqa: BLE001 - ProcessLookupError means it exited between checks
            return "exited"
        try:
            await asyncio.wait_for(proc.wait(), timeout=budget)
            return outcome
        except TimeoutError:
            continue

    logger.warning(
        "process did not exit after SIGKILL; abandoning handle (pid={})",
        getattr(proc, "pid", "?"),
    )
    return "abandoned"


def _reap_direct(proc: Any, budget: float) -> None:
    """Reap the direct child so its zombie cannot keep the group "alive"."""
    wait = getattr(proc, "wait", None)
    if wait is None:
        return
    try:
        wait(timeout=max(0.0, budget))
    except Exception:  # noqa: BLE001, S110 - timeout / already reaped
        pass


def _group_alive(pgid: int) -> bool:
    """Whether a POSIX process group still has at least one member."""
    if os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _areap_direct(proc: Any, budget: float) -> None:
    wait = getattr(proc, "wait", None)
    if wait is None:
        return
    try:
        await asyncio.wait_for(wait(), timeout=max(0.0, budget))
    except (TimeoutError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001, S110 - already reaped
        pass


def _wait_group_gone(pgid: int, budget: float) -> bool:
    deadline = time.monotonic() + max(0.0, budget)
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return not _group_alive(pgid)


def _kill_windows_tree(proc: Any, timeout: float) -> ReapOutcome:
    """``taskkill /T /F`` is the only reliable tree kill on Windows."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return "exited"
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return "killed"
    except Exception:  # noqa: BLE001 - fall back to the direct handle
        return reap_process(proc)


def kill_process_tree(
    proc: _SyncProcess,
    *,
    pgid: int | None = None,
    grace: float = DEFAULT_GRACE_SECONDS,
    kill_grace: float = DEFAULT_KILL_GRACE_SECONDS,
) -> ReapOutcome:
    """Terminate/kill ``proc`` AND its process group, bounded at every step.

    ``pgid`` is the group recorded at spawn time (with ``start_new_session=True``
    it is the child PID). Never signals our own group: a child that was not
    isolated is cleaned up with the direct-only :func:`reap_process` instead.
    """
    if os.name == "nt":
        return _kill_windows_tree(proc, grace + kill_grace)
    target = pgid if pgid is not None else getattr(proc, "pid", None)
    if not target:
        return reap_process(proc)
    try:
        own = os.getpgid(0)
    except OSError:
        own = -1
    if target == own:
        return reap_process(proc)

    _close_streams(proc)
    try:
        os.killpg(target, signal.SIGTERM)
    except ProcessLookupError:
        return "exited"
    except OSError:
        return reap_process(proc)
    _reap_direct(proc, grace)
    if _wait_group_gone(target, grace):
        return "terminated"

    try:
        os.killpg(target, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return reap_process(proc)
    _reap_direct(proc, kill_grace)
    if _wait_group_gone(target, kill_grace):
        return "killed"
    logger.warning("process group {} did not exit after SIGKILL; abandoning", target)
    return "abandoned"


async def akill_process_tree(
    proc: Any,
    *,
    pgid: int | None = None,
    grace: float = DEFAULT_GRACE_SECONDS,
    kill_grace: float = DEFAULT_KILL_GRACE_SECONDS,
) -> ReapOutcome:
    """Async counterpart of :func:`kill_process_tree` (asyncio subprocess)."""
    if os.name == "nt":
        return await asyncio.to_thread(_kill_windows_tree, proc, grace + kill_grace)
    target = pgid if pgid is not None else getattr(proc, "pid", None)
    if not target:
        return await areap_process(proc)
    try:
        own = os.getpgid(0)
    except OSError:
        own = -1
    if target == own:
        return await areap_process(proc)

    _close_streams(proc)
    try:
        os.killpg(target, signal.SIGTERM)
    except ProcessLookupError:
        return "exited"
    except OSError:
        return await areap_process(proc)
    await _areap_direct(proc, grace)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not _group_alive(target):
            return "terminated"
        await asyncio.sleep(0.05)
    if not _group_alive(target):
        return "terminated"

    try:
        os.killpg(target, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return await areap_process(proc)
    await _areap_direct(proc, kill_grace)
    deadline = time.monotonic() + max(0.0, kill_grace)
    while time.monotonic() < deadline:
        if not _group_alive(target):
            return "killed"
        await asyncio.sleep(0.05)
    if not _group_alive(target):
        return "killed"
    logger.warning("process group {} did not exit after SIGKILL; abandoning", target)
    return "abandoned"


class ProcessWatchdog:
    """Kill a child after ``timeout`` seconds, from a thread that is not blocked.

    A blocking read on the child's stdout cannot honour a deadline on its own —
    the read returns when the child writes, not when the clock says so — so the
    watchdog is what actually bounds a hang that never reaches a match cap.
    Use as a context manager around the read loop::

        with ProcessWatchdog(5.0, proc.kill) as dog:
            for line in proc.stdout: ...
        if dog.fired: ...
    """

    def __init__(self, timeout: float, on_timeout: Any) -> None:
        self._timeout = timeout
        self._on_timeout = on_timeout
        self._fired = threading.Event()
        self._timer: threading.Timer | None = None

    @property
    def fired(self) -> bool:
        """True once the timeout elapsed and ``on_timeout`` ran."""
        return self._fired.is_set()

    def _fire(self) -> None:
        self._fired.set()
        try:
            self._on_timeout()
        except Exception:  # noqa: BLE001 - a failing killer must not kill the timer thread
            logger.warning("process watchdog callback raised", exc_info=True)

    def start(self) -> ProcessWatchdog:
        self._timer = threading.Timer(self._timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def __enter__(self) -> ProcessWatchdog:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self.cancel()
        return False
