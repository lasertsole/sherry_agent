"""SQLite evidence ledger (``src/data/evidence-ledger.db``).

One row records the VERIFICATION trail: a recognized test / lint / build /
typecheck / format command the agent ran (``terminal`` and ``python_repl``
auto-record it), a file edit that invalidates earlier verification
(``{"event": "stale", "file_path": …}``), or a workspace merge that invalidates
the paths it landed.

SQLite replaces the old append-only JSONL so retention can DELETE rows instead
of rewriting the whole file. WAL mode keeps concurrent readers and writers
moving; the public class API is unchanged (``append`` / ``read_all`` /
``read_for_keys`` / ``read_for_session`` / ``mark_stale_for_path`` /
``for_session``). The full original record rides in ``payload_json`` while the
queryable fields are mirrored into columns, so unknown future fields survive a
round trip.

The legacy ``evidence-ledger.jsonl`` beside the DB is imported once (with a
per-line hash dedupe) and renamed to ``.migrated``. ``LEDGER_PATH`` remains a
class attribute so tests can redirect it into ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from config.features import EVIDENCE_CROSS_SESSION_STALENESS, EVIDENCE_LEDGER
from config.path import resolve_evidence_ledger_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event TEXT,
    file_path TEXT,
    command TEXT,
    status TEXT,
    exit_code INTEGER,
    kind TEXT,
    payload_json TEXT NOT NULL,
    legacy_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_evidence_session
    ON evidence_events(session_id, event_id);
CREATE INDEX IF NOT EXISTS idx_evidence_timestamp
    ON evidence_events(timestamp);
"""


