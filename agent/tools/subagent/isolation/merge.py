"""Merge an isolated workspace back into the parent tree — locked, CAS-checked.

The rule per file is the one the file tools already use, applied the other way
round: a change is applied only when the parent's copy is still exactly what the
child started from. A file the parent moved since the snapshot is a CONFLICT:
the parent is left untouched, the conflicting path is reported, and the
workspace is kept on disk for inspection. Two children finishing at once
serialize on a per-parent-root ``flock`` (same primitive as the file locks, and
the same rule: the lock lives on an inode that is never deleted).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from config.path import file_locks_dir
from agent.tools.pub_base import (
    StaleWriteError,
    atomic_write_bytes_no_follow,
    file_revision,
    flock_path,
)

from .tree import (
    MANIFEST_VERSION,
    MERGED_NAME,
    SNAPSHOT_NAME,
    SYMLINK_PREFIX,
    TREE_DIRNAME,
    scan_manifest,
)

__all__ = ["MergeReport", "merge_isolated_workspace"]

#: How long a merge waits for the parent root's merge lock before giving up.
MERGE_LOCK_TIMEOUT_S = 60.0


@dataclass
class MergeReport:
    """What one merge did, per relative path."""

    parent_root: Path
    applied: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when nothing conflicted (every child change landed)."""
        return not self.conflicts

    @property
    def changed_count(self) -> int:
        """Paths this merge touched in the parent tree."""
        return len(self.applied) + len(self.created) + len(self.deleted)

    def summary(self) -> str:
        """One-line human summary (used in the completion announcement)."""
        parts = [
            f"applied {len(self.applied)}",
            f"created {len(self.created)}",
            f"deleted {len(self.deleted)}",
        ]
        if self.conflicts:
            parts.append(f"conflicts {len(self.conflicts)}")
        return ", ".join(parts)


def _merge_lock_path(parent_root: Path) -> Path:
    """Per-parent-root merge lock file (kept in the shared locks directory)."""
    digest = hashlib.sha256(str(parent_root).encode("utf-8")).hexdigest()[:32]
    return file_locks_dir() / f"merge-{digest}.lock"


def _load_snapshot(meta_dir: Path) -> tuple[Path, dict[str, str]]:
    raw = json.loads((meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    version = raw.get("version")
    if version != MANIFEST_VERSION:
        raise ValueError(f"unsupported isolation manifest version: {version!r}")
    return Path(raw["parent_root"]), dict(raw.get("revisions") or {})


def _apply_bytes(source: Path, target: Path, expected_revision: str | None) -> None:
    """Write the child's file into the parent tree, preserving its mode."""
    data = source.read_bytes()
    atomic_write_bytes_no_follow(target, data, expected_revision=expected_revision)
    with contextlib.suppress(OSError):
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))


def merge_isolated_workspace(
    meta_dir: Path, *, timeout_s: float = MERGE_LOCK_TIMEOUT_S
) -> MergeReport:
    """Merge the workspace at *meta_dir* into its recorded parent root.

    Never raises for a per-file problem (a conflict, a symlink, a vanished
    file): those land in the report. Raises when the manifest is unreadable,
    the isolated tree is gone, the parent root is gone, or the merge lock
    cannot be taken — the caller decides.

    :param meta_dir: The workspace directory (``<slug>``), not the tree inside.
    :param timeout_s: How long to wait for the parent root's merge lock.
    """
    parent_root, snapshot = _load_snapshot(meta_dir)
    tree = meta_dir / TREE_DIRNAME
    # Both absences are errors, never "everything changed": a missing tree must
    # not read as "the child deleted every file", and a missing parent root must
    # not be silently re-created from the copy.
    if not tree.is_dir():
        raise FileNotFoundError(f"isolated tree is missing: {tree}")
    if not parent_root.is_dir():
        raise FileNotFoundError(f"parent project directory is missing: {parent_root}")
    report = MergeReport(parent_root=parent_root)

    with flock_path(_merge_lock_path(parent_root), timeout_s):
        current = scan_manifest(tree)
        for relpath in sorted(set(snapshot) | set(current)):
            source = tree / relpath
            target = parent_root / relpath
            snapshot_rev = snapshot.get(relpath)
            current_rev = current.get(relpath)
            try:
                _merge_one(relpath, source, target, snapshot_rev, current_rev, report)
            except StaleWriteError:
                report.conflicts.append((relpath, "changed in the parent since the snapshot"))
            except OSError as exc:
                report.conflicts.append((relpath, f"not writable in the parent: {exc}"))

    logger.info(
        "Isolated workspace merged: root={} {}",
        parent_root,
        report.summary(),
    )
    if report.is_clean:
        # Everything landed: the copy has no further use.
        from .tree import discard_isolated_workspace

        discard_isolated_workspace(meta_dir)
    else:
        # The tree is kept so the conflicting paths can be inspected, but the
        # manifest is consumed: whatever re-runs the announce flow must not
        # merge the same tree a second time.
        with contextlib.suppress(OSError):
            (meta_dir / SNAPSHOT_NAME).rename(meta_dir / MERGED_NAME)
    return report


def _merge_one(
    relpath: str,
    source: Path,
    target: Path,
    snapshot_rev: str | None,
    current_rev: str | None,
    report: MergeReport,
) -> None:
    """Classify and apply one path."""
    if current_rev is not None and current_rev == snapshot_rev:
        return  # untouched by the child

    if current_rev is not None and current_rev.startswith(SYMLINK_PREFIX):
        # The child turned this path into a symlink: a link is never written
        # through, and it must NOT read as "deleted" (that would delete the
        # parent's file).
        report.skipped.append(relpath)
        return

    if current_rev is None:
        # Deleted in the copy: only a parent that still matches may lose the file.
        if snapshot_rev is None:
            return
        if _parent_state(target) == snapshot_rev:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target)
            report.deleted.append(relpath)
        else:
            report.conflicts.append((relpath, "deleted in isolation, changed in the parent"))
        return

    source_info = source.lstat()
    if stat.S_ISLNK(source_info.st_mode):
        report.skipped.append(relpath)  # links are never merged through
        return

    if snapshot_rev is None:
        # New in the copy: create only into empty space — the atomic write
        # re-asserts "absent", so a parent file that appears in the window is a
        # refusal rather than a clobber.
        if _parent_state(target) != "absent":
            report.conflicts.append((relpath, "created in isolation, already exists in the parent"))
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        _apply_bytes(source, target, "absent")
        report.created.append(relpath)
        return

    # Modified in the copy: the parent must still be the snapshot it started from.
    _apply_bytes(source, target, snapshot_rev)
    report.applied.append(relpath)


def _parent_state(target: Path) -> str:
    """Current revision of the parent's copy, ``absent`` when there is none."""
    if target.is_symlink():
        try:
            return SYMLINK_PREFIX + os.readlink(target)
        except OSError:
            return SYMLINK_PREFIX
    return file_revision(target)
