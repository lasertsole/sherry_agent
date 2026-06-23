"""
skill_memory — Personalized PageRank (PPR)

═══════════════════════════════════════════════════════════════
个性化 PageRank（Personalized PageRank）

区别于全局 PageRank：
  全局 PR：所有节点均匀起步，算一个固定的全局排名
  个性化 PPR：从用户查询命中的种子节点出发，沿边传播权重
              离种子越近的节点分数越高

同一个图谱：
  问 "Docker 部署"   → Docker 相关 SKILL 分数最高
  问 "conda 环境"    → conda 相关 SKILL 分数最高
  问 "bilibili 爬虫" → bilibili 相关 TASK/SKILL 分数最高

计算时机：
  recall 时实时算（不存数据库），每次查询都是新鲜的
  O(iterations * edges)，几千节点 < 5ms

另外保留一个全局 PageRank 作为基线，用于：
  - topNodes 兜底（没有种子时）
  - session_end 时写入 gm_nodes.pagerank 列
═══════════════════════════════════════════════════════════════
"""

import time
from ..type import GmConfig
from typing import TypedDict
from sqlite3 import Connection
from ..store.core import update_pageranks


# ─── 图结构缓存（避免每次 recall 都查 SQL） ─────────────────

class GraphStructure(TypedDict):
    """图结构"""
    node_ids: set[str]
    adj: dict[str, list[str]]  # 无向邻接表
    n: int  # 节点数
    cached_at: int  # 缓存时间


_cached: GraphStructure | None = None
CACHE_TTL = 30_000  # 30 秒缓存


def load_graph(db: Connection) -> GraphStructure:
    """
    读取图结构（带缓存）

    compact 会新增节点/边，但 30 秒内的查询共享同一份图结构没问题

    Args:
        db: SQLite 数据库连接

    Returns:
        包含节点 ID 集合、邻接表和节点数的图结构
    """
    global _cached

    if _cached and (time.time() * 1000 - _cached['cached_at']) < CACHE_TTL:
        return _cached

    cursor = db.cursor()

    # 读取活跃节点
    cursor.execute("SELECT id FROM gm_nodes")
    node_rows = cursor.fetchall()
    node_ids = {row[0] for row in node_rows}

    # 读取边
    cursor.execute("SELECT from_id, to_id FROM gm_edges")
    edge_rows = cursor.fetchall()

    # 构建无向邻接表
    adj: dict[str, list[str]] = {node_id: [] for node_id in node_ids}

    for from_id, to_id in edge_rows:
        if from_id not in node_ids or to_id not in node_ids:
            continue
        adj[from_id].append(to_id)
        adj[to_id].append(from_id)

    return GraphStructure(node_ids=node_ids, adj=adj, n=len(node_ids), cached_at=int(time.time() * 1000))


def invalidate_graph_cache() -> None:
    """
    图结构变化时清除缓存（compact/finalize 后调用）
    """
    global _cached
    _cached = None


# ─── 个性化 PageRank ─────────────────────────────────────────

class PPRResult(TypedDict):
    """个性化 PageRank 结果"""
    scores: dict[str, float]  # nodeId → 个性化分数


