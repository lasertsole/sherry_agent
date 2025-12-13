import re
import json
import time
import random
import string
import sqlite3
from ..type import GmNode, GmEdge
from pub_func import contains_cjk
from typing import Any, Optional, List, TypedDict, Set

# ─── 工具 ─────────────────────────────────────────────────────
def get_timestamp() -> int:
    return int(time.time() * 1000)

def uid(p: str) -> str:
    timestamp = get_timestamp()
    random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))

    return f"{p}-{timestamp}-{random_str}"

def to_node(r: dict[str, Any])->GmNode:
    return GmNode(
        id = r["id"],
        type = r["type"],
        name = r["name"],
        description = r["description"] if r["description"] is not None else "",
        content = r["content"],
        validated_count = r["validated_count"],
        source_sessions = json.loads(r["source_sessions"] if r["source_sessions"] is not None else "[]"),
        community_id = r["community_id"],
        pagerank = r["pagerank"] if r["pagerank"] is not None else 0,
        created_at = r["created_at"],
        updated_at = r["updated_at"],
    )

def to_edge(r: dict[str, Any])-> GmEdge:
    return GmEdge(
        id = r["id"],
        from_id = r["from_id"],
        to_id = r["to_id"],
        type = r["type"],
        instruction = r["instruction"],
        condition = r["condition"],
        session_id = r["session_id"],
        created_at = r["created_at"],
    )

# 标准化 name：全小写，空格转连字符，保留中文
def normalize_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r'[\s_]+', '-', name)
    name = re.sub(r'[^a-z0-9\u4e00-\u9fff\-]', '', name)
    name = re.sub(r'-{2,}', '-', name)

    return name.strip('-')

# ─── 节点 CRUD ───────────────────────────────────────────────
def find_by_name(db: sqlite3.Connection, name: str) -> Optional[GmNode]:
    cursor = db.cursor()

    r = cursor.execute(
    "SELECT * FROM gm_nodes WHERE name = ?",
    (normalize_name(name),)
    ).fetchone()

    return to_node(r) if r else None

def find_by_id(db: sqlite3.Connection, id: str) -> Optional[GmNode]:
    r = db.execute("SELECT * FROM gm_nodes WHERE id = ?", (id,)).fetchone()
    return to_node(r) if r else None

def all_active_nodes(db: sqlite3.Connection) -> List[GmNode]:
    rows = db.execute("SELECT * FROM gm_nodes").fetchall()
    return [to_node(row) for row in rows]

def all_edges(db: sqlite3.Connection) -> List[GmEdge]:
    rows = db.execute("SELECT * FROM gm_edges").fetchall()
    return [to_edge(row) for row in rows]

class UpsertResult(TypedDict):
    node: Optional[GmNode]
    isNew: bool


def upsert_node(db: sqlite3.Connection, c: dict, session_id: str) -> UpsertResult:
    name = normalize_name(c['name'])
    ex: GmNode = find_by_name(db, name)
    now = get_timestamp()

    if ex:
        old_sessions: List[str] = getattr(ex, 'source_sessions', [])
        sessions_set: Set[str] = set(old_sessions)
        sessions_set.add(session_id)
        sessions_json:str = json.dumps(list(sessions_set))

        content = c['content'] if len(c['content']) > len(ex.content) else ex.content
        desc = c['description'] if len(c['description']) > len(ex.description) else ex.description
        count = ex.validated_count + 1

        with db:
            db.execute("""
                UPDATE gm_nodes 
                SET content=?, description=?, validated_count=?, source_sessions=?, updated_at=? 
                WHERE id=?
            """, (content, desc, count, sessions_json, now, ex.id))

        ex = ex.model_copy(update = {"content": content, "description": desc, "validated_count": count})
        db.commit()

        return {"node": ex, "isNew": False}

    new_id = uid("n")
    sessions_json = json.dumps([session_id])

    with db:
        db.execute("""
            INSERT INTO gm_nodes 
            (id, type, name, description, content, validated_count, source_sessions, created_at, updated_at)
            VALUES (?,?,?,?,?, 1, ?, ?, ?)
        """, (new_id, c['type'], name, c['description'], c['content'], sessions_json, now, now))
        db.commit()
    return {"node": find_by_name(db, name), "isNew": True}


