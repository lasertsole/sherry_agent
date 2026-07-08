"""
xp_graph — Cross-session recall

Parallel dual-path recall (both paths run concurrently, results merged and deduplicated):

Precise path (vector/FTS5 → community expansion → graph traversal → PPR ranking):
  Find specific triplets semantically relevant to the current query.

Generalized path (community representative nodes → graph traversal → PPR ranking):
  Provide a cross-domain global overview, covering knowledge areas the precise path may miss.

Merge strategy: Precise path results take priority (higher PPR scores),
               generalized path supplements communities not covered by the precise path.
"""
import math
import hashlib
from typing import TypedDict
from ..graph import PPRResult
from sqlite3 import Connection
from models import reranker_model
from ..type import GmConfig, GmNode, GmEdge
from langchain_core.embeddings import Embeddings
from ..graph.community import get_community_peers
from ..graph.pagerank import personalized_page_rank
from ..store.core import (
    search_nodes, vector_search_with_score,
    graph_walk, community_representatives,
    community_vector_search, nodes_by_community_ids,
    save_vector, get_vector_hash,
)


class RecallResult(TypedDict):
    """Recall result"""
    nodes: list[GmNode]
    edges: list[GmEdge]
    token_estimate: int


class ScoredNode(TypedDict):
    """Scored node"""
    node: GmNode
    score: float


class ScoredCommunity(TypedDict):
    """Scored community"""
    id: str
    summary: str
    score: float
    node_count: int