def personalized_page_rank(
    db: Connection,
    seed_ids: list[str],
    candidate_ids: list[str],
    cfg: GmConfig,
) -> PPRResult:
    """
    个性化 PageRank

    从 seed_ids 出发传播权重：
      - teleport 概率 (1-damping) 总是回到种子节点（不是均匀回到所有节点）
      - 这样种子附近的节点天然获得更高分数

    Args:
        db: SQLite 数据库连接
        seed_ids: 用户查询命中的种子节点（FTS5/向量搜索结果）
        candidate_ids: 需要排序的候选节点（图遍历结果）
        cfg: Graph Memory 配置

    Returns:
        候选节点的个性化分数字典
    """
    graph = load_graph(db)
    node_ids = graph['node_ids']
    adj = graph['adj']
    n = graph['n']

    damping = getattr(cfg, 'pagerank_damping', 0.85)
    iterations = getattr(cfg, 'pagerank_iterations', 20)

    if n == 0 or not seed_ids:
        return {'scores': {}}

    # 过滤掉不存在的种子节点
    valid_seeds = [sid for sid in seed_ids if sid in node_ids]
    if not valid_seeds:
        return {'scores': {}}

    # teleport 向量：只指向种子节点，均匀分配
    teleport_weight = 1.0 / len(valid_seeds)
    seed_set = set(valid_seeds)

    # 初始分数：集中在种子节点上
    rank: dict[str, float] = {}
    for node_id in node_ids:
        rank[node_id] = teleport_weight if node_id in seed_set else 0.0

    # 迭代
    for _ in range(iterations):
        new_rank: dict[str, float] = {}

        # teleport 分量：回到种子节点
        for node_id in node_ids:
            new_rank[node_id] = (1 - damping) * teleport_weight if node_id in seed_set else 0.0

        # 传播分量：从邻居获得权重
        for node_id, neighbors in adj.items():
            if not neighbors:
                continue

            current_rank = rank.get(node_id, 0.0)
            if current_rank == 0.0:
                continue

            contrib = current_rank / len(neighbors)

            for neighbor in neighbors:
                new_rank[neighbor] = new_rank.get(neighbor, 0.0) + damping * contrib

        # dangling nodes 的分数传播回种子节点（不是均匀分配到所有节点）
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

    # 只返回候选节点的分数
    result_scores = {cid: rank.get(cid, 0.0) for cid in candidate_ids}

    return {'scores': result_scores}


# ─── 全局 PageRank（基线，session_end 时更新） ──────────────

class GlobalPageRankResult(TypedDict):
    """全局 PageRank 结果"""
    scores: dict[str, float]
    top_k: list[dict[str, object]]  # [{id, name, score}, ...]


def compute_global_page_rank(db: Connection, cfg: GmConfig) -> GlobalPageRankResult:
    """
    全局 PageRank — 写入 gm_nodes.pagerank 作为基线

    用途：
      - topNodes 兜底排序（没有查询种子时的 fallback）
      - gm_stats 展示全局重要节点

    只在 session_end / gm_maintain 时调用

    Args:
        db: SQLite 数据库连接
        cfg: Graph Memory 配置

    Returns:
        包含所有节点分数和 top20 节点的结果
    """
    graph = load_graph(db)
    node_ids = graph['node_ids']
    adj = graph['adj']
    n = graph['n']

    damping = getattr(cfg, 'pagerank_damping', 0.85)
    iterations = getattr(cfg, 'pagerank_iterations', 20)

    if n == 0:
        return {'scores': {}, 'top_k': []}

    # 获取节点名称映射
    cursor = db.cursor()
    cursor.execute("SELECT id, name FROM gm_nodes")
    name_rows = cursor.fetchall()
    name_map = {row[0]: row[1] for row in name_rows}

    # 全局：均匀 teleport
    init_score = 1.0 / n
    rank: dict[str, float] = {node_id: init_score for node_id in node_ids}

    # 迭代
    for _ in range(iterations):
        new_rank: dict[str, float] = {}
        base = (1 - damping) / n

        # 初始化基础分数
        for node_id in node_ids:
            new_rank[node_id] = base

        # 传播分量
        for node_id, neighbors in adj.items():
            if not neighbors:
                continue

            current_rank = rank.get(node_id, 0.0)
            if current_rank == 0.0:
                continue

            contrib = current_rank / len(neighbors)

            for neighbor in neighbors:
                new_rank[neighbor] = new_rank.get(neighbor, base) + damping * contrib

        # dangling nodes 处理
        dangling_sum = sum(rank.get(node_id, 0.0) for node_id in node_ids if not adj.get(node_id))

        if dangling_sum > 0:
            dc = damping * dangling_sum / n
            for node_id in node_ids:
                new_rank[node_id] = new_rank.get(node_id, 0.0) + dc

        rank = new_rank

    # 写入数据库
    update_pageranks(db, rank)

    # 排序取 top 20
    sorted_scores = sorted(rank.items(), key=lambda x: x[1], reverse=True)[:20]
    top_k = [
        {'id': node_id, 'name': name_map.get(node_id, node_id), 'score': score}
        for node_id, score in sorted_scores
    ]

    return {'scores': rank, 'top_k': top_k}
