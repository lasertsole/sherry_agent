"""Create and discard the worktree an isolated subagent works in.

The run's tree is a ``git worktree`` of the parent project, cut from a DIRTY
baseline (``git stash create``) so the child starts from exactly what the user's
working tree holds; ignored paths a checkout cannot carry are materialized
afterwards (``materialize.py``). A manifest records every regular file with its
revision at creation time — because the merge back (``merge.py``) needs to know,
per file, whether the parent changed since (conflict) or the child changed
(apply) — plus the baseline revision and branch, so the worktree can be
unregistered when the run ends.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.path import isolated_workspaces_dir
from agent.tools.pub_base import revision_id

from .materialize import copy_untracked_files, materialize_ignored_paths
from .skipnames import (
    IGNORED_DIR_NAMES as _IGNORED_DIR_NAMES,
    ignored_dir_names as ignored_dir_names,
)
from .worktree import create_worktree, remove_worktree

#: Repository state that materializes as a FILE (a linked worktree's ``.git``):
#: skipped by name wherever it appears.
_VCS_FILE_NAMES = frozenset({".git"})

__all__ = [
    "IsolatedWorkspace",
    "SYMLINK_PREFIX",
    "create_isolated_workspace",
    "discard_isolated_workspace",
    "ignored_dir_names",
    "isolated_workspace_meta",
    "materialized_paths",
    "scan_manifest",
]

#: Marker used in place of a revision for a symlink path. The link TARGET is
#: the value, so retargeting a link is a change the merge can see — while the
#: merge still never writes through one.
SYMLINK_PREFIX = "symlink:"

#: Manifest file name inside the workspace directory; after a merge that kept
#: the tree (conflicts), the manifest is renamed to ``MERGED_NAME`` so a re-run
#: of the announce flow can never merge the same tree twice.
SNAPSHOT_NAME = "snapshot.json"
MERGED_NAME = "merged.json"
#: The child's project directory, relative to the workspace directory.
TREE_DIRNAME = "tree"
#: Manifest schema version (a future format change refuses old manifests).
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class IsolatedWorkspace:
    """One isolated workspace on disk."""

    meta_dir: Path
    tree: Path
    parent_root: Path


def _slug(child_session_key: str) -> str:
    """Filesystem-safe directory name for a child session key (keys carry colons)."""
    digest = hashlib.sha256(child_session_key.encode("utf-8")).hexdigest()[:16]
    return f"iso-{digest}"


def isolated_workspace_meta(spawned_cwd: str | Path | None) -> Path | None:
    """The workspace directory behind a child's cwd, or ``None`` when not isolated.

    The child's project directory IS ``<workspace>/tree``, so the marker file
    beside it is what tells the merge that this run was isolated — no registry
    schema, no state key, nothing to keep in sync.
    """
    if not spawned_cwd:
        return None
    tree = Path(spawned_cwd)
    if tree.name != TREE_DIRNAME:
        return None
    meta_dir = tree.parent
    if not (meta_dir / SNAPSHOT_NAME).is_file():
        return None
    return meta_dir


def _link_revision(path: Path) -> str:
    """Manifest value for a symlink: the marker plus its target."""
    try:
        return SYMLINK_PREFIX + os.readlink(path)
    except OSError:
        return SYMLINK_PREFIX


def scan_manifest(root: Path) -> dict[str, str]:
    """``{relative path: revision}`` for every regular file and symlink under *root*.

    Symlinks are listed with a :data:`SYMLINK_PREFIX` value holding their
    target rather than a real revision: the merge must know that a path IS a
    link (so it never writes through one, and never mistakes a link for a
    deletion). Sockets, devices and the ignored directories are not listed at
    all.
    """
    manifest: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIR_NAMES]
        base = Path(dirpath)
        for name in filenames:
            if name in _VCS_FILE_NAMES:
                # A worktree's `.git` is a FILE (a link back to the parent's
                # repository), so the directory-name filter above does not catch
                # it; it is repository state, never a merge candidate.
                continue
            path = base / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                manifest[str(path.relative_to(root))] = _link_revision(path)
            elif stat.S_ISREG(info.st_mode):
                manifest[str(path.relative_to(root))] = revision_id(info)
    return manifest


def create_isolated_workspace(parent_root: Path, child_session_key: str) -> IsolatedWorkspace:
    """Cut a worktree of *parent_root* for one run and materialize its ignored paths.

    :param parent_root: The parent's project directory (must exist).
    :param child_session_key: The child's session key (names the workspace).
    :raises OSError: The worktree could not be created — the caller refuses the
        spawn rather than running the child in the shared tree by accident.
    """
    root = Path(parent_root).resolve()
    if not root.is_dir():
        raise OSError(f"isolation requires an existing project directory: {root}")

    meta_dir = isolated_workspaces_dir() / _slug(child_session_key)
    if meta_dir.exists():
        # A retry of the same spawn (or a crash's leftover): start clean, the
        # previous tree is never merged implicitly.
        shutil.rmtree(meta_dir, ignore_errors=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    tree = meta_dir / TREE_DIRNAME

    created = create_worktree(root, tree, child_session_key)
    materialized = materialize_ignored_paths(root, tree)
    # `git stash create` never carries untracked files: without them the child
    # would not see a file the user just created.
    untracked = copy_untracked_files(root, tree)
    _align_mtimes_with_parent(root, tree)
    manifest = scan_manifest(tree)
    (meta_dir / SNAPSHOT_NAME).write_text(
        json.dumps(
            {
                "version": MANIFEST_VERSION,
                "backend": "worktree",
                "parent_root": str(root),
                "child_session_key": child_session_key,
                "created_at": time.time(),
                "base_sha": created.base_sha,
                "branch": created.branch,
                "initialized_repo": created.initialized_repo,
                "materialized": materialized,
                "untracked": untracked,
                "revisions": manifest,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Isolated worktree created: child={} parent={} base={} files={} materialized={}",
        child_session_key,
        root,
        created.base_sha[:12],
        len(manifest),
        len(materialized),
    )
    return IsolatedWorkspace(meta_dir=meta_dir, tree=tree, parent_root=root)


def _align_mtimes_with_parent(root: Path, tree: Path) -> None:
    """Give every checked-out file its parent counterpart's mtime.

    A revision is ``mtime:ms:size``, and a checkout stamps its own mtimes — so
    without this step every file the child never touched would read as CHANGED
    at merge time. Aligning the times makes the tree a true replica of the
    parent (what the merge's per-file CAS assumes), while a child's edit still
    moves the revision through its own write.
    """
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIR_NAMES]
        base = Path(dirpath)
        for name in filenames:
            if name in _VCS_FILE_NAMES:
                continue
            candidate = base / name
            counterpart = root / candidate.relative_to(tree)
            try:
                if candidate.is_symlink() or not counterpart.is_file():
                    continue
                info = counterpart.stat()
                os.utime(candidate, (info.st_atime, info.st_mtime), follow_symlinks=False)
            except OSError:
                continue


def _manifest_meta(meta_dir: Path) -> dict[str, object]:
    """Read a workspace manifest (``{}`` when it is unreadable)."""
    try:
        return dict(json.loads((meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def materialized_paths(project_root: str | Path) -> frozenset[str]:
    """Paths a run's manifest materialized into its tree (empty when not isolated).

    Used by the external-path gate: a read that goes THROUGH one of these links
    leaves the isolated tree on purpose, so it is not an "external file" read.
    """
    meta_dir = isolated_workspace_meta(project_root)
    if meta_dir is None:
        return frozenset()
    raw = _manifest_meta(meta_dir).get("materialized") or []
    return frozenset(str(entry) for entry in raw)


def discard_isolated_workspace(meta_dir: Path) -> None:
    """Remove a workspace directory (but never the parent tree).

    The worktree registration and its branch belong to the parent repository, so
    they are dropped first — deleting the directory alone would leave a stale
    ``.git/worktrees`` entry (and a stray branch) behind for every run.
    """
    meta = _manifest_meta(meta_dir)
    parent_root = meta.get("parent_root")
    branch = str(meta.get("branch") or "")
    if parent_root and branch:
        remove_worktree(Path(str(parent_root)), meta_dir / TREE_DIRNAME, branch)
    shutil.rmtree(meta_dir, ignore_errors=True)
