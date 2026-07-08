"""
xp_graph — Personalized PageRank (PPR)

═══════════════════════════════════════════════════════════════
Personalized PageRank (PPR)

Unlike global PageRank:
  Global PR: All nodes start with uniform scores; computes a fixed global ranking
  Personalized PPR: Starts from seed nodes hit by the user query; propagates weight along edges
                   Nodes closer to seeds get higher scores

Same graph, different queries:
  Query "Docker deployment"   → Docker-related SKILL nodes score highest
  Query "conda environment"   → conda-related SKILL nodes score highest
  Query "bilibili crawler"    → bilibili-related TASK/SKILL nodes score highest

Computation timing:
  Computed in real-time during recall (not stored in DB); every query is fresh
  O(iterations * edges), thousands of nodes < 5ms

A global PageRank is also retained as a baseline for:
  - topNodes fallback (when no seed available)
  - Written to gm_nodes.pagerank column at session_end
═══════════════════════════════════════════════════════════════
"""

import time
from ..type import GmConfig
from typing import TypedDict
from sqlite3 import Connection
from ..store.core import update_pageranks


# ─── Graph structure cache (avoids SQL query on every recall) ─────────────────

class GraphStructure(TypedDict):
    """Graph structure"""
    node_ids: set[str]
    adj: dict[str, list[str]]  # undirected adjacency list
    n: int  # node count
    cached_at: int  # cache timestamp


_cached: GraphStructure | None = None
CACHE_TTL = 30_000  # 30 second cache


def load_graph(db: Connection) -> GraphStructure:
    """
    Load graph structure (with caching).

    Compaction adds new nodes/edges, but sharing the same graph structure
    within 30 seconds is acceptable.

    Args:
        db: SQLite database connection

    Returns:
        Graph structure containing node ID set, adjacency list, and node count
    """
    global _cached

    if _cached and (time.time() * 1000 - _cached['cached_at']) < CACHE_TTL:
        return _cached

    cursor = db.cursor()

    # Load active nodes
    cursor.execute("SELECT id FROM gm_nodes")
    node_rows = cursor.fetchall()
    node_ids = {row[0] for row in node_rows}

    # Load edges
    cursor.execute("SELECT from_id, to_id FROM gm_edges")
    edge_rows = cursor.fetchall()

    # Build undirected adjacency list
    adj: dict[str, list[str]] = {node_id: [] for node_id in node_ids}

    for from_id, to_id in edge_rows:
        if from_id not in node_ids or to_id not in node_ids:
            continue
        adj[from_id].append(to_id)
        adj[to_id].append(from_id)

    return GraphStructure(node_ids=node_ids, adj=adj, n=len(node_ids), cached_at=int(time.time() * 1000))


def invalidate_graph_cache() -> None:
    """
    Clear graph structure cache (called after compact/finalize).
    """
    global _cached
    _cached = None


# ─── Personalized PageRank ─────────────────────────────────────────

class PPRResult(TypedDict):
    """Personalized PageRank result"""
    scores: dict[str, float]  # nodeId → personalized score


