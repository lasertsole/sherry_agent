"""Ensure the session's project directory is a git repository before writes.

A session with a BOUND project directory writes into a version-controlled tree
or is refused: the first write checks ``git rev-parse --is-inside-work-tree``
(cheap, and true for a repo subdirectory or a linked worktree) and runs
``git init`` when the directory is not yet a repository. A failed init refuses
the write with a readable message — writes without version control are the
thing this gate exists to prevent, and silently proceeding would be the bug.

A session WITHOUT a bound project directory is left alone (its writes resolve
against the process default and behave exactly as before): the requirement is
"a chosen working directory gets git", not "every write gets git".

The check is cached per RESOLVED ROOT (not per session): a project-directory
switch simply misses the cache and re-probes, so no cross-layer invalidation
hook is needed and switching back to a known-good root stays O(1). A FAILED
init is never cached — the next write retries it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger

from .path_utils import session_workspace_root

__all__ = ["clear_git_ready", "ensure_git_for_writes", "git_ready_roots"]

#: Resolved project roots already verified/initialized this process.
_git_ready: set[str] = set()

#: Timeout for the probe / init subprocess calls (seconds).
_PROBE_TIMEOUT_S = 5.0
_INIT_TIMEOUT_S = 10.0


def git_ready_roots() -> set[str]:
    """The roots already verified this process (tests / diagnostics)."""
    return set(_git_ready)


def clear_git_ready(root: str | Path | None = None) -> None:
    """Forget a cached root (all of them when *root* is None).

    Keyed by root, so a project-directory switch does not need this — it is
    here for tests and for an explicit re-check (a caller that just removed
    ``.git`` itself). A root verified once is not re-probed for the life of the
    process: the cache is a per-process invariant by design (the alternative is
    a git subprocess per write).
    """
    if root is None:
        _git_ready.clear()
        return
    _git_ready.discard(str(root))


def ensure_git_for_writes(session_id: str) -> tuple[bool, str]:
    """Make sure the session's project directory is a git repository.

    Returns ``(True, "")`` when the write may proceed: no bound project
    directory, an existing repository (the root itself, a subdirectory of one,
    or a linked worktree), or a successful ``git init``. Returns
    ``(False, message)`` when the directory is not a repository and
    initializing it failed — the caller refuses the write and shows the
    message.
    """
    if not session_id:
        return True, ""
    root = session_workspace_root(session_id)
    if root is None:
        return True, ""  # No binding: writes behave exactly as before.
    root = Path(root)
    key = str(root)
    if key in _git_ready:
        return True, ""
    if not root.is_dir():
        # Missing root: the write tool produces its own error about the path.
        return True, ""

    if _is_git_repo(root):
        _git_ready.add(key)
        return True, ""

    ok, error = _git_init(root)
    if not ok:
        logger.error("git init failed in project dir {} (session {}): {}", root, session_id, error)
        return False, (
            f"Git initialization failed in the project directory ({root}): {error}. "
            "The write was refused — fix the git problem, or clear the session's "
            "project directory to write without git."
        )
    logger.info("git initialized in project dir {} (session {})", root, session_id)
    _git_ready.add(key)
    return True, ""


def _is_git_repo(path: Path) -> bool:
    """True when *path* is inside a git work tree (root, subdirectory or worktree).

    Any failure — git missing, timeout, a non-repository — reads as "not a
    repository" so the caller falls through to ``git init`` (which reports the
    real reason when it cannot run either).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_init(path: Path) -> tuple[bool, str]:
    """Run ``git init`` in *path*; returns ``(ok, error_message)``.

    Plain ``git init`` with git's own defaults: no ``--bare``, no
    ``--initial-branch``, no ``user.email``/``user.name`` handling — this gate
    only makes the directory a repository, it never configures or commits.
    """
    try:
        result = subprocess.run(
            ["git", "init", str(path)],
            capture_output=True,
            text=True,
            timeout=_INIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, "git init timed out"
    except FileNotFoundError:
        return False, "git is not installed (install git, or clear the project directory)"
    except OSError as error:
        return False, str(error)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "").strip() or (
            f"git init exited {result.returncode}"
        )
    return True, ""
