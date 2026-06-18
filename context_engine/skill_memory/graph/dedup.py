"""
skill_memory — 向量余弦去重

向量余弦去重 — 发现并合并语义重复的节点

原理：两个节点的 embedding 余弦相似度 > threshold → 视为重复

例子：
  - "conda-env-create" 和 "conda-create-environment" → 同一个技能
  - "importerror-libgl1" 和 "libgl-missing-error" → 同一个事件

合并策略：
  - 保留 validated_count 更高的节点
  - 合并 source_sessions
  - 迁移边（from/to 都改指向保留节点）
  - 被合并节点标记 deprecated

复杂度：O(n²) 比较，n = 有向量的节点数。几千节点 < 50ms。
"""

import math
from ..type import GmConfig
from typing import TypedDict
from sqlite3 import Connection
from ..store.core import find_by_id, get_all_vectors, merge_nodes


class DuplicatePair(TypedDict):
    """重复节点对"""
    node_a: str
    node_b: str
    name_a: str
    name_b: str
    similarity: float


class DedupResult(TypedDict):
    """去重结果"""
    pairs: list[DuplicatePair]
    merged: int


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    计算两个向量的余弦相似度

    Args:
        a: 向量 a
        b: 向量 b

    Returns:
        余弦相似度值 (0-1 之间)
    """
    min_len = min(len(a), len(b))
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for i in range(min_len):
        dot += a[i] * b[i]
        norm_a += a[i] * a[i]
        norm_b += b[i] * b[i]

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b) + 1e-9)


def detect_duplicates(db: Connection, cfg: GmConfig) -> list[DuplicatePair]:
    """
    检测重复节点对

    需要 embedding 才能工作，没有向量的节点会被跳过。
    FTS5 名称完全匹配由 store.upsert_node 已处理，这里处理语义重复。

    Args:
        db: SQLite 数据库连接
        cfg: Graph Memory 配置

    Returns:
        按相似度降序排列的重复对列表
    """
    vectors_data = get_all_vectors(db)

    if len(vectors_data) < 2:
        return []

    threshold = getattr(cfg, 'dedup_threshold', 0.90)
    pairs: list[DuplicatePair] = []

    for i in range(len(vectors_data)):
        for j in range(i + 1, len(vectors_data)):
            vec_i = vectors_data[i]['embedding']
            vec_j = vectors_data[j]['embedding']

            sim = cosine_similarity(vec_i, vec_j)

            if sim >= threshold:
                node_a = find_by_id(db, vectors_data[i]['node_id'])
                node_b = find_by_id(db, vectors_data[j]['node_id'])

                if node_a and node_b:
                    pairs.append({
                        'node_a': node_a.id,
                        'node_b': node_b.id,
                        'name_a': node_a.name,
                        'name_b': node_b.name,
                        'similarity': sim,
                    })

    # 按相似度降序排序
    return sorted(pairs, key=lambda x: x['similarity'], reverse=True)


def dedup(db: Connection, cfg: GmConfig) -> DedupResult:
    """
    检测并自动合并重复节点

    合并规则：
      - 同类型才合并（SKILL+SKILL，EVENT+EVENT）
      - 保留 validated_count 更高的
      - validated_count 相同时保留更新时间更近的

    Args:
        db: SQLite 数据库连接
        cfg: Graph Memory 配置

    Returns:
        包含重复对和合并数量的结果字典
    """
    pairs = detect_duplicates(db, cfg)
    merged = 0

    # 已经被合并过的节点不再参与合并
    consumed = set()

    for pair in pairs:
        if pair['node_a'] in consumed or pair['node_b'] in consumed:
            continue

        node_a = find_by_id(db, pair['node_a'])
        node_b = find_by_id(db, pair['node_b'])

        if not node_a or not node_b:
            continue

        # 只合并同类型节点
        if node_a.type != node_b.type:
            continue

        # 决定保留哪个节点
        keep_id: str
        merge_id: str

        if node_a.validated_count > node_b.validated_count:
            keep_id = node_a.id
            merge_id = node_b.id
        elif node_b.validated_count > node_a.validated_count:
            keep_id = node_b.id
            merge_id = node_a.id
        else:
            # validated_count 相同则保留更新更近的
            if node_a.updated_at >= node_b.updated_at:
                keep_id = node_a.id
                merge_id = node_b.id
            else:
                keep_id = node_b.id
                merge_id = node_a.id

        merge_nodes(db, keep_id, merge_id)
        consumed.add(merge_id)
        merged += 1

    return {
        'pairs': pairs,
        'merged': merged,
    }
