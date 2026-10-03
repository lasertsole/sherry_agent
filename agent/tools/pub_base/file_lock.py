"""Cross-process advisory file lock (R7) and the combined write lock.

Same machinery the repo already used twice — a sidecar ``.lock`` file taken with
``fcntl.flock`` (``msvcrt.locking`` on Windows) — but in one place. The point of
``flock`` over a hand-rolled lock FILE is that the kernel releases it when the
holder exits or crashes: there is no dead-owner protocol to invent, no pid
reuse to defend against, and a ``kill -9`` needs no cleanup. (omo's
identity-bearing lock records exist because its runtime cannot rely on flock;
Python on a local filesystem can.)

Limits, stated where the code lives:

* **Advisory** — only writers that take the lock are serialized. An external
  editor never does; the CAS layers in the file tools are what protect against
  that class of change.
* **Local filesystems** — flock semantics are unreliable over NFS; this guards
  a local project tree, which is what Sherry's tools write.
* Lock files live under ``SRC_DIR/data/locks`` (never next to the sources), one
  per canonical target path.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path


from .path_lock import path_lock

__all__ = ["FileBusyError", "cross_process_lock", "file_write_lock", "flock_path", "lock_path_for"]

# fcntl is Unix-only; on Windows use msvcrt for file locking.
msvcrt = None
try:  # pragma: no cover - platform split
    import fcntl
except ImportError:  # pragma: no cover - Windows path
    fcntl = None
    try:
        import msvcrt
    except ImportError:  # noqa: S110
        pass

#: Poll interval while waiting for the lock, and the default wait budget.
_POLL_SECONDS = 0.05
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


class FileBusyError(Exception):
    """Another process held the lock for longer than the wait budget."""

    def __init__(self, path: Path, timeout_s: float) -> None:
        super().__init__(f"File is locked by another process (waited {timeout_s:.0f}s): {path}")
        self.path = path


def lock_path_for(target: Path) -> Path:
    """Sidecar lock path for *target* under the shared locks directory.

    Keyed by the canonical (``realpath``) target, so two spellings of one file —
    or a symlinked directory in the path — cannot take two different locks.
    """
    from config.path import file_locks_dir

    digest = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:32]
    return file_locks_dir() / f"{digest}.lock"


@contextlib.contextmanager
def flock_path(lock_path: Path, timeout_s: float | None) -> Iterator[None]:
    """Take the OS lock on *lock_path*, waiting at most ``timeout_s``.

    ``timeout_s=None`` waits indefinitely (the historical behaviour of the
    memory / skill-usage call sites, kept when they were folded in here); a
    number raises :class:`FileBusyError` once the budget is spent. On platforms
    with neither fcntl nor msvcrt the lock degrades to a no-op — exactly what
    the previous local copies did.
    """
    if fcntl is None and msvcrt is None:  # pragma: no cover - exotic platforms
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):  # pragma: no cover
        lock_path.write_text(" ", encoding="utf-8")

    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        while True:
            try:
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover - Windows path
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise FileBusyError(lock_path, timeout_s) from None
                time.sleep(_POLL_SECONDS)
        yield
    finally:
        with contextlib.suppress(OSError, ValueError):
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows path
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        fd.close()


@contextlib.contextmanager
def cross_process_lock(
    target: Path,
    timeout_s: float | None = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize writers of *target* across processes (advisory flock)."""
    with flock_path(lock_path_for(target), timeout_s):
        yield


@contextlib.contextmanager
def file_write_lock(
    target: Path,
    timeout_s: float | None = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """The write lock the file tools take: in-process path lock, then flock.

    One fixed order (thread lock → file lock) for every caller, so no two
    writers can deadlock. ``timeout_s`` only bounds the cross-process wait.
    """
    with path_lock(target), cross_process_lock(target, timeout_s):
        yield


def describe_lock_dirs() -> str:  # pragma: no cover - diagnostic helper
    """Where the lock files live (used in log lines and troubleshooting)."""
    from config.path import file_locks_dir

    return str(file_locks_dir())
