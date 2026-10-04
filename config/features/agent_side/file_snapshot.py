"""File snapshot & revert: what is captured, and what the disk may keep.

Every file-tool write captures the OLD bytes of its target before the write
lands (content-addressed blobs + a row in ``file-snapshots.db``), so an agent
that breaks a file can be reverted. This block bounds that archive: three caps
per session, an age-based retention, and the size above which the revert path
skips the content-hash fallback and trusts the revision alone.

Deletion is deliberately NOT age-only: a snapshot row names the process that
captured it (``pid`` + ``process_start``), and the GC skips rows held by a
process that is provably alive — otherwise a long turn's own snapshots would
expire mid-turn and the revert would answer ``snapshot_expired``.
"""

from typing import TypedDict


class FileSnapshot(TypedDict):
    """Off-by-default is NOT offered: a kill switch disables capture entirely."""

    enabled: bool
    max_bytes_per_session: int
    max_files_per_session: int
    retention_days: int
    hash_guard_max_bytes: int
    gc_interval_seconds: int
    gc_batch_limit: int
    orphan_grace_days: int


FILE_SNAPSHOT: FileSnapshot = {
    # Master switch. ``False`` makes the file tools behave byte-for-byte as
    # they did before snapshots existed (no read, no blob, no row).
    "enabled": True,
    # Per-session caps, enforced LRU-first by ``captured_at``.
    "max_bytes_per_session": 64 * 1024 * 1024,  # 64 MB
    "max_files_per_session": 2000,
    # Age beyond which a snapshot row is a GC candidate …
    "retention_days": 14,
    # … but a live holder is skipped regardless of age, and only an
    # undecidable holder (cross-host row, platform without a start-time
    # source) falls back to this second, longer age (see R13).
    "orphan_grace_days": 30,
    # Above this size the revert path checks the revision only — hashing a
    # multi-MB file on every revert is not worth the certainty.
    "hash_guard_max_bytes": 2 * 1024 * 1024,
    # GC cadence and batch size (a long transaction would stall SQLite).
    "gc_interval_seconds": 3600,
    "gc_batch_limit": 500,
}
