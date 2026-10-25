"""Graph-algorithm queries over the llm_wiki explicit graph.

``search_wiki`` stays the grep entry point (L1); these functions are the
recommended path once a wiki has a graph: ``path`` (bidirectional BFS),
``god_nodes``, ``explain``, ``communities`` and ``query`` (a natural-language
subgraph). Everything degrades gracefully: a missing DB is built on demand and
a missing networkx falls back to connected components.
"""

from __future__ import annotations

import importlib.util
import sys
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location("wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
sys.modules.setdefault("wiki_core", _core)
_core_spec.loader.exec_module(_core)
get_wiki_path = _core.get_wiki_path

_graph_spec = importlib.util.spec_from_file_location("wiki_graph", str(_scripts_dir / "graph.py"))
_graph = importlib.util.module_from_spec(_graph_spec)
sys.modules.setdefault("wiki_graph", _graph)
_graph_spec.loader.exec_module(_graph)
build_wiki_graph = _graph.build_wiki_graph
get_graph_db_path = _graph.get_graph_db_path

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


@dataclass(slots=True)
class WikiNode:
    """One graph node."""

    id: int
    name: str
    type: str
    file_path: str | None


@dataclass(slots=True)
class WikiHop:
    """One node on a path; ``edge_type`` is the edge that led INTO it."""

    node: WikiNode
    edge_type: str
    confidence: str


@dataclass(slots=True)
class WikiPathResult:
    """Result of :func:`path`."""

    root_a: str
    root_b: str
    hops: list[WikiHop] = field(default_factory=list)
    found: bool = False
    truncated: bool = False


@dataclass(slots=True)
class NodeDegree:
    """A node ranked by graph degree."""

    node: WikiNode
    degree: int
    in_degree: int
    out_degree: int


@dataclass(slots=True)
class NeighborInfo:
    """One neighbor of an explained node."""

    node: WikiNode
    direction: str
    edge_type: str
    confidence: str
    source_file: str


@dataclass(slots=True)
class NodeExplainResult:
    """Result of :func:`explain`."""

    node: WikiNode | None
    neighbors: list[NeighborInfo] = field(default_factory=list)
    confidence: dict[str, int] = field(default_factory=dict)
    source_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WikiCommunity:
    """One Louvain / connected-component cluster of the wiki graph."""

    community_id: int
    size: int
    symbols: list[str] = field(default_factory=list)
    engine: str = "networkx"


@dataclass(slots=True)
class SubgraphEdge:
    """One edge in a natural-language query result."""

    src: str
    dst: str
    edge_type: str
    confidence: str


@dataclass(slots=True)
class SubgraphResult:
    """Result of :func:`query`."""

    question: str
    nodes: list[WikiNode] = field(default_factory=list)
    edges: list[SubgraphEdge] = field(default_factory=list)


def _resolve_root(wiki_root: Path | None) -> Path:
    return wiki_root or get_wiki_path()


def _connect(wiki_root: Path | None) -> sqlite3.Connection | None:
    root = _resolve_root(wiki_root)
    db_path = get_graph_db_path(root)
    if not db_path.is_file():
        try:
            build_wiki_graph(wiki_root=root)
        except Exception as exc:  # noqa: BLE001 - a failed build degrades to "no graph"
            logger.warning("llm_wiki: could not build the graph: {}", exc)
            return None
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _row_to_node(row: sqlite3.Row) -> WikiNode:
    return WikiNode(
        id=int(row["id"]), name=row["name"], type=row["type"], file_path=row["file_path"]
    )


def _find_node(conn: sqlite3.Connection, name: str) -> WikiNode | None:
    row = conn.execute(
        "SELECT id, name, type, file_path FROM wiki_nodes WHERE name = ? COLLATE NOCASE",
        (name.strip(),),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id, name, type, file_path FROM wiki_nodes WHERE name LIKE ? COLLATE NOCASE "
            "ORDER BY length(name) LIMIT 1",
            (f"%{name.strip()}%",),
        ).fetchone()
    return _row_to_node(row) if row is not None else None


def _neighbours(conn: sqlite3.Connection, node_id: int) -> list[tuple[int, str, str, str]]:
    """Undirected ``(neighbor_id, edge_type, confidence, direction)`` for a node."""
    out: list[tuple[int, str, str, str]] = []
    for row in conn.execute(
        "SELECT dst_id AS neighbour, edge_type, confidence FROM wiki_edges WHERE src_id = ?",
        (node_id,),
    ):
        out.append((int(row["neighbour"]), row["edge_type"], row["confidence"], "out"))
    for row in conn.execute(
        "SELECT src_id AS neighbour, edge_type, confidence FROM wiki_edges WHERE dst_id = ?",
        (node_id,),
    ):
        out.append((int(row["neighbour"]), row["edge_type"], row["confidence"], "in"))
    return out


def path(
    a_name: str, b_name: str, max_depth: int = 5, wiki_root: Path | None = None
) -> WikiPathResult:
    """Shortest undirected path between two nodes (bidirectional BFS)."""
    conn = _connect(wiki_root)
    if conn is None:
        return WikiPathResult(root_a=a_name, root_b=b_name)
    try:
        start = _find_node(conn, a_name)
        end = _find_node(conn, b_name)
        if start is None or end is None:
            return WikiPathResult(root_a=a_name, root_b=b_name)
        if start.id == end.id:
            return WikiPathResult(
                root_a=a_name,
                root_b=b_name,
                hops=[WikiHop(start, "root", "")],
                found=True,
            )
        parents_f: dict[int, tuple[int | None, str, str]] = {start.id: (None, "root", "")}
        parents_b: dict[int, tuple[int | None, str, str]] = {end.id: (None, "root", "")}
        layer_f: set[int] = {start.id}
        layer_b: set[int] = {end.id}
        meet: int | None = None
        truncated = False
        for _depth in range(max_depth):
            if not layer_f or not layer_b:
                break
            if len(layer_f) <= len(layer_b):
                layer_f, meet = _expand(layer_f, parents_f, parents_b, conn)
            else:
                layer_b, meet = _expand(layer_b, parents_b, parents_f, conn)
            if meet is not None:
                break
            if _depth == max_depth - 1:
                truncated = bool(layer_f and layer_b)
        if meet is None:
            return WikiPathResult(root_a=a_name, root_b=b_name, truncated=truncated)
        chain = _reconstruct(meet, parents_f, parents_b)
        rows = conn.execute(
            f"SELECT id, name, type, file_path FROM wiki_nodes "  # noqa: S608
            f"WHERE id IN ({','.join('?' * len(chain))})",
            [node for node, _edge, _conf in chain],
        ).fetchall()
        by_id = {int(row["id"]): _row_to_node(row) for row in rows}
        hops = [
            WikiHop(by_id[node], edge_type, confidence)
            for node, edge_type, confidence in chain
            if node in by_id
        ]
        return WikiPathResult(root_a=a_name, root_b=b_name, hops=hops, found=bool(hops))
    finally:
        conn.close()


def _expand(
    layer: set[int],
    own: dict[int, tuple[int | None, str, str]],
    other: dict[int, tuple[int | None, str, str]],
    conn: sqlite3.Connection,
) -> tuple[set[int], int | None]:
    next_layer: set[int] = set()
    for node in sorted(layer):
        for neighbour, edge_type, confidence, _direction in _neighbours(conn, node):
            if neighbour in other:
                own.setdefault(neighbour, (node, edge_type, confidence))
                return set(), neighbour
            if neighbour in own:
                continue
            own[neighbour] = (node, edge_type, confidence)
            next_layer.add(neighbour)
    return next_layer, None


def _reconstruct(
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
    result = list(forward)
    current = parents_b[meet][0]
    while current is not None:
        prev, edge_type, confidence = parents_b[current]
        result.append((current, edge_type, confidence))
        current = prev
    return result


def god_nodes(limit: int = 10, wiki_root: Path | None = None) -> list[NodeDegree]:
    """Top nodes by total edge degree (pure SQL, no optional dependency)."""
    conn = _connect(wiki_root)
    if conn is None:
        return []
    try:
        in_degree: Counter[int] = Counter()
        out_degree: Counter[int] = Counter()
        for row in conn.execute("SELECT src_id, COUNT(*) AS n FROM wiki_edges GROUP BY src_id"):
            out_degree[int(row["src_id"])] = int(row["n"])
        for row in conn.execute("SELECT dst_id, COUNT(*) AS n FROM wiki_edges GROUP BY dst_id"):
            in_degree[int(row["dst_id"])] = int(row["n"])
        ids = set(in_degree) | set(out_degree)
        if not ids:
            return []
        ranked = sorted(ids, key=lambda node: (-(in_degree[node] + out_degree[node]), node))[:limit]
        rows = conn.execute(
            f"SELECT id, name, type, file_path FROM wiki_nodes "  # noqa: S608
            f"WHERE id IN ({','.join('?' * len(ranked))})",
            ranked,
        ).fetchall()
        by_id = {int(row["id"]): _row_to_node(row) for row in rows}
        return [
            NodeDegree(
                node=by_id[node],
                degree=in_degree[node] + out_degree[node],
                in_degree=in_degree[node],
                out_degree=out_degree[node],
            )
            for node in ranked
            if node in by_id
        ]
    finally:
        conn.close()


def explain(name: str, wiki_root: Path | None = None) -> NodeExplainResult:
    """Every neighbour of a node with edge type, confidence and source file."""
    conn = _connect(wiki_root)
    if conn is None:
        return NodeExplainResult(node=None)
    try:
        node = _find_node(conn, name)
        if node is None:
            return NodeExplainResult(node=None)
        neighbours: list[NeighborInfo] = []
        confidence: Counter[str] = Counter()
        source_files: list[str] = []
        rows = conn.execute(
            "SELECT e.edge_type, e.confidence, e.source_file, 'out' AS direction, "
            "n.id AS nid, n.name AS nname, n.type AS ntype, n.file_path AS nfile "
            "FROM wiki_edges e JOIN wiki_nodes n ON n.id = e.dst_id WHERE e.src_id = ? "
            "UNION ALL "
            "SELECT e.edge_type, e.confidence, e.source_file, 'in' AS direction, "
            "n.id AS nid, n.name AS nname, n.type AS ntype, n.file_path AS nfile "
            "FROM wiki_edges e JOIN wiki_nodes n ON n.id = e.src_id WHERE e.dst_id = ?",
            (node.id, node.id),
        ).fetchall()
        for row in rows:
            neighbours.append(
                NeighborInfo(
                    node=WikiNode(
                        id=int(row["nid"]),
                        name=row["nname"],
                        type=row["ntype"],
                        file_path=row["nfile"],
                    ),
                    direction=row["direction"],
                    edge_type=row["edge_type"],
                    confidence=row["confidence"],
                    source_file=row["source_file"],
                )
            )
            confidence[row["confidence"]] += 1
            if row["source_file"] not in source_files:
                source_files.append(row["source_file"])
        return NodeExplainResult(
            node=node,
            neighbors=neighbours,
            confidence=dict(confidence),
            source_files=source_files,
        )
    finally:
        conn.close()


def communities(wiki_root: Path | None = None) -> list[WikiCommunity]:
    """Louvain communities (networkx) or connected components when unavailable."""
    conn = _connect(wiki_root)
    if conn is None:
        return []
    try:
        names = {
            int(row["id"]): row["name"] for row in conn.execute("SELECT id, name FROM wiki_nodes")
        }
        pairs = {
            (int(row["src_id"]), int(row["dst_id"]))
            for row in conn.execute("SELECT src_id, dst_id FROM wiki_edges")
            if int(row["src_id"]) != int(row["dst_id"])
        }
        if not names:
            return []
        try:
            import networkx as nx

            graph = nx.Graph()
            graph.add_nodes_from(sorted(names))
            graph.add_edges_from(sorted(pairs))
            try:
                partitions = nx.community.louvain_communities(graph, seed=0)
            except AttributeError:
                raise ImportError("networkx without louvain")
            engine = "networkx"
        except Exception:  # noqa: BLE001 - optional dependency / old networkx
            partitions = _components(set(names), pairs)
            engine = "components"
        result: list[WikiCommunity] = []
        for index, part in enumerate(
            sorted(
                (sorted(int(node) for node in part) for part in partitions),
                key=lambda part: (-len(part), part[0] if part else 0),
            )
        ):
            result.append(
                WikiCommunity(
                    community_id=index,
                    size=len(part),
                    symbols=[names[node] for node in part],
                    engine=engine,
                )
            )
        return result
    finally:
        conn.close()


def _components(node_ids: set[int], pairs: set[tuple[int, int]]) -> list[list[int]]:
    adjacency: dict[int, set[int]] = {node: set() for node in node_ids}
    for src, dst in pairs:
        if src in node_ids and dst in node_ids:
            adjacency[src].add(dst)
            adjacency[dst].add(src)
    seen: set[int] = set()
    parts: list[list[int]] = []
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
        parts.append(sorted(members))
    return parts


def _query_tokens(question: str) -> list[str]:
    """ASCII words + CJK bigrams (no jieba dependency)."""
    tokens = [token.lower() for token in _ASCII_TOKEN_RE.findall(question)]
    for run in _CJK_RE.findall(question):
        if len(run) <= 2:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))


