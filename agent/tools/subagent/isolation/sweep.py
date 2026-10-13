"""Sweep stale isolated workspaces: orphaned, expired-conflict, corrupted.

An isolated run's workspace is normally discarded by the announce flow (clean
merge) or kept for inspection (conflict merge, manifest renamed to
``merged.json``). Two paths leave a workspace behind with nobody to notice:

* a crash between ``create_isolated_workspace`` and the run's registration —
  the directory, the git worktree registration and the branch all exist, but no
  run record ever will;
* a conflict-kept workspace that nobody comes back for.

This module is the sweeper side of that contract: run on every sweeper cycle and
once at boot, it removes workspaces no live run owns, expires conflict keeps
past ``stale_workspace_ttl_days``, and prunes the parent repositories' stale
worktree registrations. Never raises — a sweep failure must not break the
sweeper loop or the server start.
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from config.features import SUBAGENT_ISOLATION
from config.path import isolated_workspaces_dir

from .gitrun import run_git
from .tree import MERGED_NAME, SNAPSHOT_NAME, _slug, discard_isolated_workspace

__all__ = ["sweep_stale_isolated_workspaces"]

#: Directory-name prefix every workspace slug carries (``tree._slug``).
_SLUG_PREFIX = "iso-"


def sweep_stale_isolated_workspaces() -> int:
    """Remove workspaces no live run owns; returns how many were cleaned.

    Three classes, all decided from the manifest the workspace carries:

    1. ``snapshot.json`` + no run record — a crash between workspace creation
       and registration. Cleaned.
    2. ``merged.json`` older than the TTL — a conflict keep past retention.
       Cleaned.
    3. neither manifest — a damaged workspace. Cleaned.

    A workspace whose run is still in the registry is left untouched: orphan
    recovery or the announce flow owns it.
    """
    try:
        return _do_sweep()
    except Exception:
        logger.warning("stale isolated-workspace sweep failed", exc_info=True)
        return 0


def _do_sweep() -> int:
    base = isolated_workspaces_dir()
    if not base.is_dir():
        return 0

    active_slugs = _collect_active_slugs()
    if not _registry_readable:
        # The registry could not be read: with no way to tell "orphaned" from
        # "owned by a live run", the only safe move is to clean nothing.
        logger.warning("stale workspace sweep skipped: the run registry is unreadable")
        return 0

    ttl_seconds = int(SUBAGENT_ISOLATION["stale_workspace_ttl_days"]) * 86400
    now = time.time()
    cleaned = 0
    parent_roots: set[str] = set()

    for ws_dir in sorted(base.iterdir()):
        if not ws_dir.is_dir() or not ws_dir.name.startswith(_SLUG_PREFIX):
            continue
        if ws_dir.name in active_slugs:
            continue

        snapshot_path = ws_dir / SNAPSHOT_NAME
        merged_path = ws_dir / MERGED_NAME

        if snapshot_path.is_file():
            logger.warning("sweep: orphaned isolated workspace (no run record): {}", ws_dir)
        elif merged_path.is_file():
            age = now - merged_path.stat().st_mtime
            if age < ttl_seconds:
                continue  # still inside the conflict-retention window
            logger.info(
                "sweep: expired conflict workspace ({:.1f} days old): {}", age / 86400, ws_dir
            )
        else:
            logger.warning("sweep: corrupted workspace (no manifest): {}", ws_dir)

        parent_roots.add(str(_parent_root_of(ws_dir, snapshot_path, merged_path) or ""))
        discard_isolated_workspace(ws_dir)
        cleaned += 1

    for root in sorted(parent_roots - {""}):
        _prune_worktrees(Path(root))

    if cleaned:
        logger.info("sweep: cleaned {} stale isolated workspace(s)", cleaned)
    return cleaned


#: Whether the last registry read succeeded (set by ``_collect_active_slugs``).
_registry_readable = True


def _collect_active_slugs() -> set[str]:
    """Slugs owned by a registry run — empty when the registry is unreadable."""
    global _registry_readable
    try:
        from ..registry import memory

        runs = memory.values()
        _registry_readable = True
        return {_slug(run.child_session_key) for run in runs if run.child_session_key}
    except Exception:
        _registry_readable = False
        logger.warning("sweep: active run lookup failed", exc_info=True)
        return set()


def _parent_root_of(ws_dir: Path, *manifests: Path) -> str | None:
    """The parent project root recorded in whichever manifest the workspace has."""
    import json

    for manifest in manifests:
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        root = data.get("parent_root")
        if root:
            return str(root)
    return None


def _prune_worktrees(parent_root: Path) -> None:
    """Drop the parent repository's registrations whose directories are gone."""
    if not parent_root.is_dir():
        return
    try:
        run_git(parent_root, ["worktree", "prune"])
    except Exception:
        logger.debug("sweep: git worktree prune failed for {}", parent_root, exc_info=True)
