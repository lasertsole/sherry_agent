"""
skill_memory — 跨对话召回

并行双路径召回（两条路径同时跑，合并去重）：

精确路径（向量/FTS5 → 社区扩展 → 图遍历 → PPR 排序）：
  找到和当前查询语义相关的具体三元组

泛化路径（社区代表节点 → 图遍历 → PPR 排序）：
  提供跨领域的全局概览，覆盖精确路径可能遗漏的知识域

合并策略：精确路径的结果优先（PPR 分数更高），
          泛化路径补充精确路径未覆盖的社区。
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
    """召回结果"""
    nodes: list[GmNode]
    edges: list[GmEdge]
    token_estimate: int


class ScoredNode(TypedDict):
    """带分数的节点"""
    node: GmNode
    score: float


class ScoredCommunity(TypedDict):
    """带分数的社区"""
    id: str
    summary: str
    score: float
    node_count: int


class Recaller:
    """知识图谱召回器"""

    def __init__(self, db: Connection, cfg: GmConfig):
        """
        初始化召回器

        Args:
            db: SQLite 数据库连接
            cfg: Graph Memory 配置
        """
        self.db = db
        self.cfg = cfg
        self.embed: Embeddings | None = cfg.embedding

    async def recall(self, query: str) -> RecallResult:
        """
        执行召回流程

        Args:
            query: 查询字符串

        Returns:
            召回结果（节点、边、token 估算）
        """
        limit: int = getattr(self.cfg, 'recall_max_nodes', 6)

        # ── 两条路径各自独立跑满，不分配额 ──────────────────

        precise: RecallResult = await self._recall_precise(query, limit)
        generalized: RecallResult = await self._recall_generalized(query, limit)

        # ── 合并去重（全部保留，只去重复节点） ────────────────
        merged: RecallResult = self._merge_results(precise, generalized)

        return merged

    async def _recall_precise(self, query: str, limit: int) -> RecallResult:
        """
        精确召回：向量/FTS5 找种子 → 社区扩展 → 图遍历 → PPR 排序

        Args:
            query: 查询字符串
            limit: 返回节点数量限制

        Returns:
            精确召回结果
        """
        if self.embed:
            try:
                vec = await self.embed.aembed_query(query)
                scored = vector_search_with_score(
                    self.db, vec, math.ceil(limit / 2)
                )
                seeds: list[GmNode] = [s['node'] for s in scored]
                # 向量结果不足时补 FTS5
                if len(seeds) < 2:
                    fts_results = search_nodes(self.db, query, limit)
                    seen_ids: set[str] = {n.id for n in seeds}
                    seeds.extend([n for n in fts_results if n.id not in seen_ids])

            except Exception:
                seeds: list[GmNode]  = search_nodes(self.db, query, limit)
        else:
            seeds: list[GmNode]  = search_nodes(self.db, query, limit)

        # reranker 阈值过滤
        node_dict: dict[str, GmNode] = {s.content : s  for s in seeds}
        filter_contents: list[str] = reranker_model.filter(query, [s.content for s in seeds], gap_score = 0.5)
        if filter_contents:
            seeds = [node_dict[c] for c in filter_contents]

        if not seeds:
            return {'nodes': [], 'edges': [], 'token_estimate': 0}

        seed_ids: list[str] = [n.id for n in seeds]

        # 社区扩展
        expanded_ids: set[str] = set(seed_ids)

        for seed in seeds:
            peers: list[str] = get_community_peers(self.db, seed.id, 2)
            expanded_ids.update(peers)

        # 图遍历拿三元组
        walk_result = graph_walk(
            self.db,
            list(expanded_ids),
            getattr(self.cfg, 'recall_max_hops', 2)
        )

        nodes: list[GmNode] = walk_result['nodes']
        edges: list[GmEdge] = walk_result['edges']

        if not nodes:
            return {'nodes': [], 'edges': [], 'token_estimate': 0}

        # 个性化 PageRank 排序
        candidate_ids = [n.id for n in nodes]
        ppr_result = personalized_page_rank(
            self.db, seed_ids, candidate_ids, self.cfg
        )
        ppr_scores = ppr_result['scores']

        # 排序并截取前 limit 个
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
        泛化召回：社区向量搜索 → 取匹配社区的成员 → 图遍历 → PPR 排序

        有社区向量时：query vs 社区 embedding 匹配，按相似度排序社区
        无社区向量时：fallback 到 communityRepresentatives（按时间取代表节点）

        Args:
            query: 查询字符串
            limit: 返回节点数量限制

        Returns:
            泛化召回结果
        """
        seeds: list[GmNode] = []

        # 优先用社区向量搜索
        if self.embed:
            try:
                vec: list[float] = await self.embed.aembed_query(query)
                scored_communities: list[ScoredCommunity] = community_vector_search(self.db, vec)

                if scored_communities:
                    community_ids = [c['id'] for c in scored_communities]
                    seeds: list[GmNode] = nodes_by_community_ids(self.db, community_ids, 3)

            except Exception:
                # embedding 失败，fallback
                pass

        # reranker 阈值过滤
        node_dict: dict[str, GmNode] = {s.content : s  for s in seeds}
        filter_contents: list[str] = reranker_model.filter(query, [s.content for s in seeds], gap_score = 0.5)
        if filter_contents:
            seeds = [node_dict[c] for c in filter_contents]

        # fallback：按时间取社区代表节点
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

        # 个性化 PageRank 排序
        candidate_ids: list[str] = [n.id for n in nodes]
        ppr_result: PPRResult = personalized_page_rank(
            self.db, seed_ids, candidate_ids, self.cfg
        )
        ppr_scores: dict[str, float] = ppr_result['scores']

        # 排序并截取前 limit 个
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
        合并两条路径的结果：全部保留，只去重复节点

        Args:
            precise: 精确召回结果
            generalized: 泛化召回结果

        Returns:
            合并后的召回结果
        """
        node_map: dict[str, GmNode] = {}
        edge_map: dict[str, GmEdge] = {}

        # 精确路径全部入场
        for n in precise['nodes']:
            node_map[n.id] = n

        for e in precise['edges']:
            edge_map[e.id] = e

        # 泛化路径去重后全部入场
        for n in generalized['nodes']:
            if n.id not in node_map:
                node_map[n.id] = n

        # 合并边：两端都在最终节点集中的边才保留
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
        估算 token 数量

        Args:
            nodes: 节点列表

        Returns:
            估算的 token 数
        """
        total_chars = sum(
            len(getattr(n, 'content', '')) + len(getattr(n, 'description', ''))
            for n in nodes
        )
        return math.ceil(total_chars / 3)

    async def sync_embed(self, node: GmNode) -> None:
        """
        异步同步 embedding，不阻塞主流程

        Args:
            node: 需要生成 embedding 的节点
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
            # 不影响主流程
            pass
