import re
import json
import time
import random
import string
import sqlite3
from loguru import logger
from ..type import GmNode, GmEdge
from pub_func import contains_cjk
from typing import Any, TypedDict

# ─── Utilities ─────────────────────────────────────────────────
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

# Normalize name: lowercase, spaces to hyphens, preserve CJK
def normalize_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r'[\s_]+', '-', name)
    name = re.sub(r'[^a-z0-9\u4e00-\u9fff\-]', '', name)
    name = re.sub(r'-{2,}', '-', name)

    return name.strip('-')

# ─── Node CRUD ────────────────────────────────────────────────
def find_by_name(db: sqlite3.Connection, name: str) -> GmNode | None:
    cursor = db.cursor()

    r = cursor.execute(
    "SELECT * FROM gm_nodes WHERE name = ?",
    (normalize_name(name),)
    ).fetchone()

    return to_node(r) if r else None

def find_by_id(db: sqlite3.Connection, id: str) -> GmNode | None:
    r = db.execute("SELECT * FROM gm_nodes WHERE id = ?", (id,)).fetchone()
    return to_node(r) if r else None

def all_active_nodes(db: sqlite3.Connection) -> list[GmNode]:
    rows = db.execute("SELECT * FROM gm_nodes").fetchall()
    return [to_node(row) for row in rows]

def all_edges(db: sqlite3.Connection) -> list[GmEdge]:
    rows = db.execute("SELECT * FROM gm_edges").fetchall()
    return [to_edge(row) for row in rows]

class UpsertResult(TypedDict):
    node: GmNode | None
    isNew: bool


def upsert_node(db: sqlite3.Connection, c: dict, session_id: str) -> UpsertResult:
    name = normalize_name(c['name'])
    ex: GmNode = find_by_name(db, name)
    now = get_timestamp()

    if ex:
        old_sessions: list[str] = getattr(ex, 'source_sessions', [])
        sessions_set: set[str] = set(old_sessions)
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
    """Hard-delete the specified node"""
    # 1. Delete all edges associated with this node (from_id or to_id)
    db.execute("DELETE FROM gm_edges WHERE from_id=? OR to_id=?", (node_id, node_id))

    # 2. Delete vector
    db.execute("DELETE FROM gm_vectors WHERE node_id=?", (node_id,))

    # 3. Delete the node itself
    db.execute("DELETE FROM gm_nodes WHERE id=?", (node_id,))

    db.commit()

# Merge two nodes: keep keepId, delete mergeId, migrate edges
def merge_nodes(db: sqlite3.Connection, keep_id: str, merge_id: str) -> None:
    """
    Merge two nodes: keep keep_id, mark merge_id as deprecated, and migrate its edges
    """
    # Fetch info for both nodes
    keep = find_by_id(db, keep_id)
    merge = find_by_id(db, merge_id)
    if not keep or not merge:
        return

    # Merge attributes: keep the longer content, accumulate validation count, merge session sources
    sessions = list(set(keep.source_sessions + merge.source_sessions))
    count = keep.validated_count + merge.validated_count
    content = keep.content if len(keep.content) >= len(merge.content) else merge.content
    desc = keep.description if len(keep.description) >= len(merge.description) else merge.description

    # Update the retained node
    db.execute(
        "UPDATE gm_nodes SET content=?, description=?, validated_count=?, "
        "source_sessions=?, updated_at=? WHERE id=?",
        (content, desc, count, json.dumps(sessions), get_timestamp(), keep_id)
    )

    # Migrate edges: re-point edges targeting merge_id to keep_id
    db.execute("UPDATE gm_edges SET from_id=? WHERE from_id=?", (keep_id, merge_id))
    db.execute("UPDATE gm_edges SET to_id=? WHERE to_id=?", (keep_id, merge_id))

    # Delete self-loops (prevent invalid keep_id → keep_id edges)
    db.execute("DELETE FROM gm_edges WHERE from_id = to_id")

    # Delete duplicate edges (keep only one per from_id, to_id, type)
    db.execute("""
        DELETE FROM gm_edges WHERE id NOT IN (
            SELECT MIN(id) FROM gm_edges GROUP BY from_id, to_id, type
        )
    """)

    db.commit()

    # Finally delete the merged node
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
        logger.exception(f"Error updating PageRank scores: {e}")
        raise e


