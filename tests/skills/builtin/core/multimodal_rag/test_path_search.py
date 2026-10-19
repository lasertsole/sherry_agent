"""Unit tests for the GraphRAG explicit-path search (BFS + beam fallback).

Hermetic: a fake graph storage and a fake vector storage stand in for SNKV, so
these tests pin the algorithm — hop guarantees, cycle safety, caps, endpoint
fallback and the beam fallback that only runs when BFS finds nothing.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from config.features import GRAPH_RAG_PATH  # noqa: E402
from graph_rag.path_search import (  # noqa: E402
    format_path_context,
    is_relationship_query,
    search_paths,
)

pytestmark = [pytest.mark.unit]


def _config(**overrides):
    config = dict(GRAPH_RAG_PATH)
    config["relationship_patterns"] = ()
    config.update(overrides)
    return config


class FakeGraph:
    """Undirected adjacency list with LightRAG's batch-read surface."""

    def __init__(self, edges: list[tuple[str, str]], weights: dict | None = None):
        self.edges: dict[tuple[str, str], dict] = {}
        self.adjacency: dict[str, list[str]] = defaultdict(list)
        self.nodes: dict[str, dict] = {}
        for src, tgt in edges:
            self._add_edge(src, tgt, (weights or {}).get((src, tgt), 1.0))
        for node in self.adjacency:
            self.nodes[node] = {
                "entity_type": "test",
                "description": f"{node} description",
                "source_id": f"chunk-{node}",
                "file_path": f"{node}.txt",
            }

    def _add_edge(self, src: str, tgt: str, weight: float) -> None:
        key = tuple(sorted((src, tgt)))
        self.edges[key] = {
            "description": f"{src} relates to {tgt}",
            "weight": weight,
            "source_id": f"chunk-{src}-{tgt}",
            "file_path": f"{src}-{tgt}.txt",
        }
        self.adjacency[src].append(tgt)
        self.adjacency[tgt].append(src)

    async def get_nodes_batch(self, node_ids):
        return {nid: self.nodes[nid] for nid in node_ids if nid in self.nodes}

    async def get_all_labels(self):
        return list(self.nodes)

    async def get_nodes_edges_batch(self, node_ids):
        return {
            nid: [(nid, nb) for nb in self.adjacency.get(nid, [])]
            for nid in node_ids
            if nid in self.nodes
        }

    async def get_edges_batch(self, pairs):
        out = {}
        for pair in pairs:
            key = tuple(sorted((pair["src"], pair["tgt"])))
            if key in self.edges:
                out[(pair["src"], pair["tgt"])] = self.edges[key]
        return out


class FakeVectorStorage:
    """Minimal entity-vdb surface used by endpoint resolution and beam scoring."""

    def __init__(self, vectors: dict[str, list[float]], node_names: list[str] | None = None):
        self.vectors = vectors
        self.node_names = node_names or list(vectors)

    async def embedding_func(self, texts, **kwargs):
        return [self.vectors.get(text, [0.0, 0.0, 1.0]) for text in texts]

    async def query(self, query, top_k, query_embedding=None):
        if query_embedding is None:
            query_embedding = self.vectors.get(query, [0.0, 0.0, 1.0])
        scored = []
        for name in self.node_names:
            vector = self.vectors[name]
            dot = sum(a * b for a, b in zip(vector, query_embedding))
            scored.append((dot, name))
        scored.sort(reverse=True)
        return [{"entity_name": name, "distance": score} for score, name in scored[:top_k]]

    async def get_vectors_by_ids(self, ids):
        from graph_rag.vendored_lightrag.utils import compute_mdhash_id

        by_hash = {
            compute_mdhash_id(name, prefix="ent-"): vector for name, vector in self.vectors.items()
        }
        return {key: by_hash[key] for key in ids if key in by_hash}


def _graph_with_chains() -> FakeGraph:
    return FakeGraph(
        [
            ("A", "X"),
            ("X", "B"),
            ("A", "Y"),
            ("Y", "Z"),
            ("Z", "B"),
            ("D", "E"),
        ]
    )


@pytest.mark.asyncio
async def test_bfs_finds_two_and_three_hop_paths():
    result = await search_paths(_graph_with_chains(), None, ["A", "B"])

    chains = {path.nodes for path in result.paths}
    assert ("A", "X", "B") in chains
    assert ("A", "Y", "Z", "B") in chains
    # Shortest first.
    assert result.paths[0].nodes == ("A", "X", "B")
    assert result.paths[0].source == "bfs"
    assert not result.used_beam
    # Node/edge data is materialised for formatting.
    assert "X" in result.nodes
    assert ("A", "X") in result.edges


