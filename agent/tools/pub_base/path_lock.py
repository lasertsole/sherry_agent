"""Per-path in-process read/write gates for the file tools.

The main agent and every subagent share ONE process and the SAME
``build_main_tools()`` tool singletons, so two of them can be inside
``patch_file``'s read-modify-write on the same file at the same time. The tool
lane caps how MANY agents run, never WHICH file they touch — this gate is the
missing piece.

Modes:

* :func:`path_write_lock` — exclusive (the historical ``path_lock``);
* :func:`path_read_lock` — shared; several readers of one path may proceed.

Writers are preferred: once a writer is waiting, new readers queue behind it,
so a stream of reads cannot starve a write. The wait is a plain
``threading.Condition``: the file tools' ``_core`` is synchronous and runs
through ``asyncio.to_thread``, and the critical section never awaits. The
registry is reference-counted and entries are dropped when the last user
leaves, so a long-lived process does not accumulate one entry per file it ever
touched.

Lock ORDER is fixed and documented once: in-process path gate first, then the
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

__all__ = [
    "active_path_locks",
    "path_lock",
    "path_read_lock",
    "path_write_lock",
]

#: Emit a debug line when a caller had to wait at least this long.
_SLOW_WAIT_SECONDS = 0.05


@dataclass
class _LockEntry:
    """One path's read/write gate plus its live user count."""

    condition: threading.Condition = field(default_factory=threading.Condition)
    readers: int = 0
    writer: bool = False
    waiting_writers: int = 0
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


def _entry(path: Path) -> tuple[str, _LockEntry]:
    key = str(path)
    with _registry_guard:
        entry = _registry.get(key)
        if entry is None:
            entry = _LockEntry()
            _registry[key] = entry
        entry.users += 1
    return key, entry


def _release_entry(key: str, entry: _LockEntry) -> None:
    with _registry_guard:
        entry.users -= 1
        if entry.users <= 0:
            _registry.pop(key, None)


@contextlib.contextmanager
def _path_gate(path: Path, *, write: bool) -> Iterator[None]:
    key, entry = _entry(path)
    acquired = False
    started = time.monotonic()
    try:
        with entry.condition:
            if write:
                entry.waiting_writers += 1
                try:
                    while entry.writer or entry.readers > 0:
                        entry.condition.wait()
                finally:
                    entry.waiting_writers -= 1
                entry.writer = True
            else:
                # Writer preference: a pending writer blocks NEW readers.
                while entry.writer or entry.waiting_writers > 0:
                    entry.condition.wait()
                entry.readers += 1
            acquired = True
        waited = time.monotonic() - started
        if waited >= _SLOW_WAIT_SECONDS:
            logger.debug("file lock wait: {} {:.0f}ms", key, waited * 1000)
        yield
    finally:
        if acquired:
            with entry.condition:
                if write:
                    entry.writer = False
                else:
                    entry.readers -= 1
                entry.condition.notify_all()
        _release_entry(key, entry)


@contextlib.contextmanager
def path_write_lock(path: Path) -> Iterator[None]:
    """Exclusive per-path gate (the historical ``path_lock``)."""
    with _path_gate(path, write=True):
        yield


@contextlib.contextmanager
def path_read_lock(path: Path) -> Iterator[None]:
    """Shared per-path gate; writers wait for all readers, readers wait for writers."""
    with _path_gate(path, write=False):
        yield


#: Historical name used by ``file_write_lock`` and its tests.
path_lock = path_write_lock
