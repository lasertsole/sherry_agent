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
    confidence: str = "EXTRACTED"


@dataclass(slots=True)
class CalleeInfo:
    """One call emitted by a symbol."""

    name: str
    target_file: str | None
    target_line: int | None
    call_line: int
    resolved: bool
    confidence: str = ""


@dataclass(slots=True)
class ImportInfo:
    """One import edge (outgoing from a symbol/module, or a module-scope import)."""

    name: str
    target_file: str | None
    target_line: int | None
    line: int
    resolved: bool
    confidence: str = ""


@dataclass(slots=True)
class ImporterInfo:
    """One importer of a symbol (a module-scope importer carries ``kind="module"``)."""

    name: str
    kind: str
    file_path: str
    line: int
    resolved: bool
    confidence: str = ""


@dataclass(slots=True)
class InheritInfo:
    """One inheritance/mixin edge; ``name`` is the counterpart symbol."""

    name: str
    target_file: str | None
    target_line: int | None
    edge_type: str
    inherit_type: str
    line: int
    resolved: bool
    confidence: str = ""


@dataclass(slots=True)
class NodeDegree:
    """A symbol ranked by total resolved-graph degree."""

    symbol: SymbolMatch
    degree: int
    in_degree: int
    out_degree: int


@dataclass(slots=True)
class PathHop:
    """One node on a path; ``edge_type`` is the edge that led INTO it."""

    symbol: SymbolMatch
    edge_type: str
    edge_confidence: str


@dataclass(slots=True)
class PathResult:
    """Result of :meth:`CodeQuery.path`."""

    root_a: str
    root_b: str
    hops: list[PathHop] = field(default_factory=list)
    found: bool = False
    truncated: bool = False


@dataclass(slots=True)
class Community:
    """One Louvain / connected-component cluster of the symbol graph."""

    community_id: int
    size: int
    symbols: list[str] = field(default_factory=list)
    top_nodes: list[str] = field(default_factory=list)
    engine: str = "networkx"


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


