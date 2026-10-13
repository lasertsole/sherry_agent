"""Query engine over the tree-sitter symbol index (explore/callers/callees/impact)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from config.features.agent_side import CODE_INTEL, CodeIntelConfig

from .indexer import CodeIndexer, levenshtein

_SPLIT_RE = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KIND_PRIORITY = {"function": 0, "method": 1, "class": 2, "variable": 3}


@dataclass(slots=True)
class SymbolMatch:
    """A matched symbol row."""

    id: int
    name: str
    kind: str
    file_path: str
    line_start: int
    line_end: int
    score: float = 0.0
    doc_string: str | None = None


@dataclass(slots=True)
class CallerInfo:
    """One caller of a symbol."""

    name: str
    kind: str
    file_path: str
    line_start: int
    call_line: int


@dataclass(slots=True)
class CalleeInfo:
    """One call emitted by a symbol."""

    name: str
    target_file: str | None
    target_line: int | None
    call_line: int
    resolved: bool


@dataclass(slots=True)
class ExploreEntry:
    """Source + immediate call paths for one matched symbol."""

    symbol: SymbolMatch
    source: str
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExploreResult:
    """Result of :meth:`CodeQuery.explore`."""

    query: str
    entries: list[ExploreEntry] = field(default_factory=list)
    truncated: bool = False
    suggestion: str | None = None

    @property
    def found(self) -> bool:
        """True when at least one symbol matched."""
        return bool(self.entries)


@dataclass(slots=True)
class ImpactEntry:
    """One transitive caller in the blast radius."""

    symbol: SymbolMatch
    depth: int
    via: str


@dataclass(slots=True)
class ImpactResult:
    """Result of :meth:`CodeQuery.impact`."""

    root_symbol: str
    entries: list[ImpactEntry] = field(default_factory=list)
    max_depth: int = 0
    truncated: bool = False


def _tokenize(text: str) -> list[str]:
    """Split a query into lowercased words, breaking camelCase boundaries."""
    tokens: list[str] = []
    for chunk in _SPLIT_RE.split(text):
        if not chunk:
            continue
        tokens.extend(_CAMEL_RE.split(chunk))
    return [token.lower() for token in tokens if len(token) >= 2]


def _score(name: str, query: str, tokens: list[str]) -> float:
    """Score a symbol name against a query on a 0..1 scale."""
    lowered = name.lower()
    needle = query.strip().lower()
    if needle and lowered == needle:
        return 1.0
    if needle and lowered.startswith(needle):
        return 0.9
    if needle and needle in lowered:
        return 0.75
    best = 0.0
    for token in tokens:
        if token == lowered:
            best = max(best, 0.8)
        elif lowered.startswith(token):
            best = max(best, 0.65)
        elif token in lowered:
            best = max(best, 0.55)
    if tokens:
        hits = sum(1 for token in tokens if token in lowered)
        if hits:
            best = max(best, 0.35 + 0.2 * (hits / len(tokens)))
    if needle and len(needle) >= 3:
        distance = levenshtein(lowered, needle)
        if distance <= 2:
            best = max(best, max(0.0, 1.0 - distance / max(len(needle), 1)))
    return best


class CodeQuery:
    """Read-side queries over the symbol index built by :class:`CodeIndexer`."""

    def __init__(
        self,
        db_path: str | Path,
        config: CodeIntelConfig | None = None,
        indexer: CodeIndexer | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._config = config or CODE_INTEL
        self._indexer = indexer or CodeIndexer(self._db_path, self._config)

    # -- infrastructure ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _ensure_index(self, root: Path) -> None:
        """Build or refresh the incremental index for *root* (no-op when unchanged)."""
        self._indexer.index_directory(root)

    # -- matching ----------------------------------------------------------

    def _candidate_rows(self, conn: sqlite3.Connection, query: str) -> dict[int, sqlite3.Row]:
        candidates: dict[int, sqlite3.Row] = {}
        base = "SELECT id, name, kind, file_path, line_start, line_end, doc_string FROM symbols"
        rows = conn.execute(
            f"{base} WHERE lower(name) = ? LIMIT 50", (query.strip().lower(),)
        ).fetchall()
        for row in rows:
            candidates[int(row["id"])] = row
        for token in _tokenize(query):
            rows = conn.execute(
                f"{base} WHERE lower(name) LIKE ? LIMIT 200", (f"%{token}%",)
            ).fetchall()
            for row in rows:
                candidates[int(row["id"])] = row
        if not candidates:
            rows = conn.execute(f"{base} LIMIT 5000").fetchall()
            for row in rows:
                candidates[int(row["id"])] = row
        return candidates

    def _match(self, conn: sqlite3.Connection, query: str, limit: int) -> list[SymbolMatch]:
        threshold = float(self._config["code_intel_fuzzy_min_score"])
        scored: list[SymbolMatch] = []
        for row in self._candidate_rows(conn, query).values():
            score = _score(row["name"], query, _tokenize(query))
            if score >= threshold:
                scored.append(
                    SymbolMatch(
                        id=int(row["id"]),
                        name=row["name"],
                        kind=row["kind"],
                        file_path=row["file_path"],
                        line_start=int(row["line_start"]),
                        line_end=int(row["line_end"]),
                        score=round(score, 3),
                        doc_string=row["doc_string"],
                    )
                )
        scored.sort(key=lambda m: (-m.score, _KIND_PRIORITY.get(m.kind, 9), len(m.name), m.name))
        return scored[:limit]

    def _resolve_symbol_ids(self, conn: sqlite3.Connection, name: str) -> list[int]:
        """Exact-name symbol ids, falling back to a fuzzy single match."""
        rows = conn.execute(
            "SELECT id FROM symbols WHERE lower(name) = ?", (name.strip().lower(),)
        ).fetchall()
        if rows:
            return [int(row["id"]) for row in rows]
        best_id: int | None = None
        best_distance = 3
        for row in conn.execute("SELECT id, name FROM symbols LIMIT 5000").fetchall():
            distance = levenshtein(name.lower(), row["name"].lower())
            if distance < best_distance:
                best_distance = distance
                best_id = int(row["id"])
        return [best_id] if best_id is not None and best_distance <= 2 else []

    @staticmethod
    def _row_to_match(row: sqlite3.Row) -> SymbolMatch:
        return SymbolMatch(
            id=int(row["id"]),
            name=row["name"],
            kind=row["kind"],
            file_path=row["file_path"],
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
        )

    # -- public queries ----------------------------------------------------

    def explore(self, query: str, root: Path) -> ExploreResult:
        """Fuzzy intent → symbol source + immediate call paths."""
        self._ensure_index(root)
        limit = int(self._config["code_intel_explore_max_symbols"])
        conn = self._connect()
        try:
            matches = self._match(conn, query, limit)
            if not matches:
                return ExploreResult(
                    query=query,
                    suggestion=(
                        "No symbol matched. Fall back to `terminal` (rg/grep) or refine the query."
                    ),
                )
            entries = []
            for match in matches:
                callers = [
                    f"{c.name} ({c.file_path}:{c.call_line})"
                    for c in self._callers_for(conn, [match.id], match.name)
                ]
                callees = [
                    f"{c.name} ({c.target_file or '?'}:{c.call_line})"
                    for c in self._callees_for(conn, [match.id])
                ]
                entries.append(
                    ExploreEntry(
                        symbol=match,
                        source=self._source(conn, root, match),
                        callers=callers,
                        callees=callees,
                    )
                )
            return ExploreResult(query=query, entries=entries, truncated=len(matches) >= limit)
        finally:
            conn.close()

    def callers(self, symbol_name: str, root: Path) -> list[CallerInfo]:
        """All functions/methods that call *symbol_name*."""
        self._ensure_index(root)
        conn = self._connect()
        try:
            return self._callers_for(conn, self._resolve_symbol_ids(conn, symbol_name), symbol_name)
        finally:
            conn.close()

    def callees(self, symbol_name: str, root: Path) -> list[CalleeInfo]:
        """All calls emitted by *symbol_name*."""
        self._ensure_index(root)
        conn = self._connect()
        try:
            return self._callees_for(conn, self._resolve_symbol_ids(conn, symbol_name))
        finally:
            conn.close()

    def impact(self, symbol_name: str, root: Path) -> ImpactResult:
        """Transitive callers of *symbol_name*, bounded by the configured depth."""
        self._ensure_index(root)
        max_depth = int(self._config["code_intel_call_graph_max_depth"])
        conn = self._connect()
        try:
            ids = self._resolve_symbol_ids(conn, symbol_name)
            if not ids:
                return ImpactResult(root_symbol=symbol_name, max_depth=max_depth)
            frontier = set(ids)
            visited = set(ids)
            names = self._names_for(conn, frontier)
            entries: list[ImpactEntry] = []
            truncated = False
            for depth in range(1, max_depth + 1):
                via_map = self._reverse_callers(conn, frontier, names)
                next_ids = {cid for cid in via_map if cid not in visited}
                if not next_ids:
                    break
                if depth == max_depth:
                    truncated = True
                for row in self._symbols_by_ids(conn, next_ids):
                    match = self._row_to_match(row)
                    entries.append(ImpactEntry(match, depth, via_map[match.id]))
                visited |= next_ids
                frontier = next_ids
                names = self._names_for(conn, frontier)
            return ImpactResult(
                root_symbol=symbol_name, entries=entries, max_depth=max_depth, truncated=truncated
            )
        finally:
            conn.close()

    # -- shared SQL helpers ------------------------------------------------

    def _callers_for(
        self, conn: sqlite3.Connection, ids: list[int], symbol_name: str
    ) -> list[CallerInfo]:
        clauses = ["ce.callee_name = ?"]
        params: list[object] = [symbol_name]
        if ids:
            placeholders = ",".join("?" * len(ids))
            clauses.append(f"ce.callee_id IN ({placeholders})")
            params.extend(ids)
        sql = (
            "SELECT ce.line AS call_line, s.id AS caller_id, s.name AS caller_name, "
            "s.kind AS caller_kind, s.file_path AS caller_file, s.line_start AS caller_line "
            "FROM call_edges ce JOIN symbols s ON s.id = ce.caller_id "
            f"WHERE {' OR '.join(clauses)} ORDER BY s.file_path, ce.line"
        )
        rows = conn.execute(sql, params).fetchall()
        seen: set[tuple[int, int]] = set()
        out: list[CallerInfo] = []
        for row in rows:
            key = (int(row["caller_id"]), int(row["call_line"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                CallerInfo(
                    name=row["caller_name"],
                    kind=row["caller_kind"],
                    file_path=row["caller_file"],
                    line_start=int(row["caller_line"]),
                    call_line=int(row["call_line"]),
                )
            )
        return out

    def _callees_for(self, conn: sqlite3.Connection, ids: list[int]) -> list[CalleeInfo]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        sql = (
            "SELECT ce.callee_name, ce.line AS call_line, ce.resolved, "
            "s.file_path AS target_file, s.line_start AS target_line "
            "FROM call_edges ce LEFT JOIN symbols s ON s.id = ce.callee_id "
            f"WHERE ce.caller_id IN ({placeholders}) ORDER BY ce.line"
        )
        seen: set[tuple[str, int]] = set()
        out: list[CalleeInfo] = []
        for row in conn.execute(sql, ids).fetchall():
            key = (row["callee_name"], int(row["call_line"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                CalleeInfo(
                    name=row["callee_name"],
                    target_file=row["target_file"],
                    target_line=int(row["target_line"]) if row["target_line"] is not None else None,
                    call_line=int(row["call_line"]),
                    resolved=bool(row["resolved"]),
                )
            )
        return out

    def _reverse_callers(
        self, conn: sqlite3.Connection, ids: set[int], names: set[str]
    ) -> dict[int, str]:
        if not ids and not names:
            return {}
        clauses = []
        params: list[object] = []
        if ids:
            clauses.append(f"ce.callee_id IN ({','.join('?' * len(ids))})")
            params.extend(sorted(ids))
        if names:
            clauses.append(f"ce.callee_name IN ({','.join('?' * len(names))})")
            params.extend(sorted(names))
        sql = f"SELECT ce.caller_id, ce.callee_name FROM call_edges ce WHERE {' OR '.join(clauses)}"
        via: dict[int, str] = {}
        for row in conn.execute(sql, params).fetchall():
            caller_id = int(row["caller_id"])
            via.setdefault(caller_id, row["callee_name"])
        return via

    @staticmethod
    def _symbols_by_ids(conn: sqlite3.Connection, ids: set[int]) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(ids))
        return conn.execute(
            "SELECT id, name, kind, file_path, line_start, line_end FROM symbols "
            f"WHERE id IN ({placeholders}) ORDER BY file_path, line_start",
            sorted(ids),
        ).fetchall()

    def _names_for(self, conn: sqlite3.Connection, ids: set[int]) -> set[str]:
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT name FROM symbols WHERE id IN ({placeholders})", sorted(ids)
        ).fetchall()
        return {row["name"] for row in rows}

    def _source(self, conn: sqlite3.Connection, root: Path, match: SymbolMatch) -> str:
        cap = int(self._config["code_intel_explore_max_source_chars"])
        path = root / match.file_path
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, match.line_start - 1)
            stop = min(len(lines), match.line_end)
            return "\n".join(lines[start:stop])[:cap]
        except OSError:
            row = conn.execute(
                "SELECT source_snippet FROM symbols WHERE id = ?", (match.id,)
            ).fetchone()
            snippet = row["source_snippet"] if row is not None else None
            return (snippet or "")[:cap]
