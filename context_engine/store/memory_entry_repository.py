"""SQLite persistence for the long-term memory entries.

The three memory targets (``memory`` / ``user`` / ``facts``) used to be three
Markdown files with ``§``-delimited entries. They now share the MesMemory SQLite
database: one row per entry, with provenance (``origin_session``), usage
tracking (``usage_count`` / ``last_used``) and soft deletion (``archived``).

Why SQLite here:

* the memory panel needs per-entry CRUD, stable ids and archive/restore — a file
  rewrite has none of those;
* concurrent writers (main agent, background reviews, session-end salvage) are serialized by
  the store's single connection instead of a per-file lock;
* usage-aware eviction is a WHERE clause rather than a file parse.

Determinism for the system prompt is preserved by the caller: entries are read
ordered by ``created_at ASC, id ASC``, exactly like the old top-to-bottom file
order, so the rendered prompt is stable and remains a good prefix-cache citizen.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from context_engine.store.db import ConnectionLike, build_memory_schema, get_db

#: The three long-term memory targets. Kept here because every repository read
#: validates against it; ``agent.tools.memory`` imports this tuple.
MEMORY_TARGETS: tuple[str, ...] = ("memory", "user", "facts")

#: Allowed ``archived_reason`` values (the UI offers the first three).
ARCHIVE_REASONS: tuple[str, ...] = (
    "manual",
    "evicted",
    "contradicted",
    "consolidated",
    "superseded",
)

_SORT_COLUMNS: dict[str, str] = {
    "created": "created_at",
    "updated": "COALESCE(updated_at, created_at)",
    "usage": "usage_count",
    "last_used": "COALESCE(last_used, created_at)",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pack_embedding(vector: list[float]) -> bytes:
    import array

    return array.array("d", vector).tobytes()


def _unpack_embedding(blob: bytes) -> list[float]:
    import array

    values = array.array("d")
    values.frombytes(blob)
    return values.tolist()


def _validate_target(target: str) -> str:
    if target not in MEMORY_TARGETS:
        raise ValueError(f"Invalid memory target: {target!r}")
    return target


class MemoryEntryRepository:
    """CRUD + usage/aging queries over ``memory_entries``.

    ``db_path`` is for tests and one-off tools: when set, the repository owns a
    private connection and creates the schema on first use. Production passes
    nothing and reuses the process-wide serialized MesMemory connection.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._conn: ConnectionLike | None = None
        self._lock = threading.RLock()

    # -- connection ---------------------------------------------------------

    @property
    def db(self) -> ConnectionLike:
        if self._db_path is None:
            return get_db()
        with self._lock:
            if self._conn is None:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    self._db_path,
                    check_same_thread=False,
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                build_memory_schema(conn)
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def transaction(self) -> Iterator[ConnectionLike]:
        """One explicit transaction (migration imports are all-or-nothing).

        The shared MesMemory handle serializes statements ONE at a time, so
        BEGIN/COMMIT alone do not isolate the unit: a concurrent SELECT could
        land between two INSERTs on the same connection and observe a
        half-applied write (the PUT /memory path runs at runtime, not just at
        boot). The proxy's begin/end pair holds its statement lock for the whole
        transaction; a private SQLite connection (tests / one-off tools) has no
        proxy and keeps the plain BEGIN/COMMIT behavior.
        """
        with self._lock:
            conn = self.db
            begin = getattr(conn, "begin_transaction", None)
            if begin is not None:
                begin()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                end = getattr(conn, "end_transaction", None)
                if end is not None:
                    end()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            conn = self.db
            cursor = conn.execute(sql, params)
            if not conn.in_transaction:
                conn.commit()
            return cursor

    # -- reads --------------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM memory_entries WHERE id = ?", (entry_id,)).fetchone()
        return self._row(row)

    def list_entries(
        self,
        target: str | None = None,
        *,
        include_archived: bool = False,
        sort: str = "created",
        order: str = "desc",
        limit: int | None = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List entries with the UI's sort/filter modes.

        ``order="asc"`` with ``sort="created"`` is the system-prompt read: the
        old file order (top-to-bottom) and therefore a deterministic prefix.
        """
        if target is not None:
            _validate_target(target)
        column = _SORT_COLUMNS.get(sort)
        if column is None:
            raise ValueError(f"Invalid memory sort: {sort!r}")
        direction = "ASC" if order.lower() == "asc" else "DESC"
        clauses: list[str] = []
        params: list[Any] = []
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        if not include_archived:
            clauses.append("archived = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM memory_entries {where} ORDER BY {column} {direction}, id {direction}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((int(limit), int(offset)))
        rows = self.db.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def active_entries_ordered(self, target: str) -> list[dict[str, Any]]:
        """The prompt/capacity view: active entries in creation order."""
        return self.list_entries(target, sort="created", order="asc", limit=None)

    def get_entries_by_ids(self, entry_ids: list[int]) -> list[dict[str, Any]]:
        if not entry_ids:
            return []
        placeholders = ",".join("?" for _ in entry_ids)
        rows = self.db.execute(
            f"SELECT * FROM memory_entries WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            tuple(int(i) for i in entry_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_missing_embeddings(
        self, target: str | None = None, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Active entries whose embedding has not been generated yet."""
        clauses = ["archived = 0", "embedding IS NULL"]
        params: list[Any] = []
        if target is not None:
            clauses.append("target = ?")
            params.append(_validate_target(target))
        rows = self.db.execute(
            "SELECT * FROM memory_entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id ASC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_with_embeddings(
        self, target: str | None = None
    ) -> list[tuple[dict[str, Any], list[float]]]:
        """Active entries that carry an embedding, decoded to float lists."""
        clauses = ["archived = 0", "embedding IS NOT NULL"]
        params: list[Any] = []
        if target is not None:
            clauses.append("target = ?")
            params.append(_validate_target(target))
        rows = self.db.execute(
            "SELECT * FROM memory_entries WHERE " + " AND ".join(clauses) + " ORDER BY id ASC",
            tuple(params),
        ).fetchall()
        result: list[tuple[dict[str, Any], list[float]]] = []
        for row in rows:
            entry = dict(row)
            blob = entry.pop("embedding", None)
            if not blob:
                continue
            result.append((entry, _unpack_embedding(blob)))
        return result

    def set_embedding(
        self,
        entry_id: int,
        embedding: list[float],
        *,
        model: str,
    ) -> None:
        """Persist one entry's vector (idempotent; callers batch externally)."""
        self._write(
            "UPDATE memory_entries SET embedding = ?, embedding_model = ?, embedding_dim = ? "
            "WHERE id = ? AND archived = 0",
            (_pack_embedding(embedding), model, len(embedding), entry_id),
        )

    def count_embeddings(self, target: str | None = None) -> int:
        """Active entries that already carry a vector."""
        clauses = ["archived = 0", "embedding IS NOT NULL"]
        params: list[Any] = []
        if target is not None:
            clauses.append("target = ?")
            params.append(_validate_target(target))
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM memory_entries WHERE " + " AND ".join(clauses),
            tuple(params),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def find_active(self, target: str, substring: str) -> list[dict[str, Any]]:
        """Case-sensitive substring matches, in creation order (tool semantics)."""
        _validate_target(target)
        return [
            entry for entry in self.active_entries_ordered(target) if substring in entry["content"]
        ]

    def total_char_count(self, target: str) -> int:
        _validate_target(target)
        row = self.db.execute(
            "SELECT COALESCE(SUM(char_count), 0) AS total FROM memory_entries "
            "WHERE target = ? AND archived = 0",
            (target,),
        ).fetchone()
        return int(row["total"] if row is not None else 0)

    def count_entries(self) -> int:
        """Every row, archived included (migration's idempotency probe)."""
        row = self.db.execute("SELECT COUNT(*) AS n FROM memory_entries").fetchone()
        return int(row["n"] if row is not None else 0)

    def count(self, target: str | None = None, *, include_archived: bool = False) -> int:
        """Rows for the UI's pager (active by default)."""
        if target is not None:
            _validate_target(target)
        clauses: list[str] = []
        params: list[Any] = []
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        if not include_archived:
            clauses.append("archived = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.db.execute(
            f"SELECT COUNT(*) AS n FROM memory_entries {where}", tuple(params)
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def stats(self) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(
            "SELECT target, "
            "COALESCE(SUM(CASE WHEN archived = 0 THEN 1 ELSE 0 END), 0) AS count, "
            "COALESCE(SUM(CASE WHEN archived = 0 THEN char_count ELSE 0 END), 0) AS chars, "
            "COALESCE(AVG(CASE WHEN archived = 0 THEN usage_count END), 0) AS avg_usage, "
            "COALESCE(SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END), 0) AS archived "
            "FROM memory_entries GROUP BY target"
        ).fetchall()
        result = {
            target: {"count": 0, "chars": 0, "avg_usage": 0.0, "archived": 0}
            for target in MEMORY_TARGETS
        }
        for row in rows:
            result[str(row["target"])] = {
                "count": int(row["count"]),
                "chars": int(row["chars"]),
                "avg_usage": float(row["avg_usage"]),
                "archived": int(row["archived"]),
            }
        return result

    # -- writes -------------------------------------------------------------

    def add_entry(
        self,
        target: str,
        content: str,
        *,
        entry_type: str | None = None,
        origin_session: str | None = None,
        compaction_epoch: int = 0,
        created_at: int | None = None,
    ) -> dict[str, Any]:
        _validate_target(target)
        created = int(created_at) if created_at is not None else _now_ms()
        cursor = self._write(
            "INSERT INTO memory_entries "
            "(target, content, entry_type, origin_session, created_at, updated_at, "
            "usage_count, char_count, archived, compaction_epoch) "
            "VALUES (?, ?, ?, ?, ?, NULL, 0, ?, 0, ?)",
            (
                target,
                content,
                entry_type,
                origin_session,
                created,
                len(content),
                int(compaction_epoch),
            ),
        )
        lastrowid = cursor.lastrowid
        assert lastrowid is not None
        entry = self.get_entry(int(lastrowid))
        assert entry is not None
        return entry

    def update_entry(
        self,
        entry_id: int,
        *,
        content: str | None = None,
        entry_type: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_entry(entry_id)
        if current is None:
            return None
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [_now_ms()]
        if content is not None:
            fields.extend(
                [
                    "content = ?",
                    "char_count = ?",
                    "embedding = NULL",
                    "embedding_model = NULL",
                    "embedding_dim = NULL",
                ]
            )
            params.extend([content, len(content)])
        if entry_type is not None:
            fields.append("entry_type = ?")
            params.append(entry_type)
        params.append(entry_id)
        self._write(
            f"UPDATE memory_entries SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )
        return self.get_entry(entry_id)

    def archive_entry(self, entry_id: int, reason: str = "manual") -> dict[str, Any] | None:
        if reason not in ARCHIVE_REASONS:
            raise ValueError(f"Invalid archive reason: {reason!r}")
        self._write(
            "UPDATE memory_entries SET archived = 1, archived_at = ?, archived_reason = ? "
            "WHERE id = ? AND archived = 0",
            (_now_ms(), reason, entry_id),
        )
        return self.get_entry(entry_id)

    def restore_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._write(
            "UPDATE memory_entries SET archived = 0, archived_at = NULL, archived_reason = NULL "
            "WHERE id = ?",
            (entry_id,),
        )
        return self.get_entry(entry_id)

    def increment_usage(self, entry_id: int, *, used_at: int | None = None) -> None:
        self._write(
            "UPDATE memory_entries SET usage_count = usage_count + 1, last_used = ? "
            "WHERE id = ? AND archived = 0",
            (int(used_at) if used_at is not None else _now_ms(), entry_id),
        )

    def prune_unused(self, max_unused_days: int, batch_size: int = 200) -> int:
        """Archive entries untouched for longer than ``max_unused_days``.

        Age is ``COALESCE(last_used, created_at)``; a never-used entry ages from
        its creation. Soft delete only — the panel can restore it.
        """
        if max_unused_days <= 0:
            return 0
        cutoff = _now_ms() - int(max_unused_days) * 86_400_000
        rows = self.db.execute(
            "SELECT id FROM memory_entries "
            "WHERE archived = 0 AND COALESCE(last_used, created_at) < ? "
            "ORDER BY COALESCE(last_used, created_at) ASC, id ASC LIMIT ?",
            (cutoff, int(batch_size)),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        for entry_id in ids:
            self.archive_entry(entry_id, "evicted")
        return len(ids)

    def delete_all(self, target: str | None = None) -> int:
        """Hard delete (tests and a full reset; the UI archives instead)."""
        if target is not None:
            _validate_target(target)
            cursor = self._write("DELETE FROM memory_entries WHERE target = ?", (target,))
        else:
            cursor = self._write("DELETE FROM memory_entries")
        return int(cursor.rowcount or 0)

    # -- UI-editable settings ----------------------------------------------

    def get_settings(self) -> dict[str, str]:
        rows = self.db.execute("SELECT key, value FROM memory_settings").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def clear_settings(self) -> None:
        """Drop every UI override (returns the store to the code defaults)."""
        self._write("DELETE FROM memory_settings")

    def set_settings(self, values: dict[str, Any]) -> None:
        if not values:
            return
        with self._lock:
            conn = self.db
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO memory_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(key), str(value)),
                )
            if not conn.in_transaction:
                conn.commit()


#: Process-wide repository over the MesMemory connection. Tests replace the
#: module-level object (``memory_module.memory_entry_repo``) or inject one.
memory_entry_repo: MemoryEntryRepository = MemoryEntryRepository()