def delete_node(db: sqlite3.Connection, node_id: str) -> None:
    """将指定节点硬删除"""
    # 1. 先删除所有与该节点相关的边（from_id 或 to_id 等于该节点）
    db.execute("DELETE FROM gm_edges WHERE from_id=? OR to_id=?", (node_id, node_id))

    # 2. 后删除向量节点
    db.execute("DELETE FROM gm_vectors WHERE node_id=?", (node_id,))

    # 3. 再删除节点本身
    db.execute("DELETE FROM gm_nodes WHERE id=?", (node_id,))

    db.commit()

# 合并两个节点：keepId 保留，mergeId 删除，边迁移
def merge_nodes(db: sqlite3.Connection, keep_id: str, merge_id: str) -> None:
    """
    合并两个节点：保留 keep_id，将 merge_id 标记为已弃用，并迁移其边关系
    """
    # 获取两个节点的信息
    keep = find_by_id(db, keep_id)
    merge = find_by_id(db, merge_id)
    if not keep or not merge:
        return

    # 合并属性：取内容更长的作为新内容，累加验证次数，合并会话来源
    sessions = list(set(keep.source_sessions + merge.source_sessions))
    count = keep.validated_count + merge.validated_count
    content = keep.content if len(keep.content) >= len(merge.content) else merge.content
    desc = keep.description if len(keep.description) >= len(merge.description) else merge.description

    # 更新保留节点的信息
    db.execute(
        "UPDATE gm_nodes SET content=?, description=?, validated_count=?, "
        "source_sessions=?, updated_at=? WHERE id=?",
        (content, desc, count, json.dumps(sessions), get_timestamp(), keep_id)
    )

    # 迁移边关系：将指向 merge_id 的边重新指向 keep_id
    db.execute("UPDATE gm_edges SET from_id=? WHERE from_id=?", (keep_id, merge_id))
    db.execute("UPDATE gm_edges SET to_id=? WHERE to_id=?", (keep_id, merge_id))

    # 删除自环（防止出现 keep_id → keep_id 的无效边）
    db.execute("DELETE FROM gm_edges WHERE from_id = to_id")

    # 删除重复边（相同 from_id, to_id, type 的只保留一条）
    db.execute("""
        DELETE FROM gm_edges WHERE id NOT IN (
            SELECT MIN(id) FROM gm_edges GROUP BY from_id, to_id, type
        )
    """)

    db.commit()

    # 最后将被合并的节点删除
    delete_node(db, merge_id)

def update_pageranks(db: sqlite3.Connection, scores: dict) -> None:
    """批量更新 PageRank 分数"""
    cursor = db.cursor()
    try:
        db.execute("BEGIN")
        for node_id, score in scores.items():
            cursor.execute("UPDATE gm_nodes SET pagerank=? WHERE id=?", (score, node_id))
        db.commit()
    except Exception as e:
        db.rollback()
        raise e