def personalized_page_rank(
    db: Connection,
    seed_ids: list[str],
    candidate_ids: list[str],
    cfg: GmConfig,
) -> PPRResult:
    """
    Personalized PageRank.

    Propagates weight from seed_ids:
      - teleport probability (1-damping) always returns to seed nodes
        (not uniformly to all nodes)
      - This means nodes near seeds naturally get higher scores

    Args:
        db: SQLite database connection
        seed_ids: Seed nodes hit by the user query (FTS5/vector search results)
        candidate_ids: Candidate nodes to rank (graph traversal results)
        cfg: Graph Memory configuration

    Returns:
        Dictionary of personalized scores for candidate nodes
    """
    graph = load_graph(db)
    node_ids = graph['node_ids']
    adj = graph['adj']
    n = graph['n']

    damping = getattr(cfg, 'pagerank_damping', 0.85)
    iterations = getattr(cfg, 'pagerank_iterations', 20)

    if n == 0 or not seed_ids:
        return {'scores': {}}

    # Filter out non-existent seed nodes
    valid_seeds = [sid for sid in seed_ids if sid in node_ids]
    if not valid_seeds:
        return {'scores': {}}

    # Teleport vector: points only to seed nodes, uniformly distributed
    teleport_weight = 1.0 / len(valid_seeds)
    seed_set = set(valid_seeds)

    # Initial scores: concentrated on seed nodes
    rank: dict[str, float] = {}
    for node_id in node_ids:
        rank[node_id] = teleport_weight if node_id in seed_set else 0.0

    # Iterate
    for _ in range(iterations):
        new_rank: dict[str, float] = {}

        # Teleport component: return to seed nodes
        for node_id in node_ids:
            new_rank[node_id] = (1 - damping) * teleport_weight if node_id in seed_set else 0.0

        # Propagation component: gain weight from neighbors
        for node_id, neighbors in adj.items():
            if not neighbors:
                continue

            current_rank = rank.get(node_id, 0.0)
            if current_rank == 0.0:
                continue

            contrib = current_rank / len(neighbors)

            for neighbor in neighbors:
                new_rank[neighbor] = new_rank.get(neighbor, 0.0) + damping * contrib

        # Dangling nodes' scores propagate back to seed nodes
        # (not uniformly distributed to all nodes)
        dangling_sum = 0.0
        for node_id in node_ids:
            neighbors = adj.get(node_id, [])
            if not neighbors:
                dangling_sum += rank.get(node_id, 0.0)

        if dangling_sum > 0:
            dangling_contrib = damping * dangling_sum * teleport_weight
            for seed_id in valid_seeds:
                new_rank[seed_id] = new_rank.get(seed_id, 0.0) + dangling_contrib

        rank = new_rank

    # Only return scores for candidate nodes
    result_scores = {cid: rank.get(cid, 0.0) for cid in candidate_ids}

    return {'scores': result_scores}


# ─── Global PageRank (baseline, updated at session_end) ──────────────

class GlobalPageRankResult(TypedDict):
    """Global PageRank result"""
    scores: dict[str, float]
    top_k: list[dict[str, object]]  # [{id, name, score}, ...]


def compute_global_page_rank(db: Connection, cfg: GmConfig) -> GlobalPageRankResult:
    """
    Global PageRank — written to gm_nodes.pagerank as a baseline.

    Purpose:
      - topNodes fallback ordering (when no query seed is available)
      - gm_stats display of globally important nodes

    Only called at session_end / gm_maintain.

    Args:
        db: SQLite database connection
        cfg: Graph Memory configuration

    Returns:
        Result containing all node scores and top 20 nodes
    """
    graph = load_graph(db)
    node_ids = graph['node_ids']
    adj = graph['adj']
    n = graph['n']

    damping = getattr(cfg, 'pagerank_damping', 0.85)
    iterations = getattr(cfg, 'pagerank_iterations', 20)

    if n == 0:
        return {'scores': {}, 'top_k': []}

    # Get node name mapping
    cursor = db.cursor()
    cursor.execute("SELECT id, name FROM gm_nodes")
    name_rows = cursor.fetchall()
    name_map = {row[0]: row[1] for row in name_rows}

    # Global: uniform teleport
    init_score = 1.0 / n
    rank: dict[str, float] = {node_id: init_score for node_id in node_ids}

    # Iterate
    for _ in range(iterations):
        new_rank: dict[str, float] = {}
        base = (1 - damping) / n

        # Initialize base scores
        for node_id in node_ids:
            new_rank[node_id] = base

        # Propagation component
        for node_id, neighbors in adj.items():
            if not neighbors:
                continue

            current_rank = rank.get(node_id, 0.0)
            if current_rank == 0.0:
                continue

            contrib = current_rank / len(neighbors)

            for neighbor in neighbors:
                new_rank[neighbor] = new_rank.get(neighbor, base) + damping * contrib

        # Dangling node handling
        dangling_sum = sum(rank.get(node_id, 0.0) for node_id in node_ids if not adj.get(node_id))

        if dangling_sum > 0:
            dc = damping * dangling_sum / n
            for node_id in node_ids:
                new_rank[node_id] = new_rank.get(node_id, 0.0) + dc

        rank = new_rank

    # Write to database
    update_pageranks(db, rank)

    # Sort and take top 20
    sorted_scores = sorted(rank.items(), key=lambda x: x[1], reverse=True)[:20]
    top_k = [
        {'id': node_id, 'name': name_map.get(node_id, node_id), 'score': score}
        for node_id, score in sorted_scores
    ]

    return {'scores': rank, 'top_k': top_k}
