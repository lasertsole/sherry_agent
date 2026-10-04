"""Snapshot retention: age is a candidate, identity is the verdict.

A snapshot row names the process that captured it (``owner_pid`` +
``owner_start``). The GC deletes rows that are old AND whose holder is provably
gone — never a row a living process is still using, however old it is. Plain
age-based LRU would expire a long turn's own snapshots while the turn is still
running, and the revert would then answer ``snapshot_expired`` mid-turn.

The ``os.kill(pid, 0)`` probe has three outcomes, exactly like omo's lock
module (which is where this shape comes from):

* ``ESRCH`` — no process has that pid: the holder is dead. ``process_start`` is
  deliberately NOT consulted here: with no process there is no start time to
  compare, so a check that required one would be dead code and the sweep would
  never delete anything.
* the call succeeds and the recorded start matches — the holder is alive and
  still the same process: skip the row, whatever its age.
* the call succeeds but the start differs — the pid was recycled by a new
  process, so the original holder is dead: the row may go.
* ``EPERM`` — the process exists but belongs to another user: it is ALIVE.
  Treating that as "unknown" would let a short-lived process owned by someone
  else expire on age while it is still working.

When no verdict is possible (a row captured on another host, or a platform with
no start-time source) the row falls back to a longer orphan grace period.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from config.features import FILE_SNAPSHOT
from config.path import session_file_snapshots_dir

from .snapshot import SnapshotRow, snapshot_store

__all__ = [
    "GcResult",
    "OwnerState",
    "process_start_token",
    "run_snapshot_gc",
    "snapshot_owner_state",
    "start_file_snapshot_gc",
]

OwnerState = Literal["alive", "dead", "reused", "unknown"]

#: Threads spawned by :func:`start_file_snapshot_gc` (tests assert it is idle).
_gc_thread: threading.Thread | None = None


def process_start_token(pid: int) -> str | None:
    """A stable per-process start token, or ``None`` when unobtainable.

    Linux reads ``/proc/<pid>/stat`` field 22 (start time in clock ticks); other
    platforms fall back to ``ps -o lstart=``. Both are cheap and dependency-free
    — a psutil dependency is not worth this.
    """
    if pid <= 0:
        return None
    try:
        stat = open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace").read()
        # The comm field is parenthesised and may contain spaces; field 22 is
        # the first token after the closing paren + state.
        after_comm = stat.rsplit(")", 1)[1].split()
        return f"proc:{after_comm[19]}"  # utime(14)…starttime = index 19 after state
    except (OSError, IndexError):
        pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            timeout=5,
        )
        if completed.returncode == 0:
            token = completed.stdout.decode("utf-8", "replace").strip()
            return f"ps:{token}" if token else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def snapshot_owner_state(row: SnapshotRow) -> OwnerState:
    """Classify the process that captured *row* (see the module docstring)."""
    if row.owner_pid <= 0:
        return "unknown"
    try:
        os.kill(row.owner_pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    current_start = process_start_token(row.owner_pid)
    if not row.owner_start or not current_start:
        return "unknown"
    return "alive" if current_start == row.owner_start else "reused"


@dataclass(slots=True)
class GcResult:
    """What one sweep did."""

    rows_deleted: int = 0
    blobs_deleted: int = 0
    rows_skipped_live: int = 0
    batches: int = 0

    def as_dict(self) -> dict:
        return {
            "rows_deleted": self.rows_deleted,
            "blobs_deleted": self.blobs_deleted,
            "rows_skipped_live": self.rows_skipped_live,
            "batches": self.batches,
        }


def _delete_blob(sha256: str, session_id: str) -> bool:
    base = session_file_snapshots_dir(session_id)
    if base is None:
        return False
    blob = base / sha256[:2] / f"{sha256}.blob"
    try:
        blob.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _blob_referenced_elsewhere(rows: list[SnapshotRow], sha256: str) -> bool:
    return any(row.blob_sha256 == sha256 for row in rows)


def _sweep_orphan_blobs(
    session_id: str,
    *,
    referenced: set[str | None],
    cutoff: float,
    limit: int,
    result: GcResult,
) -> None:
    """Delete blob files no row references and nobody touched recently."""
    blob_root = session_file_snapshots_dir(session_id)
    if blob_root is None or not blob_root.is_dir():
        return
    deleted_here = 0
    for shard in blob_root.iterdir():
        if not shard.is_dir() or deleted_here >= limit:
            continue
        for blob in shard.glob("*.blob"):
            if deleted_here >= limit:
                break
            if blob.stem in referenced:
                continue
            try:
                if blob.stat().st_mtime >= cutoff:
                    continue
                blob.unlink()
                result.blobs_deleted += 1
                deleted_here += 1
            except OSError:
                continue


def run_snapshot_gc(now: float | None = None) -> GcResult:
    """One GC sweep over every session's snapshots.

    Order: age+identity, then the per-session file and byte caps (LRU by
    ``captured_at``), then orphan blobs. Every deletion is bounded by
    ``gc_batch_limit``.
    """
    from config.path import SESSIONS_DIR

    current = time.time() if now is None else now
    retention_cutoff = current - int(FILE_SNAPSHOT["retention_days"]) * 86400
    orphan_cutoff = current - int(FILE_SNAPSHOT["orphan_grace_days"]) * 86400
    max_rows = int(FILE_SNAPSHOT["max_files_per_session"])
    max_bytes = int(FILE_SNAPSHOT["max_bytes_per_session"])
    batch_limit = int(FILE_SNAPSHOT["gc_batch_limit"])

    result = GcResult()
    sessions_dir = SESSIONS_DIR
    if not sessions_dir.is_dir():
        return result

    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        try:
            rows = snapshot_store.rows_for_session(session_id)
        except Exception as exc:  # noqa: BLE001 — one broken session must not stop the sweep
            logger.warning("file snapshot GC: session {} unreadable: {}", session_id, exc)
            continue
        if not rows:
            # No rows, but a crashed capture may still have left an orphan blob.
            _sweep_orphan_blobs(
                session_id, referenced=set(), cutoff=orphan_cutoff, limit=batch_limit, result=result
            )
            continue
        result.batches += 1

        doomed: list[SnapshotRow] = []
        live_or_fresh: list[SnapshotRow] = []
        for row in rows:
            if row.captured_at >= retention_cutoff:
                live_or_fresh.append(row)
                continue
            state = snapshot_owner_state(row)
            if state in ("alive", "reused") or state == "dead":
                if state == "alive":
                    # The holder is still writing: keep it, however old.
                    result.rows_skipped_live += 1
                    live_or_fresh.append(row)
                    continue
                doomed.append(row)
                continue
            # Unknown holder: a longer grace period, then age decides.
            if row.captured_at < orphan_cutoff:
                doomed.append(row)
            else:
                live_or_fresh.append(row)

        # Caps: oldest first, and never a live holder's row.
        def _reclaimable(pool: list[SnapshotRow]) -> list[SnapshotRow]:
            return [row for row in pool if snapshot_owner_state(row) != "alive"]

        kept = list(live_or_fresh)
        if len(kept) > max_rows:
            overflow = len(kept) - max_rows
            candidates = _reclaimable(sorted(kept, key=lambda row: row.captured_at))
            for row in candidates[:overflow]:
                doomed.append(row)
                kept.remove(row)
        total_bytes = sum(row.before_size for row in kept)
        if total_bytes > max_bytes:
            for row in _reclaimable(sorted(kept, key=lambda row: row.captured_at)):
                if total_bytes <= max_bytes:
                    break
                doomed.append(row)
                kept.remove(row)
                total_bytes -= row.before_size

        if not doomed:
            continue
        doomed = doomed[:batch_limit]
        doomed_ids = [row.id for row in doomed]
        snapshot_store.delete_rows(doomed_ids)
        result.rows_deleted += len(doomed_ids)

        # A blob goes when no surviving row references it.
        surviving = snapshot_store.rows_for_session(session_id)
        for row in doomed:
            if row.blob_sha256 and not _blob_referenced_elsewhere(surviving, row.blob_sha256):
                if _delete_blob(row.blob_sha256, session_id):
                    result.blobs_deleted += 1

        # Orphan blobs: written but never indexed (a crashed capture).
        _sweep_orphan_blobs(
            session_id,
            referenced={row.blob_sha256 for row in surviving if row.blob_sha256},
            cutoff=orphan_cutoff,
            limit=batch_limit,
            result=result,
        )

    logger.info(
        "file snapshot GC: rows={} blobs={} kept_live={} sessions={}",
        result.rows_deleted,
        result.blobs_deleted,
        result.rows_skipped_live,
        result.batches,
    )
    return result


def start_file_snapshot_gc(interval_seconds: int | None = None) -> threading.Thread | None:
    """Start the daemon GC thread (idempotent), or ``None`` when disabled.

    Shaped like ``auth_service.start_blacklist_cleanup``: a daemon thread that
    sleeps between sweeps and swallows every error — it must never block boot,
    the event loop, or shutdown.
    """
    global _gc_thread
    if not FILE_SNAPSHOT["enabled"]:
        return None
    if _gc_thread is not None and _gc_thread.is_alive():
        return _gc_thread
    interval = int(interval_seconds or FILE_SNAPSHOT["gc_interval_seconds"])

    def _loop() -> None:
        while True:
            try:
                run_snapshot_gc()
            except Exception as exc:  # noqa: BLE001 — a GC tick must never kill the thread
                logger.warning("file snapshot GC tick failed: {}", exc)
            time.sleep(interval)

    _gc_thread = threading.Thread(target=_loop, name="file-snapshot-gc", daemon=True)
    _gc_thread.start()
    return _gc_thread