def query(question: str, wiki_root: Path | None = None) -> SubgraphResult:
    """Natural-language → node recall → 1-hop subgraph (no embedding dependency)."""
    conn = _connect(wiki_root)
    if conn is None:
        return SubgraphResult(question=question)
    try:
        tokens = _query_tokens(question)
        if not tokens:
            return SubgraphResult(question=question)
        scores: Counter[int] = Counter()
        for token in tokens:
            for row in conn.execute(
                "SELECT id FROM wiki_nodes WHERE name LIKE ? COLLATE NOCASE LIMIT 50",
                (f"%{token}%",),
            ):
                scores[int(row["id"])] += 1
        matched = [node for node, _score in scores.most_common(10)]
        if not matched:
            return SubgraphResult(question=question)
        included = set(matched)
        for node in list(matched):
            for neighbour, _edge_type, _confidence, _direction in _neighbours(conn, node):
                included.add(neighbour)
        rows = conn.execute(
            f"SELECT id, name, type, file_path FROM wiki_nodes "  # noqa: S608
            f"WHERE id IN ({','.join('?' * len(included))}) ORDER BY name",
            list(included),
        ).fetchall()
        nodes = [_row_to_node(row) for row in rows]
        by_id = {node.id: node.name for node in nodes}
        edges: list[SubgraphEdge] = []
        for row in conn.execute("SELECT src_id, dst_id, edge_type, confidence FROM wiki_edges"):
            src, dst = int(row["src_id"]), int(row["dst_id"])
            if src in by_id and dst in by_id:
                edges.append(
                    SubgraphEdge(by_id[src], by_id[dst], row["edge_type"], row["confidence"])
                )
        return SubgraphResult(question=question, nodes=nodes, edges=edges)
    finally:
        conn.close()
