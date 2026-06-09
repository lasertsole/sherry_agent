"""
skill_memory — 图谱维护

调用时机：session_end（finalize 之后）

执行顺序：
  1. 去重（先合并再算分数，避免重复节点干扰排名）
  2. 全局 PageRank（基线分数写入 DB，供 topNodes 兜底用）
  3. 社区检测（重新划分知识域）
  4. 社区描述生成（LLM 为每个社区生成一句话摘要）

注意：个性化 PPR 不在这里跑，它在 recall 时实时计算。
"""

import os
import time
from loguru import logger
from ..type import GmConfig
from sqlite3 import Connection
from .dedup import dedup, DedupResult
from typing import Optional, TypedDict
from langchain_core.embeddings import Embeddings
from .community import detect_communities, summarize_communities, CommunityResult
from .pagerank import compute_global_page_rank, invalidate_graph_cache, GlobalPageRankResult


class MaintenanceResult(TypedDict):
    """维护操作的结果"""
    dedup: DedupResult
    pagerank: GlobalPageRankResult
    community: CommunityResult
    community_summaries: int
    duration_ms: int


# LLM 和 Embedding 函数类型定义
CompleteFn = callable  # Callable[[str, str], Awaitable[str]]
EmbedFn = callable  # Callable[[str], Awaitable[list[float]]]


async def run_maintenance(
        db: Connection,
        cfg: GmConfig,
        llm: Optional[CompleteFn] = None,
        embed: Optional[Embeddings] = None,
) -> MaintenanceResult:
    """
    执行图谱维护流程

    Args:
        db: SQLite 数据库连接
        cfg: Graph Memory 配置
        llm: LLM 补全函数（可选）
        embed: Embedding

    Returns:
        包含去重、PageRank、社区检测结果的字典
    """
    start: float = time.time()

    # 去重/新增节点后清除图结构缓存
    invalidate_graph_cache()

    # 1. 去重
    dedup_result: DedupResult = dedup(db, cfg)

    # 去重可能合并了节点，再清一次缓存
    if dedup_result.get('merged', 0) > 0:
        invalidate_graph_cache()

    # 2. 全局 PageRank（基线）
    pagerank_result: GlobalPageRankResult = compute_global_page_rank(db, cfg)

    # 3. 社区检测
    community_result: CommunityResult = detect_communities(db)

    # 4. 社区描述生成（需要 LLM）
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
