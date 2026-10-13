"""Materialize the ignored paths a worktree checkout never carries.

A ``git worktree`` checks out committed content only, so everything the project
keeps out of git — live stores under ``src/``, the session workspace, dependency
caches — would simply be missing inside the child's tree. This module puts those
paths back, per path, in one of two modes:

- **symlink**: shared machine state. One inode serves every tree, so a lock
  taken through the link still excludes a writer in the parent (correct), and a
  whole-directory link keeps SQLite's ``-wal``/``-shm`` files consistent.
- **copy**: live mutable state two concurrent trees must not share (task plans
  and progress files); the merge back writes the copy's changes home.

The declaration is the plan's adopted one: a ``.worktreeinclude`` file in the
project root (gitignore-glob syntax, ``# wti=symlink|copy`` directive on the
line after each pattern) whose entries EXTEND the configured tables. A declared
path is materialized only when git actually ignores it — tracked files already
arrive with the checkout, and overlaying one with a link would break the
worktree. Directories are never walked: each declaration is one symlink or one
copy of a whole subtree.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from loguru import logger

from config.features import SUBAGENT_ISOLATION

from .gitrun import run_git
from .skipnames import ignored_dir_names

__all__ = [
    "IncludeEntry",
    "copy_untracked_files",
    "materialize_ignored_paths",
    "parse_worktreeinclude",
    "worktreeinclude_entries",
]

#: The two materialization modes; anything else in a directive is a parse error
#: surfaced by :func:`parse_worktreeinclude` as ``ValueError``.
_MODE_SYMLINK = "symlink"
_MODE_COPY = "copy"
_MODES = frozenset({_MODE_SYMLINK, _MODE_COPY})


class IncludeEntry:
    """One materialization declaration: a relative path and its mode."""

    __slots__ = ("path", "mode")

    def __init__(self, path: str, mode: str) -> None:
        self.path = path.strip().strip("/")
        self.mode = mode

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IncludeEntry({self.path!r}, {self.mode!r})"


def parse_worktreeinclude(text: str) -> list[IncludeEntry]:
    """Parse a ``.worktreeinclude`` body into entries.

    Format (one declaration per block)::

        node_modules/
        # wti=symlink
        .omo
        # wti=copy

    Blank lines and ``#`` comments (other than a ``# wti=`` directive) are
    ignored. A path without a directive defaults to ``symlink`` — the safe mode
    for shared state; a directive naming anything else raises ``ValueError``.
    """
    entries: list[IncludeEntry] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            marker = line.lstrip("#").strip()
            if not marker.startswith("wti="):
                continue
            if pending is None:
                raise ValueError(
                    f"{SUBAGENT_ISOLATION['include_file_name']}: 'wti=' without a path"
                )
            mode = marker.removeprefix("wti=").strip()
            if mode not in _MODES:
                raise ValueError(
                    f"{SUBAGENT_ISOLATION['include_file_name']}: unknown mode {mode!r}"
                )
            entries.append(IncludeEntry(pending, mode))
            pending = None
            continue
        if pending is not None:
            # A path without its own directive: the previous one was directive-less.
            entries.append(IncludeEntry(pending, _MODE_SYMLINK))
        pending = line
    if pending is not None:
        entries.append(IncludeEntry(pending, _MODE_SYMLINK))
    return entries


def worktreeinclude_entries(project_root: Path) -> list[IncludeEntry]:
    """Configured tables plus the project's own ``.worktreeinclude`` (which wins).

    A path declared more than once keeps the LAST mode, so a project file can
    retarget a default (e.g. copy ``.omo`` instead of linking it).
    """
    by_path: dict[str, IncludeEntry] = {}
    for path in SUBAGENT_ISOLATION["include_symlink"]:
        entry = IncludeEntry(path, _MODE_SYMLINK)
        by_path[entry.path] = entry
    for path in SUBAGENT_ISOLATION["include_copy"]:
        entry = IncludeEntry(path, _MODE_COPY)
        by_path[entry.path] = entry

    include_file = project_root / SUBAGENT_ISOLATION["include_file_name"]
    if include_file.is_file():
        try:
            for entry in parse_worktreeinclude(include_file.read_text(encoding="utf-8")):
                by_path[entry.path] = entry
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable {}: {}", include_file, exc)
    return list(by_path.values())


def _is_ignored(project_root: Path, relpath: str) -> bool:
    """True when git ignores *relpath* in the parent (the intersection rule).

    Tracked content already arrives with the checkout; materializing over it
    would replace a real file with a link and confuse the merge.
    """
    completed = run_git(
        project_root,
        ["check-ignore", "-q", "--", relpath],
        timeout_s=SUBAGENT_ISOLATION["git_timeout_s"],
    )
    return completed.returncode == 0


def copy_untracked_files(project_root: Path, tree: Path) -> list[str]:
    """Copy the parent's untracked (but not ignored) files into *tree*.

    ``git stash create`` carries modified files only — an untracked file never
    reaches the baseline commit, and an ignored one is covered by the include
    table — so without this step the child would be blind to files the user has
    just created. Each file is copied individually (``copy2``, so its mtime
    travels and a revision stays comparable), pruning the same heavy
    directories the manifest walk skips.

    :returns: The relative paths copied.
    """
    completed = run_git(
        project_root,
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        timeout_s=SUBAGENT_ISOLATION["git_timeout_s"],
    )
    if completed.returncode != 0:
        return []
    done: list[str] = []
    for entry in completed.stdout.split("\0"):
        # Format per entry: "XY <path>"; only untracked ("??") entries matter.
        if not entry.startswith("?? "):
            continue
        relpath = entry[3:]
        if not relpath or any(part in ignored_dir_names() for part in Path(relpath).parts):
            continue
        source = project_root / relpath
        target = tree / relpath
        if not source.is_file() or source.is_symlink():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            done.append(relpath)
        except OSError as exc:
            logger.warning("Failed to copy untracked {}: {}", relpath, exc)
    return done


def materialize_ignored_paths(project_root: Path, tree: Path) -> list[str]:
    """Put the declared ignored paths into *tree*; returns the relative paths done.

    Symlink targets are the parent's absolute paths; copied subtrees carry the
    same ignore-pruning the manifest walk uses (dependency caches inside a copy
    are noise). A path the parent does not have is skipped — there is nothing to
    share.
    """
    done: list[str] = []
    for entry in worktreeinclude_entries(project_root):
        source = project_root / entry.path
        target = tree / entry.path
        if not source.exists() and not source.is_symlink():
            continue
        if not _is_ignored(project_root, entry.path):
            logger.debug("Not materializing tracked path: {}", entry.path)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                # The checkout may carry a directory at the path (empty after
                # pruning) — clear the way rather than nesting a link in it.
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            if entry.mode == _MODE_SYMLINK:
                os.symlink(source, target, target_is_directory=source.is_dir())
            else:
                shutil.copytree(
                    source,
                    target,
                    symlinks=True,
                    ignore=lambda _dir, names: [n for n in names if n in ignored_dir_names()],
                )
            done.append(entry.path)
        except OSError as exc:
            logger.warning("Failed to materialize {} ({}): {}", entry.path, entry.mode, exc)
    return done
