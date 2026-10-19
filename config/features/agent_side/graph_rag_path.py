"""GraphRAG multi-hop path-query configuration.

The vendored LightRAG retrieves entities and relations by vector similarity
only; it has no path algorithm, so a relationship question whose bridge entity
never joins the top-k recall cannot be joined by traversal. ``path_search`` adds
explicit BFS (a 2-3 hop guarantee) and an embedding-guided beam fallback for
longer chains.

``relationship_patterns`` decides when ``mix``/``hybrid`` queries auto-merge a
path section; an explicit ``mode="path"`` query always runs the search.
"""

from typing import TypedDict


class GraphRagPathConfig(TypedDict):
    """Tuneables for the GraphRAG explicit-path query layer."""

    enabled: bool
    """Master switch; off = ``path`` mode behaves like ``mix``."""
    auto_merge: bool
    """Merge path evidence into ``mix``/``hybrid`` relationship queries."""
    relationship_patterns: tuple[str, ...]
    """Case-insensitive regexes that mark a query as relationship-shaped."""
    max_depth: int
    """BFS hop ceiling (the plan's 2-3 hop guarantee)."""
    top_k_paths: int
    """Paths kept after scoring across all endpoint pairs."""
    endpoint_top_k: int
    """Vector candidates resolved per extracted keyword."""
    max_endpoints: int
    """Distinct endpoint candidates considered per query."""
    max_keywords: int
    """Extracted keywords considered for endpoint resolution."""
    max_visit_nodes: int
    """BFS expansion budget (paths visited) before the search truncates."""
    max_frontier: int
    """BFS frontier cap after each layer."""
    beam_enabled: bool
    """Run the embedding-guided beam fallback when BFS finds nothing."""
    beam_width: int
    """Candidates kept per beam per hop."""
    beam_max_hops: int
    """Total path-length ceiling for the beam fallback."""
    beam_edge_weight: float
    """Weight of the average edge weight in a beam candidate's score."""
    max_evidence_chunks: int
    """Source chunks quoted under the path section."""
    max_description_chars: int
    """Per-node / per-edge description truncation in the path section."""
    max_chunk_chars: int
    """Per-chunk truncation in the path section."""


GRAPH_RAG_PATH: GraphRagPathConfig = {
    "enabled": True,
    "auto_merge": True,
    "relationship_patterns": (
        r"\brelationship\b",
        r"\brelated\b",
        r"\bconnect(?:ed|ion)?\b",
        r"\blinked?\b",
        r"\bbetween\b.*\band\b",
        r"关系",
        r"联系",
        r"关联",
        r"之间",
        r"関係",
        r"つながり",
        r"関連",
        r"관계",
        r"연결",
        r"관련",
    ),
    "max_depth": 3,
    "top_k_paths": 10,
    "endpoint_top_k": 3,
    "max_endpoints": 4,
    "max_keywords": 6,
    "max_visit_nodes": 2_000,
    "max_frontier": 400,
    "beam_enabled": True,
    "beam_width": 5,
    "beam_max_hops": 5,
    "beam_edge_weight": 0.2,
    "max_evidence_chunks": 12,
    "max_description_chars": 600,
    "max_chunk_chars": 1_200,
}