def _confidence(resolved: object) -> str:
    """Map the stored 0/1 resolver flag onto the graphify confidence vocabulary."""
    return "EXTRACTED" if bool(resolved) else "INFERRED"


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

    # -- graph queries (SHERRY-PATCH: imports/inherits/mixins + path) -------

    def imports_of(self, symbol_name: str, root: Path) -> list[ImportInfo]:
        """Imports of a symbol (function-local) plus its file's module-scope imports."""
        self._ensure_index(root)
        conn = self._connect()
        try:
            ids = self._resolve_symbol_ids(conn, symbol_name)
            files = self._files_for(conn, ids)
            if not ids and not files:
                # A module-style name ("pkg.mod") maps to its indexed file.
                files = self._files_matching_module(conn, symbol_name)
            clauses = []
            params: list[object] = []
            if ids:
                clauses.append(f"ie.importer_id IN ({','.join('?' * len(ids))})")
                params.extend(ids)
            if files:
                clauses.append(
                    f"(ie.importer_id = 0 AND ie.file_path IN ({','.join('?' * len(files))}))"
                )
                params.extend(files)
            if not clauses:
                return []
            sql = (
                "SELECT ie.imported_name, ie.line, ie.resolved, "
                "s.file_path AS target_file, s.line_start AS target_line "
                "FROM import_edges ie LEFT JOIN symbols s ON s.id = ie.imported_id "
                f"WHERE {' OR '.join(clauses)} ORDER BY ie.line, ie.imported_name"
            )
            seen: set[tuple[str, int]] = set()
            out: list[ImportInfo] = []
            for row in conn.execute(sql, params).fetchall():
                key = (row["imported_name"], int(row["line"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ImportInfo(
                        name=row["imported_name"],
                        target_file=row["target_file"],
                        target_line=int(row["target_line"])
                        if row["target_line"] is not None
                        else None,
                        line=int(row["line"]),
                        resolved=bool(row["resolved"]),
                        confidence=_confidence(row["resolved"]),
                    )
                )
            return out
        finally:
            conn.close()

    def importers_of(self, symbol_name: str, root: Path) -> list[ImporterInfo]:
        """Symbols (and module scopes) that import *symbol_name*."""
        self._ensure_index(root)
        conn = self._connect()
        try:
            ids = self._resolve_symbol_ids(conn, symbol_name)
            clauses = []
            params: list[object] = []
            if ids:
                clauses.append(f"ie.imported_id IN ({','.join('?' * len(ids))})")
                params.extend(ids)
            clauses.append("(ie.imported_id IS NULL AND ie.imported_name = ?)")
            params.append(symbol_name)
            sql = (
                "SELECT ie.line, ie.resolved, ie.file_path AS source_file, "
                "s.name AS importer_name, s.kind AS importer_kind, "
                "s.file_path AS importer_file, s.line_start AS importer_line "
                "FROM import_edges ie LEFT JOIN symbols s ON s.id = ie.importer_id "
                f"WHERE {' OR '.join(clauses)} ORDER BY ie.file_path, ie.line"
            )
            seen: set[tuple[str, int]] = set()
            out: list[ImporterInfo] = []
            for row in conn.execute(sql, params).fetchall():
                name = row["importer_name"] or Path(row["source_file"]).stem
                key = (name, int(row["line"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ImporterInfo(
                        name=name,
                        kind=row["importer_kind"] or "module",
                        file_path=row["importer_file"] or row["source_file"],
                        line=int(row["line"]),
                        resolved=bool(row["resolved"]),
                        confidence=_confidence(row["resolved"]),
                    )
                )
            return out
        finally:
            conn.close()

    def inherits_from(self, symbol_name: str, root: Path) -> list[InheritInfo]:
        """Parents a class/interface/trait extends."""
        return self._inherit_rows(symbol_name, root, direction="up")

    def subclasses_of(self, symbol_name: str, root: Path) -> list[InheritInfo]:
        """Classes/interfaces that extend *symbol_name*."""
        return self._inherit_rows(symbol_name, root, direction="down")

    def _inherit_rows(self, symbol_name: str, root: Path, *, direction: str) -> list[InheritInfo]:
        self._ensure_index(root)
        conn = self._connect()
        try:
            ids = self._resolve_symbol_ids(conn, symbol_name)
            if direction == "up":
                clauses = []
                params: list[object] = []
                if ids:
                    clauses.append(f"he.child_id IN ({','.join('?' * len(ids))})")
                    params.extend(ids)
                if not clauses:
                    return []
                sql = (
                    "SELECT he.parent_name AS name, he.inherit_type, he.line, he.resolved, "
                    "s.file_path AS target_file, s.line_start AS target_line "
                    "FROM inherit_edges he LEFT JOIN symbols s ON s.id = he.parent_id "
                    f"WHERE {' OR '.join(clauses)} ORDER BY he.line, he.parent_name"
                )
                rows = conn.execute(sql, params).fetchall()
            else:
                clauses = []
                params = []
                if ids:
                    clauses.append(f"he.parent_id IN ({','.join('?' * len(ids))})")
                    params.extend(ids)
                clauses.append("(he.parent_id IS NULL AND he.parent_name = ?)")
                params.append(symbol_name)
                sql = (
                    "SELECT s.name AS name, he.inherit_type, he.line, he.resolved, "
                    "s.file_path AS target_file, s.line_start AS target_line "
                    "FROM inherit_edges he JOIN symbols s ON s.id = he.child_id "
                    f"WHERE {' OR '.join(clauses)} ORDER BY s.file_path, he.line"
                )
                rows = conn.execute(sql, params).fetchall()
            seen: set[tuple[str, int]] = set()
            out: list[InheritInfo] = []
            for row in rows:
                key = (row["name"], int(row["line"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    InheritInfo(
                        name=row["name"],
                        target_file=row["target_file"],
                        target_line=int(row["target_line"])
                        if row["target_line"] is not None
                        else None,
                        edge_type="inherits",
                        inherit_type=row["inherit_type"],
                        line=int(row["line"]),
                        resolved=bool(row["resolved"]),
                        confidence=_confidence(row["resolved"]),
                    )
                )
            return out
        finally:
            conn.close()

    def mixins_of(self, symbol_name: str, root: Path) -> list[InheritInfo]:
        """Interfaces/traits/mixins implemented by *symbol_name*."""
        return self._mixin_rows(symbol_name, root, direction="up")

    def mixins_in_by(self, symbol_name: str, root: Path) -> list[InheritInfo]:
        """Hosts that implement the given interface/trait/mixin."""
        return self._mixin_rows(symbol_name, root, direction="down")

    def _mixin_rows(self, symbol_name: str, root: Path, *, direction: str) -> list[InheritInfo]:
        self._ensure_index(root)
        conn = self._connect()
        try:
            ids = self._resolve_symbol_ids(conn, symbol_name)
            if direction == "up":
                if not ids:
                    return []
                sql = (
                    "SELECT me.mixin_name AS name, me.line, me.resolved, "
                    "s.file_path AS target_file, s.line_start AS target_line "
                    "FROM mixin_edges me LEFT JOIN symbols s ON s.id = me.mixin_id "
                    f"WHERE me.host_id IN ({','.join('?' * len(ids))}) "
                    "ORDER BY me.line, me.mixin_name"
                )
                rows = conn.execute(sql, ids).fetchall()
            else:
                clauses = []
                params: list[object] = []
                if ids:
                    clauses.append(f"me.mixin_id IN ({','.join('?' * len(ids))})")
                    params.extend(ids)
                clauses.append("(me.mixin_id IS NULL AND me.mixin_name = ?)")
                params.append(symbol_name)
                sql = (
                    "SELECT s.name AS name, me.line, me.resolved, "
                    "s.file_path AS target_file, s.line_start AS target_line "
                    "FROM mixin_edges me JOIN symbols s ON s.id = me.host_id "
                    f"WHERE {' OR '.join(clauses)} ORDER BY s.file_path, me.line"
                )
                rows = conn.execute(sql, params).fetchall()
            seen: set[tuple[str, int]] = set()
            out: list[InheritInfo] = []
            for row in rows:
                key = (row["name"], int(row["line"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    InheritInfo(
                        name=row["name"],
                        target_file=row["target_file"],
                        target_line=int(row["target_line"])
                        if row["target_line"] is not None
                        else None,
                        edge_type="mixes_in",
                        inherit_type="mixin",
                        line=int(row["line"]),
                        resolved=bool(row["resolved"]),
                        confidence=_confidence(row["resolved"]),
                    )
                )
            return out
        finally:
            conn.close()

    def path(
        self,
        symbol_a: str,
        symbol_b: str,
        root: Path,
        edge_types: list[str] | None = None,
    ) -> PathResult:
        """Shortest path between two symbols across calls/imports/inherits/mixins.

        Bidirectional BFS: forward from A over outgoing edges, backward from B
        over incoming edges. Independent of :meth:`impact` (C2).
        """
        self._ensure_index(root)
        allowed = tuple(edge_types or ("calls", "imports", "inherits", "mixes_in"))
        max_hops = int(self._config["code_intel_call_graph_max_depth"])
        conn = self._connect()
        try:
            starts = self._resolve_symbol_ids(conn, symbol_a)
            ends = self._resolve_symbol_ids(conn, symbol_b)
            if not starts or not ends:
                return PathResult(root_a=symbol_a, root_b=symbol_b)
            start, end = starts[0], ends[0]
            if start == end:
                row = self._symbols_by_ids(conn, {start})[0]
                return PathResult(
                    root_a=symbol_a,
                    root_b=symbol_b,
                    hops=[PathHop(self._row_to_match(row), "root", "")],
                    found=True,
                )
            parents_f: dict[int, tuple[int | None, str, str]] = {start: (None, "root", "")}
            parents_b: dict[int, tuple[int | None, str, str]] = {end: (None, "root", "")}
            layer_f: set[int] = {start}
            layer_b: set[int] = {end}
            meet: int | None = None
            truncated = False
            for _depth in range(max_hops):
                if not layer_f or not layer_b:
                    break
                if len(layer_f) <= len(layer_b):
                    layer_f, found = self._expand_path_layer(
                        conn, layer_f, parents_f, parents_b, allowed, outgoing=True
                    )
                else:
                    layer_b, found = self._expand_path_layer(
                        conn, layer_b, parents_b, parents_f, allowed, outgoing=False
                    )
                if found is not None:
                    meet = found
                    break
                if _depth == max_hops - 1:
                    truncated = bool(layer_f and layer_b)
            if meet is None:
                return PathResult(root_a=symbol_a, root_b=symbol_b, truncated=truncated)
            chain = self._reconstruct_path(meet, parents_f, parents_b)
            rows = self._symbols_by_ids(conn, {node for node, _edge, _conf in chain})
            by_id = {int(row["id"]): self._row_to_match(row) for row in rows}
            hops = [
                PathHop(by_id[node], edge_type, confidence)
                for node, edge_type, confidence in chain
                if node in by_id
            ]
            return PathResult(root_a=symbol_a, root_b=symbol_b, hops=hops, found=bool(hops))
        finally:
            conn.close()

    def _expand_path_layer(
        self,
        conn: sqlite3.Connection,
        layer: set[int],
        own: dict[int, tuple[int | None, str, str]],
        other: dict[int, tuple[int | None, str, str]],
        allowed: tuple[str, ...],
        *,
        outgoing: bool,
    ) -> tuple[set[int], int | None]:
        next_layer: set[int] = set()
        for node in sorted(layer):
            for neighbour, edge_type, confidence in self._path_neighbours(
                conn, node, allowed, outgoing=outgoing
            ):
                if neighbour == node:
                    continue
                if neighbour in other:
                    own.setdefault(neighbour, (node, edge_type, confidence))
                    return set(), neighbour
                if neighbour in own:
                    continue
                own[neighbour] = (node, edge_type, confidence)
                next_layer.add(neighbour)
        return next_layer, None

    def _path_neighbours(
        self,
        conn: sqlite3.Connection,
        node_id: int,
        allowed: tuple[str, ...],
        *,
        outgoing: bool,
    ) -> list[tuple[int, str, str]]:
        row = conn.execute(
            "SELECT id, name, file_path, language FROM symbols WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return []
        name = row["name"]
        file_path = row["file_path"]
        language = row["language"]
        out: list[tuple[int, str, str]] = []

        def _target(target_id: int | None, target_name: str, edge_type: str, resolved: object):
            if target_id is not None:
                return int(target_id)
            found = conn.execute(
                "SELECT id FROM symbols WHERE name = ? AND language = ? LIMIT 1",
                (target_name, language),
            ).fetchone()
            if found is None:
                found = conn.execute(
                    "SELECT id FROM symbols WHERE name = ? LIMIT 1", (target_name,)
                ).fetchone()
            return int(found["id"]) if found is not None else None

        if "calls" in allowed:
            if outgoing:
                rows = conn.execute(
                    "SELECT callee_id, callee_name, resolved FROM call_edges WHERE caller_id = ?",
                    (node_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT caller_id AS callee_id, callee_name, resolved FROM call_edges "
                    "WHERE callee_id = ? OR (callee_id IS NULL AND callee_name = ?)",
                    (node_id, name),
                ).fetchall()
            for edge in rows:
                target = _target(edge["callee_id"], edge["callee_name"], "calls", edge["resolved"])
                if target is not None:
                    out.append((target, "calls", _confidence(edge["resolved"])))
        if "imports" in allowed:
            if outgoing:
                rows = conn.execute(
                    "SELECT imported_id, imported_name, resolved FROM import_edges "
                    "WHERE importer_id = ? OR (importer_id = 0 AND file_path = ?)",
                    (node_id, file_path),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT importer_id AS imported_id, imported_name, resolved "
                    "FROM import_edges WHERE imported_id = ? "
                    "OR (imported_id IS NULL AND imported_name = ?)",
                    (node_id, name),
                ).fetchall()
            for edge in rows:
                if edge["imported_id"] == 0:
                    continue
                target = _target(
                    edge["imported_id"], edge["imported_name"], "imports", edge["resolved"]
                )
                if target is not None:
                    out.append((target, "imports", _confidence(edge["resolved"])))
        if "inherits" in allowed:
            if outgoing:
                rows = conn.execute(
                    "SELECT parent_id AS target_id, parent_name AS target_name, resolved "
                    "FROM inherit_edges WHERE child_id = ?",
                    (node_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT child_id AS target_id, parent_name AS target_name, resolved "
                    "FROM inherit_edges WHERE parent_id = ? "
                    "OR (parent_id IS NULL AND parent_name = ?)",
                    (node_id, name),
                ).fetchall()
            for edge in rows:
                target = _target(
                    edge["target_id"], edge["target_name"], "inherits", edge["resolved"]
                )
                if target is not None:
                    out.append((target, "inherits", _confidence(edge["resolved"])))
        if "mixes_in" in allowed:
            if outgoing:
                rows = conn.execute(
                    "SELECT mixin_id AS target_id, mixin_name AS target_name, resolved "
                    "FROM mixin_edges WHERE host_id = ?",
                    (node_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT host_id AS target_id, mixin_name AS target_name, resolved "
                    "FROM mixin_edges WHERE mixin_id = ? "
                    "OR (mixin_id IS NULL AND mixin_name = ?)",
                    (node_id, name),
                ).fetchall()
            for edge in rows:
                target = _target(
                    edge["target_id"], edge["target_name"], "mixes_in", edge["resolved"]
                )
                if target is not None:
                    out.append((target, "mixes_in", _confidence(edge["resolved"])))
        return out

    @staticmethod
    def _reconstruct_path(
        meet: int,
        parents_f: dict[int, tuple[int | None, str, str]],
        parents_b: dict[int, tuple[int | None, str, str]],
    ) -> list[tuple[int, str, str]]:
        forward: list[tuple[int, str, str]] = []
        current: int | None = meet
        while current is not None:
            prev, edge_type, confidence = parents_f[current]
            forward.append((current, edge_type, confidence))
            current = prev
        forward.reverse()
        path = list(forward)
        current = parents_b[meet][0]
        while current is not None:
            prev, edge_type, confidence = parents_b[current]
            path.append((current, edge_type, confidence))
            current = prev
        return path

    def god_nodes(self, root: Path, limit: int = 10) -> list[NodeDegree]:
        """Top symbols by total resolved degree across the four edge families."""
        self._ensure_index(root)
        conn = self._connect()
        try:
            in_degree: dict[int, int] = {}
            out_degree: dict[int, int] = {}
            for table, src, dst in (
                ("call_edges", "caller_id", "callee_id"),
                ("import_edges", "importer_id", "imported_id"),
                ("inherit_edges", "child_id", "parent_id"),
                ("mixin_edges", "host_id", "mixin_id"),
            ):
                for row in conn.execute(
                    f"SELECT {src} AS id, COUNT(*) AS n FROM {table} "  # noqa: S608 - fixed identifiers
                    f"GROUP BY {src}"
                ):
                    node_id = int(row["id"])
                    if node_id:
                        out_degree[node_id] = out_degree.get(node_id, 0) + int(row["n"])
                for row in conn.execute(
                    f"SELECT {dst} AS id, COUNT(*) AS n FROM {table} "  # noqa: S608
                    f"WHERE {dst} IS NOT NULL GROUP BY {dst}"
                ):
                    node_id = int(row["id"])
                    in_degree[node_id] = in_degree.get(node_id, 0) + int(row["n"])
            ids = set(in_degree) | set(out_degree)
            if not ids:
                return []
            ranked = sorted(
                ids,
                key=lambda node_id: (
                    -(in_degree.get(node_id, 0) + out_degree.get(node_id, 0)),
                    node_id,
                ),
            )[:limit]
            rows = self._symbols_by_ids(conn, set(ranked))
            by_id = {int(row["id"]): row for row in rows}
            out: list[NodeDegree] = []
            for node_id in ranked:
                row = by_id.get(node_id)
                if row is None:
                    continue
                out.append(
                    NodeDegree(
                        symbol=self._row_to_match(row),
                        degree=in_degree.get(node_id, 0) + out_degree.get(node_id, 0),
                        in_degree=in_degree.get(node_id, 0),
                        out_degree=out_degree.get(node_id, 0),
                    )
                )
            return out
        finally:
            conn.close()

    def communities(self, root: Path) -> list[Community]:
        """Louvain communities over the resolved graph (networkx optional)."""
        self._ensure_index(root)
        conn = self._connect()
        try:
            max_symbols = int(self._config["code_intel_communities_max_symbols"])
            rows = conn.execute(
                "SELECT id, name FROM symbols ORDER BY id LIMIT ?", (max_symbols,)
            ).fetchall()
            names = {int(row["id"]): row["name"] for row in rows}
            node_ids = set(names)
            edges: set[tuple[int, int]] = set()
            for table, src, dst in (
                ("call_edges", "caller_id", "callee_id"),
                ("import_edges", "importer_id", "imported_id"),
                ("inherit_edges", "child_id", "parent_id"),
                ("mixin_edges", "host_id", "mixin_id"),
            ):
                for row in conn.execute(
                    f"SELECT {src} AS s, {dst} AS d FROM {table} "  # noqa: S608
                    f"WHERE {dst} IS NOT NULL"
                ):
                    s, d = int(row["s"]), int(row["d"])
                    if s in node_ids and d in node_ids and s != d:
                        edges.add((s, d))
            if not node_ids:
                return []
            try:
                import networkx as nx
            except ImportError:
                return self._components(node_ids, edges, names)
            graph = nx.Graph()
            graph.add_nodes_from(sorted(node_ids))
            graph.add_edges_from(sorted(edges))
            try:
                partitions = nx.community.louvain_communities(graph, seed=0)
            except AttributeError:  # networkx < 3.0
                return self._components(node_ids, edges, names)
            out: list[Community] = []
            for index, part in enumerate(
                sorted(partitions, key=lambda part: (-len(part), min(part)))
            ):
                members = sorted(int(node) for node in part)
                ranked = sorted(members, key=lambda node: (-graph.degree[node], node))
                out.append(
                    Community(
                        community_id=index,
                        size=len(members),
                        symbols=[names[node] for node in members],
                        top_nodes=[names[node] for node in ranked[:5]],
                        engine="networkx",
                    )
                )
            return out
        finally:
            conn.close()

    def community_of(self, symbol_name: str, root: Path) -> Community | None:
        for community in self.communities(root):
            if symbol_name in community.symbols:
                return community
        return None

    @staticmethod
    def _components(
        node_ids: set[int], edges: set[tuple[int, int]], names: dict[int, str]
    ) -> list[Community]:
        adjacency: dict[int, set[int]] = {node: set() for node in node_ids}
        for src, dst in edges:
            adjacency[src].add(dst)
            adjacency[dst].add(src)
        seen: set[int] = set()
        out: list[Community] = []
        for node in sorted(node_ids):
            if node in seen:
                continue
            stack = [node]
            members: list[int] = []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                members.append(current)
                stack.extend(sorted(adjacency[current] - seen))
            members.sort()
            ranked = sorted(members, key=lambda member: (-len(adjacency[member]), member))
            out.append(
                Community(
                    community_id=len(out),
                    size=len(members),
                    symbols=[names[member] for member in members],
                    top_nodes=[names[member] for member in ranked[:5]],
                    engine="components",
                )
            )
        out.sort(key=lambda community: (-community.size, community.community_id))
        return out

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
            "SELECT ce.line AS call_line, ce.resolved AS edge_resolved, "
            "s.id AS caller_id, s.name AS caller_name, "
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
                    confidence=_confidence(row["edge_resolved"]),
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
                    confidence=_confidence(row["resolved"]),
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

    @staticmethod
    def _files_for(conn: sqlite3.Connection, ids: list[int]) -> list[str]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT DISTINCT file_path FROM symbols WHERE id IN ({placeholders})", ids
        ).fetchall()
        return [row["file_path"] for row in rows]

    @staticmethod
    def _files_matching_module(conn: sqlite3.Connection, module_name: str) -> list[str]:
        """Map a dotted module name (``pkg.mod``) onto indexed file paths."""
        dotted = module_name.replace("::", ".").replace(".", "/").strip("/")
        rows = conn.execute(
            "SELECT file_path FROM index_meta WHERE file_path LIKE ? OR file_path LIKE ? LIMIT 5",
            (f"%{dotted}%", f"%{module_name}%"),
        ).fetchall()
        return [row["file_path"] for row in rows]

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
