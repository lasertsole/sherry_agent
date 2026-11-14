"""Atomic, symlink-refusing writes for the file tools.

The file tools must keep ``_open_no_follow``'s policy — symlinks are REFUSED at
EVERY component, never followed — while still writing crash-safely. On POSIX the
parent directory is opened component-by-component with ``O_NOFOLLOW`` and every
subsequent call (sweep, stat, temp create, replace, in-place fallback) rides that
ONE verified dir fd, so a directory swapped for a symlink after path validation
cannot redirect the write. That
rules out ``pub/func/atomic_replace.py``, which deliberately resolves a symlink
target and writes THROUGH it (it exists to preserve managed deployments that
symlink their config); here the semantics are the opposite, so the helper is
implemented separately.

Three mechanisms:

* **Atomicity (R1)** — a same-directory temporary file is written, flushed and
  ``fsync``-ed, its mode copied from the target (a script's execute bit must
  survive), then ``os.replace`` swaps it into place. A reader therefore sees the
  old content or the new content, never a torn write, and a crash mid-write
  cannot truncate the target. In the secure path ``os.replace`` is relative to
  the verified parent dir fd (the legacy fallback relies on ``os.replace`` not
  following a final symlink), so a target swapped for a link between the check
  and the swap cannot redirect the write either. When the filesystem refuses the
  rename (rare: some mounts), the helper degrades to an in-place write —
  availability over atomicity, logged.
* **A write-time revision precondition (R2, second layer)** — callers that
  probed the file first pass ``expected_revision`` (``mtime:<int ms>:size:<n>``);
  the helper re-asserts it immediately before the replace, narrowing the
  check-to-write window to the minimum. Unchanged content is never a false
  conflict: the tools compare content hashes before deciding.
* **Leftover sweeping** — a hard kill between the temporary file's creation and
  the replace leaves it behind; every failure path unlinks it, so only SIGKILL
  or power loss can. The next write into the same directory sweeps those
  (:func:`sweep_stale_temp_files`), which is why the prefix is fixed and dotted.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path

from loguru import logger

from .path_utils import _can_walk_beneath, _open_dir_no_symlinks, _open_no_follow

__all__ = [
    "StaleWriteError",
    "atomic_write_bytes_no_follow",
    "atomic_write_text_no_follow",
    "file_revision",
    "read_bytes_no_follow",
    "revision_id",
    "sweep_stale_temp_files",
]

#: Prefix/suffixes of the temporary files this sweep recognises. The dot keeps
#: them out of globs; the PREFIX is the contract. ``.swp`` is an atomic write's
#: in-flight copy; ``.log`` is the terminal tool's fd-direct-write stdout (the
#: child writes straight into it, and a hard kill can leave it behind).
_TMP_PREFIX = ".sherry-tmp-"
_TMP_SUFFIX = ".swp"
_TMP_SUFFIXES = (_TMP_SUFFIX, ".log")

#: Age at which a leftover temporary file counts as abandoned. Every failure
#: path inside the write helper unlinks its temporary file, so only a hard kill
#: (SIGKILL, power loss) can leave one behind — and a file this old cannot
#: belong to a write still in flight (a live one is seconds old at most).
_STALE_TMP_AGE_S = 60 * 60.0

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


def _write_bytes_in_place_no_follow(path: Path, data: bytes) -> None:
    """Legacy-path fallback used when the temp-file + rename path is unsupported.

    Keeps the final-component ``O_NOFOLLOW`` refusal; trades atomicity for
    availability. Only reachable from :func:`_atomic_write_bytes_legacy`'s
    rename-failure branch (the secure path has its own dir-fd version).
    """
    fd = _open_no_follow(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def _sweep_stale_temp_files_at(dir_fd: int, *, now: float | None = None) -> int:
    """fd-based twin of :func:`sweep_stale_temp_files` (no path re-resolution)."""
    removed = 0
    current = time.time() if now is None else now
    try:
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                if not (entry.name.startswith(_TMP_PREFIX) and entry.name.endswith(_TMP_SUFFIXES)):
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=dir_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if current - info.st_mtime < _STALE_TMP_AGE_S:
                        continue
                    os.unlink(entry.name, dir_fd=dir_fd)
                    removed += 1
                except OSError:
                    continue
    except OSError:
        return removed
    if removed:
        logger.debug("swept {} stale temp file(s) via dir fd", removed)
    return removed


def _sweep_stale_temp_files_legacy(directory: Path, *, now: float | None = None) -> int:
    """Path-based sweep (platforms without a secure dir walk)."""
    removed = 0
    current = time.time() if now is None else now
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (entry.name.startswith(_TMP_PREFIX) and entry.name.endswith(_TMP_SUFFIXES)):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if current - info.st_mtime < _STALE_TMP_AGE_S:
                        continue
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    continue
    except OSError:
        return removed
    if removed:
        logger.debug("swept {} stale temp file(s) in {}", removed, directory)
    return removed


def sweep_stale_temp_files(directory: Path, *, now: float | None = None) -> int:
    """Remove abandoned ``.sherry-tmp-*`` (``.swp`` / ``.log``) files from *directory*.

    Called on the way into every write, so a directory a writer keeps returning
    to cleans itself one entry at a time; no startup hook or background task is
    involved. Best-effort by design — never raises, and a failing scan cannot
    fail the write it precedes. Only regular files past ``_STALE_TMP_AGE_S`` are
    removed, so a slow-but-live writer (or a symlink someone parked under the
    prefix) is left alone.

    :param directory: Directory to scan, normally the target's parent.
    :param now: Injectable clock for tests (defaults to ``time.time()``).
    :returns: How many files were removed.
    """
    target = Path(directory)
    if _can_walk_beneath() and target.is_absolute():
        try:
            with _open_dir_no_symlinks(target, readable=True) as dir_fd:
                return _sweep_stale_temp_files_at(dir_fd, now=now)
        except OSError:
            return 0
    return _sweep_stale_temp_files_legacy(target, now=now)


def _atomic_write_bytes_legacy(
    path: Path,
    data: bytes,
    expected_revision: str | None = None,
) -> None:
    """Path-based writer for platforms without a secure dir-fd walk.

    Write *data* to *path* atomically, refusing symlinks.

    :param path: Resolved target (callers already passed their own path gates).
    :param data: Full new content.
    :param expected_revision: When given, the target must still carry this
        revision (:func:`revision_id`) — asserted before the temporary file is
        created AND again right before the replace.
    :raises StaleWriteError: The target changed after the caller probed it.
    :raises OSError: ``ELOOP`` for a symlinked target (the established policy),
        or any I/O failure. The target is left untouched on every failure path.
    """
    sweep_stale_temp_files(path.parent)
    target = path.lstat() if path.exists() or path.is_symlink() else None
    if target is not None and stat.S_ISLNK(target.st_mode):
        raise OSError(errno.ELOOP, "Symbolic link not allowed")
    if expected_revision is not None and revision_id(target) != expected_revision:
        raise StaleWriteError(path)
    mode = stat.S_IMODE(target.st_mode) if target is not None else 0o644

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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
            _write_bytes_in_place_no_follow(path, data)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _create_temp_at(dir_fd: int) -> tuple[str, int]:
    """Create one unique ``.sherry-tmp-*.swp`` in the verified parent directory."""
    for _ in range(32):
        name = f"{_TMP_PREFIX}{uuid.uuid4().hex}{_TMP_SUFFIX}"
        try:
            return name, os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "could not create a unique temporary file")


def _write_bytes_in_place_at(dir_fd: int, name: str, data: bytes) -> None:
    """In-place fallback opened relative to the VERIFIED parent directory."""
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o644,
        dir_fd=dir_fd,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def _atomic_write_bytes_secure(
    path: Path,
    data: bytes,
    expected_revision: str | None,
) -> None:
    """Atomic write through one verified parent dir fd (POSIX TOCTOU closure).

    Every filesystem call — sweep, target stat, temp creation, the replace and
    the in-place fallback — is relative to ``dir_fd``, opened component-by-
    component with ``O_NOFOLLOW``. A directory swapped for a symlink after the
    path gates therefore fails with ELOOP instead of redirecting the write.
    """
    with _open_dir_no_symlinks(path.parent) as dir_fd:
        # The sweep needs a READABLE fd (scandir), while the write itself works
        # with O_PATH. Opening the readable twin separately keeps write-only
        # directories writable (their sweep is simply skipped, as before).
        try:
            with _open_dir_no_symlinks(path.parent, readable=True) as read_fd:
                _sweep_stale_temp_files_at(read_fd)
        except OSError:
            logger.debug("temp sweep skipped for {}", path.parent)
        try:
            target: os.stat_result | None = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            target = None
        if target is not None and stat.S_ISLNK(target.st_mode):
            raise OSError(errno.ELOOP, "Symbolic link not allowed")
        if expected_revision is not None and revision_id(target) != expected_revision:
            raise StaleWriteError(path)
        mode = stat.S_IMODE(target.st_mode) if target is not None else 0o644

        tmp_name, tmp_fd = _create_temp_at(dir_fd)
        try:
            os.fchmod(tmp_fd, mode)
            with os.fdopen(tmp_fd, "wb") as f:
                tmp_fd = -1
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if expected_revision is not None:
                # Content is on disk but not visible yet: keep the check-to-
                # replace window minimal (the path-based stat is fine here; a
                # swapped target also fails the revision compare).
                if file_revision(path) != expected_revision:
                    raise StaleWriteError(path)
            try:
                _replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except OSError as exc:
                logger.warning(
                    "atomic replace failed for {} ({}); falling back to an in-place write",
                    path,
                    exc,
                )
                _write_bytes_in_place_at(dir_fd, path.name, data)
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name, dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)
            raise
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)


def atomic_write_bytes_no_follow(
    path: Path,
    data: bytes,
    expected_revision: str | None = None,
) -> None:
    """Write *data* to *path* atomically, refusing symlinks in ANY component.

    POSIX uses :func:`_atomic_write_bytes_secure` (one verified parent dir fd);
    platforms without ``dir_fd`` fall back to the historical path-based writer.
    """
    target = Path(path)
    if _can_walk_beneath() and target.is_absolute():
        _atomic_write_bytes_secure(target, data, expected_revision)
        return
    _atomic_write_bytes_legacy(target, data, expected_revision)


def atomic_write_text_no_follow(
    path: Path,
    text: str,
    expected_revision: str | None = None,
) -> None:
    """UTF-8 :func:`atomic_write_bytes_no_follow` — the file tools' entry point."""
    atomic_write_bytes_no_follow(path, text.encode("utf-8"), expected_revision=expected_revision)
