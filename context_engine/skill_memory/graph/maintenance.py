"""
skill_memory — Graph Maintenance

Invoked at: session_end (after finalize)

Execution order:
  1. Deduplication (merge first, then compute scores — avoids duplicate nodes skewing rankings)
  2. Global PageRank (baseline score written to DB, used as fallback for topNodes)
  3. Community detection (re-partition knowledge domains)
  4. Community summary generation (LLM generates one-sentence summary per community)

Note: Personalized PPR is NOT run here — it is computed in real-time during recall.
"""

import os
import time
from loguru import logger
from ..type import GmConfig
from sqlite3 import Connection
from .dedup import dedup, DedupResult
from langchain_core.embeddings import Embeddings
from typing import TypedDict, Callable, Awaitable
from .community import detect_communities, summarize_communities, CommunityResult
from .pagerank import compute_global_page_rank, invalidate_graph_cache, GlobalPageRankResult


class MaintenanceResult(TypedDict):
    """Result of maintenance operations"""
    dedup: DedupResult
    pagerank: GlobalPageRankResult
    community: CommunityResult
    community_summaries: int
    duration_ms: int


# LLM and Embedding function type definitions
CompleteFn = Callable[[str, str], Awaitable[str]]
EmbedFn = Callable[[str], Awaitable[list[float]]]


async def run_maintenance(
        db: Connection,
        cfg: GmConfig,
        llm: CompleteFn | None = None,
        embed: Embeddings | None = None,
) -> MaintenanceResult:
    """
    Execute the graph maintenance pipeline.

    Args:
        db: SQLite database connection
        cfg: Graph Memory configuration
        llm: LLM completion function (optional)
        embed: Embedding model (optional)

    Returns:
        Dictionary containing dedup, PageRank, and community detection results
    """
    start: float = time.time()

    # Clear graph structure cache after dedup/new node insertion
    invalidate_graph_cache()

    # 1. Deduplication
    dedup_result: DedupResult = dedup(db, cfg)

    # Dedup may have merged nodes, clear cache again
    if dedup_result.get('merged', 0) > 0:
        invalidate_graph_cache()

    # 2. Global PageRank (baseline)
    pagerank_result: GlobalPageRankResult = compute_global_page_rank(db, cfg)

    # 3. Community detection
    community_result: CommunityResult = detect_communities(db)

    # 4. Community summary generation (requires LLM)
    community_summaries: int = 0
    if llm and len(community_result.get('communities', {})) > 0:
        try:
            await summarize_communities(db, community_result['communities'], llm, embed)
        except Exception as err:
            if os.environ.get('GM_DEBUG'):
                logger.error(f'  [DEBUG] maintenance: community summarization failed: {err}')

    return {
        'dedup': dedup_result,
        'pagerank': pagerank_result,
        'community': community_result,
        'community_summaries': community_summaries,
        'duration_ms': int((time.time() - start) * 1000),
    }
