"""Windows Git Bash detection — POSIX syntax on a Windows host.

Sherry's unsandboxed fallback spawns through the platform shell: ``/bin/sh -c``
on POSIX (every POSIX shell is a superset of what the tool promises) but
``cmd.exe /c`` on Windows, where ``&`` chains instead of backgrounding, ``&&``
does not short-circuit, ``$()`` does not exist and ``2>&1`` means something else.
A Git-for-Windows install ships a real ``bash.exe``; using it hands the model the
same shell contract on every platform.

Resolution mirrors ZCode's ``bash-shell-provider``: the two standard install
locations first, then inferred from ``git.exe`` on PATH (``<git>/../bin`` and
``<git>/../../bin``). The result is cached for the process lifetime — a shell
does not move under a running server — and a host without Git Bash gets ``None``,
which callers treat as "keep the platform default".
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

from loguru import logger

__all__ = ["resolve_windows_bash", "reset_windows_bash_cache"]

#: Standard Git-for-Windows install locations (64-bit first).
_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

_cached_bash: str | None = None
_cache_filled = False


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_windows_bash() -> str | None:
    """Absolute path to a usable ``bash.exe``, or ``None``.

    ``None`` on every non-Windows host (POSIX already has ``/bin/sh``) and on a
    Windows host without Git Bash. Cached after the first call.
    """
    global _cached_bash, _cache_filled
    if _cache_filled:
        return _cached_bash
    _cache_filled = True
    if platform.system() != "Windows":
        return None
    for candidate in _GIT_BASH_CANDIDATES:
        if _is_executable(candidate):
            _cached_bash = candidate
            logger.debug("shell_resolver: using Git Bash at {}", candidate)
            return candidate
    git_exe = shutil.which("git")
    if git_exe:
        git_dir = Path(git_exe).resolve().parent
        for relative in ("../bin/bash.exe", "../../bin/bash.exe"):
            inferred = str((git_dir / relative).resolve())
            if _is_executable(inferred):
                _cached_bash = inferred
                logger.debug("shell_resolver: using Git Bash inferred from git.exe at {}", inferred)
                return inferred
    logger.debug("shell_resolver: no Git Bash found; the platform shell stays in use")
    return None


def reset_windows_bash_cache() -> None:
    """Forget the cached probe (tests; a host does not change its shell)."""
    global _cached_bash, _cache_filled
    _cached_bash = None
    _cache_filled = False
