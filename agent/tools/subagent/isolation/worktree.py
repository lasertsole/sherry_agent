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
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.features import SUBAGENT_ISOLATION

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
    """Make *project_root* a repository root, initializing one when needed.

    An existing repository (whose root IS the project root) is left completely
    alone. A fresh one gets an ``.git/info/exclude`` carrying the configured
    deny patterns BEFORE the baseline commit, then one commit, then the marker
    file that documents how to undo the initialization.

    :returns: True when this call initialized the repository.
    :raises OSError: ``git`` is missing or a step failed.
    """
    toplevel = _repo_toplevel(project_root)
    if toplevel is not None and Path(toplevel).resolve() == project_root.resolve():
        return False

    logger.info("Isolation requires a repository; initializing one at {}", project_root)
    run_git(project_root, ["init", "-q"], check=True)

    # The exclude file must exist BEFORE `git add -A`: what git ever commits
    # stays in the object database even after the file is deleted.
    exclude = project_root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    header = "# sherry isolation baseline: never committed (secrets, bulk, repository state)"
    lines = [header, *SUBAGENT_ISOLATION["deny_patterns"]]
    exclude.write_text(
        existing
        + ("\n" if existing and not existing.endswith("\n") else "")
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )

    run_git(project_root, ["add", "-A"], check=True)
    run_git(
        project_root,
        [
            "-c",
            "user.name=sherry-isolation",
            "-c",
            "user.email=sherry-isolation@localhost",
            "commit",
            "-q",
            "-m",
            SUBAGENT_ISOLATION["baseline_commit_message"],
        ],
        check=True,
    )

    marker = project_root / SUBAGENT_ISOLATION["repo_marker_name"]
    marker.write_text(
        "\n".join(
            [
                "This .git directory was created by sherry for subagent workspace isolation.",
                f"created_at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "sherry never pushes, never adds a remote, and never rewrites history.",
                "To undo: stop any running subagent, then delete this file and the .git directory:",
                f"    rm -rf {project_root / '.git'} {marker}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return True


def baseline_revision(project_root: Path) -> str:
    """The revision an isolated run should start from.

    A dirty working tree becomes a stash commit (``git stash create`` leaves the
    stash stack alone), so the child sees the user's uncommitted edits; a clean
    tree simply starts from HEAD.

    :raises OSError: The project has neither uncommitted changes nor a commit.
    """
    dirty = run_git(project_root, ["stash", "create"])
    if dirty.returncode == 0 and dirty.stdout.strip():
        return dirty.stdout.strip()
    head = run_git(project_root, ["rev-parse", "HEAD"])
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    raise OSError(f"project has no baseline revision: {project_root}")


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
