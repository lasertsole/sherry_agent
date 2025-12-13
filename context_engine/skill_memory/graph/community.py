"""
skill_memory - Community Detection

用途：
  - 发现知识域（Docker 相关技能自动聚成一组）
  - recall 时可以拉整个社区的节点
  - assemble 时同社区节点放一起，上下文更连贯
  - kg_stats 展示社区分布
"""

import re
import sqlite3
import leidenalg
import igraph as ig
from sqlite3 import Connection
from langchain_core.messages import AIMessage
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from typing import Callable, Awaitable, Optional, TypedDict, List, Any
from ..store.core import update_communities, upsert_community_summary, prune_community_summaries


class CommunityResult(TypedDict):
    """社区检测结果"""
    labels: dict[str, str]
    communities: dict[str, list[str]]
    count: int

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

    g = ig.Graph(len(node_ids), edges, directed=False)

    partition = leidenalg.find_partition(
        g,
        leidenalg.ModularityVertexPartition,
        n_iterations=2
    )

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
    获取同社区的节点 ID 列表

    recall 时用：找到种子节点 → 拉同社区的其他节点作为补充

    Args:
        db: SQLite 数据库连接
        node_id: 种子节点 ID
        limit: 返回的最大节点数

    Returns:
        同社区的节点 ID 列表
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


# ─── 社区描述生成 ────────────────────────────────────────────

# 类型定义
CompleteFn = Callable[[str, str], Awaitable[str]]
EmbedFn = Callable[[str], Awaitable[list[float]]]

COMMUNITY_SUMMARY_SYS = """你是知识图谱摘要引擎。根据节点列表，用简短的描述概括这组节点的主题领域。
要求：
- 只返回短语本身，不要解释
- 描述涵盖的工具/技术/任务领域
- 不要使用"社区"这个词\n"""


async def summarize_communities(
    db: Connection,
    communities: dict[str, list[str]],
    llm: BaseChatModel,
    embed: Embeddings = None,
) -> int:
    """
    为所有社区生成 LLM 摘要描述 + embedding 向量

    调用时机：runMaintenance → detectCommunities 之后

    Args:
        db: SQLite 数据库连接
        communities: 社区 ID 到成员节点 ID 列表的映射
        llm: LLM 补全函数
        embed: Embedding

    Returns:
        生成的摘要数量
    """
    prune_community_summaries(db)
    generated: int = 0

    cursor: sqlite3.Cursor = db.cursor()

    for community_id, member_ids in communities.items():
        if not member_ids:
            continue

        placeholders: str = ",".join("?" * len(member_ids))

        cursor.execute(f"""
            SELECT name, type, description FROM gm_nodes
            WHERE id IN ({placeholders})
            ORDER BY validated_count DESC
            LIMIT 10
        """, (*member_ids,))

        members: List[Any] = [dict(c) for c in cursor.fetchall()]

        if not members:
            continue

        member_text:str = "\n".join(
            f"{member['type']}:{member['name']} — {member['description']}"
            for member in members
        )

        try:
            # LLM 生成描述
            summary: AIMessage = await llm.ainvoke(
                COMMUNITY_SUMMARY_SYS+f"社区成员：\n{member_text}",
            )

            # 清理输出
            cleaned: str = summary.content.strip()
            cleaned: str = re.sub(r'<think>[\s\S]*?</think>', '', cleaned, flags=re.IGNORECASE)
            cleaned: str = re.sub(r'<think>[\s\S]*', '', cleaned, flags=re.IGNORECASE)
            cleaned: str = re.sub(r'^["\'「"]|["\'「""]$', '', cleaned)
            cleaned: str = cleaned.replace('\n', ' ')
            cleaned: str = re.sub(r'\s{2,}', ' ', cleaned)
            cleaned: str = cleaned.strip()[:100]

            if not cleaned:
                continue

            # 生成社区 embedding（用描述 + 成员名拼接）
            embedding: Optional[list[float]] = None
            try:
                embed_text = f"{cleaned}\n{', '.join([m['description'] for m in members])}"
                embedding: List[float] = await embed.aembed_query(embed_text)
            except Exception:
                print(f"  [DEBUG] community embedding failed for {community_id}")

            upsert_community_summary(db, community_id, cleaned, len(member_ids), embedding)
            generated += 1

        except Exception as err:
            print(f"  [WARN] community summary failed for {community_id}: {err}")

    return generated
