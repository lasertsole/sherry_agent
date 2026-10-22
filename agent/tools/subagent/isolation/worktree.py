"""The per-run git worktree backend of workspace isolation.

One isolated run = one `git worktree` of the parent project, created from a
DIRTY baseline (``git stash create``), so the child starts from exactly what the
user's working tree holds — committed or not. A project that is not a repository
is initialized first (baseline commit + a marker file + an exclude file that
keeps secrets and bulk out of the history it creates).

Measured facts this module is built around (see the plan's evidence table):

- an empty repository cannot host a worktree (``fatal: 无效引用：HEAD``), hence
  the mandatory baseline commit;
- ``git stash create`` captures MODIFIED files but NOT untracked ones, so the
  manifest is scanned from the resulting tree after materialization;
- ``git add -A`` in a fresh repository commits secrets unless an exclude file is
  in place FIRST — removing them afterwards cannot clean history.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path


from config.features import SUBAGENT_ISOLATION
from agent.tools.pub_base import shadow_git

from .gitrun import run_git

__all__ = [
    "WorktreeBase",
    "baseline_revision",
    "create_worktree",
    "ensure_repository",
    "remove_worktree",
    "run_slug",
]


@dataclass(frozen=True)
class WorktreeBase:
    """Where a run's worktree came from."""

    #: Revision the worktree was checked out from (a stash commit, or HEAD).
    base_sha: str
    #: The run's branch (``sherry/<run8>``).
    branch: str
    #: True when this call initialized the repository (auto-init happened).
    initialized_repo: bool


def run_slug(child_session_key: str, length: int = 8) -> str:
    """Stable short id for a child session key (keys carry colons)."""
    return hashlib.sha256(child_session_key.encode("utf-8")).hexdigest()[:length]


def _repo_toplevel(project_root: Path) -> str | None:
    """The repository root of *project_root*, or ``None`` when it is not a repo."""
    completed = run_git(project_root, ["rev-parse", "--show-toplevel"])
    if completed.returncode != 0:
        return None
    toplevel = completed.stdout.strip()
    return toplevel or None


def ensure_repository(project_root: Path) -> bool:
    """Shadow git needs no user repository; keep the name for callers.

    The shadow store is initialized lazily by the first snapshot. The user's
    ``.git`` is never created or modified here.
    """
    if shadow_git.shadow_head(project_root) is None:
        shadow_git.take_snapshot(project_root, "manual:isolation baseline")
    return False


def baseline_revision(project_root: Path) -> str:
    """The shadow chain's current tip (creating a first snapshot if needed)."""
    if shadow_git.shadow_head(project_root) is None:
        shadow_git.take_snapshot(project_root, "manual:isolation baseline")
    head = run_git(project_root, ["rev-parse", shadow_git.SHADOW_REF])
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        raise OSError(f"shadow git has no baseline for {project_root}")
    return sha


def create_worktree(project_root: Path, tree: Path, child_session_key: str) -> WorktreeBase:
    """Create (or re-create) the run's worktree at *tree*.

    A leftover from a crashed attempt is discarded first: the previous tree is
    never reused and never merged implicitly.

    :raises OSError: Any git step failed.
    """
    slug = run_slug(child_session_key)
    branch = f"{SUBAGENT_ISOLATION['branch_prefix']}{slug}"
    initialized = ensure_repository(project_root)
    base_sha = baseline_revision(project_root)

    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
    run_git(project_root, ["worktree", "prune"])
    run_git(project_root, ["branch", "-D", branch])

    completed = run_git(
        project_root,
        ["worktree", "add", "-b", branch, str(tree), base_sha],
    )
    if completed.returncode != 0:
        raise OSError(f"git worktree add failed: {(completed.stderr or '').strip()[:200]}")
    return WorktreeBase(base_sha=base_sha, branch=branch, initialized_repo=initialized)


def remove_worktree(project_root: Path, tree: Path, branch: str) -> None:
    """Unregister and delete a run's worktree, then drop its branch.

    Best-effort by design: the caller is discarding the workspace anyway, and a
    leftover registration is cleaned by ``git worktree prune`` on the next run.
    """
    run_git(project_root, ["worktree", "remove", "--force", str(tree)])
    run_git(project_root, ["branch", "-D", branch])
    run_git(project_root, ["worktree", "prune"])
