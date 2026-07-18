"""
xp_graph - Community Detection

Purpose:
  - Discover knowledge domains (e.g., Docker-related skills cluster together automatically)
  - Pull entire community's nodes during recall
  - Group same-community nodes together during assemble for coherent context
  - Show community distribution in kg_stats
"""

import re
import json
import sqlite3
import leidenalg
import igraph as ig
from loguru import logger
from sqlite3 import Connection
from langchain_core.messages import AIMessage
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from typing import Callable, Awaitable, TypedDict, Any
from ..store.core import update_communities, upsert_community_summary, prune_community_summaries


class CommunityResult(TypedDict):
    """Community detection result"""
    labels: dict[str, str]
    communities: dict[str, list[str]]
    count: int

def _compute_safe_resolution(g: ig.Graph) -> float:
    graph_density = g.density()
    if graph_density < 0.15:
        safe_resolution = graph_density * 1.5
    elif 0.15 <= graph_density < 0.3:
        safe_resolution = graph_density * 1.2
    else:
        safe_resolution = 0.85
    return max(min(safe_resolution, 0.99), 0.001)


def detect_communities(db: Connection) -> CommunityResult:
    cursor = db.cursor()

    cursor.execute("SELECT id FROM gm_nodes")
    node_ids = [row[0] for row in cursor.fetchall()]
    if not node_ids:
        return {"labels": {}, "communities": {}, "count": 0}

    id_to_idx = {node_id: i for i, node_id in enumerate(node_ids)}
    idx_to_id = {i: node_id for i, node_id in enumerate(node_ids)}

    cursor.execute("SELECT from_id, to_id FROM gm_edges")
    edges = []
    for f, t in cursor.fetchall():
        if f in id_to_idx and t in id_to_idx:
            edges.append((id_to_idx[f], id_to_idx[t]))

    g = ig.Graph(len(node_ids), edges, directed=True)
    g.simplify(multiple=True, loops=True)
    safe_resolution = _compute_safe_resolution(g)

    partition = leidenalg.find_partition(
        g,
        leidenalg.CPMVertexPartition,
        resolution_parameter = safe_resolution,
        n_iterations = -1
    )

    return _partition_to_result(partition, idx_to_id, db)


def _partition_to_result(
    partition,
    idx_to_id: dict[int, str],
    db: Connection,
) -> CommunityResult:
    temp_communities = {}
    for idx, community_id in enumerate(partition.membership):
        node_id = idx_to_id[idx]
        if community_id not in temp_communities:
            temp_communities[community_id] = []
        temp_communities[community_id].append(node_id)

    sorted_comm_items = sorted(
        temp_communities.items(),
        key=lambda x: len(x[1]),
        reverse=True
    )

    final_labels = {}
    final_communities = {}

    for i, (old_id, members) in enumerate(sorted_comm_items):
        new_label = f"c-{i + 1}"
        final_communities[new_label] = members
        for m in members:
            final_labels[m] = new_label

    update_communities(db, final_labels)

    return {
        "labels": final_labels,
        "communities": final_communities,
        "count": len(final_communities),
    }

def get_community_peers(
    db: Connection,
    node_id: str,
    limit: int = 5
) -> list[str]:
    """
    Get node IDs belonging to the same community.

    Used during recall: find seed node → pull other nodes from the same community as supplement.

    Args:
        db: SQLite database connection
        node_id: Seed node ID
        limit: Maximum number of nodes to return

    Returns:
        List of node IDs in the same community
    """
    cursor = db.cursor()
    cursor.execute(
        "SELECT community_id FROM gm_nodes WHERE id=?",
        (node_id,)
    )
    row = cursor.fetchone()

    if not row or not row[0]:
        return []

    community_id = row[0]

    cursor.execute("""
        SELECT id FROM gm_nodes
        WHERE community_id=? AND id!=?
        ORDER BY validated_count DESC, updated_at DESC
        LIMIT ?
    """, (community_id, node_id, limit))

    return [r[0] for r in cursor.fetchall()]


