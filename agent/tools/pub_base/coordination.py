"""Per-project coordination store: path claims and peer findings.

The write-side protection (locks, write license, revision CAS) is mechanical:
it stops byte-level loss without knowing what anyone INTENDS to do. This store
adds the two pieces of shared intent a fan-out actually needs, both advisory
and both opt-in (``COORDINATION``):

* **claims** — "I am about to write these paths". A peer writing the same path
  gets one advisory line in its tool result; nothing is ever refused (a refusal
  path would stack claim → license → CAS into one decision and buy complexity
  far beyond the threat).
* **findings** — the FAIL ("tried this, it does not work") / FACT ("confirmed")
  / PATCH ("this file's interface just changed") notes a sibling can read back
  through a read-only tool instead of rediscovering them.

Storage is ONE SQLite file under the project's own ``.sherry/`` (created by
``WorkspaceNotice``; this module never creates it — a read-only query must not
leave files in a user's project). Every entry is scoped by the project root, so
two different projects never see each other's claims.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

__all__ = [
    "ClaimRow",
    "FindingRow",
    "active_claims",
    "claim_paths",
    "coordination_db_path",
    "record_finding",
    "release_claims",
    "search_findings",
]

#: Same 30 s budget as the other stores: contention waits instead of raising.
_BUSY_TIMEOUT_MS = 30_000

_DDL = (
    "CREATE TABLE IF NOT EXISTS claims ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " session_id TEXT NOT NULL,"
    " path TEXT NOT NULL,"
    " note TEXT NOT NULL DEFAULT '',"
    " created_at REAL NOT NULL,"
    " expires_at REAL NOT NULL,"
    " UNIQUE(session_id, path)"
    ")",
    "CREATE TABLE IF NOT EXISTS findings ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " session_id TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " text TEXT NOT NULL,"
    " files_json TEXT NOT NULL DEFAULT '[]',"
    " created_at REAL NOT NULL"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_claims_path ON claims(path)",
    "CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_session ON findings(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_kind ON findings(kind)",
)


@dataclass(frozen=True, slots=True)
class ClaimRow:
    """One active claim: who intends to write which path, and why."""

    session_id: str
    path: str
    note: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class FindingRow:
    """One shared finding."""

    session_id: str
    kind: str
    text: str
    files: tuple[str, ...]
    created_at: float


def coordination_db_path(project_root: Path) -> Path:
    """``<project>/.sherry/coordination.db`` — the per-project coordination store."""
    return Path(project_root) / ".sherry" / "coordination.db"


def _connect(project_root: Path) -> sqlite3.Connection | None:
    """Open the store, or ``None`` when the project has none yet (fail-open).

    A missing ``.sherry/`` means "this project has not been used by sherry
    yet" — creating it from a read path would litter the tree, so the caller
    gets ``None`` and answers empty.
    """
    db_path = coordination_db_path(project_root)
    if not db_path.parent.is_dir():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        logger.debug("coordination store unavailable: {}", exc)
        return None
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    for ddl in _DDL:
        conn.execute(ddl)
    return conn


def _close(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            logger.debug("coordination store close failed", exc_info=True)


def claim_paths(
    project_root: Path,
    session_id: str,
    paths: list[str],
    *,
    note: str = "",
    ttl_seconds: int = 1800,
) -> int:
    """Record (or refresh) this session's claim on each path; returns the count.

    Paths are already project-relative (the tool normalizes them). An empty
    list, a blank session or a missing store is a no-op. Fail-open: a store
    error logs and reports 0 — a claim is advice, never a precondition.
    """
    session = (session_id or "").strip()
    wanted = [path for path in (str(item).strip() for item in paths) if path]
    if not session or not wanted:
        return 0
    conn = _connect(project_root)
    if conn is None:
        return 0
    now = time.time()
    expires_at = now + max(1, int(ttl_seconds))
    try:
        with conn:
            conn.executemany(
                "INSERT INTO claims(session_id, path, note, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, path) DO UPDATE SET "
                " note = excluded.note, expires_at = excluded.expires_at",
                [(session, path, note, now, expires_at) for path in wanted],
            )
            conn.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
        return len(wanted)
    except sqlite3.Error as exc:
        logger.warning("claim write failed (fail-open): {}", exc)
        return 0
    finally:
        _close(conn)


def active_claims(
    project_root: Path,
    paths: list[str],
    *,
    exclude_session: str = "",
    limit: int = 10,
) -> list[ClaimRow]:
    """Active claims on *paths* by OTHER sessions (the advisory lookup)."""
    wanted = [path for path in (str(item).strip() for item in paths) if path]
    if not wanted:
        return []
    conn = _connect(project_root)
    if conn is None:
        return []
    try:
        placeholders = ",".join("?" for _ in wanted)
        params: list[object] = [*wanted, time.time()]
        sql = (
            "SELECT session_id, path, note, expires_at FROM claims "
            f"WHERE path IN ({placeholders}) AND expires_at > ?"
        )
        if exclude_session:
            sql += " AND session_id != ?"
            params.append(exclude_session)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        logger.debug("claim read failed (fail-open): {}", exc)
        return []
    finally:
        _close(conn)
    return [
        ClaimRow(
            session_id=str(row[0]), path=str(row[1]), note=str(row[2]), expires_at=float(row[3])
        )
        for row in rows
    ]


def release_claims(project_root: Path, session_id: str) -> int:
    """Drop every claim this session holds in the project; returns the count."""
    session = (session_id or "").strip()
    if not session:
        return 0
    conn = _connect(project_root)
    if conn is None:
        return 0
    try:
        with conn:
            cursor = conn.execute("DELETE FROM claims WHERE session_id = ?", (session,))
            return int(cursor.rowcount)
    except sqlite3.Error as exc:
        logger.warning("claim release failed (fail-open): {}", exc)
        return 0
    finally:
        _close(conn)


def record_finding(
    project_root: Path,
    session_id: str,
    kind: str,
    text: str,
    *,
    files: list[str] | None = None,
    max_text_chars: int = 2000,
) -> int | None:
    """Append one finding; returns its row id, or ``None`` (fail-open)."""
    session = (session_id or "").strip()
    body = (text or "").strip()[: max(1, int(max_text_chars))]
    if not session or not body:
        return None
    conn = _connect(project_root)
    if conn is None:
        return None
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO findings(session_id, kind, text, files_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session,
                    kind,
                    body,
                    json.dumps(list(files or []), ensure_ascii=False),
                    time.time(),
                ),
            )
            return int(cursor.lastrowid or 0)
    except sqlite3.Error as exc:
        logger.warning("finding write failed (fail-open): {}", exc)
        return None
    finally:
        _close(conn)


def search_findings(
    project_root: Path,
    *,
    query: str = "",
    kind: str = "",
    limit: int = 10,
) -> list[FindingRow]:
    """Read-only search: every whitespace-separated term must appear (AND).

    Matches ``text`` OR the JSON file list, case-insensitively. Newest first.
    """
    conn = _connect(project_root)
    if conn is None:
        return []
    try:
        sql = "SELECT session_id, kind, text, files_json, created_at FROM findings"
        clauses: list[str] = []
        params: list[object] = []
        for term in str(query or "").split():
            clauses.append("(LOWER(text) LIKE ? OR LOWER(files_json) LIKE ?)")
            pattern = f"%{term.lower()}%"
            params.extend([pattern, pattern])
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        logger.debug("finding read failed (fail-open): {}", exc)
        return []
    finally:
        _close(conn)
    results: list[FindingRow] = []
    for row in rows:
        try:
            files = tuple(str(item) for item in json.loads(row[3] or "[]"))
        except (ValueError, TypeError):
            files = ()
        results.append(
            FindingRow(
                session_id=str(row[0]),
                kind=str(row[1]),
                text=str(row[2]),
                files=files,
                created_at=float(row[4]),
            )
        )
    return results
