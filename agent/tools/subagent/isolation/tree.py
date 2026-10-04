"""Create and discard the isolated copy a subagent works in.

The copy carries a manifest — every regular file with its revision at copy time
— because the merge back (``merge.py``) needs to know, per file, whether the
parent changed since (conflict) or the child changed (apply). The manifest walk
skips the same heavy directories the copy skips, and symlinks are never
followed: a link is copied as a link and left alone by the merge.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.path import isolated_workspaces_dir
from agent.tools.pub_base import revision_id

__all__ = [
    "IsolatedWorkspace",
    "SYMLINK_PREFIX",
    "create_isolated_workspace",
    "discard_isolated_workspace",
    "ignored_dir_names",
    "isolated_workspace_meta",
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

#: Directories never copied and never merged: dependency caches and VCS state
#: are large, machine-specific, and meaningless to merge file by file.
_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
    }
)


def ignored_dir_names() -> frozenset[str]:
    """Directory names the copy and the merge both skip."""
    return _IGNORED_DIR_NAMES


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


def _prune_ignored(root: Path) -> None:
    """Remove every ignored directory from a freshly copied tree (never descends)."""
    for dirpath, dirnames, _files in os.walk(root, topdown=True, followlinks=False):
        base = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            if name in _IGNORED_DIR_NAMES:
                shutil.rmtree(base / name, ignore_errors=True)
            else:
                keep.append(name)
        dirnames[:] = keep


def _copy_tree(src: Path, dst: Path) -> str:
    """Copy *src* into *dst*; returns the method used (for the log line).

    ``rsync -a`` first (it applies the ignore list natively), then
    ``cp -a --reflink=auto`` (copy-on-write on btrfs/XFS, a plain C-speed copy
    elsewhere) with the ignored directories pruned afterwards, then
    ``shutil.copytree``. Symlinks are preserved as links on every path.
    """
    if shutil.which("rsync"):
        try:
            completed = subprocess.run(
                [
                    "rsync",
                    "-a",
                    *[f"--exclude={name}" for name in sorted(_IGNORED_DIR_NAMES)],
                    f"{src}{os.sep}",
                    str(dst),
                ],
                capture_output=True,
                timeout=600,
            )
            if completed.returncode == 0:
                return "rsync -a"
            logger.debug(
                "rsync copy failed (rc={}): {}",
                completed.returncode,
                completed.stderr.decode("utf-8", "replace").strip()[:200],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("rsync copy unavailable: {}", exc)
    if os.name == "posix":
        try:
            completed = subprocess.run(
                ["cp", "-a", "--reflink=auto", f"{src}{os.sep}.", str(dst)],
                capture_output=True,
                timeout=300,
            )
            if completed.returncode == 0:
                _prune_ignored(dst)
                return "cp --reflink=auto"
            logger.debug(
                "cp copy failed (rc={}): {}",
                completed.returncode,
                completed.stderr.decode("utf-8", "replace").strip()[:200],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("cp copy unavailable: {}", exc)
    shutil.copytree(
        src,
        dst,
        symlinks=True,
        ignore=lambda _dir, names: [n for n in names if n in _IGNORED_DIR_NAMES],
    )
    return "copytree"


def create_isolated_workspace(parent_root: Path, child_session_key: str) -> IsolatedWorkspace:
    """Copy *parent_root* into a private workspace and return it.

    :param parent_root: The parent's project directory (must exist).
    :param child_session_key: The child's session key (names the workspace).
    :raises OSError: The copy failed — the caller refuses the spawn rather than
        running the child in the shared tree by accident.
    """
    root = Path(parent_root).resolve()
    if not root.is_dir():
        raise OSError(f"isolation requires an existing project directory: {root}")

    meta_dir = isolated_workspaces_dir() / _slug(child_session_key)
    if meta_dir.exists():
        # A retry of the same spawn (or a crash's leftover): start clean, the
        # previous tree is never merged implicitly.
        shutil.rmtree(meta_dir, ignore_errors=True)
    tree = meta_dir / TREE_DIRNAME
    tree.mkdir(parents=True, exist_ok=True)

    method = _copy_tree(root, tree)
    manifest = scan_manifest(tree)
    (meta_dir / SNAPSHOT_NAME).write_text(
        json.dumps(
            {
                "version": MANIFEST_VERSION,
                "parent_root": str(root),
                "child_session_key": child_session_key,
                "created_at": time.time(),
                "copy_method": method,
                "revisions": manifest,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Isolated workspace created: child={} parent={} files={} method={}",
        child_session_key,
        root,
        len(manifest),
        method,
    )
    return IsolatedWorkspace(meta_dir=meta_dir, tree=tree, parent_root=root)


def discard_isolated_workspace(meta_dir: Path) -> None:
    """Remove a workspace directory (but never the parent tree)."""
    shutil.rmtree(meta_dir, ignore_errors=True)