# ─── Community Summary Generation ────────────────────────────

# Type definitions
CompleteFn = Callable[[str, str], Awaitable[str]]
EmbedFn = Callable[[str], Awaitable[list[float]]]

COMMUNITY_SUMMARY_SYS = """You are a knowledge graph summarization engine. Based on the node list, summarize the topic area of these nodes in a brief description.
Requirements:
- Return only the phrase itself, no explanations
- The description should cover the tools/technologies/task domains
- Do not use the word "community"\n"""


async def summarize_communities(
    db: Connection,
    communities: dict[str, list[str]],
    llm: BaseChatModel,
    embed: Embeddings = None,
) -> int:
    """
    Generate LLM summary descriptions + embedding vectors for all communities.

    Invoked after: runMaintenance → detectCommunities

    Args:
        db: SQLite database connection
        communities: Mapping from community ID to list of member node IDs
        llm: LLM completion function
        embed: Embedding model

    Returns:
        Number of summaries generated
    """
    prune_community_summaries(db)
    generated: int = 0

    cursor: sqlite3.Cursor = db.cursor()

    # Skip summary generation when community members haven't changed, to save tokens
    for community_id, member_ids in communities.items():
        if not member_ids:
            continue

        sorted_member_ids = sorted(member_ids)
        cursor.execute(
            "SELECT node_ids_snapshot FROM gm_communities WHERE id = ?",
            (community_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            try:
                old_node_ids_snapshot = json.loads(row[0])
                if sorted(old_node_ids_snapshot) == sorted_member_ids:
                    logger.debug(f"[xp_graph] Skip unchanged community: {community_id}")
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        placeholders: str = ",".join("?" * len(member_ids))

        cursor.execute(f"""
            SELECT name, type, description FROM gm_nodes
            WHERE id IN ({placeholders})
            ORDER BY validated_count DESC
            LIMIT 10
        """, (*member_ids,))

        members: list[Any] = [dict(c) for c in cursor.fetchall()]

        if not members:
            continue

        member_text:str = "\n".join(
            f"{member['type']}:{member['name']} — {member['description']}"
            for member in members
        )

        try:
            # LLM generates the description
            summary: AIMessage = await llm.ainvoke(
                COMMUNITY_SUMMARY_SYS+f"Community members:\n{member_text}",
            )

            # Clean up output
            cleaned: str = summary.content.strip()
            cleaned: str = re.sub(r'<think>[\s\S]*?</think>', '', cleaned, flags=re.IGNORECASE)
            cleaned: str = re.sub(r'<think>[\s\S]*', '', cleaned, flags=re.IGNORECASE)
            cleaned: str = re.sub(r'^["\'「"]|["\'「""]$', '', cleaned)
            cleaned: str = cleaned.replace('\n', ' ')
            cleaned: str = re.sub(r'\s{2,}', ' ', cleaned)
            cleaned: str = cleaned.strip()[:100]

            if not cleaned:
                continue

            # Generate community embedding (concatenate description + member names)
            embedding: list[float] | None = None
            try:
                embed_text = f"{cleaned}\n{', '.join([m['description'] for m in members])}"
                embedding: list[float] = await embed.aembed_query(embed_text)
            except Exception:
                logger.error(f"[DEBUG] community embedding failed for {community_id}")

            upsert_community_summary(db, community_id, cleaned, embedding, member_ids)
            generated += 1

        except Exception as e:
            logger.error(f"[WARN] community summary failed for {community_id}: {e}")

    return generated

def _normalize_labels(labels: dict[str, int]) -> dict[str, int]:
    """Remap arbitrary community IDs to 0-based sequential for comparison"""
    unique = sorted(set(labels.values()))
    remap = {old: new for new, old in enumerate(unique)}
    return {nid: remap[cid] for nid, cid in labels.items()}