class Recaller:
    """Knowledge graph recaller"""

    def __init__(self, db: Connection, cfg: GmConfig):
        """
        Initialize the recaller.

        Args:
            db: SQLite database connection
            cfg: Graph Memory configuration
        """
        self.db = db
        self.cfg = cfg
        self.embed: Embeddings | None = cfg.embedding

    async def recall(self, query: str) -> RecallResult:
        """
        Execute the recall pipeline.

        Args:
            query: Query string

        Returns:
            Recall result (nodes, edges, token estimate)
        """
        limit: int = getattr(self.cfg, 'recall_max_nodes', 6)

        # ── Both paths run independently to full capacity, no quota split ──────────────────

        precise: RecallResult = await self._recall_precise(query, limit)
        generalized: RecallResult = await self._recall_generalized(query, limit)

        # ── Merge and deduplicate (keep all, only remove duplicate nodes) ────────────────
        merged: RecallResult = self._merge_results(precise, generalized)

        return merged

    async def _recall_precise(self, query: str, limit: int) -> RecallResult:
        """
        Precise recall: vector/FTS5 seeds → community expansion → graph traversal → PPR ranking.

        Args:
            query: Query string
            limit: Maximum number of nodes to return

        Returns:
            Precise recall result
        """
        if self.embed:
            try:
                vec = await self.embed.aembed_query(query)
                scored = vector_search_with_score(
                    self.db, vec, math.ceil(limit / 2)
                )
                seeds: list[GmNode] = [s['node'] for s in scored]
                # Fall back to FTS5 if insufficient vector results
                if len(seeds) < 2:
                    fts_results = search_nodes(self.db, query, limit)
                    seen_ids: set[str] = {n.id for n in seeds}
                    seeds.extend([n for n in fts_results if n.id not in seen_ids])

            except Exception:
                seeds: list[GmNode]  = search_nodes(self.db, query, limit)
        else:
            seeds: list[GmNode]  = search_nodes(self.db, query, limit)

        # Reranker threshold filtering
        node_dict: dict[str, GmNode] = {s.content : s  for s in seeds}
        filter_contents: list[str] = reranker_model.filter(query, [s.content for s in seeds], gap_score = 0.5)
        if filter_contents:
            seeds = [node_dict[c] for c in filter_contents]

        if not seeds:
            return {'nodes': [], 'edges': [], 'token_estimate': 0}

        seed_ids: list[str] = [n.id for n in seeds]

        # Community expansion
        expanded_ids: set[str] = set(seed_ids)

        for seed in seeds:
            peers: list[str] = get_community_peers(self.db, seed.id, 2)
            expanded_ids.update(peers)

        # Graph traversal to fetch triplets
        walk_result = graph_walk(
            self.db,
            list(expanded_ids),
            getattr(self.cfg, 'recall_max_hops', 2)
        )

        nodes: list[GmNode] = walk_result['nodes']
        edges: list[GmEdge] = walk_result['edges']

        if not nodes:
            return {'nodes': [], 'edges': [], 'token_estimate': 0}

        # Personalized PageRank ranking
        candidate_ids = [n.id for n in nodes]
        ppr_result = personalized_page_rank(
            self.db, seed_ids, candidate_ids, self.cfg
        )
        ppr_scores = ppr_result['scores']

        # Sort and truncate to limit
        filtered = sorted(
            nodes,
            key=lambda n: (
                ppr_scores.get(n.id, 0.0),
                n.validated_count,
                n.updated_at
            ),
            reverse=True
        )[:limit]

        final_ids = {n.id for n in filtered}

        return {
            'nodes': filtered,
            'edges': [
                e for e in edges
                if e.from_id in final_ids and e.to_id in final_ids
            ],
            'token_estimate': self._estimate_tokens(filtered),
        }

    async def _recall_generalized(self, query: str, limit: int) -> RecallResult:
        """
        Generalized recall: community vector search → fetch matching community members → graph traversal → PPR ranking.

        When community vectors exist: query vs community embedding matching, sort communities by similarity.
        When no community vectors: fallback to communityRepresentatives (time-based representative nodes).

        Args:
            query: Query string
            limit: Maximum number of nodes to return

        Returns:
            Generalized recall result
        """
        seeds: list[GmNode] = []

        # Prefer community vector search
        if self.embed:
            try:
                vec: list[float] = await self.embed.aembed_query(query)
                scored_communities: list[ScoredCommunity] = community_vector_search(self.db, vec)

                if scored_communities:
                    community_ids = [c['id'] for c in scored_communities]
                    seeds: list[GmNode] = nodes_by_community_ids(self.db, community_ids, 3)

            except Exception:
                # embedding failed, fallback
                pass

        # Reranker threshold filtering
        node_dict: dict[str, GmNode] = {s.content : s  for s in seeds}
        filter_contents: list[str] = reranker_model.filter(query, [s.content for s in seeds], gap_score = 0.5)
        if filter_contents:
            seeds = [node_dict[c] for c in filter_contents]

        # fallback: time-based community representative nodes
        if not seeds:
            seeds: list[GmNode] = community_representatives(self.db, 2)

        if not seeds:
            return {'nodes': [], 'edges': [], 'token_estimate': 0}

        seed_ids: list[str] = [n.id for n in seeds]

        walk_result = graph_walk(self.db, seed_ids, 1)

        nodes: list[GmNode] = walk_result['nodes']
        edges: list[GmEdge] = walk_result['edges']

        if not nodes:
            return {'nodes': [], 'edges': [], 'token_estimate': 0}

        # Personalized PageRank ranking
        candidate_ids: list[str] = [n.id for n in nodes]
        ppr_result: PPRResult = personalized_page_rank(
            self.db, seed_ids, candidate_ids, self.cfg
        )
        ppr_scores: dict[str, float] = ppr_result['scores']

        # Sort and truncate to limit
        filtered: list[GmNode] = sorted(
            nodes,
            key=lambda n: (
                ppr_scores.get(n.id, 0.0),
                n.updated_at,
                n.validated_count
            ),
            reverse=True
        )[:limit]

        final_ids: set[str] = {n.id for n in filtered}

        return {
            'nodes': filtered,
            'edges': [
                e for e in edges
                if e.from_id in final_ids and e.to_id in final_ids
            ],
            'token_estimate': self._estimate_tokens(filtered),
        }

    def _merge_results(
        self, precise: RecallResult, generalized: RecallResult
    ) -> RecallResult:
        """
        Merge results from both paths: keep all, only deduplicate nodes.

        Args:
            precise: Precise recall result
            generalized: Generalized recall result

        Returns:
            Merged recall result
        """
        node_map: dict[str, GmNode] = {}
        edge_map: dict[str, GmEdge] = {}

        # Precise path results take precedence
        for n in precise['nodes']:
            node_map[n.id] = n

        for e in precise['edges']:
            edge_map[e.id] = e

        # Generalized path results included after deduplication
        for n in generalized['nodes']:
            if n.id not in node_map:
                node_map[n.id] = n

        # Merge edges: only keep edges whose both endpoints are in the final node set
        final_ids: set[str] = set(node_map.keys())

        for e in generalized['edges']:
            if (e.id not in edge_map and
                e.from_id in final_ids and
                e.to_id in final_ids):
                edge_map[e.id] = e

        nodes: list[GmNode] = list(node_map.values())
        edges: list[GmEdge] = list(edge_map.values())

        return {
            'nodes': nodes,
            'edges': edges,
            'token_estimate': self._estimate_tokens(nodes),
        }

    def _estimate_tokens(self, nodes: list[GmNode]) -> int:
        """
        Estimate token count.

        Args:
            nodes: List of nodes

        Returns:
            Estimated number of tokens
        """
        total_chars = sum(
            len(getattr(n, 'content', '')) + len(getattr(n, 'description', ''))
            for n in nodes
        )
        return math.ceil(total_chars / 3)

    async def sync_embed(self, node: GmNode) -> None:
        """
        Async embedding sync, non-blocking for the main flow.

        Args:
            node: Node that needs embedding generation
        """

        if not self.embed:
            return

        content: str = getattr(node, 'content', '')
        hash_obj: str = hashlib.md5(content.encode()).hexdigest()

        existing_hash: str = get_vector_hash(self.db, node.id)

        if existing_hash == hash_obj:
            return

        try:
            name: str = getattr(node, 'name', '')
            description: str = getattr(node, 'description', '')
            text: str = f"{name}: {description}\n{content[:500]}"
            vec: list[float] = await self.embed.aembed_query(text)
            if vec:
                save_vector(self.db, getattr(node, 'id', ''), content, vec)

        except Exception:
            # Does not affect the main flow
            pass
