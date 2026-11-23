"""High-risk guard stores: keyword registry + executed-operation ledger.

One SQLite file (:func:`config.path.resolve_high_risk_guard_path`,
``src/data/high_risk_guard.db``) holds two tables:

* ``high_risk_keywords`` — the keyword registry. A keyword is either a
  ``block`` (scan + require approval) or ``allow`` (skip); the same table
  carries the one-shot ``executed`` marker and the first result summary, so a
  repeat of an already-executed operation is refused with the original result
  instead of running again (the verify-retest guard);
* ``high_risk_ops`` — the operation ledger for tools that are BOTH
  ``high_risk`` and non-idempotent (PTC ``execute_code`` and every MCP tool by
  default), keyed by ``(session, tool, args_hash)``. One row per call identity
  carries a lifecycle: ``in_flight`` (written BEFORE the handler runs, so an
  interrupted process leaves a marker), ``committed`` (the call returned) and
  ``failed`` (the tool reported an error; never blocks a retry). A row left
  ``in_flight`` by a previous boot is refused as "outcome unknown".

Scope: a main session owns its rows; every descendant resolves the ROOT main
session and passes that id, so a re-spawned TaskFlow step still sees the
earlier execution. The client panel and the ``high_risk_guard`` tool are the
two writers; the middleware only reads the effective blockers and records
executions.

Sync by design: the guardrail hot path runs a handful of indexed queries on a
short-lived connection whose FIRST pragma is ``busy_timeout`` (the same
contract as every other SQLite store here). Schema creation is once per
process per path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from loguru import logger

from config.features import HIGH_RISK_GUARD
from config.path import resolve_high_risk_guard_path

__all__ = [
    "HIGH_RISK_GUARD",
    "KeywordHit",
    "HighRiskGuardStore",
    "default_store",
    "scan_high_risk_keywords",
    "validate_keyword",
    "args_hash",
]

_BUILTIN = "builtin"
_ALLOW = "allow"
_BLOCK = "block"
_POLICIES = frozenset({_ALLOW, _BLOCK})
_BUSY_TIMEOUT_MS = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS high_risk_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    policy TEXT NOT NULL DEFAULT 'block',
    source TEXT NOT NULL DEFAULT 'user',
    executed INTEGER NOT NULL DEFAULT 0,
    result_summary TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_id, keyword)
);

CREATE TABLE IF NOT EXISTS high_risk_ops (
    op_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_preview TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    executed_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'committed',
    boot_id TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, tool_name, args_hash)
);
"""

#: Columns added after the first release. An existing database file keeps its
#: original CREATE TABLE shape, so these are applied with a PRAGMA-guarded
#: additive ``ALTER TABLE`` (the side-chat store's precedent) instead of
#: recreating the table.
_ADDED_OP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("status", "TEXT NOT NULL DEFAULT 'committed'"),
    ("boot_id", "TEXT NOT NULL DEFAULT ''"),
    ("pid", "INTEGER NOT NULL DEFAULT 0"),
)

#: This process's identity: an ``in_flight`` row written by ANOTHER boot means
#: the call was interrupted and its outcome is unknown. ``time.monotonic`` is
#: process-relative, so it cannot answer this question.
_BOOT_ID = uuid.uuid4().hex

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_high_risk_keywords_session "
    "ON high_risk_keywords(session_id, policy)",
    "CREATE INDEX IF NOT EXISTS idx_high_risk_ops_session "
    "ON high_risk_ops(session_id, executed_at)",
)

_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _now() -> int:
    """Unix seconds (the store's only clock)."""
    return int(time.time())