@pytest.mark.asyncio
async def test_bfs_never_repeats_a_node_within_a_path():
    # A triangle would let a naive traversal walk A→X→A→X…
    graph = FakeGraph([("A", "X"), ("X", "B"), ("A", "B"), ("B", "Y"), ("Y", "A")])
    result = await search_paths(graph, None, ["A", "B"])

    assert result.paths
    for path in result.paths:
        assert len(path.nodes) == len(set(path.nodes))


@pytest.mark.asyncio
async def test_bfs_respects_the_depth_ceiling():
    graph = FakeGraph([("A", "1"), ("1", "2"), ("2", "3"), ("3", "B")])
    result = await search_paths(
        graph,
        None,
        ["A", "B"],
        config=_config(max_depth=2, beam_enabled=False),
    )
    assert result.paths == []


@pytest.mark.asyncio
async def test_bfs_caps_the_number_of_returned_paths():
    edges = [("A", f"m{i}") for i in range(12)] + [(f"m{i}", "B") for i in range(12)]
    result = await search_paths(
        FakeGraph(edges),
        None,
        ["A", "B"],
        config=_config(top_k_paths=5, beam_enabled=False),
    )
    assert len(result.paths) == 5


@pytest.mark.asyncio
async def test_endpoint_resolution_matches_labels_inside_descriptive_keywords():
    # Keyword extraction often returns "Kepler-7 launch pad" while the graph
    # names the entity "Kepler-7"; label containment resolves it without a
    # vector search.
    graph = FakeGraph([("Nova Aerospace", "Kepler-7"), ("Kepler-7", "Fenwick Holdings")])
    result = await search_paths(
        graph,
        None,
        ["Kepler-7 launch pad", "Fenwick Holdings"],
    )

    assert "Kepler-7" in result.endpoints
    assert any("Kepler-7" in path.nodes for path in result.paths)


@pytest.mark.asyncio
async def test_endpoint_resolution_falls_back_to_vector_recall():
    # The graph names the company "Nova Aerospace Inc."; extraction yields the
    # short form, which must resolve through the entity vector store.
    graph = FakeGraph([("Nova Aerospace Inc.", "Kepler-7"), ("Kepler-7", "Titan Alloys")])
    vdb = FakeVectorStorage(
        {
            "Nova Aerospace": [1.0, 0.0, 0.0],
            "Nova Aerospace Inc.": [0.99, 0.01, 0.0],
            "Titan Alloys": [0.0, 1.0, 0.0],
        }
    )
    result = await search_paths(graph, vdb, ["Nova Aerospace", "Titan Alloys"])

    assert "Nova Aerospace Inc." in result.endpoints
    assert any(
        "Nova Aerospace Inc." in path.nodes and "Titan Alloys" in path.nodes
        for path in result.paths
    )


@pytest.mark.asyncio
async def test_beam_finds_a_chain_beyond_the_bfs_ceiling():
    # A-1-2-3-4-B is five hops; BFS is capped at two. Node vectors form a
    # gradient toward B so the beam can be guided along the chain.
    graph = FakeGraph([("A", "1"), ("1", "2"), ("2", "3"), ("3", "4"), ("4", "B")])
    vectors = {
        "A": [0.0, 1.0],
        "1": [0.2, 0.8],
        "2": [0.4, 0.6],
        "3": [0.6, 0.4],
        "4": [0.8, 0.2],
        "B": [1.0, 0.0],
    }
    result = await search_paths(
        graph,
        FakeVectorStorage(vectors),
        ["A", "B"],
        config=_config(max_depth=2, beam_width=2, beam_max_hops=5),
    )

    assert result.used_beam
    assert any(len(path.nodes) == 6 for path in result.paths)
    assert result.paths[0].source == "beam"


@pytest.mark.asyncio
async def test_format_path_context_renders_chain_and_chunks():
    result = await search_paths(_graph_with_chains(), None, ["A", "B"])
    chain = next(path for path in result.paths if len(path.nodes) == 3)
    source_ids = {
        result.edges[(chain.nodes[i], chain.nodes[i + 1])]["source_id"]
        for i in range(len(chain.nodes) - 1)
    }
    chunks = {
        chunk_id: {"content": f"evidence {chunk_id}", "file_path": "doc.txt"}
        for chunk_id in source_ids
    }
    text, evidence_ids = format_path_context(result, source_chunks=chunks)

    assert "Explicit relationship paths" in text
    assert "A → X → B" in text
    assert "evidence" in text
    # Edge chunks lead the evidence list; node source chunks follow.
    assert source_ids <= set(evidence_ids)


def test_is_relationship_query_covers_all_four_languages():
    assert is_relationship_query("How is Nova Aerospace connected to Fenwick Holdings?")
    assert is_relationship_query("Nova Aerospace 和 Fenwick Holdings 是什么关系？")
    assert is_relationship_query("Nova AerospaceとFenwick Holdingsの関係は？")
    assert is_relationship_query("Nova Aerospace와 Fenwick Holdings의 관계는?")
    assert not is_relationship_query("What does Nova Aerospace operate?")