def update_communities(db: sqlite3.Connection, labels: dict) -> None:
    """Batch update community IDs"""
    cursor = db.cursor()
    try:
        db.execute("BEGIN TRANSACTION")
        # Use executemany for batch operations
        cursor.executemany(
            "UPDATE gm_nodes SET community_id=? WHERE id=?",
            [(cid, node_id) for node_id, cid in labels.items()]
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise e

# ─── Edge CRUD ────────────────────────────────────────────────
def upsert_edge(
        db: sqlite3.Connection,
        edge_data: dict
) -> None:
    """Insert or update an edge: update if exists, otherwise create a new record"""
    # Check if the same edge already exists (from_id, to_id, type)
    existing = db.execute(
        "SELECT id FROM gm_edges WHERE from_id=? AND to_id=? AND type=?",
        (edge_data['from_id'], edge_data['to_id'], edge_data['type'])
    ).fetchone()

    if existing:
        # Update instruction if edge already exists
        db.execute(
            "UPDATE gm_edges SET instruction=? WHERE id=?",
            (edge_data['instruction'], existing[0])
        )
    else:
        # Insert new record if it doesn't exist
        db.execute(
            """INSERT INTO gm_edges 
            (id, from_id, to_id, type, instruction, condition, session_id, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                uid("e"),  # Generate unique ID
                edge_data['from_id'],
                edge_data['to_id'],
                edge_data['type'] if isinstance(edge_data['type'], str) else edge_data['type'].value,
                edge_data['instruction'],
                edge_data.get('condition'),  # Use get to avoid KeyError
                edge_data['session_id'],
                get_timestamp()  # Current timestamp
            )
        )
    db.commit()


def edges_from(db: sqlite3.Connection, node_id: str) -> list:
    """Get all edges starting from the specified node"""
    rows = db.execute("SELECT * FROM gm_edges WHERE from_id=?", (node_id,)).fetchall()
    return [to_edge(dict(row)) for row in rows]


def edges_to(db: sqlite3.Connection, node_id: str) -> list:
    """Get all edges pointing to the specified node"""
    rows = db.execute("SELECT * FROM gm_edges WHERE to_id=?", (node_id,)).fetchall()
    return [to_edge(dict(row)) for row in rows]


# ─── FTS5 Search ──────────────────────────────────────────────
_fts5_available: bool | None = None

def fts5_available(db: sqlite3.Connection) -> bool:
    """Check whether the database supports FTS5 full-text search"""

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
    """Search nodes: prefer FTS5 full-text search, fall back to LIKE fuzzy matching"""
    # Parse query terms
    terms = [term for term in query.strip().split() if term][:8]
    if not terms:
        return top_nodes(db, limit)

    # Try FTS5 search first
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
            # FTS query failed, fall back to plain search
            pass

    # Fallback: use LIKE fuzzy matching
    where_conditions = " OR ".join(["(name LIKE ? OR description LIKE ? OR content LIKE ?)" for _ in terms])
    like_values = [f"%{term}%" for term in terms for _ in range(3)]  # Each term → name/desc/content

    sql = f"""
        SELECT * FROM gm_nodes WHERE ({where_conditions})
        ORDER BY pagerank DESC, validated_count DESC, updated_at DESC LIMIT ?
    """
    rows = db.execute(sql, (*like_values, limit)).fetchall()
    return [to_node(dict(row)) for row in rows]


def top_nodes(db: sqlite3.Connection, limit: int = 6) -> list:
    """Get top nodes: sorted by pagerank, validation count, and update time"""

    sql = """
        SELECT * FROM gm_nodes
        ORDER BY pagerank DESC, validated_count DESC, updated_at DESC LIMIT ?
    """
    rows = db.execute(sql, (limit,)).fetchall()
    return [to_node(dict(row)) for row in rows]


# ─── Recursive CTE Graph Traversal ───────────────────────────
def graph_walk(
        db: sqlite3.Connection,
        seed_ids: list[str],
        max_depth: int,
) -> dict[str, list]:
    """Use recursive CTE for graph traversal to get neighbors of seed nodes"""

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

# ─── Query by Sessions ────────────────────────────────────────

def get_by_session(db: sqlite3.Connection, session_id: str) -> list[GmNode]:
    """Get nodes related to the given session ID"""
    sql = """
        SELECT DISTINCT n.* FROM gm_nodes n, json_each(n.source_sessions) j
        WHERE j.value = ?
    """
    rows = db.execute(sql, (session_id,)).fetchall()
    return [to_node(dict(row)) for row in rows]


# ─── Signal CRUD ──────────────────────────────────────────────
def save_signal(db: sqlite3.Connection, session_id: str, signal_data: dict) -> None:
    """Save a signal to the database"""

    db.execute("""
        INSERT INTO gm_signals (id, session_id, turn_index, type, data, created_at)
        VALUES (?,?,?,?,?,?)
    """, (uid("s"), session_id, signal_data['turnIndex'], signal_data['type'],
          json.dumps(signal_data['data']), get_timestamp()))
    db.commit()

def pending_signals(db: sqlite3.Connection, session_id: str) -> list:
    """Get pending signals"""
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
    """Mark signals as processed"""
    db.execute("""
        UPDATE gm_signals 
        SET processed=1 
        WHERE session_id=?
    """, (session_id,))
    db.commit()

# ─── Statistics ───────────────────────────────────────────────
def get_stats(db: sqlite3.Connection) -> dict:
    """Get graph statistics"""
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


# ─── Vector Storage + Search ─────────────────────────────────

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

def get_vector_hash(db: sqlite3.Connection, node_id: str) -> str | None:
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
    embedding: list[float],
    node_ids: list[str] | None = None
) -> None:
    """插入或更新社区摘要

    node_count 从 node_ids 长度自动推算，调用方无需再传入。
    """
    now = get_timestamp()
    node_ids_json = json.dumps(sorted(node_ids)) if node_ids else '[]'
    node_count = len(node_ids) if node_ids else 0

    existing = db.execute(
        "SELECT id FROM gm_communities WHERE id=?",
        (summary_id,)
    ).fetchone()

    if existing:
        if embedding:
            db.execute("""
                UPDATE gm_communities 
                SET summary=?, node_count=?, node_ids=?, embedding=?, updated_at=? 
                WHERE id=?
            """, (summary_text, node_count, node_ids_json, json.dumps(embedding), now, summary_id))
        else:
            db.execute("""
                UPDATE gm_communities 
                SET summary=?, node_count=?, node_ids=?, updated_at=? 
                WHERE id=?
            """, (summary_text, node_count, node_ids_json, now, summary_id))
    else:
        db.execute("""
            INSERT INTO gm_communities 
            (id, summary, node_count, node_ids, embedding, created_at, updated_at) 
            VALUES (?,?,?,?,?,?,?)
        """, (summary_id, summary_text, node_count, node_ids_json,
              json.dumps(embedding) if embedding else None, now, now))

    db.commit()

def get_community_summary(
        db: sqlite3.Connection,
        summary_id: str
) -> CommunitySummary | None:
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