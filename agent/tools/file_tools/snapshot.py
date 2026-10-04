"""Pre-write snapshots: the old bytes of every file the tools are about to change.

The write path already holds the target's bytes (``patch_file`` reads for its
CAS; ``write_file`` reads for the read-before-write license) — this module turns
that moment into a durable record: the old content goes into a
content-addressed blob, and one row per write lands in ``file-snapshots.db``
carrying both the revision it captured and the revision/hash the write produced.
Revert (``revert.py``) reads those rows; the GC (``snapshot_gc.py``) trims them.

Design points worth keeping in mind when editing:

* **Fail-open** — a snapshot problem must never fail the write it precedes
  (R8): every entry point swallows and warns. A blob without a row is an
  orphan, and the GC collects it.
* **Content-addressed** — writing the same old content twice costs one blob;
  the create is ``O_EXCL``, so a concurrent capture of the same bytes is a win,
  not a race.
* **Capture inside the lock, before the change** — callers run under
  ``file_write_lock``, so what a capture records is exactly what the write
  replaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.features import FILE_SNAPSHOT
from config.path import file_snapshots_db, session_file_snapshots_dir
from agent.tools.pub_base import file_revision, read_bytes_no_follow
from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository

__all__ = [
    "Capture",
    "SnapshotRow",
    "blob_bytes",
    "capture_pre_write",
    "delete_rows",
    "delete_snapshots_by_session",
    "file_changes_payload",
    "finalize_capture",
    "rows_for_session",
    "snapshot_store",
    "store_blob",
]

#: Process-level state the shared SQLite base reads through this module's
#: namespace (the same contract every store here follows; tests repoint the
#: two path entries).
_DB_DIR: Path = file_snapshots_db().parent
_DB_PATH: Path = file_snapshots_db()
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = 5.0
_INIT_WAIT_TIMEOUT_S = 10.0
_initialized = False
_init_loop = None
_init_lock = asyncio.Lock()
_sync_tables_ready = False
_sync_init_lock = threading.Lock()

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS file_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    path            TEXT    NOT NULL,
    root            TEXT    NOT NULL DEFAULT '',
    blob_sha256     TEXT,
    before_size     INTEGER NOT NULL DEFAULT 0,
    before_revision TEXT    NOT NULL DEFAULT '',
    after_revision  TEXT    NOT NULL DEFAULT '',
    after_sha256    TEXT    NOT NULL DEFAULT '',
    existed_before  INTEGER NOT NULL DEFAULT 0,
    tool_call_id    TEXT    NOT NULL DEFAULT '',
    captured_at     REAL    NOT NULL,
    owner_pid       INTEGER NOT NULL DEFAULT 0,
    owner_start     TEXT    NOT NULL DEFAULT ''
)
"""

_INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_fs_session_time ON file_snapshots (session_id, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_fs_session_path ON file_snapshots (session_id, path)",
    "CREATE INDEX IF NOT EXISTS idx_fs_tool_call ON file_snapshots (tool_call_id)",
)

_SELECT_COLUMNS = (
    "id, session_id, path, root, blob_sha256, before_size, before_revision, "
    "after_revision, after_sha256, existed_before, tool_call_id, captured_at, "
    "owner_pid, owner_start"
)


