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
import subprocess
import threading
from types import TracebackType
from typing import Any, Literal, Protocol

from loguru import logger

__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_KILL_GRACE_SECONDS",
    "ProcessWatchdog",
    "areap_process",
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
