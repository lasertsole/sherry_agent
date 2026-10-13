"""Per-path in-process mutex for the file tools (R3).

The main agent and every subagent share ONE process and the SAME
``build_main_tools()`` tool singletons, so two of them can be inside
``patch_file``'s read-modify-write on the same file at the same time. The
tool lane caps how MANY agents run, never WHICH file they touch — this lock is
the missing piece.

Implementation notes:

* ``threading.Lock`` is enough: the tools' ``_core`` is synchronous and called
  through ``asyncio.to_thread``, and the critical section never awaits.
* The registry is reference-counted and entries are dropped when the last user
  leaves, so a long-lived process does not accumulate one dict entry per file
  it ever wrote.
* Lock ORDER is fixed and documented once: in-process path lock first, then the
  cross-process file lock (see :mod:`~agent.tools.pub_base.file_lock`). Only one
  order exists, so two writers cannot deadlock against each other.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

__all__ = ["path_lock", "active_path_locks"]

#: Emit a debug line when a caller had to wait at least this long.
_SLOW_WAIT_SECONDS = 0.05


@dataclass
class _LockEntry:
    """One path's lock plus its live user count."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


_registry: dict[str, _LockEntry] = {}
_registry_guard = threading.Lock()


def active_path_locks() -> int:
    """Number of paths currently registered (0 when everything is released).

    Exposed for tests and diagnostics: a leak would show up as a registry that
    never drains.
    """
    with _registry_guard:
        return len(_registry)


@contextlib.contextmanager
def path_lock(path: Path) -> Iterator[None]:
    """Serialize writers of one path within this process.

    :param path: The RESOLVED target path (same object the write uses, so two
        spellings of one file cannot take two locks).
    :yields: Nothing; the critical section runs while the path is held.
    """
    key = str(path)
    with _registry_guard:
        entry = _registry.get(key)
        if entry is None:
            entry = _LockEntry()
            _registry[key] = entry
        entry.users += 1

    started = time.monotonic()
    with entry.lock:
        waited = time.monotonic() - started
        if waited >= _SLOW_WAIT_SECONDS:
            logger.debug("file lock wait: {} {:.0f}ms", key, waited * 1000)
        try:
            yield
        finally:
            with _registry_guard:
                entry.users -= 1
                if entry.users <= 0:
                    _registry.pop(key, None)
