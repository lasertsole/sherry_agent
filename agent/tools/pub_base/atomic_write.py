"""Atomic, symlink-refusing writes for the file tools.

The file tools must keep ``_open_no_follow``'s policy — a symlinked final
component is REFUSED, never followed — while still writing crash-safely. That
rules out ``pub/func/atomic_replace.py``, which deliberately resolves a symlink
target and writes THROUGH it (it exists to preserve managed deployments that
symlink their config); here the semantics are the opposite, so the helper is
implemented separately.

Two mechanisms, matching the plan ``TODO/实施计划_文件写入并发保护.md``:

* **Atomicity (R1)** — a same-directory temporary file is written, flushed and
  ``fsync``-ed, its mode copied from the target (a script's execute bit must
  survive), then ``os.replace`` swaps it into place. A reader therefore sees the
  old content or the new content, never a torn write, and a crash mid-write
  cannot truncate the target. ``os.replace`` does not follow symlinks, so a
  target swapped for a link between the check and the swap cannot redirect the
  write either. When the filesystem refuses the rename (rare: some mounts), the
  helper degrades to an in-place write — availability over atomicity, logged.
* **A write-time revision precondition (R2, second layer)** — callers that
  probed the file first pass ``expected_revision`` (``mtime:<int ms>:size:<n>``);
  the helper re-asserts it immediately before the replace, narrowing the
  check-to-write window to the minimum. Unchanged content is never a false
  conflict: the tools compare content hashes before deciding.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
from pathlib import Path

from loguru import logger

from .path_utils import _open_no_follow

__all__ = [
    "StaleWriteError",
    "atomic_write_text_no_follow",
    "file_revision",
    "read_bytes_no_follow",
    "revision_id",
]

#: Prefix/suffix of the same-directory temporary file. The dot keeps it out of
#: globs; the name is recognisable for the stale-file sweep.
_TMP_PREFIX = ".sherry-tmp-"
_TMP_SUFFIX = ".swp"

#: ``os.replace`` behind a module-level name: tests fault-inject the rename
#: failure that triggers the in-place fallback (some mounts refuse it).
_replace = os.replace


class StaleWriteError(Exception):
    """The target changed after it was probed, so the write was refused.

    Carries the path; the tool layer turns it into the actionable ``changed
    since it was read`` error (R4) so the model re-reads and retries instead of
    silently overwriting the other writer's work.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"File changed since it was read: {path}")
        self.path = path


def revision_id(stat_result: os.stat_result | None) -> str:
    """Cheap change token for a target: ``mtime:<int ms>:size:<bytes>``.

    Mirrors ZCode's ``revisionId``: integer milliseconds, so a filesystem that
    reports sub-millisecond precision cannot make an untouched file look
    changed. ``absent`` means "the path does not exist".
    """
    if stat_result is None:
        return "absent"
    return f"mtime:{int(stat_result.st_mtime_ns // 1_000_000)}:size:{stat_result.st_size}"


def file_revision(path: Path) -> str:
    """Current revision of *path* (``absent`` when it does not exist)."""
    try:
        return revision_id(path.lstat())
    except FileNotFoundError:
        return "absent"


def read_bytes_no_follow(path: Path) -> tuple[bytes, os.stat_result]:
    """Read *path* in binary through ``_open_no_follow``, with its fstat.

    The stat comes from the SAME descriptor as the bytes, so a caller that
    records a fingerprint is describing exactly the content it read, not a
    same-named file that raced in between.
    """
    fd = _open_no_follow(path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        chunks: list[bytes] = []
        with os.fdopen(fd, "rb") as f:
            fd = -1
            while chunk := f.read(1 << 20):
                chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        if fd >= 0:
            os.close(fd)


def _write_in_place_no_follow(path: Path, text: str) -> None:
    """Fallback used when the temp-file + rename path is unsupported.

    Keeps the ``O_NOFOLLOW`` refusal; trades atomicity for availability. Only
    reachable from :func:`atomic_write_text_no_follow`'s rename-failure branch.
    """
    fd = _open_no_follow(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            fd = -1
            f.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def atomic_write_text_no_follow(
    path: Path,
    text: str,
    expected_revision: str | None = None,
) -> None:
    """Write *text* to *path* atomically, refusing symlinks.

    :param path: Resolved target (callers already passed their own path gates).
    :param text: Full new content.
    :param expected_revision: When given, the target must still carry this
        revision (:func:`revision_id`) — asserted before the temporary file is
        created AND again right before the replace.
    :raises StaleWriteError: The target changed after the caller probed it.
    :raises OSError: ``ELOOP`` for a symlinked target (the established policy),
        or any I/O failure. The target is left untouched on every failure path.
    """
    target = path.lstat() if path.exists() or path.is_symlink() else None
    if target is not None and stat.S_ISLNK(target.st_mode):
        raise OSError(errno.ELOOP, "Symbolic link not allowed")
    if expected_revision is not None and revision_id(target) != expected_revision:
        raise StaleWriteError(path)
    mode = stat.S_IMODE(target.st_mode) if target is not None else 0o644

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        if expected_revision is not None:
            # The content is on disk but not yet visible: re-assert here so the
            # window between the caller's probe and this replace stays minimal.
            if file_revision(path) != expected_revision:
                raise StaleWriteError(path)
        try:
            _replace(tmp_path, path)
        except OSError as exc:
            logger.warning(
                "atomic replace failed for {} ({}); falling back to an in-place write",
                path,
                exc,
            )
            _write_in_place_no_follow(path, text)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
