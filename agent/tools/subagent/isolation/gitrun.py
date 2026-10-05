"""Thin git runner for the isolation backend.

Every call is bounded (a wedged git must not hold a spawn) and never raises for
a non-zero exit: the callers classify the result and refuse the isolation
instead of crashing the spawn.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from loguru import logger

from config.features import SUBAGENT_ISOLATION

__all__ = ["git_available", "run_git"]


def git_available() -> bool:
    """True when a ``git`` binary is on PATH (isolation needs one)."""
    return shutil.which("git") is not None


def run_git(
    cwd: Path,
    args: list[str],
    *,
    timeout_s: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in *cwd* with a bounded timeout.

    :param cwd: Repository (or project) directory to run in.
    :param args: Arguments after ``git`` (e.g. ``["worktree", "add", ...]``).
    :param timeout_s: Seconds before the call is killed (config default).
    :param check: Raise ``subprocess.CalledProcessError`` on a non-zero exit.
    :returns: The completed process (stdout/stderr captured as text).
    """
    timeout = SUBAGENT_ISOLATION["git_timeout_s"] if timeout_s is None else timeout_s
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        logger.debug(
            "git {} failed (rc={}): {}",
            " ".join(args[:3]),
            completed.returncode,
            (completed.stderr or "").strip()[:200],
        )
        if check:
            raise subprocess.CalledProcessError(
                completed.returncode, completed.args, completed.stdout, completed.stderr
            )
    return completed