class EvidenceLedger:
    """Class-level API over the shared SQLite evidence ledger."""

    # Repo absolute (never cwd-relative). Deliberately NOT session-scoped:
    # this ledger is the shared audit trail across every plan and session.
    LEDGER_PATH = str(resolve_evidence_ledger_path())

    # Schema/PRAGMA setup is once per DB path per process: re-running
    # ``executescript`` on every reader takes a write lock and made concurrent
    # readers collide with writers ("database is locked") despite busy_timeout.
    _init_lock = threading.Lock()
    _initialized_paths: set[str] = set()

    # ── Storage plumbing ──────────────────────────────────────────────────

    @classmethod
    def _db_path(cls) -> Path:
        return Path(cls.LEDGER_PATH)

    @classmethod
    def _legacy_path(cls) -> Path | None:
        """The pre-SQLite JSONL path, or None when it would be the DB itself."""
        db = cls._db_path()
        legacy = db.with_suffix(".jsonl")
        return None if legacy == db else legacy

    @classmethod
    @contextmanager
    def _connection(cls) -> Iterator[sqlite3.Connection]:
        db = cls._db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        key = str(db.resolve())
        # A test (or an operator) may delete the file between calls; do not
        # trust the cached flag for a path whose DB no longer exists.
        if not db.exists():
            cls._initialized_paths.discard(key)
        conn = sqlite3.connect(str(db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if key not in cls._initialized_paths:
                with cls._init_lock:
                    if key not in cls._initialized_paths:
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.executescript(_SCHEMA)
                        cls._migrate_legacy(conn)
                        cls._initialized_paths.add(key)
            yield conn
        finally:
            conn.close()

    @classmethod
    def _migrate_legacy(cls, conn: sqlite3.Connection) -> None:
        """Import an old JSONL file once; repeated runs are deduped by line hash."""
        legacy = cls._legacy_path()
        if legacy is None or not legacy.exists():
            return
        imported = 0
        try:
            with legacy.open(encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(
                            "evidence ledger migration: skipped an unparsable legacy line"
                        )
                        continue
                    if not isinstance(record, dict):
                        continue
                    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    if cls._insert(conn, record, legacy_key=key):
                        imported += 1
            conn.commit()
        except Exception:
            conn.rollback()
            logger.warning("evidence ledger JSONL migration failed (fail-open)", exc_info=True)
            return
        try:
            legacy.rename(legacy.with_suffix(legacy.suffix + ".migrated"))
        except FileExistsError:
            logger.debug(
                "evidence ledger: {} already exists",
                legacy.with_suffix(legacy.suffix + ".migrated"),
            )
        except OSError:
            logger.warning("evidence ledger: could not rename legacy JSONL", exc_info=True)
            return
        logger.info("evidence ledger: migrated {} legacy row(s) from {}", imported, legacy.name)

    @classmethod
    def _insert(
        cls, conn: sqlite3.Connection, record: dict, *, legacy_key: str | None = None
    ) -> bool:
        """Insert one record; ``False`` when a legacy hash was already imported."""
        payload = dict(record)
        payload_json = json.dumps(payload, ensure_ascii=False)
        exit_code = payload.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            exit_code = None
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO evidence_events
                    (timestamp, session_id, event, file_path, command, status,
                     exit_code, kind, payload_json, legacy_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("timestamp") or ""),
                    str(payload.get("session_id") or ""),
                    payload.get("event"),
                    payload.get("file_path"),
                    payload.get("command"),
                    payload.get("status"),
                    exit_code,
                    payload.get("kind"),
                    payload_json,
                    legacy_key,
                ),
            )
        except sqlite3.Error:
            logger.warning("evidence ledger insert failed (fail-open)", exc_info=True)
            return False
        return cursor.rowcount > 0

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        for key in (
            "timestamp",
            "session_id",
            "event",
            "file_path",
            "command",
            "status",
            "exit_code",
            "kind",
        ):
            value = row[key]
            if value is not None:
                payload[key] = value
        return payload

    # ── Public API (signatures unchanged from the JSONL implementation) ───

    @classmethod
    def append(cls, entry: dict) -> None:
        """Insert one entry, stamping a UTC timestamp; prune lazily afterwards.

        The caller's dict is copied, not mutated. Pruning is fail-open: a
        retention failure never loses the row that was just recorded.
        """
        record = dict(entry)
        record["timestamp"] = datetime.now(UTC).isoformat()
        with cls._connection() as conn:
            if not cls._insert(conn, record):
                return
            conn.commit()
            try:
                cls._prune_old_events(conn)
            except Exception:
                logger.warning("evidence ledger pruning failed (fail-open)", exc_info=True)

    @classmethod
    def read_all(cls) -> list[dict]:
        """Read every entry in insertion order; a missing DB is an empty ledger."""
        try:
            with cls._connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM evidence_events ORDER BY event_id ASC"
                ).fetchall()
            return [cls._record(row) for row in rows]
        except sqlite3.Error:
            logger.warning("evidence ledger read_all failed (fail-open)", exc_info=True)
            return []

    @classmethod
    def read_for_keys(cls, session_keys: Iterable[str]) -> dict[str, list[dict]]:
        """Group the requested sessions' rows, preserving insertion order."""
        wanted = [str(key).strip() for key in session_keys if str(key or "").strip()]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        try:
            with cls._connection() as conn:
                rows = conn.execute(
                    f"SELECT * FROM evidence_events WHERE session_id IN ({placeholders}) "
                    "ORDER BY event_id ASC",
                    tuple(wanted),
                ).fetchall()
        except sqlite3.Error:
            logger.warning("evidence ledger read_for_keys failed (fail-open)", exc_info=True)
            return {}
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(str(row["session_id"]), []).append(cls._record(row))
        return grouped

    @classmethod
    def for_session(cls, session_key: str) -> SessionEvidenceLedger:
        """Return a session-scoped view over the shared ledger."""
        return SessionEvidenceLedger(session_key)

    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]:
        """Read every entry whose ``session_id`` equals ``session_key``."""
        try:
            with cls._connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM evidence_events WHERE session_id = ? ORDER BY event_id ASC",
                    (session_key,),
                ).fetchall()
            return [cls._record(row) for row in rows]
        except sqlite3.Error:
            logger.warning("evidence ledger read_for_session failed (fail-open)", exc_info=True)
            return []

    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int:
        """Record a stale event; return how many earlier rows it affects.

        Staleness stays derived on read: an evidence row is stale iff a LATER
        ``{"event": "stale"}`` row names a ``file_path`` contained in its
        ``command``. The count is for logging only.
        """
        with cls._connection() as conn:
            conditions = ["event IS NULL", "instr(COALESCE(command, ''), ?) > 0"]
            params: list[object] = [file_path]
            if session_key:
                conditions.append("session_id = ?")
                params.append(session_key)
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM evidence_events WHERE {' AND '.join(conditions)}",
                    tuple(params),
                ).fetchone()[0]
            except sqlite3.Error:
                logger.warning("evidence ledger stale count failed (fail-open)", exc_info=True)
                count = 0
            record = {
                "event": "stale",
                "file_path": file_path,
                "session_id": session_key or "",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            if cls._insert(conn, record):
                conn.commit()
                try:
                    cls._prune_old_events(conn)
                except Exception:
                    logger.warning("evidence ledger pruning failed (fail-open)", exc_info=True)
        return int(count)

    @classmethod
    def mark_stale_for_path_all_sessions(cls, file_path: str, writer_session_id: str) -> int:
        """Broadcast a write to every OTHER session whose evidence names ``file_path``.

        The writer's own stale event is appended by
        :meth:`SessionEvidenceLedger.mark_stale_for_path`; this method covers the
        sessions that a later write can invalidate without touching their ledger
        scope. One ``{"event": "stale"}`` row is appended per affected session,
        carrying ``scope="cross_session"`` and ``source_session=<writer>``, so
        the existing per-session staleness derivation sees it without a schema
        change. Returns how many session rows were appended.

        Fail-open: a scan/insert failure only logs and reports 0.
        """
        if not file_path:
            return 0
        try:
            limit = max(1, int(EVIDENCE_CROSS_SESSION_STALENESS["max_sessions_scan"]))
            exclude_self = bool(EVIDENCE_CROSS_SESSION_STALENESS["exclude_self"])
            with cls._connection() as conn:
                conditions = [
                    "event IS NULL",
                    "session_id != ''",
                    "instr(COALESCE(command, ''), ?) > 0",
                ]
                params: list[object] = [file_path]
                if exclude_self and writer_session_id:
                    conditions.append("session_id != ?")
                    params.append(writer_session_id)
                params.append(limit)
                try:
                    sessions = [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT DISTINCT session_id FROM evidence_events "
                            f"WHERE {' AND '.join(conditions)} "
                            "ORDER BY session_id LIMIT ?",
                            tuple(params),
                        ).fetchall()
                        if str(row[0])
                    ]
                except sqlite3.Error:
                    logger.warning(
                        "evidence cross-session stale scan failed (fail-open)",
                        exc_info=True,
                    )
                    return 0
                if not sessions:
                    return 0
                timestamp = datetime.now(UTC).isoformat()
                inserted = 0
                for session_key in sessions:
                    record = {
                        "event": "stale",
                        "file_path": file_path,
                        "session_id": session_key,
                        "source_session": writer_session_id,
                        "scope": "cross_session",
                        "timestamp": timestamp,
                    }
                    if cls._insert(conn, record):
                        inserted += 1
                if not inserted:
                    return 0
                conn.commit()
                try:
                    cls._prune_old_events(conn)
                except Exception:
                    logger.warning("evidence ledger pruning failed (fail-open)", exc_info=True)
                return inserted
        except Exception:
            logger.warning(
                "evidence cross-session stale propagation failed (fail-open)", exc_info=True
            )
            return 0

    # ── Deletion (session purge / boot sweep) ─────────────────────────────

    @classmethod
    def purge_session(cls, session_id: str) -> int:
        """Delete one session's rows; fail-open and return the row count."""
        try:
            with cls._connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM evidence_events WHERE session_id = ?", (session_id,)
                )
                conn.commit()
                return int(cursor.rowcount)
        except Exception:
            logger.warning(
                "evidence purge_session failed (fail-open): {}", session_id, exc_info=True
            )
            return 0

    @classmethod
    def purge_orphans(cls, known_session_ids: Iterable[str]) -> int:
        """Delete rows whose session id is not in the known set.

        Empty session ids (unscoped stale events) are left alone. Returns the
        number of rows deleted; fail-open.
        """
        known = {str(sid) for sid in known_session_ids if str(sid or "").strip()}
        removed = 0
        try:
            with cls._connection() as conn:
                ids = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT DISTINCT session_id FROM evidence_events"
                    ).fetchall()
                ]
                for sid in ids:
                    if not sid or sid in known:
                        continue
                    cursor = conn.execute(
                        "DELETE FROM evidence_events WHERE session_id = ?", (sid,)
                    )
                    removed += int(cursor.rowcount)
                conn.commit()
            if removed:
                logger.info("evidence ledger: purged {} orphan row(s)", removed)
        except Exception:
            logger.warning("evidence purge_orphans failed (fail-open)", exc_info=True)
            return 0
        return removed

    # ── Retention ─────────────────────────────────────────────────────────

    @classmethod
    def _prune_old_events(cls, conn: sqlite3.Connection) -> int:
        """Three-layer lazy retention: age TTL, per-session cap, global cap."""
        if not EVIDENCE_LEDGER["pruning_enabled"]:
            return 0
        removed = 0
        try:
            max_age = int(EVIDENCE_LEDGER["max_age_days"])
            if max_age > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=max_age)).isoformat()
                cursor = conn.execute("DELETE FROM evidence_events WHERE timestamp < ?", (cutoff,))
                removed += cursor.rowcount

            per_session = int(EVIDENCE_LEDGER["max_events_per_session"])
            if per_session > 0:
                sessions = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT DISTINCT session_id FROM evidence_events"
                    ).fetchall()
                ]
                for sid in sessions:
                    cursor = conn.execute(
                        """
                        DELETE FROM evidence_events
                        WHERE event_id IN (
                            SELECT event_id FROM evidence_events
                            WHERE session_id = ?
                            ORDER BY event_id DESC
                            LIMIT -1 OFFSET ?
                        )
                        """,
                        (sid, per_session),
                    )
                    removed += cursor.rowcount

            max_total = int(EVIDENCE_LEDGER["max_total_events"])
            if max_total > 0:
                total = conn.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0]
                excess = int(total) - max_total
                if excess > 0:
                    cursor = conn.execute(
                        """
                        DELETE FROM evidence_events
                        WHERE event_id IN (
                            SELECT event_id FROM evidence_events
                            ORDER BY event_id ASC LIMIT ?
                        )
                        """,
                        (excess,),
                    )
                    removed += cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            logger.warning("evidence ledger pruning failed (fail-open)", exc_info=True)
            return 0
        return int(removed)


class SessionEvidenceLedger:
    """Session-scoped view over :class:`EvidenceLedger` (one shared ledger)."""

    def __init__(self, session_key: str):
        self.session_key = session_key

    def append(self, **entry_fields) -> None:
        """Append an evidence row, stamping this view's ``session_id``."""
        entry_fields["session_id"] = self.session_key
        EvidenceLedger.append(entry_fields)

    def list_records(self) -> list[dict]:
        """List this session's evidence rows."""
        return EvidenceLedger.read_for_session(self.session_key)

    def mark_stale_for_path(self, file_path: str) -> int:
        """Append a stale event for ``file_path`` scoped to this session."""
        return EvidenceLedger.mark_stale_for_path(file_path, self.session_key)


__all__ = ["EvidenceLedger", "SessionEvidenceLedger"]