def update_communities(db: sqlite3.Connection, labels: dict) -> None:
    """批量更新社区 ID"""
    cursor = db.cursor()
    try:
        db.execute("BEGIN TRANSACTION")
        # 使用 executemany 进行批量操作
        cursor.executemany(
            "UPDATE gm_nodes SET community_id=? WHERE id=?",
            [(cid, node_id) for node_id, cid in labels.items()]
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise e

# ─── 边 CRUD ─────────────────────────────────────────────────
def upsert_edge(
        db: sqlite3.Connection,
        edge_data: dict
) -> None:
    """插入或更新边关系：如果已存在则更新，否则创建新记录"""
    # 检查是否已存在相同的边（from_id, to_id, type）
    existing = db.execute(
        "SELECT id FROM gm_edges WHERE from_id=? AND to_id=? AND type=?",
        (edge_data['from_id'], edge_data['to_id'], edge_data['type'])
    ).fetchone()

    if existing:
        # 已存在则更新 instruction
        db.execute(
            "UPDATE gm_edges SET instruction=? WHERE id=?",
            (edge_data['instruction'], existing[0])
        )
    else:
        # 不存在则插入新记录
        db.execute(
            """INSERT INTO gm_edges 
            (id, from_id, to_id, type, instruction, condition, session_id, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                uid("e"),  # 生成唯一ID
                edge_data['from_id'],
                edge_data['to_id'],
                edge_data['type'] if isinstance(edge_data['type'], str) else edge_data['type'].value,
                edge_data['instruction'],
                edge_data.get('condition'),  # 使用get避免KeyError
                edge_data['session_id'],
                get_timestamp()  # 当前时间戳
            )
        )
    db.commit()


def edges_from(db: sqlite3.Connection, node_id: str) -> list:
    """获取从指定节点出发的所有边"""
    rows = db.execute("SELECT * FROM gm_edges WHERE from_id=?", (node_id,)).fetchall()
    return [to_edge(dict(row)) for row in rows]


def edges_to(db: sqlite3.Connection, node_id: str) -> list:
    """获取指向指定节点的所有边"""
    rows = db.execute("SELECT * FROM gm_edges WHERE to_id=?", (node_id,)).fetchall()
    return [to_edge(dict(row)) for row in rows]


# ─── FTS5 搜索 ───────────────────────────────────────────────
_fts5_available: bool | None = None

def fts5_available(db: sqlite3.Connection) -> bool:
    """检查数据库是否支持FTS5全文搜索"""

    global _fts5_available
    if _fts5_available is not None:
        return _fts5_available

    try:
        db.execute("SELECT * FROM gm_nodes_fts LIMIT 0").fetchall()
        db.execute("SELECT * FROM gm_nodes_fts_trigram LIMIT 0").fetchall()
        _fts5_available = True
    except Exception:
        _fts5_available = False

    return _fts5_available


def search_nodes(db: sqlite3.Connection, query: str, limit: int = 6) -> list:
    """搜索节点：优先使用FTS5全文搜索，降级为LIKE模糊匹配"""
    # 解析查询词
    terms = [term for term in query.strip().split() if term][:8]
    if not terms:
        return top_nodes(db, limit)

    # 优先尝试FTS5搜索
    if fts5_available(db):
        try:
            fts_query = " OR ".join(f'"{term.replace('"', "")}"' for term in terms)
            if contains_cjk(query):
                sql = """
                    SELECT n.*, rank FROM gm_nodes_fts_trigram ftst
                    JOIN gm_nodes n ON n.rowid = ftst.rowid
                    WHERE gm_nodes_fts_trigram MATCH ?
                    ORDER BY rank LIMIT ?
                """
            else:
                sql = """
                    SELECT n.*, rank FROM gm_nodes_fts fts
                    JOIN gm_nodes n ON n.rowid = fts.rowid
                    WHERE gm_nodes_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """

            rows = db.execute(sql, (fts_query, limit)).fetchall()
            if rows:
                return [to_node(dict(row)) for row in rows]
        except Exception:
            # FTS查询失败，降级到普通搜索
            pass

    # 降级方案：使用LIKE进行模糊匹配
    where_conditions = " OR ".join(["(name LIKE ? OR description LIKE ? OR content LIKE ?)" for _ in terms])
    like_values = [f"%{term}%" for term in terms for _ in range(3)]  # 每个term对应name/desc/content

    sql = f"""
        SELECT * FROM gm_nodes WHERE ({where_conditions})
        ORDER BY pagerank DESC, validated_count DESC, updated_at DESC LIMIT ?
    """
    rows = db.execute(sql, (*like_values, limit)).fetchall()
    return [to_node(dict(row)) for row in rows]


def top_nodes(db: sqlite3.Connection, limit: int = 6) -> list:
    """获取热门节点：按pagerank、验证次数和更新时间排序"""

    sql = """
        SELECT * FROM gm_nodes
        ORDER BY pagerank DESC, validated_count DESC, updated_at DESC LIMIT ?
    """
    rows = db.execute(sql, (limit,)).fetchall()
    return [to_node(dict(row)) for row in rows]


# ─── 递归 CTE 图遍历 ────────────────────────────────────────
def graph_walk(
        db: sqlite3.Connection,
        seed_ids: list[str],
        max_depth: int,
) -> dict[str, list]:
    """使用递归 CTE 进行图遍历，获取种子节点的邻居"""

    if not seed_ids:
        return {"nodes": [], "edges": []}

    placeholders = ",".join("?" * len(seed_ids))

    walk_sql = f"""
        WITH RECURSIVE walk(node_id, depth) AS (
            SELECT id, 0 FROM gm_nodes WHERE id IN ({placeholders})
            UNION
            SELECT
                CASE WHEN e.from_id = w.node_id THEN e.to_id ELSE e.from_id END,
                w.depth + 1
            FROM walk w
            JOIN gm_edges e ON (e.from_id = w.node_id OR e.to_id = w.node_id)
            WHERE w.depth < ?
        )
        SELECT DISTINCT node_id FROM walk
    """

    walk_rows = db.execute(walk_sql, (*seed_ids, max_depth)).fetchall()
    node_ids = [dict(row)['node_id'] for row in walk_rows]

    if not node_ids:
        return {"nodes": [], "edges": []}

    np = ",".join("?" * len(node_ids))

    nodes_sql = f"""
        SELECT * FROM gm_nodes WHERE id IN ({np})
    """
    nodes = [to_node(dict(row)) for row in db.execute(nodes_sql, (*node_ids,)).fetchall()]

    edges_sql = f"""
        SELECT * FROM gm_edges WHERE from_id IN ({np}) AND to_id IN ({np})
    """
    edges = [to_edge(dict(row)) for row in db.execute(edges_sql, (*node_ids, *node_ids)).fetchall()]

    return {"nodes": nodes, "edges": edges}

# ─── 按 sessions 查询 ────────────────────────────────────────

def get_by_session(db: sqlite3.Connection, session_id: str) -> list[GmNode]:
    """根据 sessions ID 获取相关节点"""
    sql = """
        SELECT DISTINCT n.* FROM gm_nodes n, json_each(n.source_sessions) j
        WHERE j.value = ?
    """
    rows = db.execute(sql, (session_id,)).fetchall()
    return [to_node(dict(row)) for row in rows]


# ─── 消息 CRUD ───────────────────────────────────────────────
def save_message(
        db: sqlite3.Connection,
        session_id: str,
        turn: int,
        role: str,
        content: Any
) -> None:
    """保存消息到数据库"""
    import json
    import time

    db.execute("""
        INSERT OR IGNORE INTO gm_messages (id, session_id, turn_index, role, content, created_at)
        VALUES (?,?,?,?,?,?)
    """, (uid("m"), session_id, turn, role, json.dumps(content, ensure_ascii=False), get_timestamp()))
    db.commit()


def get_unextracted(db: sqlite3.Connection, session_id: str, limit: int) -> list:
    """获取未提取的消息"""

    sql = """
        SELECT * FROM gm_messages 
        WHERE session_id=?
        ORDER BY turn_index 
        LIMIT ?
    """
    rows = db.execute(sql, (session_id, limit)).fetchall()
    return [dict(row) for row in rows]


def delete_extracted(db: sqlite3.Connection, session_id: str, up_to_turn: int) -> None:
    """删除已提取的消息记录"""
    db.execute("""
        DELETE FROM gm_messages 
        WHERE session_id=? AND turn_index<=?
    """, (session_id, up_to_turn))
    db.commit()

def get_episodic_messages(
        db: sqlite3.Connection,
        session_ids: list[str],
        near_time: int,
        max_chars: int = 1500,
) -> list[dict]:
    """
    溯源选拉：按 sessions 拉取 human/ai 核心对话（跳过 tool/tool_result）
    用于 assemble 时补充三元组的原始上下文

    Args:
        session_ids: sessions ID 列表
        near_time: 优先取时间最接近的消息（节点的 updated_at）
        max_chars: 总字符上限

    Returns:
        包含 session_id, turnIndex, role, text, createdAt 的字典列表
    """
    if not session_ids:
        return []

    results = []
    used_chars = 0

    for session_id in session_ids:
        if used_chars >= max_chars:
            break

        sql = """
            SELECT turn_index, role, content, created_at FROM gm_messages
            WHERE session_id = ? AND role IN ('human', 'ai')
            ORDER BY ABS(created_at - ?) ASC
            LIMIT 6
        """
        rows = db.execute(sql, (session_id, near_time)).fetchall()

        for r in rows:
            if used_chars >= max_chars:
                break

            row_dict = dict(r)
            
            try:
                parsed = json.loads(row_dict['content'])
                if isinstance(parsed, str):
                    text = parsed
                elif isinstance(parsed, dict) and isinstance(parsed.get('content'), str):
                    text = parsed['content']
                elif isinstance(parsed, list):
                    text_parts = [
                        item.get('text', '')
                        for item in parsed
                        if isinstance(item, dict) and item.get('type') == 'text'
                    ]
                    text = "\n".join(text_parts)
                else:
                    text = str(parsed)[:300]
            except (json.JSONDecodeError, Exception):
                text = str(row_dict['content'])[:300]

            if not text.strip():
                continue

            truncated = text[:max_chars - used_chars]
            results.append({
                'session_id': session_id,
                'turn_index': row_dict['turn_index'],
                'role': row_dict['role'],
                'text': truncated,
                'created_at': row_dict['created_at'],
            })
            used_chars += len(truncated)

    return results


# ─── 信号 CRUD ───────────────────────────────────────────────
def save_signal(db: sqlite3.Connection, session_id: str, signal_data: dict) -> None:
    """保存信号到数据库"""

    db.execute("""
        INSERT INTO gm_signals (id, session_id, turn_index, type, data, created_at)
        VALUES (?,?,?,?,?,?)
    """, (uid("s"), session_id, signal_data['turnIndex'], signal_data['type'],
          json.dumps(signal_data['data']), get_timestamp()))
    db.commit()

def pending_signals(db: sqlite3.Connection, session_id: str) -> list:
    """获取未处理的信号"""
    sql = """
        SELECT * FROM gm_signals 
        WHERE session_id=? AND processed=0 
        ORDER BY turn_index
    """
    rows = db.execute(sql, (session_id,)).fetchall()

    return [
        {
            'type': row['type'],
            'turn_index': row['turn_index'],
            'data': json.loads(row['data'])
        }
        for row in rows
    ]


def mark_signals_done(db: sqlite3.Connection, session_id: str) -> None:
    """标记信号处理完成"""
    db.execute("""
        UPDATE gm_signals 
        SET processed=1 
        WHERE session_id=?
    """, (session_id,))
    db.commit()

# ─── 统计 ────────────────────────────────────────────────────
def get_stats(db: sqlite3.Connection) -> dict:
    """获取图谱统计信息"""
    total_nodes = db.execute(
        "SELECT COUNT(*) as c FROM gm_nodes"
    ).fetchone()['c']

    by_type = {}
    type_rows = db.execute("""
        SELECT type, COUNT(*) as c 
        FROM gm_nodes 
        GROUP BY type
    """).fetchall()
    for row in type_rows:
        by_type[row['type']] = row['c']

    total_edges = db.execute("SELECT COUNT(*) as c FROM gm_edges").fetchone()['c']

    by_edge_type = {}
    edge_type_rows = db.execute("""
        SELECT type, COUNT(*) as c 
        FROM gm_edges 
        GROUP BY type
    """).fetchall()
    for row in edge_type_rows:
        by_edge_type[row['type']] = row['c']

    communities = db.execute("""
        SELECT COUNT(DISTINCT community_id) as c 
        FROM gm_nodes 
        WHERE community_id IS NOT NULL
    """).fetchone()['c']

    return {
        'total_nodes': total_nodes,
        'by_type': by_type,
        'total_edges': total_edges,
        'by_edge_type': by_edge_type,
        'communities': communities
    }


# ─── 向量存储 + 搜索 ────────────────────────────────────────

def save_vector(
        db: sqlite3.Connection,
        node_id: str,
        content: str,
        vec: list[float]
) -> None:
    """保存向量到数据库"""
    import hashlib

    content_hash = hashlib.md5(content.encode()).hexdigest()

    db.execute("""
        INSERT INTO gm_vectors (node_id, content_hash, embedding) VALUES (?,?,?)
        ON CONFLICT(node_id) DO UPDATE SET content_hash=excluded.content_hash, embedding=excluded.embedding
    """, (node_id, content_hash, json.dumps(vec)))
    db.commit()

def get_vector_hash(db: sqlite3.Connection, node_id: str) -> Optional[str]:
    """获取向量的内容哈希"""

    row = db.execute(
        "SELECT content_hash FROM gm_vectors WHERE node_id=?",
        (node_id,)
    ).fetchone()
    return row['content_hash'] if row else None


def get_all_vectors(db: sqlite3.Connection) -> list[dict]:
    """获取所有向量（供去重/聚类用）"""
    rows = db.execute("""
        SELECT v.node_id, v.embedding FROM gm_vectors v
        JOIN gm_nodes n ON n.id = v.node_id
    """).fetchall()

    return [
        {
            'node_id': row['node_id'],
            'embedding': json.loads(row['embedding'])
        }
        for row in rows
    ]


class ScoredNode(TypedDict):
    """带分数的节点"""
    node: GmNode
    score: float


def vector_search_with_score(
        db: sqlite3.Connection,
        query_vec: list[float],
        limit: int,
        min_score: float = 0.35
) -> list[ScoredNode]:
    """向量搜索并返回带余弦相似度的节点"""
    import math

    rows = db.execute("""
        SELECT v.node_id, v.embedding, n.*
        FROM gm_vectors v JOIN gm_nodes n ON n.id = v.node_id
    """).fetchall()

    if not rows:
        return []

    q_norm = math.sqrt(sum(x * x for x in query_vec))
    if q_norm == 0:
        return []

    results = []
    for row in rows:
        v = json.loads(row['embedding'])
        min_len = min(len(v), len(query_vec))

        dot = sum(v[i] * query_vec[i] for i in range(min_len))
        v_norm = math.sqrt(sum(v[i] * v[i] for i in range(min_len)))

        score = dot / (v_norm * q_norm + 1e-9)
        if score > min_score:
            results.append({
                'node': to_node(dict(row)),
                'score': score
            })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:limit]


def vector_search(
        db: sqlite3.Connection,
        query_vec: list[float],
        limit: int,
        min_score: float = 0.5
) -> list[GmNode]:
    """向量搜索（兼容旧接口）"""
    scored = vector_search_with_score(db, query_vec, limit, min_score)
    return [item['node'] for item in scored]


def community_representatives(
        db: sqlite3.Connection,
        per_community: int = 2
) -> list[GmNode]:
    """
    社区代表节点：每个社区取最近更新的 topN 个节点
    用于泛化召回 —— 用户问"做了哪些工作"时按领域返回概览
    """
    rows = db.execute("""
        SELECT * FROM gm_nodes
        WHERE community_id IS NOT NULL
        ORDER BY community_id, updated_at DESC
    """).fetchall()

    by_community: dict[str, list[GmNode]] = {}
    for row in rows:
        node = to_node(dict(row))
        cid = row['community_id']
        if cid not in by_community:
            by_community[cid] = []
        if len(by_community[cid]) < per_community:
            by_community[cid].append(node)

    # 社区按最新更新时间排序
    sorted_communities = sorted(
        by_community.items(),
        key=lambda x: max(n.updated_at for n in x[1]),
        reverse=True
    )

    result: list[GmNode] = []
    for _, nodes in sorted_communities:
        result.extend(nodes)

    return result


# ─── 社区描述 CRUD ──────────────────────────────────────────

class CommunitySummary(TypedDict):
    """社区摘要"""
    id: str
    summary: str
    node_count: int
    created_at: int
    updated_at: int


def upsert_community_summary(
        db: sqlite3.Connection,
        summary_id: str,
        summary_text: str,
        node_count: int,
        embedding: list[float]
) -> None:
    """插入或更新社区摘要"""
    now = get_timestamp()

    existing = db.execute(
        "SELECT id FROM gm_communities WHERE id=?",
        (summary_id,)
    ).fetchone()

    if existing:
        if embedding:
            db.execute("""
                UPDATE gm_communities 
                SET summary=?, node_count=?, embedding=?, updated_at=? 
                WHERE id=?
            """, (summary_text, node_count, json.dumps(embedding), now, summary_id))
        else:
            db.execute("""
                UPDATE gm_communities 
                SET summary=?, node_count=?, updated_at=? 
                WHERE id=?
            """, (summary_text, node_count, now, summary_id))
    else:
        db.execute("""
            INSERT INTO gm_communities 
            (id, summary, node_count, embedding, created_at, updated_at) 
            VALUES (?,?,?,?,?,?)
        """, (summary_id, summary_text, node_count,
              json.dumps(embedding) if embedding else None, now, now))

    db.commit()

def get_community_summary(
        db: sqlite3.Connection,
        summary_id: str
) -> Optional[CommunitySummary]:
    """获取社区摘要"""
    row = db.execute(
        "SELECT * FROM gm_communities WHERE id=?",
        (summary_id,)
    ).fetchone()

    if not row:
        return None

    return CommunitySummary(
        id = row['id'],
        summary = row['summary'],
        node_count = row['node_count'],
        created_at = row['created_at'],
        updated_at = row['updated_at']
    )

def get_all_community_summaries(
        db: sqlite3.Connection
) -> list[CommunitySummary]:
    """获取所有社区摘要"""

    rows = db.execute("""
        SELECT * FROM gm_communities 
        ORDER BY node_count DESC
    """).fetchall()

    return [
        {
            'id': row['id'],
            'summary': row['summary'],
            'node_count': row['node_count'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at']
        }
        for row in rows
    ]


class ScoredCommunity(TypedDict):
    """带分数的社区"""
    id: str
    summary: str
    score: float
    node_count: int


def community_vector_search(
        db: sqlite3.Connection,
        query_vec: list[float],
        min_score: float = 0.35
) -> list[ScoredCommunity]:
    """
    社区向量搜索：用 query 向量匹配社区 embedding，返回按相似度排序的社区
    """
    import math

    rows = db.execute("""
        SELECT id, summary, node_count, embedding 
        FROM gm_communities 
        WHERE embedding IS NOT NULL
    """).fetchall()

    if not rows:
        return []

    q_norm = math.sqrt(sum(x * x for x in query_vec))
    if q_norm == 0:
        return []

    results = []
    for row in rows:
        v = json.loads(row['embedding'])
        min_len = min(len(v), len(query_vec))

        dot = sum(v[i] * query_vec[i] for i in range(min_len))
        v_norm = math.sqrt(sum(v[i] * v[i] for i in range(min_len)))

        score = dot / (v_norm * q_norm + 1e-9)

        if score > min_score:
            results.append({
                'id': row['id'],
                'summary': row['summary'],
                'score': score,
                'node_count': row['node_count']
            })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results


def nodes_by_community_ids(
        db: sqlite3.Connection,
        community_ids: list[str],
        per_community: int = 3
) -> list[GmNode]:
    """
    按社区 ID 列表获取成员节点（按时间倒序）
    """
    if not community_ids:
        return []

    placeholders = ",".join("?" * len(community_ids))

    rows = db.execute(f"""
        SELECT * FROM gm_nodes
        WHERE community_id IN ({placeholders})
        ORDER BY community_id, updated_at DESC
    """, (*community_ids,)).fetchall()

    by_community: dict[str, list[GmNode]] = {}
    for row in rows:
        node = to_node(dict(row))
        cid = row['community_id']
        if cid not in by_community:
            by_community[cid] = []
        if len(by_community[cid]) < per_community:
            by_community[cid].append(node)

    result: list[GmNode] = []
    for cid in community_ids:
        members = by_community.get(cid)
        if members:
            result.extend(members)

    return result


def prune_community_summaries(db: sqlite3.Connection) -> int:
    """清除已不存在的社区描述"""
    cursor = db.cursor()
    cursor.execute("""
        DELETE FROM gm_communities WHERE id NOT IN (
            SELECT DISTINCT community_id FROM gm_nodes 
            WHERE community_id IS NOT NULL
        )
    """)
    db.commit()
    return cursor.rowcount