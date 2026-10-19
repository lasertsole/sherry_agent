"""Explicit multi-hop path search over the LightRAG knowledge graph.

LightRAG retrieves entities/relations by vector similarity and leaves the
"multi-hop" joining to the LLM. When a bridge entity never enters the top-k
recall, the join is a probabilistic event, not a property of the graph. This
module adds a deterministic traversal layer:

* ``bfs`` — shortest paths up to ``GRAPH_RAG_PATH["max_depth"]`` hops (the
  2-3 hop guarantee the plan asks for). Every path is materialised from real
  ``get_nodes_edges_batch`` reads; cycles are impossible by construction.
* ``beam`` — an embedding-guided bidirectional beam for longer chains, run
  ONLY when BFS found nothing. Candidates are scored by cosine similarity to
  the opposite endpoint plus edge weight, so the expansion is guided rather
  than breadth-first.

The module is skill-owned (not vendored): the vendored LightRAG only gains a
small dispatch hook in ``operate.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from config.features import GRAPH_RAG_PATH

from graph_rag.vendored_lightrag.constants import GRAPH_FIELD_SEP
from graph_rag.vendored_lightrag.utils import logger

_RELATIONSHIP_RE = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in GRAPH_RAG_PATH["relationship_patterns"]
)


def is_relationship_query(query: str) -> bool:
    """True when the query asks how two things relate (four languages)."""
    return bool(query) and any(pattern.search(query) for pattern in _RELATIONSHIP_RE)


@dataclass(frozen=True)
class GraphPath:
    """One materialised chain: ordered node ids plus the edges between them."""

    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    avg_weight: float
    score: float
    source: str = "bfs"


@dataclass
class PathSearchResult:
    """Everything the caller needs to format a path section."""

    endpoints: list[str] = field(default_factory=list)
    paths: list[GraphPath] = field(default_factory=list)
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[tuple[str, str], dict] = field(default_factory=dict)
    used_beam: bool = False
    truncated: bool = False


def _cosine(a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.0
    try:
        import numpy as np

        va = np.asarray(a, dtype=np.float32).ravel()
        vb = np.asarray(b, dtype=np.float32).ravel()
        if va.size == 0 or vb.size == 0 or va.size != vb.size:
            return 0.0
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        return 0.0


async def _neighbor_map(graph: Any, node_ids: list[str]) -> dict[str, list[str]]:
    """One-hop neighbours for a batch of nodes (deduped, self-loops dropped)."""
    if not node_ids:
        return {}
    batch = await graph.get_nodes_edges_batch(node_ids)
    out: dict[str, list[str]] = {}
    for nid in node_ids:
        neighbours: list[str] = []
        for pair in batch.get(nid) or ():
            src, tgt = pair[0], pair[1]
            nb = tgt if src == nid else src
            if nb and nb != nid and nb not in neighbours:
                neighbours.append(nb)
        out[nid] = neighbours
    return out


async def _node_vectors(entities_vdb: Any, node_ids: list[str]) -> dict[str, Any]:
    """Fetch node embeddings keyed by GRAPH node id.

    The entity vdb keys are ``compute_mdhash_id(entity_name, "ent-")`` while the
    graph keys nodes by entity name — translate before the batch read.
    """
    getter = getattr(entities_vdb, "get_vectors_by_ids", None)
    if getter is None or not node_ids:
        return {}
    from graph_rag.vendored_lightrag.utils import compute_mdhash_id

    vdb_ids = {node_id: compute_mdhash_id(node_id, prefix="ent-") for node_id in node_ids}
    try:
        raw = await getter(list(vdb_ids.values()))
    except Exception as exc:  # pragma: no cover - defensive, storage-specific
        logger.debug(f"[path_search] vector fetch failed: {exc}")
        return {}
    return {node_id: raw[vdb_id] for node_id, vdb_id in vdb_ids.items() if vdb_id in raw}


async def _edge_weights(graph: Any, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
    if not pairs:
        return {}
    batch = await graph.get_edges_batch([{"src": a, "tgt": b} for a, b in pairs])
    out: dict[tuple[str, str], float] = {}
    for pair in pairs:
        data = batch.get(pair) or {}
        try:
            out[pair] = float(data.get("weight", 1.0))
        except (TypeError, ValueError):
            out[pair] = 1.0
    return out


async def _match_labels(graph: Any, keywords: list[str]) -> dict[str, str]:
    """Resolve keywords by label containment (``"Kepler-7 launch pad"`` → ``"Kepler-7"``).

    LLM keyword extraction often returns a descriptive phrase around the entity
    name, and the exact-name pass misses it. Comparing against the graph's
    labels is cheaper and more precise than a vector hop, so it runs before the
    vector fallback.
    """
    getter = getattr(graph, "get_all_labels", None)
    if getter is None:
        return {}
    try:
        labels = await getter()
    except Exception as exc:  # pragma: no cover - storage-specific
        logger.debug(f"[path_search] label scan failed: {exc}")
        return {}

    labels = [label for label in labels or () if isinstance(label, str) and len(label) >= 3]
    if not labels:
        return {}

    folded_labels = [(label, label.casefold()) for label in labels]
    resolved: dict[str, str] = {}
    for keyword in keywords:
        folded = keyword.casefold()
        best: tuple[int, str] | None = None
        for label, folded_label in folded_labels:
            if folded == folded_label:
                rank = 0
            elif len(folded) >= 3 and folded in folded_label:
                # The label is the query's phrase plus qualifiers.
                rank = 100 + len(folded_label) - len(folded)
            elif len(folded_label) >= 3 and folded_label in folded:
                # The keyword is the label plus qualifiers (the common case).
                rank = 1 + len(folded) - len(folded_label)
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, label)
        if best is not None:
            resolved[keyword] = best[1]
    return resolved


async def resolve_endpoints(
    graph: Any,
    entities_vdb: Any,
    keywords: list[str],
    *,
    top_k: int,
    max_endpoints: int,
    max_keywords: int,
) -> list[str]:
    """Map extracted keyword strings to graph node ids.

    Three passes, in cost order: exact name hits → graph-label containment
    (``"Kepler-7 launch pad"`` → ``"Kepler-7"``) → a vector search of the
    keyword itself (``"Nova Aerospace"`` vs ``"Nova Aerospace Inc."``). Later
    passes only run while fewer than two endpoints are resolved.
    """
    keywords = [kw.strip() for kw in keywords if kw and kw.strip()][:max_keywords]
    if not keywords:
        return []

    endpoints: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen and len(endpoints) < max_endpoints:
            seen.add(name)
            endpoints.append(name)

    # Pass 1: exact node-name hits.
    candidates = await graph.get_nodes_batch(keywords)
    for kw in keywords:
        if kw in candidates:
            _add(kw)

    # Pass 2: label containment.
    if len(endpoints) < 2:
        matched = await _match_labels(graph, keywords)
        for kw in keywords:
            name = matched.get(kw)
            if name:
                _add(name)

    if len(endpoints) >= 2 or entities_vdb is None:
        return endpoints

    # Pass 3: vector fallback — embed every keyword in one batch, then search.
    embed_func = getattr(entities_vdb, "embedding_func", None)
    embeddings: list[Any] = [None] * len(keywords)
    if embed_func is not None:
        try:
            embedded = await embed_func(keywords, context="query", _priority=5)
            if embedded is not None and len(embedded) == len(keywords):
                embeddings = list(embedded)
        except Exception as exc:  # pragma: no cover - provider-specific
            logger.debug(f"[path_search] keyword embedding failed: {exc}")

    resolved: list[str] = []
    for kw, emb in zip(keywords, embeddings):
        try:
            results = await entities_vdb.query(kw, top_k=top_k, query_embedding=emb)
        except Exception as exc:  # pragma: no cover - storage-specific
            logger.debug(f"[path_search] endpoint search failed for {kw!r}: {exc}")
            continue
        for item in results or ():
            name = item.get("entity_name") or item.get("id")
            if name:
                resolved.append(name)

    if resolved:
        existing = await graph.get_nodes_batch(list(dict.fromkeys(resolved)))
        for name in resolved:
            if name in existing:
                _add(name)
    return endpoints


async def _bfs_paths(
    graph: Any,
    sources: list[str],
    targets: set[str],
    *,
    max_depth: int,
    max_visit_nodes: int,
    max_frontier: int,
) -> tuple[list[tuple[str, ...]], bool]:
    """Enumerate all simple paths of length 1..max_depth between sources and targets."""
    found: list[tuple[str, ...]] = []
    visits = 0
    truncated = False

    for source in sources:
        frontier: list[tuple[str, ...]] = [(source,)]
        for _depth in range(max_depth):
            if not frontier:
                break
            last_nodes = list(dict.fromkeys(path[-1] for path in frontier))
            neighbours = await _neighbor_map(graph, last_nodes)
            next_frontier: list[tuple[str, ...]] = []
            stop = False
            for path in frontier:
                for neighbour in neighbours.get(path[-1], ()):
                    if neighbour in path:
                        continue
                    visits += 1
                    if visits > max_visit_nodes:
                        truncated = True
                        stop = True
                        break
                    candidate = (*path, neighbour)
                    if neighbour in targets:
                        found.append(candidate)
                    next_frontier.append(candidate)
                if stop:
                    break
            if stop:
                break
            frontier = next_frontier[:max_frontier]
        if truncated:
            break

    return found, truncated


def _meet(forward: tuple[str, ...], backward: tuple[str, ...]) -> tuple[str, ...] | None:
    """Join two growing chains if they share a node (backward starts at the target)."""
    common = set(forward) & set(backward)
    if not common:
        return None
    pivot = next(node for node in reversed(forward) if node in common)
    head = forward[: forward.index(pivot) + 1]
    tail = tuple(reversed(backward[: backward.index(pivot)]))
    return head + tail


async def _expand_beam(
    graph: Any,
    entities_vdb: Any,
    beam: list[tuple[tuple[str, ...], float]],
    goal_vector: Any,
    *,
    beam_width: int,
    beam_edge_weight: float,
) -> list[tuple[tuple[str, ...], float]]:
    if not beam:
        return []
    last_nodes = list(dict.fromkeys(path[-1] for path, _score in beam))
    neighbours = await _neighbor_map(graph, last_nodes)

    candidates: list[tuple[tuple[str, ...], float, tuple[str, str]]] = []
    new_nodes: list[str] = []
    for path, score in beam:
        for neighbour in neighbours.get(path[-1], ()):
            if neighbour in path:
                continue
            pair = (path[-1], neighbour)
            candidates.append(((*path, neighbour), score, pair))
            new_nodes.append(neighbour)

    weights = await _edge_weights(graph, list(dict.fromkeys(c[2] for c in candidates)))
    vectors = await _node_vectors(entities_vdb, list(dict.fromkeys(new_nodes)))

    scored: list[tuple[tuple[str, ...], float]] = []
    for path, score, pair in candidates:
        weight = weights.get(pair, 1.0)
        similarity = _cosine(vectors.get(pair[1]), goal_vector)
        scored.append((path, score + similarity + beam_edge_weight * weight))

    scored.sort(key=lambda item: item[1], reverse=True)
    deduped: list[tuple[tuple[str, ...], float]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for path, score in scored:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        deduped.append((path, score))
        if len(deduped) >= beam_width:
            break
    return deduped


async def _beam_paths(
    graph: Any,
    entities_vdb: Any,
    source: str,
    target: str,
    *,
    beam_width: int,
    max_hops: int,
    beam_edge_weight: float,
) -> list[tuple[str, ...]]:
    """Bidirectional embedding-guided beam from ``source`` to ``target``."""
    vectors = await _node_vectors(entities_vdb, [source, target])
    source_vector = vectors.get(source)
    target_vector = vectors.get(target)
    if source_vector is None or target_vector is None:
        return []

    forward: list[tuple[tuple[str, ...], float]] = [((source,), 0.0)]
    backward: list[tuple[tuple[str, ...], float]] = [((target,), 0.0)]
    meets: list[tuple[str, ...]] = []

    for step in range(max_hops):
        if step % 2 == 0:
            forward = await _expand_beam(
                graph,
                entities_vdb,
                forward,
                target_vector,
                beam_width=beam_width,
                beam_edge_weight=beam_edge_weight,
            )
        else:
            backward = await _expand_beam(
                graph,
                entities_vdb,
                backward,
                source_vector,
                beam_width=beam_width,
                beam_edge_weight=beam_edge_weight,
            )
        for fwd_path, _fwd_score in forward:
            for bwd_path, _bwd_score in backward:
                joined = _meet(fwd_path, bwd_path)
                if joined is not None and joined not in meets:
                    meets.append(joined)
        if meets:
            break
        if not forward or not backward:
            break

    return meets


def _score_paths(
    paths: list[tuple[str, ...]],
    weights: dict[tuple[str, str], float],
    source: str,
) -> list[GraphPath]:
    scored: list[GraphPath] = []
    for nodes in paths:
        edges = tuple(zip(nodes, nodes[1:]))
        edge_weights = [weights.get((a, b), weights.get((b, a), 1.0)) for a, b in edges]
        avg_weight = sum(edge_weights) / len(edge_weights) if edge_weights else 1.0
        scored.append(
            GraphPath(
                nodes=nodes,
                edges=edges,
                avg_weight=avg_weight,
                score=float(len(edges)),
                source=source,
            )
        )
    scored.sort(key=lambda path: (path.score, -path.avg_weight))
    return scored


async def search_paths(
    graph: Any,
    entities_vdb: Any,
    keywords: list[str],
    *,
    config: dict | None = None,
) -> PathSearchResult:
    """Resolve endpoint candidates and search for paths between them."""
    cfg = config or GRAPH_RAG_PATH
    result = PathSearchResult()
    if not cfg["enabled"]:
        return result

    result.endpoints = await resolve_endpoints(
        graph,
        entities_vdb,
        keywords,
        top_k=cfg["endpoint_top_k"],
        max_endpoints=cfg["max_endpoints"],
        max_keywords=cfg["max_keywords"],
    )
    if len(result.endpoints) < 2:
        logger.info("[path_search] fewer than two endpoints resolved; skipping")
        return result

    found, result.truncated = await _bfs_paths(
        graph,
        result.endpoints,
        set(result.endpoints),
        max_depth=cfg["max_depth"],
        max_visit_nodes=cfg["max_visit_nodes"],
        max_frontier=cfg["max_frontier"],
    )

    if not found and cfg["beam_enabled"]:
        logger.info("[path_search] BFS found no path; trying embedding-guided beam")
        beam_found: list[tuple[str, ...]] = []
        endpoints = result.endpoints
        for i, source in enumerate(endpoints):
            for target in endpoints[i + 1 :]:
                beam_found.extend(
                    await _beam_paths(
                        graph,
                        entities_vdb,
                        source,
                        target,
                        beam_width=cfg["beam_width"],
                        max_hops=cfg["beam_max_hops"],
                        beam_edge_weight=cfg["beam_edge_weight"],
                    )
                )
        if beam_found:
            result.used_beam = True
            found = beam_found

    if not found:
        return result

    # Deduplicate undirected chains (A→X→B and B→X→A are the same relationship).
    unique: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for path in found:
        key = tuple(sorted(path))
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        unique.append(path)

    all_pairs: list[tuple[str, str]] = []
    for path in unique:
        all_pairs.extend(zip(path, path[1:]))
    weights = await _edge_weights(graph, list(dict.fromkeys(all_pairs)))

    scored = _score_paths(unique, weights, "beam" if result.used_beam else "bfs")
    result.paths = scored[: cfg["top_k_paths"]]

    node_ids = list(dict.fromkeys(node for path in result.paths for node in path.nodes))
    edge_pairs = list(dict.fromkeys(pair for path in result.paths for pair in path.edges))
    result.nodes = await graph.get_nodes_batch(node_ids)
    result.edges = await graph.get_edges_batch([{"src": a, "tgt": b} for a, b in edge_pairs])

    logger.info(
        "[path_search] %d path(s) between %s (source=%s, truncated=%s)",
        len(result.paths),
        ", ".join(result.endpoints),
        "beam" if result.used_beam else "bfs",
        result.truncated,
    )
    return result


def format_path_context(
    result: PathSearchResult,
    *,
    max_description_chars: int = 600,
    source_chunks: dict[str, dict] | None = None,
    max_chunk_chars: int = 1_200,
) -> tuple[str, list[str]]:
    """Render the path section for the LLM prompt; return (text, evidence chunk ids)."""
    if not result.paths:
        return "", []

    def _short(value: Any, limit: int) -> str:
        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"

    lines: list[str] = [
        "Explicit relationship paths (shortest-hop graph traversal; each arrow is a "
        "real edge in the knowledge graph):",
    ]
    evidence_ids: list[str] = []

    for index, path in enumerate(result.paths, start=1):
        hops = len(path.edges)
        lines.append(f"\nPath {index} — {hops} hop(s), average edge weight {path.avg_weight:.2f}:")
        chain = " → ".join(path.nodes)
        lines.append(chain)
        for node in path.nodes:
            data = result.nodes.get(node) or {}
            label = data.get("entity_type", "UNKNOWN")
            description = _short(data.get("description", ""), max_description_chars)
            lines.append(f"  - {node} ({label}): {description}")
        for src, tgt in path.edges:
            data = result.edges.get((src, tgt)) or result.edges.get((tgt, src)) or {}
            weight = data.get("weight", 1.0)
            description = _short(data.get("description", ""), max_description_chars)
            lines.append(f"  - {src} → {tgt} (weight {weight}): {description}")
            for chunk_id in _split_source_ids(data.get("source_id")):
                if chunk_id not in evidence_ids:
                    evidence_ids.append(chunk_id)
            for endpoint in (src, tgt):
                node_data = result.nodes.get(endpoint) or {}
                for chunk_id in _split_source_ids(node_data.get("source_id")):
                    if chunk_id not in evidence_ids:
                        evidence_ids.append(chunk_id)

    text = "\n".join(lines)

    if source_chunks:
        quoted: list[str] = []
        for chunk_id in evidence_ids:
            chunk = source_chunks.get(chunk_id)
            if not chunk:
                continue
            content = _short(
                chunk.get("content") or chunk.get("chunk_content") or "", max_chunk_chars
            )
            file_path = chunk.get("file_path") or "unknown_source"
            quoted.append(f"[{chunk_id}] ({file_path})\n{content}")
        if quoted:
            text += "\n\nSupporting document chunks for these edges:\n" + "\n---\n".join(quoted)

    return text, evidence_ids


def _split_source_ids(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [part for part in str(value).split(GRAPH_FIELD_SEP) if part]


__all__ = [
    "GraphPath",
    "PathSearchResult",
    "format_path_context",
    "is_relationship_query",
    "resolve_endpoints",
    "search_paths",
]
