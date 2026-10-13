"""Shared fd-direct-write output plumbing for the exec tools.

Why a child's stdout goes to a FILE and never a pipe:

* nothing oversized rides the parent's heap — the kernel writes the child's
  bytes to disk and the parent only previews them;
* a pipe must be drained to EOF, and a grandchild that inherited the write end
  keeps it open forever (an unbounded ``communicate()`` then hangs the caller).
  With a file, ``wait()`` depends only on the direct child's exit.

Used by ``terminal`` (foreground + background), ``python_repl``, the operator
console (``server/service/user_terminal_service.py``) and ``ptc``'s runner. The
prefix and the evicted file's name are parameters so each caller keeps its own
naming contract: ``terminal``/``python_repl``/``ptc`` sweep temp files with the
shared ``.sherry-tmp-`` prefix, while a background task keeps its file for later
reads instead of evicting it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from agent.tools.pub_base.atomic_write import sweep_stale_temp_files
from config.features import TOOLS_TIMEOUTS
from pub.func.message.eviction import build_file_preview, get_eviction_dir

__all__ = [
    "FD_WRITE_THRESHOLD",
    "discard_stdout_tmp",
    "open_stdout_tmp",
    "read_or_evict_output",
]

#: Output at or above this many bytes never rides the Python heap: the child
#: writes straight to a temp file and only a head/tail preview comes back (the
#: file itself moves into the session's ``evicted/`` directory). Below it the
#: bytes are read back whole — the historical behaviour, and the only sane one
#: for the small outputs nearly every command produces. 0 (or less) disables
#: eviction entirely: every output is read back in memory.
FD_WRITE_THRESHOLD = TOOLS_TIMEOUTS["fd_write_threshold_bytes"]

#: Prefix for every fd-direct-write temp file. The sweep recognises this prefix
#: (``.swp`` / ``.log`` suffixes) and removes abandoned leftovers, so a hard kill
#: cannot accumulate them (see ``agent/tools/pub_base/atomic_write.py``).
_STDOUT_TMP_PREFIX = ".sherry-tmp-stdout-"
_STDOUT_TMP_SUFFIX = ".log"

#: Bytes hashed for the evicted file's name — hashing the whole payload would
#: read it all back, which is exactly what the fd path exists to avoid.
_EVICT_NAME_HASH_BYTES = 65_536


def open_stdout_tmp(
    session_id: str,
    *,
    prefix: str = _STDOUT_TMP_PREFIX,
    suffix: str = _STDOUT_TMP_SUFFIX,
) -> tuple[Path, Any]:
    """Create the fd-direct-write output file; return ``(path, handle)``.

    The file lives in the SESSION directory whenever the id is a safe path
    segment, so a later ``rename`` into ``evicted/`` is a same-filesystem move;
    an unsafe/empty id falls back to the OS temp directory (where the move
    degrades to a streamed copy). Abandoned leftovers from a hard kill are
    swept here — the same prefix contract the atomic writes use.
    """
    from config import SESSIONS_DIR
    from config.path import is_safe_session_segment

    directory: str | None = None
    if session_id and is_safe_session_segment(session_id):
        candidate = Path(SESSIONS_DIR) / session_id
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            sweep_stale_temp_files(candidate)
            directory = str(candidate)
        except OSError:
            directory = None
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    return Path(name), os.fdopen(fd, "wb")


def read_or_evict_output(
    tmp_path: Path,
    session_id: str,
    encoding: str,
    *,
    evict_prefix: str = "terminal-",
) -> str:
    """Return an exec tool's output: read whole under the threshold, else preview.

    Over the threshold the file is MOVED into the session's ``evicted/``
    directory (a same-filesystem rename when the temp file was created there)
    and only a head/tail preview is returned — the payload never rides the
    Python heap. Every failure path degrades to reading the file, because
    losing a command's output is worse than one oversized read.
    """
    try:
        size = tmp_path.stat().st_size
    except OSError:
        return ""
    if FD_WRITE_THRESHOLD <= 0 or size < FD_WRITE_THRESHOLD:
        data = b""
        try:
            data = tmp_path.read_bytes()
        except OSError:
            logger.debug("stdout_fd: temp file read failed", exc_info=True)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("stdout_fd: temp file unlink failed", exc_info=True)
        return data.decode(encoding, errors="replace")

    evict_dir = get_eviction_dir(session_id) if session_id else None
    if evict_dir is not None:
        try:
            with tmp_path.open("rb") as handle:
                digest = hashlib.sha256(handle.read(_EVICT_NAME_HASH_BYTES)).hexdigest()[:16]
            dest = evict_dir / f"{evict_prefix}{digest}.log"
            try:
                tmp_path.rename(dest)
            except OSError:
                # Cross-filesystem (OS temp dir fallback): stream, never read whole.
                shutil.copyfile(tmp_path, dest)
                tmp_path.unlink(missing_ok=True)
            logger.debug(
                "stdout_fd: output {} bytes > {} — evicted to {}",
                size,
                FD_WRITE_THRESHOLD,
                dest,
            )
            return build_file_preview(dest, dest)
        except OSError:
            logger.warning("stdout_fd: eviction failed; reading it back", exc_info=True)

    data = b""
    try:
        data = tmp_path.read_bytes()
    except OSError:
        logger.debug("stdout_fd: temp file read failed", exc_info=True)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("stdout_fd: temp file unlink failed", exc_info=True)
    return data.decode(encoding, errors="replace")


def discard_stdout_tmp(handle: Any, path: Path | None) -> None:
    """Close + unlink the fd output file on a failure path (never raises)."""
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