def args_hash(tool_name: str, args: dict[str, Any] | None) -> str:
    """Stable digest of one tool call's arguments (sorted keys, no whitespace)."""
    try:
        payload = json.dumps(
            {"tool": tool_name, "args": args or {}},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        payload = f"{tool_name}:{args!r}"
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _preview(args: dict[str, Any] | None, limit: int = 300) -> str:
    """A bounded single-line preview of the call arguments for the panel."""
    try:
        text = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class KeywordHit:
    """One scanned keyword match and a little surrounding context."""

    keyword: str
    context: str = ""


def scan_high_risk_keywords(text: str, keywords: Iterable[str]) -> KeywordHit | None:
    """Return the first keyword contained in *text* (case-insensitive substring).

    No word boundaries on purpose: ``password`` must match
    ``user_password=…`` — the scan is a tripwire, not a parser.
    """
    lowered = str(text or "").lower()
    for keyword in keywords:
        needle = str(keyword or "").strip().lower()
        if not needle:
            continue
        position = lowered.find(needle)
        if position >= 0:
            start = max(0, position - 20)
            end = min(len(text), position + len(needle) + 20)
            return KeywordHit(keyword=str(keyword), context=text[start:end])
    return None


def validate_keyword(keyword: str | None) -> str | None:
    """Return ``None`` when *keyword* is usable, else the refusal message.

    Length is counted in Unicode characters (Python's ``len``): ``密码`` is 2
    and refused, ``银行卡`` is 3 and accepted — a two-character keyword would
    match nearly every Chinese sentence.
    """
    cleaned = str(keyword or "").strip()
    minimum = int(HIGH_RISK_GUARD["min_keyword_chars"])
    maximum = int(HIGH_RISK_GUARD["max_keyword_chars"])
    if not cleaned:
        return "关键字不能为空 / keyword must not be empty"
    if len(cleaned) < minimum:
        return (
            f"关键字长度不能小于 {minimum} 个字符（当前 {len(cleaned)} 个字符）。"
            f"过短的关键字会误拦大量正常操作。 / keyword must be at least {minimum} characters"
        )
    if len(cleaned) > maximum:
        return f"关键字长度不能超过 {maximum} 个字符 / keyword must be at most {maximum} characters"
    return None


class HighRiskGuardStore:
    """Keyword registry + operation ledger over one SQLite file."""

    def __init__(self, path: str | None = None) -> None:
        """Bind the store to *path* (default: the configured guard database)."""
        from pathlib import Path

        self.path = Path(path) if path is not None else resolve_high_risk_guard_path()
        # ``None`` = never swept; a monotonic seconds value otherwise.
        self._last_purge: float | None = None

    # ── connection / schema ─────────────────────────────────────────────

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        key = str(self.path)
        if key in _INITIALIZED_PATHS:
            return
        with _INIT_LOCK:
            if key in _INITIALIZED_PATHS:
                return
            conn.executescript(_SCHEMA)
            for ddl in _INDEXES:
                conn.execute(ddl)
            existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(high_risk_ops)")}
            for name, declaration in _ADDED_OP_COLUMNS:
                if name not in existing:
                    # Additive, PRAGMA-guarded: an older database file keeps its
                    # rows and gains the new columns with their defaults.
                    conn.execute(f"ALTER TABLE high_risk_ops ADD COLUMN {name} {declaration}")
            conn.commit()
            _INITIALIZED_PATHS.add(key)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.row_factory = sqlite3.Row
            self._ensure_schema(conn)
            yield conn
        finally:
            conn.close()

    def _maybe_purge(self, conn: sqlite3.Connection) -> None:
        """Lazy retention sweep (at most once an hour per store instance)."""
        now = time.monotonic()
        if self._last_purge is not None and now - self._last_purge < 3600:
            return
        self._last_purge = now
        cutoff = _now() - int(HIGH_RISK_GUARD["retention_hours"]) * 3600
        try:
            conn.execute("DELETE FROM high_risk_ops WHERE executed_at < ?", (cutoff,))
            conn.execute(
                "UPDATE high_risk_keywords SET executed = 0, result_summary = '' "
                "WHERE executed = 1 AND updated_at < ?",
                (cutoff,),
            )
            conn.commit()
        except sqlite3.Error:
            logger.debug("high-risk guard retention sweep failed", exc_info=True)

    # ── keyword registry ────────────────────────────────────────────────

    def _keyword_rows(self, conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM high_risk_keywords WHERE session_id IN ('global', ?) "
                "ORDER BY session_id, keyword",
                (session_id,),
            )
        )

    def list_keywords(self, session_id: str) -> list[dict[str, Any]]:
        """Every keyword the session sees: built-ins plus custom registrations.

        A built-in appears once; its effective policy/executed state comes from
        the session (then global) override row when one exists, else the
        built-in default (``block``, not executed).
        """
        with self._connection() as conn:
            self._maybe_purge(conn)
            rows = self._keyword_rows(conn, session_id)
        scoped: dict[str, sqlite3.Row] = {}
        for row in rows:
            keyword = str(row["keyword"])
            # Session rows are read last in SQL order (session_id > 'global'),
            # so a later row overwrites the global one.
            scoped[keyword] = row
        builtins = tuple(HIGH_RISK_GUARD["builtin_keywords"])
        out: list[dict[str, Any]] = []
        for keyword in builtins:
            row = scoped.pop(keyword, None)
            out.append(
                {
                    "id": int(row["id"]) if row is not None else None,
                    "keyword": keyword,
                    "policy": str(row["policy"]) if row is not None else _BLOCK,
                    "source": _BUILTIN,
                    "executed": bool(row["executed"]) if row is not None else False,
                    "result_summary": str(row["result_summary"]) if row is not None else "",
                    "scope": str(row["session_id"]) if row is not None else _BUILTIN,
                }
            )
        for keyword, row in sorted(scoped.items()):
            out.append(
                {
                    "id": int(row["id"]),
                    "keyword": keyword,
                    "policy": str(row["policy"]),
                    "source": str(row["source"]),
                    "executed": bool(row["executed"]),
                    "result_summary": str(row["result_summary"] or ""),
                    "scope": str(row["session_id"]),
                }
            )
        return out

    def load_active_blockers(self, session_id: str) -> list[str]:
        """Keywords a scan must treat as ``block`` for *session_id*.

        Built-ins default to block; a global/session ``allow`` row removes any
        keyword from the list (the allow wins over a block in either scope).
        """
        with self._connection() as conn:
            rows = self._keyword_rows(conn, session_id)
        allowed = {str(row["keyword"]) for row in rows if row["policy"] == _ALLOW}
        custom_block = [
            str(row["keyword"])
            for row in rows
            if row["policy"] == _BLOCK and str(row["keyword"]) not in allowed
        ]
        blockers = [kw for kw in HIGH_RISK_GUARD["builtin_keywords"] if kw not in allowed]
        for keyword in custom_block:
            if keyword not in blockers and keyword not in allowed:
                blockers.append(keyword)
        return blockers

    def get_keyword(self, session_id: str, keyword: str) -> dict[str, Any] | None:
        """The effective entry for one keyword (session, then global, then builtin)."""
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return None
        with self._connection() as conn:
            rows = self._keyword_rows(conn, session_id)
        for row in reversed(rows):
            if str(row["keyword"]) == cleaned:
                return {
                    "id": int(row["id"]),
                    "keyword": cleaned,
                    "policy": str(row["policy"]),
                    "source": str(row["source"]),
                    "executed": bool(row["executed"]),
                    "result_summary": str(row["result_summary"] or ""),
                    "scope": str(row["session_id"]),
                }
        if cleaned in HIGH_RISK_GUARD["builtin_keywords"]:
            return {
                "id": None,
                "keyword": cleaned,
                "policy": _BLOCK,
                "source": _BUILTIN,
                "executed": False,
                "result_summary": "",
                "scope": _BUILTIN,
            }
        return None

    def add_keyword(
        self,
        session_id: str,
        keyword: str,
        *,
        source: str = "agent",
        policy: str = _BLOCK,
        scope: str = "session",
    ) -> tuple[bool, str]:
        """Register/overwrite one keyword; returns ``(ok, message)``."""
        error = validate_keyword(keyword)
        if error is not None:
            return False, error
        if policy not in _POLICIES:
            return False, f"policy must be one of: {', '.join(sorted(_POLICIES))}"
        cleaned = str(keyword).strip()
        target = "global" if scope == "global" else session_id
        if not target:
            return False, "session_id is required"
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO high_risk_keywords "
                "(session_id, keyword, policy, source, executed, result_summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, '', ?, ?) "
                "ON CONFLICT(session_id, keyword) DO UPDATE SET "
                "policy = excluded.policy, source = excluded.source, updated_at = excluded.updated_at",
                (target, cleaned, policy, source, now, now),
            )
            conn.commit()
        return True, f"keyword '{cleaned}' registered ({target}, {policy})"

    def set_policy(self, session_id: str, keyword: str, policy: str) -> tuple[bool, str]:
        """Switch an existing (or built-in) keyword between block and allow."""
        if policy not in _POLICIES:
            return False, f"policy must be one of: {', '.join(sorted(_POLICIES))}"
        cleaned = str(keyword or "").strip()
        entry = self.get_keyword(session_id, cleaned)
        if entry is None:
            return False, f"unknown keyword: {cleaned!r}"
        # Write the override at the row's own scope when one exists; otherwise a
        # session-scoped row shadows the built-in default.
        target = entry["scope"] if entry["scope"] in {"global", session_id} else session_id
        source = _BUILTIN if cleaned in HIGH_RISK_GUARD["builtin_keywords"] else entry["source"]
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO high_risk_keywords "
                "(session_id, keyword, policy, source, executed, result_summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, '', ?, ?) "
                "ON CONFLICT(session_id, keyword) DO UPDATE SET "
                "policy = excluded.policy, updated_at = excluded.updated_at",
                (target, cleaned, policy, source, now, now),
            )
            conn.commit()
        return True, f"keyword '{cleaned}' policy={policy}"

    def remove_keyword(self, session_id: str, keyword_or_id: str | int) -> tuple[bool, str]:
        """Delete a custom keyword row (a built-in override returns to default)."""
        with self._connection() as conn:
            if isinstance(keyword_or_id, int):
                row = conn.execute(
                    "SELECT keyword, session_id FROM high_risk_keywords WHERE id = ?",
                    (keyword_or_id,),
                ).fetchone()
                if row is None:
                    return False, f"unknown keyword id: {keyword_or_id}"
                conn.execute("DELETE FROM high_risk_keywords WHERE id = ?", (keyword_or_id,))
                conn.commit()
                return True, f"keyword '{row['keyword']}' removed"
            cleaned = str(keyword_or_id or "").strip()
            # Session row first, then the global row.
            cursor = conn.execute(
                "DELETE FROM high_risk_keywords WHERE keyword = ? AND session_id IN ('global', ?)",
                (cleaned, session_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return False, f"unknown keyword: {cleaned!r}"
            return True, f"keyword '{cleaned}' removed"

    def mark_executed(self, session_id: str, keyword: str, result_summary: str) -> bool:
        """Mark one keyword as executed in this session and store its result."""
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return False
        now = _now()
        summary = " ".join(str(result_summary or "").split())[:500]
        with self._connection() as conn:
            row = conn.execute(
                "SELECT session_id FROM high_risk_keywords "
                "WHERE keyword = ? AND session_id IN ('global', ?) "
                "ORDER BY session_id = 'global' LIMIT 1",
                (cleaned, session_id),
            ).fetchone()
            target = str(row["session_id"]) if row is not None else session_id
            source = _BUILTIN if cleaned in HIGH_RISK_GUARD["builtin_keywords"] else "agent"
            conn.execute(
                "INSERT INTO high_risk_keywords "
                "(session_id, keyword, policy, source, executed, result_summary, created_at, updated_at) "
                "VALUES (?, ?, 'block', ?, 1, ?, ?, ?) "
                "ON CONFLICT(session_id, keyword) DO UPDATE SET "
                "executed = 1, result_summary = excluded.result_summary, "
                "updated_at = excluded.updated_at",
                (target, cleaned, source, summary, now, now),
            )
            conn.commit()
        return True

    def reset_executed(self, session_id: str, keyword: str) -> bool:
        """Forget the executed marker of one keyword (the panel's re-run path)."""
        cleaned = str(keyword or "").strip()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE high_risk_keywords SET executed = 0, result_summary = '', updated_at = ? "
                "WHERE keyword = ? AND session_id IN ('global', ?)",
                (_now(), cleaned, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ── executed-operation ledger ───────────────────────────────────────

    def check_op(self, session_id: str, tool_name: str, args: dict[str, Any] | None) -> dict | None:
        """The recorded execution row for this exact call, or ``None``.

        The row's ``status`` decides what it means: ``committed`` ran to
        completion, ``in_flight`` is running (or was interrupted before it
        reported — compare ``boot_id``), ``failed`` reported an error.
        :meth:`check_blocking` applies that policy; this is the raw read.
        """
        digest = args_hash(tool_name, args)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM high_risk_ops WHERE session_id = ? AND tool_name = ? AND args_hash = ?",
                (session_id, tool_name, digest),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["op_id"]),
            "tool_name": str(row["tool_name"]),
            "args_preview": str(row["args_preview"]),
            "result_summary": str(row["result_summary"]),
            "executed_at": int(row["executed_at"]),
            "status": str(row["status"] or "committed"),
            "boot_id": str(row["boot_id"] or ""),
            "pid": int(row["pid"] or 0),
        }

    def check_blocking(
        self, session_id: str, tool_name: str, args: dict[str, Any] | None
    ) -> tuple[str, dict[str, Any]] | None:
        """``(reason, row)`` when this call must be refused, else ``None``.

        Reasons: ``committed`` (already ran), ``in_flight`` (running right now
        in this process) and ``unknown`` — a row this process left in flight
        when it died, whose side effects are therefore unknown. A ``failed``
        row never blocks: the tool itself reported an error, so a retry is
        legitimate.
        """
        row = self.check_op(session_id, tool_name, args)
        if row is None:
            return None
        status = row["status"]
        if status == "committed":
            return ("committed", row)
        if status == "in_flight":
            return ("in_flight" if row["boot_id"] == _BOOT_ID else "unknown", row)
        return None

    def record_intent(self, session_id: str, tool_name: str, args: dict[str, Any] | None) -> None:
        """Mark this exact call as in flight BEFORE the handler runs.

        The upsert deliberately never rewrites a ``committed`` row: a late or
        concurrent intent must not erase the refusal reason and the first
        result summary.
        """
        digest = args_hash(tool_name, args)
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO high_risk_ops "
                "(session_id, tool_name, args_hash, args_preview, result_summary, "
                " executed_at, status, boot_id, pid) "
                "VALUES (?, ?, ?, ?, '', ?, 'in_flight', ?, ?) "
                "ON CONFLICT(session_id, tool_name, args_hash) DO UPDATE SET "
                "status = 'in_flight', boot_id = excluded.boot_id, pid = excluded.pid, "
                "args_preview = excluded.args_preview, executed_at = excluded.executed_at "
                "WHERE high_risk_ops.status != 'committed'",
                (session_id, tool_name, digest, _preview(args), now, _BOOT_ID, os.getpid()),
            )
            conn.commit()

    def commit_op(
        self,
        session_id: str,
        tool_name: str,
        args: dict[str, Any] | None,
        result_summary: str,
    ) -> None:
        """Record one successful execution (idempotent by call identity)."""
        digest = args_hash(tool_name, args)
        now = _now()
        summary = " ".join(str(result_summary or "").split())[:500]
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO high_risk_ops "
                "(session_id, tool_name, args_hash, args_preview, result_summary, "
                " executed_at, status, boot_id, pid) "
                "VALUES (?, ?, ?, ?, ?, ?, 'committed', ?, ?) "
                "ON CONFLICT(session_id, tool_name, args_hash) DO UPDATE SET "
                "result_summary = excluded.result_summary, executed_at = excluded.executed_at, "
                "status = 'committed', boot_id = excluded.boot_id, pid = excluded.pid",
                (
                    session_id,
                    tool_name,
                    digest,
                    _preview(args),
                    summary,
                    now,
                    _BOOT_ID,
                    os.getpid(),
                ),
            )
            conn.commit()

    def mark_failed(self, session_id: str, tool_name: str, args: dict[str, Any] | None) -> None:
        """Mark an in-flight row as failed (visible in the panel, never blocking)."""
        digest = args_hash(tool_name, args)
        with self._connection() as conn:
            conn.execute(
                "UPDATE high_risk_ops SET status = 'failed' "
                "WHERE session_id = ? AND tool_name = ? AND args_hash = ? "
                "AND status != 'committed'",
                (session_id, tool_name, digest),
            )
            conn.commit()

    def record_op(
        self,
        session_id: str,
        tool_name: str,
        args: dict[str, Any] | None,
        result_summary: str,
    ) -> None:
        """Record one execution as committed (also the preventive registry path)."""
        self.commit_op(session_id, tool_name, args, result_summary)

    def list_ops(self, session_id: str) -> list[dict[str, Any]]:
        """Every recorded high-risk execution for the session, newest first.

        ``failed`` rows are hidden: they are not a repeat the panel should
        offer to delete-and-rerun (the tool already reported the failure), and
        hiding them keeps the existing panel response shape truthful.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM high_risk_ops WHERE session_id = ? AND status != 'failed' "
                "ORDER BY executed_at DESC",
                (session_id,),
            ).fetchall()
        return [
            {
                "id": int(row["op_id"]),
                "tool_name": str(row["tool_name"]),
                "args_preview": str(row["args_preview"]),
                "result_summary": str(row["result_summary"]),
                "executed_at": int(row["executed_at"]),
                "status": str(row["status"] or "committed"),
            }
            for row in rows
        ]

    def remove_op(self, session_id: str, op_id: int) -> bool:
        """Delete one ledger row (the user's explicit re-run)."""
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM high_risk_ops WHERE op_id = ? AND session_id = ?",
                (op_id, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ── lifecycle ───────────────────────────────────────────────────────

    def purge_session(self, session_id: str) -> None:
        """Drop one session's keyword rows and ledger rows (never the global ones)."""
        with self._connection() as conn:
            conn.execute("DELETE FROM high_risk_keywords WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM high_risk_ops WHERE session_id = ?", (session_id,))
            conn.commit()


def default_store() -> HighRiskGuardStore:
    """A store bound to the configured database path (cheap: connects lazily)."""
    return HighRiskGuardStore()
