# Skill Memory - 智能知识图谱记忆系统

> **Skill Memory** 是 EMA AI Agent 的核心记忆引擎，能够从对话中自动提取、组织和检索结构化知识，构建动态演化的技能知识图谱。

---

## 📋 目录

- [系统概述](#系统概述)
- [核心架构](#核心架构)
- [工作流程](#工作流程)
- [关键组件](#关键组件)
- [数据模型](#数据模型)
- [召回机制](#召回机制)
- [图谱维护](#图谱维护)
- [使用示例](#使用示例)
- [性能优化](#性能优化)

---

## 系统概述

### 设计理念

Skill Memory 是一个**基于图的知识记忆系统**，旨在解决传统 RAG 系统的局限性：

| 传统 RAG | Skill Memory |
|---------|-------------|
| 扁平化向量检索 | 结构化图谱 + 多跳推理 |
| 孤立的知识片段 | 节点间的语义关系网络 |
| 静态知识库 | 动态演化、自我更新 |
| 单一召回路径 | 双路径并行召回（精确 + 泛化） |

### 核心能力

1. **自动知识提取** - 从对话中识别 TASK/SKILL/EVENT 三元组
2. **图谱社区发现** - 使用 Leiden 算法自动聚类相关知识域
3. **个性化 PageRank** - 基于查询上下文的动态节点排序
4. **混合检索策略** - 向量相似度 + FTS5 全文搜索 + 图遍历
5. **异步后台处理** - 不阻塞主对话流程的增量更新

---

## 核心架构

```
┌─────────────────────────────────────────────────────────┐
│                    Skill Memory Core                     │
├──────────────┬──────────────┬──────────────┬────────────┤
│  Extractor   │   Recaller   │    Graph     │   Store    │
│  (提取器)     │  (召回器)     │  (图谱引擎)   │  (存储层)   │
├──────────────┼──────────────┼──────────────┼────────────┤
│ • LLM 提取    │ • 双路径召回  │ • 社区检测    │ • SQLite   │
│ • 节点验证    │ • PPR 排序   │ • PageRank   │ • FTS5     │
│ • Session整理 │ • reranker   │ • 去重合并    │ • 向量索引  │
└──────────────┴──────────────┴──────────────┴────────────┘
```

### 模块职责

| 模块 | 文件路径 | 核心功能 |
|------|---------|---------|
| **Extractor** | `extractor/core.py` | 从对话中提取节点和边，Session 结束时最终审查 |
| **Recaller** | `recaller/core.py` | 执行双路径召回（精确 + 泛化），合并结果 |
| **Graph** | `graph/*.py` | 社区检测、PageRank 计算、图谱维护 |
| **Store** | `store/core.py` | SQLite CRUD、向量存储、FTS5 搜索 |
| **Core** | `core.py` | 协调各模块，提供统一 API |

---

## 工作流程

### 1️⃣ 消息摄入阶段（同步）

```python
# 每轮对话后调用
from context_engine.skill_memory import ingest_message, after_turn

# 保存用户/AI 消息到数据库
ingest_message(session_id="session_001", message=user_message)
ingest_message(session_id="session_001", message=ai_message)
```

**执行内容：**
- 将消息存入 `gm_messages` 表
- 记录 `turn_index`（轮次序号）
- 过滤 ToolMessage，只保留 human/ai 对话
- 估算 token 数量（用于上下文长度控制）

---

### 2️⃣ 知识提取阶段（异步后台）

```python
# after_turn() 自动触发异步任务
async def after_turn(session_id, last_turn_messages):
    # 1. 保存消息
    for msg in last_turn_messages:
        ingest_message(session_id, msg)
    
    # 2. 异步提取知识（不阻塞主流程）
    asyncio.create_task(run_turn_extract(session_id))
```

**Extractor 工作流程：**

```
对话消息 → LLM 提取 → 节点/边验证 → 入库 → 异步生成 embedding
```

#### 提取规则

**节点类型（Node Types）：**

| 类型 | 说明 | 命名规范 | 示例 |
|------|------|---------|------|
| **TASK** | 用户要求完成的任务或讨论的主题 | `动词-对象` | `deploy-bilibili-mcp` |
| **SKILL** | 可复用的操作技能（工具/命令/API） | `工具-操作` | `conda-env-create` |
| **EVENT** | 一次性报错或异常 | `现象-工具` | `importerror-libgl1` |

**边类型（Edge Types）：**

| 类型 | 方向约束 | 含义 | instruction 内容 |
|------|---------|------|-----------------|
| **USED_SKILL** | TASK → SKILL | 任务使用了该技能 | 第几步用的、怎么调用的 |
| **SOLVED_BY** | EVENT → SKILL | 报错被该技能解决 | 具体执行的命令/操作 |
| **REQUIRES** | SKILL → SKILL | 前置依赖关系 | 为什么依赖、如何判断 |
| **PATCHES** | SKILL → SKILL | 新技能修正旧技能 | 旧方案问题、新方案改进 |
| **CONFLICTS_WITH** | SKILL ↔ SKILL | 互斥关系 | 冲突表现、应该选哪个 |

**提取 Prompt 示例：**

```python
# System Prompt 节选
EXTRACT_SYS = """你是 skill_memory 知识图谱提取引擎...

1. 节点提取：
   - TASK：用户要求 Agent 完成的具体任务
   - SKILL：可复用的操作技能，有明确触发条件和步骤
   - EVENT：一次性的报错或异常

2. 关系提取（严格遵守方向约束）：
   - USED_SKILL: TASK → SKILL
   - SOLVED_BY: EVENT → SKILL
   - REQUIRES/PATCHES/CONFLICTS_WITH: SKILL → SKILL

输出严格 JSON：{"nodes":[...],"edges":[...]}
"""
```

---

### 3️⃣ 图谱组装阶段（对话前）

```python
# 在发送给 LLM 之前调用
from context_engine.skill_memory import assemble

result = await assemble(
    user_text="如何用 Docker 部署应用？",
    messages=conversation_history
)

# result 结构：
{
    "messages": [...],  # 规范化后的消息列表
    "estimated_tokens": 1200,  # 预估 token 数
    "system_prompt_addition": "<skill_memory>...</skill_memory>"  # 注入的知识上下文
}
```

**组装流程：**

```
用户查询 → Recaller 召回相关节点 → 图遍历扩展 → PPR 排序 
         → 格式化 XML 上下文 → 注入 system prompt
```

---

### 4️⃣ Session 结束时的知识固化

```python
from context_engine.skill_memory import rectification_and_standardization

# Session 结束时调用
await rectification_and_standardization(session_id="session_001")
```

**执行内容：**

1. **最终审查（Finalize）**
   - EVENT 升级为 SKILL（如果具有通用价值）
   - 补充遗漏的跨节点关系
   - 标记失效节点（因新发现而过时）

2. **图谱维护（Maintenance）**
   - 社区检测（Leiden 算法）
   - 生成社区摘要（LLM 描述 + embedding）
   - 全局 PageRank 更新
   - 节点去重合并

3. **状态清理**
   - 清除 session 运行时状态
   - 释放内存缓存

---

## 关键组件

### 🔍 Extractor（知识提取器）

**位置：** `extractor/core.py`

**核心方法：**

```python
class Extractor:
    @staticmethod
    async def extract(messages, existing_names) -> ExtractionResult:
        """从对话中提取知识图谱"""
        
    @staticmethod
    async def finalize(session_nodes, graph_summary) -> FinalizeResult:
        """Session 结束前的最终审查"""
```

**提取策略：**

- **宁多勿漏** - 所有对话内容都尝试提取（包括讨论、分析、对比）
- **错误纠正追踪** - 用户纠正 AI 错误时，旧做法和新做法都提取，用 `PATCHES` 边关联
- **命名一致性** - 已有节点列表会提供给 LLM，确保相同事物复用已有 name

---

### 🎯 Recaller（召回器）

**位置：** `recaller/core.py`

**双路径召回架构：**

```
用户查询
  ├─ 精确路径（Precise Path）
  │   ├─ 向量搜索 / FTS5 → 种子节点
  │   ├─ 社区扩展（同社区节点）
  │   ├─ 图遍历（BFS max_depth=2）
  │   └─ PPR 排序
  │
  └─ 泛化路径（Generalized Path）
      ├─ 社区向量搜索 → 匹配社区
      ├─ 取社区代表节点
      ├─ 图遍历（BFS max_depth=1）
      └─ PPR 排序
  
  ↓ 合并去重
最终结果（nodes + edges）
```

**代码示例：**

```python
class Recaller:
    async def recall(self, query: str) -> RecallResult:
        # 两条路径并行执行
        precise = await self._recall_precise(query, limit)
        generalized = await self._recall_generalized(query, limit)
        
        # 合并去重
        return self._merge_results(precise, generalized)
```

**reranker 过滤：**

```python
# 召回后使用 reranker 二次过滤
filter_contents = reranker_model.filter(
    query=query,
    candidates=[node.content for node in seeds],
    gap_score=0.85  # 阈值
)
```

---

### 🕸️ Graph Engine（图谱引擎）

#### 社区检测（Community Detection）

**位置：** `graph/community.py`

**算法：** Leiden Algorithm（比 Louvain 更快更准确）

```python
def detect_communities(db: Connection) -> CommunityResult:
    # 1. 读取图结构
    cursor.execute("SELECT id FROM gm_nodes")
    cursor.execute("SELECT from_id, to_id FROM gm_edges")
    
    # 2. 构建 igraph
    g = ig.Graph(len(node_ids), edges, directed=False)
    
    # 3. Leiden 分区
    partition = leidenalg.find_partition(
        g,
        leidenalg.ModularityVertexPartition,
        n_iterations=2
    )
    
    # 4. 更新数据库
    update_communities(db, final_labels)
```

**社区摘要生成：**

```python
async def summarize_communities(db, communities, llm, embed):
    for community_id, member_ids in communities.items():
        # LLM 生成描述
        summary = await llm.ainvoke(
            COMMUNITY_SUMMARY_SYS + f"社区成员：\n{member_text}"
        )
        
        # 生成社区 embedding
        embedding = await embed.aembed_query(embed_text)
        
        # 保存到 gm_communities 表
        upsert_community_summary(db, community_id, summary, len(member_ids), embedding)
```

**用途：**
- 召回时拉取整个社区的节点（扩大上下文覆盖面）
- 泛化召回（用户问"做了哪些工作"时按领域返回概览）
- assemble 时同社区节点放一起（上下文更连贯）

---

#### PageRank 计算

**位置：** `graph/pagerank.py`

**两种 PageRank：**

| 类型 | 计算时机 | 用途 | Teleport 策略 |
|------|---------|------|--------------|
| **个性化 PPR** | recall 时实时计算 | 查询相关节点排序 | 回到种子节点 |
| **全局 PR** | session_end 时批量更新 | topNodes 兜底排序 | 均匀分布 |

**个性化 PageRank 核心逻辑：**

```python
def personalized_page_rank(db, seed_ids, candidate_ids, cfg):
    # teleport 向量：只指向种子节点
    teleport_weight = 1.0 / len(valid_seeds)
    
    # 初始分数：集中在种子节点
    rank = {node_id: teleport_weight if node_id in seed_set else 0.0}
    
    # 迭代传播
    for _ in range(iterations):
        new_rank = {}
        
        # teleport 分量：回到种子节点
        for node_id in node_ids:
            new_rank[node_id] = (1 - damping) * teleport_weight if node_id in seed_set else 0.0
        
        # 传播分量：从邻居获得权重
        for node_id, neighbors in adj.items():
            contrib = rank[node_id] / len(neighbors)
            for neighbor in neighbors:
                new_rank[neighbor] += damping * contrib
        
        rank = new_rank
    
    return {'scores': {cid: rank.get(cid, 0.0) for cid in candidate_ids}}
```

**性能：**
- 几千节点 < 5ms
- O(iterations × edges)
- 图结构缓存 30 秒（避免每次 recall 都查 SQL）

---

### 💾 Store（存储层）

**位置：** `store/core.py`

**数据库 Schema：**

```sql
-- 节点表
CREATE TABLE gm_nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,           -- TASK/SKILL/EVENT
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    validated_count INTEGER DEFAULT 1,  -- 验证次数（重复出现累加）
    source_sessions TEXT,               -- JSON 数组，来源 sessions
    community_id TEXT,                  -- 社区 ID
    pagerank REAL DEFAULT 0,
    created_at INTEGER,
    updated_at INTEGER
);

-- 边表
CREATE TABLE gm_edges (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT NOT NULL,           -- USED_SKILL/SOLVED_BY/...
    instruction TEXT NOT NULL,
    condition TEXT,
    session_id TEXT,
    created_at INTEGER,
    FOREIGN KEY (from_id) REFERENCES gm_nodes(id),
    FOREIGN KEY (to_id) REFERENCES gm_nodes(id)
);

-- 消息表
CREATE TABLE gm_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    role TEXT NOT NULL,           -- human/ai
    content TEXT,                 -- JSON 格式
    created_at INTEGER
);

-- 向量表
CREATE TABLE gm_vectors (
    node_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embedding TEXT NOT NULL,      -- JSON 数组
    FOREIGN KEY (node_id) REFERENCES gm_nodes(id)
);

-- 社区摘要表
CREATE TABLE gm_communities (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    node_count INTEGER,
    embedding TEXT,               -- JSON 数组
    created_at INTEGER,
    updated_at INTEGER
);

-- FTS5 全文搜索
CREATE VIRTUAL TABLE gm_nodes_fts USING fts5(
    text,
    content='gm_nodes',
    content_rowid='rowid'
);

CREATE VIRTUAL TABLE gm_nodes_fts_trigram USING fts5(
    text,
    content='gm_nodes',
    content_rowid='rowid',
    tokenize='trigram'  -- 支持中文分词
);
```

**核心 CRUD 操作：**

```python
# 节点 UPSERT（自动去重）
def upsert_node(db, c, session_id) -> UpsertResult:
    name = normalize_name(c['name'])
    ex = find_by_name(db, name)
    
    if ex:
        # 已存在：累加 validated_count，合并 source_sessions
        count = ex.validated_count + 1
        sessions = list(set(ex.source_sessions + [session_id]))
        UPDATE gm_nodes SET validated_count=?, source_sessions=? ...
    else:
        # 新建节点
        INSERT INTO gm_nodes VALUES (...)
```

```python
# 图遍历（递归 CTE）
def graph_walk(db, seed_ids, max_depth):
    walk_sql = """
        WITH RECURSIVE walk(node_id, depth) AS (
            SELECT id, 0 FROM gm_nodes WHERE id IN (?)
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
```

```python
# 混合搜索（FTS5 + 降级 LIKE）
def search_nodes(db, query, limit):
    if fts5_available(db):
        # 优先 FTS5
        sql = "SELECT n.*, rank FROM gm_nodes_fts MATCH ? ORDER BY rank LIMIT ?"
    else:
        # 降级 LIKE
        sql = "SELECT * FROM gm_nodes WHERE name LIKE ? OR content LIKE ? ..."
```

---

## 数据模型

### 节点（GmNode）

```python
class GmNode(BaseModel):
    id: str                      # 唯一 ID，格式 "n-{timestamp}-{random}"
    type: Literal["TASK", "SKILL", "EVENT"]
    name: str                    # 标准化名称（全小写连字符）
    description: str             # 一句话说明触发场景
    content: str                 # 纯文本知识内容
    validated_count: int = 1     # 验证次数（重复出现累加）
    source_sessions: List[str]   # 来源 sessions 列表
    community_id: Optional[str]  # 所属社区 ID
    pagerank: float = 0          # 全局 PageRank 分数
    created_at: int              # 创建时间戳（毫秒）
    updated_at: int              # 更新时间戳（毫秒）
```

### 边（GmEdge）

```python
class GmEdge(BaseModel):
    id: str                      # 唯一 ID，格式 "e-{timestamp}-{random}"
    from_id: str                 # 源节点 ID
    to_id: str                   # 目标节点 ID
    type: str                    # 边类型（5 种合法值）
    instruction: str             # 执行步骤/调用方式
    condition: Optional[str]     # 触发条件（SOLVED_BY 必填）
    session_id: str              # 来源 session
    created_at: int              # 创建时间戳
```

### 配置（GmConfig）

```python
class GmConfig(BaseModel):
    db_path: str = "skill_memory.db"
    compact_turn_count: int = 6       # 社区维护间隔（轮次）
    recall_max_nodes: int = 6         # 召回节点上限
    recall_max_depth: int = 2         # 图遍历最大深度
    fresh_tail_count: int = 10        # 新鲜尾部节点数
    dedup_threshold: float = 0.90     # 去重相似度阈值
    pagerank_damping: float = 0.85    # PageRank 阻尼系数
    pagerank_iterations: int = 20     # PageRank 迭代次数
    embedding: Embeddings             # 嵌入模型
    llm: BaseChatModel                # LLM 模型
```

---

## 召回机制

### 精确召回（Precise Recall）

**目标：** 找到与查询语义高度相关的具体三元组

**流程：**

```python
async def _recall_precise(query, limit):
    # 1. 向量搜索找种子节点
    vec = await embed.aembed_query(query)
    scored = vector_search_with_score(db, vec, ceil(limit/2))
    seeds = [s['node'] for s in scored]
    
    # 2. 不足时补 FTS5
    if len(seeds) < 2:
        fts_results = search_nodes(db, query, limit)
        seeds.extend([n for n in fts_results if n.id not in seen_ids])
    
    # 3. reranker 过滤
    filter_contents = reranker_model.filter(query, [s.content for s in seeds], gap_score=0.85)
    seeds = [node_dict[c] for c in filter_contents]
    
    # 4. 社区扩展
    expanded_ids = set(seed_ids)
    for seed in seeds:
        peers = get_community_peers(db, seed.id, 2)
        expanded_ids.update(peers)
    
    # 5. 图遍历
    walk_result = graph_walk(db, list(expanded_ids), max_depth=2)
    
    # 6. PPR 排序
    ppr_result = personalized_page_rank(db, seed_ids, candidate_ids, cfg)
    filtered = sorted(nodes, key=lambda n: ppr_scores[n.id], reverse=True)[:limit]
    
    return {'nodes': filtered, 'edges': ..., 'token_estimate': ...}
```

---

### 泛化召回（Generalized Recall）

**目标：** 提供跨领域的全局概览，覆盖精确路径可能遗漏的知识域

**流程：**

```python
async def _recall_generalized(query, limit):
    # 1. 社区向量搜索
    vec = await embed.aembed_query(query)
    scored_communities = community_vector_search(db, vec)
    
    if scored_communities:
        community_ids = [c['id'] for c in scored_communities]
        seeds = nodes_by_community_ids(db, community_ids, 3)
    
    # 2. fallback：按时间取社区代表节点
    if not seeds:
        seeds = community_representatives(db, 2)
    
    # 3. reranker 过滤
    filter_contents = reranker_model.filter(query, [s.content for s in seeds], gap_score=0.85)
    
    # 4. 图遍历（浅层）
    walk_result = graph_walk(db, seed_ids, max_depth=1)
    
    # 5. PPR 排序
    ppr_result = personalized_page_rank(db, seed_ids, candidate_ids, cfg)
    filtered = sorted(nodes, key=lambda n: ppr_scores[n.id], reverse=True)[:limit]
    
    return {'nodes': filtered, 'edges': ..., 'token_estimate': ...}
```

---

### 合并策略

```python
def _merge_results(precise, generalized):
    node_map = {}
    edge_map = {}
    
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
    final_ids = set(node_map.keys())
    for e in generalized['edges']:
        if e.id not in edge_map and e.from_id in final_ids and e.to_id in final_ids:
            edge_map[e.id] = e
    
    return {
        'nodes': list(node_map.values()),
        'edges': list(edge_map.values()),
        'token_estimate': ...
    }
```

---

## 图谱维护

### 定期维护任务

**触发时机：** 每 N 轮对话（默认 6 轮）

```python
async def after_turn(session_id, last_turn_messages):
    turns = turn_counter.get(session_id, 0) + 1
    maintain_interval = DEFAULT_CONFIG.compact_turn_count  # 6
    
    if turns >= maintain_interval:
        turn_counter[session_id] = 0
        
        # 1. 清除缓存
        invalidate_graph_cache()
        
        # 2. 社区检测
        comm = detect_communities(db)
        
        # 3. 生成社区摘要
        if comm["communities"]:
            summaries = await summarize_communities(
                db, comm["communities"], DEFAULT_CONFIG.llm, embed
            )
```

---

### Session 结束时的维护

**触发时机：** Session 结束时调用 `rectification_and_standardization()`

**执行步骤：**

```python
async def rectification_and_standardization(session_id):
    # 1. 获取该 session 的所有节点
    nodes = get_by_session(db, session_id)
    
    # 2. 构建图谱摘要（Top 20 节点）
    cursor.execute("SELECT name, type, validated_count, pagerank FROM gm_nodes ORDER BY pagerank DESC LIMIT 20")
    summary = ", ".join(f"{n['type']}:{n['name']}(v{n['validated_count']},pr{n['pagerank']})" for n in top_nodes)
    
    # 3. 最终审查
    fin = await extractor.finalize(session_nodes=nodes, graph_summary=summary)
    
    # 4. 处理升级的技能
    for nc in fin.promoted_skills:
        upsert_node(db, {"type": "SKILL", "name": nc.name, ...}, session_id)
    
    # 5. 处理新边
    for ec in fin.new_edges:
        upsert_edge(db, {...})
    
    # 6. 标记失效节点
    for node_id in fin.invalidations:
        delete_node(db, node_id)
    
    # 7. 执行图谱维护
    result = await run_maintenance(db, DEFAULT_CONFIG, DEFAULT_CONFIG.llm, embed)
    
    # 8. 清理 Session 状态
    msg_seq.pop(session_id, None)
    turn_counter.pop(session_id, None)
```

---

### 节点去重合并

**策略：** 基于向量相似度 + 名称标准化

```python
def merge_nodes(db, keep_id, merge_id):
    # 1. 合并属性（取内容更长的，累加验证次数）
    sessions = list(set(keep.source_sessions + merge.source_sessions))
    count = keep.validated_count + merge.validated_count
    content = keep.content if len(keep.content) >= len(merge.content) else merge.content
    
    # 2. 更新保留节点
    UPDATE gm_nodes SET content=?, validated_count=?, source_sessions=? WHERE id=?
    
    # 3. 迁移边关系
    UPDATE gm_edges SET from_id=? WHERE from_id=?
    UPDATE gm_edges SET to_id=? WHERE to_id=?
    
    # 4. 删除自环和重复边
    DELETE FROM gm_edges WHERE from_id = to_id
    DELETE FROM gm_edges WHERE id NOT IN (SELECT MIN(id) GROUP BY from_id, to_id, type)
    
    # 5. 删除被合并节点
    delete_node(db, merge_id)
```

---

## 使用示例

### 基础用法

```python
from context_engine.skill_memory import ingest_message, after_turn, assemble, rectification_and_standardization

# === 1. 对话过程中 ===
for turn in conversation:
    # 保存消息
    ingest_message(session_id, user_message)
    ingest_message(session_id, ai_message)
    
    # 异步提取知识（后台任务）
    await after_turn(session_id, [user_message, ai_message])

# === 2. 发送请求前 ===
# 组装上下文（召回相关知识）
context = await assemble(
    user_text="如何用 Docker 部署应用？",
    messages=conversation_history
)

# 注入 system prompt
if "system_prompt_addition" in context:
    system_prompt += "\n\n" + context["system_prompt_addition"]

# === 3. Session 结束时 ===
await rectification_and_standardization(session_id)
```

---

### 高级用法：自定义配置

```python
from context_engine.skill_memory.core import DEFAULT_CONFIG
from models.embed_model.core import embed_model
from models.chat_model import chat_model

# 修改配置
custom_config = DEFAULT_CONFIG.model_copy(update={
    "db_path": "./custom_skill_memory.db",
    "compact_turn_count": 10,      # 每 10 轮维护一次
    "recall_max_nodes": 10,        # 召回 10 个节点
    "recall_max_depth": 3,         # 图遍历深度 3
    "dedup_threshold": 0.95,       # 更高的去重阈值
    "pagerank_iterations": 30,     # 更多 PageRank 迭代
})
```

---

### 查询统计信息

```python
from context_engine.skill_memory.store import get_db, get_stats

db = get_db()
stats = get_stats(db)

print(f"总节点数: {stats['total_nodes']}")
print(f"按类型分布: {stats['by_type']}")
print(f"总边数: {stats['total_edges']}")
print(f"按边类型分布: {stats['by_edge_type']}")
print(f"社区数: {stats['communities']}")

# 输出示例：
# 总节点数: 156
# 按类型分布: {'TASK': 45, 'SKILL': 89, 'EVENT': 22}
# 总边数: 234
# 按边类型分布: {'USED_SKILL': 120, 'SOLVED_BY': 67, 'REQUIRES': 30, 'PATCHES': 12, 'CONFLICTS_WITH': 5}
# 社区数: 12
```

---

## 性能优化

### 1. 异步任务队列

**位置：** `async_task_queue.py`

```python
# embedding 生成不阻塞主流程
async_task_queue.add_task(recaller.sync_embed(node))
```

**优势：**
- 知识提取完成后立即返回
- embedding 在后台异步生成
- 避免等待 LLM 响应

---

### 2. 图结构缓存

```python
_cached: Optional[GraphStructure] = None
CACHE_TTL = 30_000  # 30 秒

def load_graph(db):
    if _cached and (time.time() * 1000 - _cached['cached_at']) < CACHE_TTL:
        return _cached
    
    # 重新加载图结构
    ...
```

**优势：**
- 避免每次 recall 都查 SQL
- 30 秒内共享同一份图结构
- compact 后自动失效

---

### 3. 向量哈希去重

```python
def sync_embed(node):
    content_hash = hashlib.md5(content.encode()).hexdigest()
    existing_hash = get_vector_hash(db, node.id)
    
    if existing_hash == hash_obj:
        return  # 跳过未变化的节点
```

**优势：**
- 避免重复计算 embedding
- 节省 LLM 调用成本

---

### 4. FTS5 全文搜索

```python
# 优先 FTS5（快速）
if fts5_available(db):
    sql = "SELECT n.*, rank FROM gm_nodes_fts MATCH ? ORDER BY rank LIMIT ?"
else:
    # 降级 LIKE（慢速）
    sql = "SELECT * FROM gm_nodes WHERE name LIKE ? OR content LIKE ?"
```

**优势：**
- FTS5 比 LIKE 快 10-100 倍
- 支持 trigram 分词（中文友好）
- 自动维护索引（触发器）

---

### 5. 批量操作

```python
# 批量更新 PageRank
def update_pageranks(db, scores):
    db.execute("BEGIN")
    for node_id, score in scores.items():
        cursor.execute("UPDATE gm_nodes SET pagerank=? WHERE id=?", (score, node_id))
    db.commit()
```

**优势：**
- 减少事务开销
- 提升写入性能

---

## 常见问题

### Q1: 如何调整召回节点数量？

```python
from context_engine.skill_memory.core import DEFAULT_CONFIG

DEFAULT_CONFIG.recall_max_nodes = 10  # 默认 6
```

---

### Q2: 如何禁用社区检测？

目前不支持完全禁用，但可以增大维护间隔：

```python
DEFAULT_CONFIG.compact_turn_count = 100  # 每 100 轮才维护一次
```

---

### Q3: 如何查看提取的知识？

```python
from context_engine.skill_memory.store import get_db, all_active_nodes, all_edges

db = get_db()
nodes = all_active_nodes(db)
edges = all_edges(db)

for node in nodes:
    print(f"[{node.type}] {node.name}: {node.description}")
```

---

### Q4: 如何清理过期数据？

```python
from context_engine.skill_memory.store import get_db, delete_node

db = get_db()

# 删除指定节点（自动清理相关边和向量）
delete_node(db, "n-1234567890-abcde")
```

---

## 技术栈

| 组件 | 技术选型 |
|------|---------|
| **数据库** | SQLite 3 + FTS5 |
| **向量存储** | SQLite JSON 字段 |
| **图算法** | igraph + Leiden Algorithm |
| **PageRank** | 自定义实现（Python） |
| **嵌入模型** | BGE/BAAI 系列 |
| **LLM** | LangChain ChatModel |
| **异步框架** | asyncio |

---

## 许可证

本项目遵循 EMA AI Agent 的开源协议。

---

**作者：** MOYE 
**最后更新：** 2026-05-30