class FileSnapshotStore(BaseSQLiteRepository):
    """The ``file_snapshots`` table over the shared SQLite skeleton."""

    wal_label = "file-snapshots"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_TABLE_DDL,)

    def _index_ddls(self) -> tuple[str, ...]:
        return _INDEX_DDLS

    def _connect_sync(self) -> sqlite3.Connection:
        """Open the sync connection this store's short-lived statements use."""
        self.ensure_tables_sync()
        return sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)

    def insert_row(
        self,
        *,
        session_id: str,
        path: str,
        root: str,
        blob_sha256: str | None,
        before_size: int,
        before_revision: str,
        after_revision: str,
        after_sha256: str,
        existed_before: bool,
        tool_call_id: str,
        captured_at: float,
        owner_pid: int,
        owner_start: str,
    ) -> None:
        """Append one snapshot row (sync: called from the tools' worker thread)."""
        conn = self._connect_sync()
        try:
            conn.execute(
                """
                INSERT INTO file_snapshots (
                    session_id, path, root, blob_sha256, before_size, before_revision,
                    after_revision, after_sha256, existed_before, tool_call_id,
                    captured_at, owner_pid, owner_start
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    path,
                    root,
                    blob_sha256,
                    before_size,
                    before_revision,
                    after_revision,
                    after_sha256,
                    int(existed_before),
                    tool_call_id,
                    captured_at,
                    owner_pid,
                    owner_start,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def rows_for_session(self, session_id: str) -> list[SnapshotRow]:
        """Every row of one session, oldest first."""
        conn = self._connect_sync()
        try:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM file_snapshots WHERE session_id = ? "
                "ORDER BY captured_at ASC, id ASC",
                (session_id,),
            )
            return [_row_from_sql(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def delete_rows(self, row_ids: list[int]) -> None:
        """Consume rows in one transaction (a revert that succeeded)."""
        if not row_ids:
            return
        conn = self._connect_sync()
        try:
            conn.executemany("DELETE FROM file_snapshots WHERE id = ?", [(i,) for i in row_ids])
            conn.commit()
        finally:
            conn.close()

    def delete_snapshots_by_session(self, session_id: str) -> int:
        """Drop a session's rows (session deletion); returns rows removed."""
        conn = self._connect_sync()
        try:
            cursor = conn.execute("DELETE FROM file_snapshots WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount or 0
        finally:
            conn.close()


snapshot_store = FileSnapshotStore(globals())


@dataclass(slots=True)
class Capture:
    """The target's state immediately before a write. Build under the lock.

    ``before_bytes`` is the old content, held only until ``finalize_capture``
    runs in the same critical section — it is what the blob stores, and the
    reason no second read is needed after the write.
    """

    path: Path
    root: Path
    existed_before: bool
    before_size: int
    before_revision: str
    blob_sha256: str | None
    before_bytes: bytes | None = None


@dataclass(slots=True)
class SnapshotRow:
    """One captured write, as stored."""

    id: int
    session_id: str
    path: str
    root: str
    blob_sha256: str | None
    before_size: int
    before_revision: str
    after_revision: str
    after_sha256: str
    existed_before: bool
    tool_call_id: str
    captured_at: float
    owner_pid: int
    owner_start: str


def _row_from_sql(row: tuple) -> SnapshotRow:
    return SnapshotRow(
        id=row[0],
        session_id=row[1],
        path=row[2],
        root=row[3],
        blob_sha256=row[4],
        before_size=row[5],
        before_revision=row[6],
        after_revision=row[7],
        after_sha256=row[8],
        existed_before=bool(row[9]),
        tool_call_id=row[10],
        captured_at=row[11],
        owner_pid=row[12],
        owner_start=row[13],
    )


def snapshot_enabled() -> bool:
    """Whether capture is on (the tools skip every snapshot call otherwise)."""
    return bool(FILE_SNAPSHOT["enabled"])


def capture_pre_write(
    resolved: Path,
    root: Path,
    *,
    before_bytes: bytes | None = None,
    before_revision: str | None = None,
) -> Capture | None:
    """Capture the bytes a write is about to replace (``None`` when disabled).

    A missing target records ``existed_before=False`` and reads nothing — the
    common "create a new file" write pays no extra I/O. A caller that already
    read the target (``patch_file`` reads for its CAS) passes ``before_bytes``
    so nothing is read twice.
    """
    if not snapshot_enabled():
        return None
    try:
        if before_revision is None:
            before_revision = file_revision(resolved)
        if before_revision == "absent":
            return Capture(resolved, root, False, 0, "absent", None)
        if before_bytes is None:
            before_bytes, _stat = read_bytes_no_follow(resolved)
        return Capture(
            resolved,
            root,
            True,
            len(before_bytes),
            before_revision,
            hashlib.sha256(before_bytes).hexdigest(),
            before_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open by contract (R8)
        logger.warning("file snapshot capture failed for {}: {}", resolved, exc)
        return None


def _blob_path(session_id: str, sha256: str) -> Path | None:
    """Blob location for one content hash; ``None`` for an unsafe session id."""
    base = session_file_snapshots_dir(session_id)
    if base is None:
        return None
    return base / sha256[:2] / f"{sha256}.blob"


def store_blob(session_id: str, sha256: str, content: bytes) -> bool:
    """Write the old content once (``O_EXCL`` + replace); False on any failure."""
    blob = _blob_path(session_id, sha256)
    if blob is None:
        return False
    try:
        blob.parent.mkdir(parents=True, exist_ok=True)
        if blob.exists():
            return True  # content-addressed: this exact old content is stored
        tmp = blob.with_name(f"{blob.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, blob)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open by contract (R8)
        logger.warning("file snapshot blob write failed ({}): {}", blob, exc)
        return False


def blob_bytes(session_id: str, sha256: str) -> bytes | None:
    """Read one blob back (``None`` when the GC already collected it)."""
    blob = _blob_path(session_id, sha256)
    if blob is None or not blob.is_file():
        return None
    try:
        return blob.read_bytes()
    except OSError:
        return None


def _owner_identity() -> tuple[int, str]:
    """``(pid, process_start)`` of this process — the GC's ownership evidence."""
    pid = os.getpid()
    try:
        from .snapshot_gc import process_start_token

        return pid, process_start_token(pid) or ""
    except Exception:  # noqa: BLE001 — identity is best-effort
        return pid, ""


def finalize_capture(
    capture: Capture | None,
    *,
    session_id: str,
    tool_call_id: str,
    written: bytes,
) -> None:
    """Record a completed write (fail-open).

    :param written: The WHOLE new content of the file — hashed here, so the row
        carries the post-write hash a later revert compares against the disk
        without anyone re-reading the file. Appends pass old + appended.
    """
    if capture is None or not snapshot_enabled():
        return
    try:
        if capture.existed_before and capture.blob_sha256 and capture.before_bytes is not None:
            store_blob(session_id, capture.blob_sha256, capture.before_bytes)
        owner_pid, owner_start = _owner_identity()
        capture.before_bytes = None  # release the reference; the blob owns it now
        snapshot_store.insert_row(
            session_id=session_id,
            path=str(capture.path),
            root=str(capture.root),
            blob_sha256=capture.blob_sha256,
            before_size=capture.before_size,
            before_revision=capture.before_revision,
            after_revision=file_revision(capture.path),
            after_sha256=hashlib.sha256(written).hexdigest(),
            existed_before=capture.existed_before,
            tool_call_id=tool_call_id,
            captured_at=time.time(),
            owner_pid=owner_pid,
            owner_start=owner_start,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open by contract (R8)
        logger.warning("file snapshot record failed for {}: {}", capture.path, exc)


def rows_for_session(session_id: str) -> list[SnapshotRow]:
    """Every row of one session, oldest first (fail-open: ``[]`` on any error)."""
    try:
        return snapshot_store.rows_for_session(session_id)
    except Exception as exc:  # noqa: BLE001 — reads never break a caller
        logger.warning("file snapshot read failed for {}: {}", session_id, exc)
        return []


def delete_rows(row_ids: list[int]) -> None:
    """Consume rows in one transaction (a revert that succeeded)."""
    with contextlib.suppress(Exception):
        snapshot_store.delete_rows(row_ids)


def delete_snapshots_by_session(session_id: str) -> int:
    """Drop a session's rows (session deletion); returns rows removed."""
    try:
        return snapshot_store.delete_snapshots_by_session(session_id)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("file snapshot session purge failed for {}: {}", session_id, exc)
        return 0


def file_changes_payload(session_id: str) -> dict:
    """The WS/refresh payload: the session's snapshots grouped per tool call."""
    rows = rows_for_session(session_id)
    by_call: dict[str, dict] = {}
    for row in rows:
        entry = by_call.setdefault(
            row.tool_call_id or "",
            {"tool_call_id": row.tool_call_id, "captured_at": row.captured_at, "paths": []},
        )
        entry["captured_at"] = max(entry["captured_at"], row.captured_at)
        if row.path not in entry["paths"]:
            entry["paths"].append(row.path)
    changes = sorted(by_call.values(), key=lambda item: item["captured_at"])
    return {
        "session_id": session_id,
        "total_rows": len(rows),
        "changes": changes,
        "canRevert": bool(changes),
    }
