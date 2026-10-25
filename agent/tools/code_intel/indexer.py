"""SQLite-backed tree-sitter symbol index and call graph.

Streaming by design: files are walked one at a time, parsed, written and
released, so a repository's full AST never lives in memory. The walk is
incremental (``mtime`` + size in ``index_meta``) and bounded (max files,
per-file byte ceiling, time budget). Every failure mode is fail-open — a
syntax-error file, an oversized file, an unknown extension or a read error
records an ``index_meta`` row and is skipped; it never aborts a tool call.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from agent.tools.pub_base import should_skip_dir
from config.features.agent_side import CODE_INTEL, CodeIntelConfig

from .extract import CallSite, SymbolDef, canonical_language, extract_symbols

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS symbols (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        parent_id INTEGER,
        language TEXT NOT NULL,
        source_snippet TEXT,
        doc_string TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS call_edges (
        id INTEGER PRIMARY KEY,
        caller_id INTEGER NOT NULL,
        callee_id INTEGER,
        callee_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        line INTEGER NOT NULL,
        resolved INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_meta (
        file_path TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        language TEXT NOT NULL,
        indexed_at REAL NOT NULL,
        symbol_count INTEGER,
        error TEXT
    )
    """,
    # Parallel edge tables (SHERRY-PATCH). ``call_edges`` is left untouched so an
    # older index keeps working; each new table is created on first open.
    """
    CREATE TABLE IF NOT EXISTS import_edges (
        id INTEGER PRIMARY KEY,
        importer_id INTEGER NOT NULL,   -- 0 = module scope
        imported_name TEXT NOT NULL,
        imported_id INTEGER,
        file_path TEXT NOT NULL,
        line INTEGER NOT NULL,
        resolved INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inherit_edges (
        id INTEGER PRIMARY KEY,
        child_id INTEGER NOT NULL,
        parent_name TEXT NOT NULL,
        parent_id INTEGER,
        inherit_type TEXT NOT NULL,
        file_path TEXT NOT NULL,
        line INTEGER NOT NULL,
        resolved INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mixin_edges (
        id INTEGER PRIMARY KEY,
        host_id INTEGER NOT NULL,
        mixin_name TEXT NOT NULL,
        mixin_id INTEGER,
        file_path TEXT NOT NULL,
        line INTEGER NOT NULL,
        resolved INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)",
    "CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind)",
    "CREATE INDEX IF NOT EXISTS idx_edges_caller ON call_edges(caller_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_callee ON call_edges(callee_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_callee_name ON call_edges(callee_name)",
    "CREATE INDEX IF NOT EXISTS idx_edges_resolved ON call_edges(resolved)",
    "CREATE INDEX IF NOT EXISTS idx_imports_importer ON import_edges(importer_id)",
    "CREATE INDEX IF NOT EXISTS idx_imports_imported ON import_edges(imported_id)",
    "CREATE INDEX IF NOT EXISTS idx_imports_name ON import_edges(imported_name)",
    "CREATE INDEX IF NOT EXISTS idx_inherits_child ON inherit_edges(child_id)",
    "CREATE INDEX IF NOT EXISTS idx_inherits_parent ON inherit_edges(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_inherits_name ON inherit_edges(parent_name)",
    "CREATE INDEX IF NOT EXISTS idx_mixins_host ON mixin_edges(host_id)",
    "CREATE INDEX IF NOT EXISTS idx_mixins_mixin ON mixin_edges(mixin_id)",
    "CREATE INDEX IF NOT EXISTS idx_mixins_name ON mixin_edges(mixin_name)",
)

_KIND_PRIORITY_SQL = (
    "CASE kind WHEN 'function' THEN 0 WHEN 'method' THEN 1 WHEN 'class' THEN 2 ELSE 3 END"
)


@dataclass(slots=True)
class IndexResult:
    """Outcome of one :meth:`CodeIndexer.index_directory` invocation."""

    indexed_files: int = 0
    skipped_files: int = 0
    deleted_files: int = 0
    error_files: int = 0
    resolved_edges: int = 0
    total_symbols: int = 0
    total_calls: int = 0
    total_imports: int = 0
    total_inherits: int = 0
    total_mixins: int = 0
    truncated: bool = False
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when anything was written (new/updated/deleted files)."""
        return bool(self.indexed_files or self.deleted_files)


def levenshtein(left: str, right: str) -> int:
    """Iterative Levenshtein distance (no dependency; bounded by name length)."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, lchar in enumerate(left, start=1):
        current = [i]
        for j, rchar in enumerate(right, start=1):
            cost = 0 if lchar == rchar else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


class CodeIndexer:
    """Incremental tree-sitter indexer with a two-pass call-graph resolution."""

    def __init__(self, db_path: str | Path, config: CodeIntelConfig | None = None) -> None:
        self._db_path = str(db_path)
        self._config = config or CODE_INTEL
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        """Open a WAL connection with ``busy_timeout`` as its first statement."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        for ddl in _SCHEMA:
            conn.execute(ddl)

    def index_directory(self, root: str | Path, force: bool = False) -> IndexResult:
        """Walk *root*, (re)indexing changed files; serialized per indexer instance."""
        with self._lock:
            return self._index_directory(Path(root).resolve(), force)

    def _index_directory(self, root: Path, force: bool) -> IndexResult:
        started = time.monotonic()
        result = IndexResult()
        if not root.is_dir():
            result.errors.append(f"root is not a directory: {root}")
            return result

        cfg = self._config
        supported = {ext.lower() for ext in cfg["code_intel_supported_extensions"]}
        prune = set(cfg["code_intel_prune_dirs"])
        max_files = cfg["code_intel_index_max_files"]
        timeout = cfg["code_intel_index_timeout_s"]
        batch_size = max(1, cfg["code_intel_index_batch_size"])

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            known = self._load_known(conn)
            seen: set[str] = set()
            pending: list[tuple[Path, str, float, int]] = []
            stop = False

            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames if d not in prune and not should_skip_dir(Path(dirpath) / d)
                ]
                dirnames.sort()  # os.walk order is filesystem-dependent; index deterministically
                for filename in sorted(filenames):
                    if stop:
                        break
                    if Path(filename).suffix.lower() not in supported:
                        continue
                    path = Path(dirpath) / filename
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    rel = os.path.relpath(path, root).replace(os.sep, "/")
                    seen.add(rel)
                    if not force and known.get(rel) == (stat.st_mtime, stat.st_size):
                        result.skipped_files += 1
                        continue
                    if len(seen) > max_files:
                        result.truncated = True
                        stop = True
                        break
                    pending.append((path, rel, stat.st_mtime, stat.st_size))
                    if time.monotonic() - started > timeout:
                        result.truncated = True
                        stop = True
                        break
                    if len(pending) >= batch_size:
                        self._flush_batch(conn, pending, result)
                        pending = []
                if stop:
                    break

            if pending:
                self._flush_batch(conn, pending, result)

            removed = [rel for rel in known if rel not in seen]
            if removed:
                self._drop_many(conn, removed)
                conn.commit()
                result.deleted_files = len(removed)

            if result.changed:
                result.resolved_edges = (
                    self._resolve_call_edges(conn)
                    + self._resolve_import_edges(conn)
                    + self._resolve_inherit_edges(conn)
                    + self._resolve_mixin_edges(conn)
                )
                conn.commit()

            result.total_symbols = self._count(conn, "symbols")
            result.total_calls = self._count(conn, "call_edges")
            result.total_imports = self._count(conn, "import_edges")
            result.total_inherits = self._count(conn, "inherit_edges")
            result.total_mixins = self._count(conn, "mixin_edges")
        except Exception as exc:  # noqa: BLE001 - index build must never crash a tool call
            logger.warning("code_intel: index_directory failed for {}: {}", root, exc)
            result.errors.append(str(exc))
        finally:
            conn.close()

        result.elapsed_s = time.monotonic() - started
        return result

    def _load_known(self, conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
        rows = conn.execute("SELECT file_path, mtime, size FROM index_meta").fetchall()
        return {row["file_path"]: (row["mtime"], row["size"]) for row in rows}

    @staticmethod
    def _count(conn: sqlite3.Connection, table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608

    def _flush_batch(
        self,
        conn: sqlite3.Connection,
        pending: list[tuple[Path, str, float, int]],
        result: IndexResult,
    ) -> None:
        """Write one batch in a single transaction; a DB failure rolls the batch back."""
        try:
            for path, rel, mtime, size in pending:
                self._index_one(conn, path, rel, mtime, size, result)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - fail-open per plan
            conn.rollback()
            logger.warning("code_intel: batch of {} files rolled back: {}", len(pending), exc)
            result.error_files += len(pending)
            result.errors.append(str(exc))

    def _index_one(
        self,
        conn: sqlite3.Connection,
        path: Path,
        rel: str,
        mtime: float,
        size: int,
        result: IndexResult,
    ) -> None:
        language = self._config["code_intel_language_map"].get(path.suffix.lower(), "unknown")
        self._drop_many(conn, [rel])

        max_bytes = self._config["code_intel_index_max_file_bytes"]
        error: str | None = None
        symbols: list[SymbolDef] = []
        calls: list[CallSite] = []
        imports: list = []
        inherits: list = []
        mixins: list = []
        lines: list[str] = []

        if size > max_bytes:
            error = f"file too large: {size} bytes > {max_bytes}"
        else:
            try:
                data = path.read_bytes()
                lines = data.decode("utf-8", errors="replace").splitlines()
                extraction = extract_symbols(data, language)
                symbols = extraction.symbols
                calls = extraction.calls
                imports = extraction.imports
                inherits = extraction.inherits
                mixins = extraction.mixins
                error = extraction.error
            except Exception as exc:  # noqa: BLE001 - a bad file must not fail the batch
                error = f"index failed: {exc}"

        if error is not None:
            self._write_meta(conn, rel, mtime, size, language, 0, error)
            result.error_files += 1
            return

        snippet_lines = self._config["code_intel_source_snippet_lines"]
        id_by_index: list[int] = []
        canonical = canonical_language(language)
        for symbol in symbols:
            snippet = _snippet(lines, symbol, snippet_lines)
            cursor = conn.execute(
                "INSERT INTO symbols(name, kind, file_path, line_start, line_end, parent_id, "
                "language, source_snippet, doc_string) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    symbol.name,
                    symbol.kind,
                    rel,
                    symbol.line_start,
                    symbol.line_end,
                    canonical,
                    snippet,
                    symbol.doc_string,
                ),
            )
            id_by_index.append(int(cursor.lastrowid or 0))

        for index, symbol in enumerate(symbols):
            if symbol.parent_index is not None and 0 <= symbol.parent_index < len(id_by_index):
                conn.execute(
                    "UPDATE symbols SET parent_id = ? WHERE id = ?",
                    (id_by_index[symbol.parent_index], id_by_index[index]),
                )

        for call in calls:
            if 0 <= call.caller_index < len(id_by_index):
                conn.execute(
                    "INSERT INTO call_edges(caller_id, callee_id, callee_name, file_path, line, "
                    "resolved) VALUES (?, NULL, ?, ?, ?, 0)",
                    (id_by_index[call.caller_index], call.callee_name, rel, call.line),
                )

        # Module-scope imports have importer_index=-1 → id 0 (see the schema).
        for imp in imports:
            importer_id = (
                id_by_index[imp.importer_index] if 0 <= imp.importer_index < len(id_by_index) else 0
            )
            conn.execute(
                "INSERT INTO import_edges(importer_id, imported_name, imported_id, file_path, "
                "line, resolved) VALUES (?, ?, NULL, ?, ?, 0)",
                (importer_id, imp.imported_name, rel, imp.line),
            )
        for inherit in inherits:
            if 0 <= inherit.child_index < len(id_by_index):
                conn.execute(
                    "INSERT INTO inherit_edges(child_id, parent_name, parent_id, inherit_type, "
                    "file_path, line, resolved) VALUES (?, ?, NULL, ?, ?, ?, 0)",
                    (
                        id_by_index[inherit.child_index],
                        inherit.parent_name,
                        inherit.inherit_type,
                        rel,
                        inherit.line,
                    ),
                )
        for mixin in mixins:
            if 0 <= mixin.host_index < len(id_by_index):
                conn.execute(
                    "INSERT INTO mixin_edges(host_id, mixin_name, mixin_id, file_path, line, "
                    "resolved) VALUES (?, ?, NULL, ?, ?, 0)",
                    (id_by_index[mixin.host_index], mixin.mixin_name, rel, mixin.line),
                )

        self._write_meta(conn, rel, mtime, size, language, len(symbols), None)
        result.indexed_files += 1

    @staticmethod
    def _write_meta(
        conn: sqlite3.Connection,
        rel: str,
        mtime: float,
        size: int,
        language: str,
        symbol_count: int,
        error: str | None,
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO index_meta"
            "(file_path, mtime, size, language, indexed_at, symbol_count, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rel, mtime, size, language, time.time(), symbol_count, error),
        )

    @staticmethod
    def _drop_many(conn: sqlite3.Connection, rel_paths: list[str]) -> None:
        """Delete a set of files' symbols + caller edges and detach inbound resolved edges."""
        for rel in rel_paths:
            old_ids = [
                int(row[0])
                for row in conn.execute("SELECT id FROM symbols WHERE file_path = ?", (rel,))
            ]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM call_edges WHERE caller_id IN ({placeholders})", old_ids)
                conn.execute(
                    f"UPDATE call_edges SET callee_id = NULL, resolved = 0 "
                    f"WHERE callee_id IN ({placeholders})",
                    old_ids,
                )
                conn.execute(f"DELETE FROM symbols WHERE id IN ({placeholders})", old_ids)
            # The parallel edge tables key their rows by the owning file, so a
            # file replacement removes its own rows and detaches inbound ones.
            conn.execute("DELETE FROM import_edges WHERE file_path = ?", (rel,))
            conn.execute("DELETE FROM inherit_edges WHERE file_path = ?", (rel,))
            conn.execute("DELETE FROM mixin_edges WHERE file_path = ?", (rel,))
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(
                    f"UPDATE import_edges SET imported_id = NULL, resolved = 0 "
                    f"WHERE imported_id IN ({placeholders})",
                    old_ids,
                )
                conn.execute(
                    f"UPDATE inherit_edges SET parent_id = NULL, resolved = 0 "
                    f"WHERE parent_id IN ({placeholders})",
                    old_ids,
                )
                conn.execute(
                    f"UPDATE mixin_edges SET mixin_id = NULL, resolved = 0 "
                    f"WHERE mixin_id IN ({placeholders})",
                    old_ids,
                )
            conn.execute("DELETE FROM index_meta WHERE file_path = ?", (rel,))

    def _resolve_call_edges(self, conn: sqlite3.Connection) -> int:
        """Second pass: resolve unresolved ``callee_name`` → symbol id."""
        rows = conn.execute(
            "SELECT id, callee_name, file_path FROM call_edges WHERE resolved = 0"
        ).fetchall()
        if not rows:
            return 0
        grouped: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            grouped.setdefault((row["callee_name"], row["file_path"]), []).append(int(row["id"]))

        resolved = 0
        for (name, file_path), edge_ids in grouped.items():
            target = self._match_symbol(conn, name, file_path)
            if target is None:
                continue
            placeholders = ",".join("?" * len(edge_ids))
            conn.execute(
                f"UPDATE call_edges SET callee_id = ?, resolved = 1 WHERE id IN ({placeholders})",
                [target, *edge_ids],
            )
            resolved += len(edge_ids)
        return resolved

    def _resolve_import_edges(self, conn: sqlite3.Connection) -> int:
        """Second pass: resolve an imported name/module path to a symbol id."""
        rows = conn.execute(
            "SELECT id, imported_name, file_path FROM import_edges WHERE resolved = 0"
        ).fetchall()
        return self._resolve_named_edges(
            conn,
            rows,
            table="import_edges",
            target_column="imported_id",
            name_column="imported_name",
        )

    def _resolve_inherit_edges(self, conn: sqlite3.Connection) -> int:
        """Second pass: resolve an inheritance parent name to a symbol id."""
        rows = conn.execute(
            "SELECT id, parent_name, file_path FROM inherit_edges WHERE resolved = 0"
        ).fetchall()
        return self._resolve_named_edges(
            conn,
            rows,
            table="inherit_edges",
            target_column="parent_id",
            name_column="parent_name",
        )

    def _resolve_mixin_edges(self, conn: sqlite3.Connection) -> int:
        """Second pass: resolve a mixin/trait/interface name to a symbol id."""
        rows = conn.execute(
            "SELECT id, mixin_name, file_path FROM mixin_edges WHERE resolved = 0"
        ).fetchall()
        return self._resolve_named_edges(
            conn,
            rows,
            table="mixin_edges",
            target_column="mixin_id",
            name_column="mixin_name",
        )

    def _resolve_named_edges(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        table: str,
        target_column: str,
        name_column: str,
    ) -> int:
        """Shared resolver for the three named-edge tables (no SQL interpolation of names)."""
        if not rows:
            return 0
        grouped: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            grouped.setdefault((row[name_column], row["file_path"]), []).append(int(row["id"]))

        resolved = 0
        for (name, file_path), edge_ids in grouped.items():
            target = self._match_symbol_named(conn, name, file_path)
            if target is None:
                continue
            placeholders = ",".join("?" * len(edge_ids))
            conn.execute(
                f"UPDATE {table} SET {target_column} = ?, resolved = 1 "  # noqa: S608 - fixed identifiers
                f"WHERE id IN ({placeholders})",
                [target, *edge_ids],
            )
            resolved += len(edge_ids)
        return resolved

    def _match_symbol_named(
        self, conn: sqlite3.Connection, name: str, file_path: str
    ) -> int | None:
        """Resolve a raw path/name within the SAME language, falling back to its last segment.

        The call-graph resolver deliberately keeps its original cross-language
        order (C2); these three additive tables prefer the file's own language so
        a TypeScript ``Base`` never links to a Python ``Base``.
        """
        target = self._match_named_in_language(conn, name, file_path)
        if target is not None:
            return target
        if "/" in name or "::" in name or "." in name:
            from .extract import clean_type_name

            cleaned = clean_type_name(name)
            if cleaned and cleaned != name:
                return self._match_named_in_language(conn, cleaned, file_path)
        return None

    def _match_named_in_language(
        self, conn: sqlite3.Connection, name: str, file_path: str
    ) -> int | None:
        language = self._config["code_intel_language_map"].get(
            Path(file_path).suffix.lower(), "unknown"
        )
        candidates = conn.execute(
            "SELECT id, file_path FROM symbols WHERE name = ? AND language = ? LIMIT 500",
            (name, language),
        ).fetchall()
        if candidates:
            for row in candidates:
                if row["file_path"] == file_path:
                    return int(row["id"])
            directory = os.path.dirname(file_path)
            prefix = f"{directory}/" if directory else ""
            if prefix:
                for row in candidates:
                    if row["file_path"].startswith(prefix):
                        return int(row["id"])
            return int(candidates[0]["id"])

        local = conn.execute(
            "SELECT id, name FROM symbols WHERE file_path = ? AND language = ?",
            (file_path, language),
        ).fetchall()
        best_id: int | None = None
        best_distance = 3
        for row in local:
            distance = levenshtein(name.lower(), row["name"].lower())
            if distance < best_distance:
                best_distance = distance
                best_id = int(row["id"])
        return best_id if best_distance <= 2 else None

    @staticmethod
    def _match_symbol(conn: sqlite3.Connection, name: str, file_path: str) -> int | None:
        """Resolve a callee name by priority: same file → same dir → global → fuzzy."""
        candidates = conn.execute(
            "SELECT id, file_path, kind FROM symbols WHERE name = ? LIMIT 500", (name,)
        ).fetchall()
        if candidates:
            for row in candidates:
                if row["file_path"] == file_path:
                    return int(row["id"])
            directory = os.path.dirname(file_path)
            prefix = f"{directory}/" if directory else ""
            if prefix:
                for row in candidates:
                    if row["file_path"].startswith(prefix):
                        return int(row["id"])
            return int(candidates[0]["id"])

        local = conn.execute(
            "SELECT id, name FROM symbols WHERE file_path = ?", (file_path,)
        ).fetchall()
        best_id: int | None = None
        best_distance = 3
        for row in local:
            distance = levenshtein(name.lower(), row["name"].lower())
            if distance < best_distance:
                best_distance = distance
                best_id = int(row["id"])
        return best_id if best_distance <= 2 else None


def _snippet(lines: list[str], symbol: SymbolDef, snippet_lines: int) -> str | None:
    """First N lines of the symbol body, or None when snippets are disabled."""
    if snippet_lines <= 0 or not lines:
        return None
    start = max(0, symbol.line_start - 1)
    stop = min(len(lines), start + snippet_lines, symbol.line_end)
    if start >= stop:
        return None
    return "\n".join(lines[start:stop])
