# Skill Memory — 智能知识图谱记忆系统

[**English**](README.md) | **中文文档**

> **Skill Memory** 是 EMA AI Agent 的核心记忆引擎，自动从对话中提取、组织和检索结构化知识，构建动态演化的技能知识图谱。

---

## 目录

- [概述](#概述)
- [与 Hermes 的对比](#与-hermes-的对比)
- [架构](#架构)
- [工作流程](#工作流程)
- [核心机制](#核心机制)
- [核心组件](#核心组件)
- [数据模型](#数据模型)
- [召回机制](#召回机制)
- [图谱维护](#图谱维护)
- [使用示例](#使用示例)
- [性能优化](#性能优化)
- [常见问题](#常见问题)
- [技术栈](#技术栈)
- [许可证](#许可证)

---

## 概述

### 设计理念

Skill Memory 是一个**基于图谱的知识记忆系统**，旨在克服传统 SKILL 系统的局限：

| 传统 RAG | Skill Memory |
|----------|-------------|
| 扁平向量检索 | 结构化图谱 + 多跳推理 |
| 孤立知识块 | 节点间语义关系网络 |
| 静态知识库 | 动态演化，自我更新 |
| 单一召回路径 | 双路并行召回（精确 + 泛化） |

### 核心能力

1. **自动知识提取** — 从对话中识别 TASK/SKILL/EVENT 三元组
2. **图谱社区检测** — 使用 Leiden 算法自动聚类相关知识领域
3. **个性化 PageRank** — 基于查询上下文的动态节点排序
4. **混合检索** — 向量相似度 + FTS5 全文搜索 + 图遍历
5. **异步后台处理** — 增量更新，不阻塞主对话流程

---

## 与 Hermes 的对比

Skill Memory 与 Hermes（Function Calling 方案）都致力于让 AI 学会复用经验，但设计哲学完全不同。

### 核心差异

| 维度 | Hermes（传统方案） | Skill Memory（本系统） |
|------|-------------------|----------------------|
| **存储形式** | 每次对话生成技能文本，放在上下文中 | 结构化图谱节点+边，按需召回 |
| **检索方式** | 无——全量携带在上下文中 | 双路召回（向量+图谱+社区） |
| **去重机制** | 无——同一技能反复生成 | 图谱自动合并相似节点 |
| **Token 开销** | 随对话数线性增长，O(n) | 仅召回相关节点，O(k)，k ≪ n |
| **路由能力** | 无——技能放在上下文但无调用映射 | 路由表（边 `instruction` 字段）保存调用方式 |
| **知识演化** | 无——历史技能不更新 | validated_count 累加 → 自动升级为 SKILL |
| **长尾管理** | 上下文爆炸 | 图谱精炼（社区检测、去重、合并） |

### Token 对比：实际场景

假设 100 轮对话，每轮产生 2 个技能节点：

| 指标 | Hermes | Skill Memory |
|------|--------|-------------|
| 总技能数 | 200（全量在上下文） | 200（在图谱中，仅召回 6-10） |
| 上下文 Token | ~50K+ | ~1.5K-3K |
| 冗余率 | 高（同一技能反复生成） | 低（自动合并） |
| 知识遗忘风险 | 高（上下文窗口溢出） | 低（持久化存储） |

### 核心优势

Skill Memory 不是"更好的 Hermes"，而是完全不同的范式：

1. **图谱即路由** — 节点间的关系（USED_SKILL/SOLVED_BY/PATCHES）天然构成路由表，LLM 通过 `instruction` 字段知道如何调用
2. **经验→技能自动升级** — EVENT 节点匹配 3+ 次后自动升格为 SKILL，无需人工干预
3. **图谱精炼** — 社区检测使同领域节点自动聚类，PageRank 排序让高频节点自然浮现
4. **无 Token 膨胀** — 仅召回与当前查询最相关的节点，不随对话历史增长

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    Skill Memory Core                     │
├──────────────┬──────────────┬──────────────┬────────────┤
│  Extractor   │   Recaller   │    Graph     │   Store    │
├──────────────┼──────────────┼──────────────┼────────────┤
│ • LLM 提取   │ • 双路召回   │ • 社区检测   │ • SQLite   │
│ • 节点校验   │ • PPR 排序   │ • PageRank   │ • FTS5     │
│ • 会话收尾   │ • 重排序    │ • 去重合并   │ • 向量存储 │
└──────────────┴──────────────┴──────────────┴────────────┘
```

### 模块职责

| 模块 | 文件路径 | 核心功能 |
|------|---------|----------|
| **Extractor** | `extractor/core.py` | 从对话中提取节点/边；会话结束时终审 |
| **Recaller** | `recaller/core.py` | 执行双路召回（精确 + 泛化）；合并结果 |
| **Graph** | `graph/*.py` | 社区检测、PageRank 计算、图谱维护 |
| **Store** | `store/core.py` | SQLite CRUD、向量存储、FTS5 搜索 |
| **Core** | `core.py` | 编排各模块；提供统一 API |

---

## 工作流程

### 1️⃣ 消息摄取（同步）

```python
# 每轮对话后调用
from context_engine.skill_memory import ingest_message, after_turn

# 将用户/AI 消息保存到数据库
ingest_message(session_id="session_001", message=user_message)
ingest_message(session_id="session_001", message=ai_message)
```

**执行内容：**
- 消息存储在 `gm_messages` 表中
- 记录 `turn_index`（轮次序号）
- 过滤掉 ToolMessage，只保留 human/ai 对话
- 估算 token 数量（用于上下文长度控制）

---

### 2️⃣ 知识提取（异步后台）

```python
# after_turn() 自动触发异步任务
async def after_turn(session_id, last_turn_messages):
    # 1. 保存消息
    for msg in last_turn_messages:
        ingest_message(session_id, msg)
    
    # 2. 异步提取知识（非阻塞）
    asyncio.create_task(run_turn_extract(session_id))
```

**提取器流水线：**

```
对话消息 → LLM 提取 → 节点/边校验 → 数据库插入 → 异步生成嵌入向量
```

#### 提取规则

**节点类型：**

| 类型 | 说明 | 命名惯例 | 示例 |
|------|------|---------|------|
| **TASK** | 用户请求的任务或主题 | `动词-宾语` | `deploy-bilibili-mcp` |
| **SKILL** | 可复用的操作技能（工具/命令/API） | `工具-操作` | `conda-env-create` |
| **EVENT** | 一次性错误或异常 | `现象-工具` | `importerror-libgl1` |

**边类型：**

| 类型 | 方向约束 | 含义 | `instruction` 内容 |
|------|---------|------|-------------------|
| **USED_SKILL** | TASK → SKILL | 任务使用了该技能 | 哪一步、如何调用 |
| **SOLVED_BY** | EVENT → SKILL | 错误由此技能解决 | 执行的具体命令/操作 |
| **REQUIRES** | SKILL → SKILL | 前置依赖 | 为何依赖、如何判断 |
| **PATCHES** | SKILL → SKILL | 新技能修正旧技能 | 旧方案的问题、新修复 |
| **CONFLICTS_WITH** | SKILL ↔ SKILL | 互斥关系 | 冲突表现、选择哪个 |

**提取提示词示例：**

```python
# 系统提示词摘录
EXTRACT_SYS = """你是 skill_memory 知识图谱提取引擎...

1. 节点提取：
   - TASK：用户要求 Agent 完成的特定任务
   - SKILL：具有明确触发条件和步骤的可复用操作技能
   - EVENT：一次性错误或异常

2. 关系提取（严格遵循方向约束）：
   - USED_SKILL: TASK → SKILL
   - SOLVED_BY: EVENT → SKILL
   - REQUIRES/PATCHES/CONFLICTS_WITH: SKILL → SKILL

输出严格 JSON: {"nodes":[...],"edges":[...]}
"""
```

---

### 3️⃣ 图谱组装（对话前）

```python
# 在发送给 LLM 之前调用
from context_engine.skill_memory import assemble

result = await assemble(
    user_text="如何用 Docker 部署应用？",
    messages=conversation_history
)

# result 结构：
{
    "messages": [...],               # 标准化后的消息列表
    "estimated_tokens": 1200,        # 估算 token 数
    "system_prompt_addition": "<skill_memory>...</skill_memory>"  # 注入的知识上下文
}
```

**组装流水线：**

```
用户查询 → Recaller 召回相关节点 → 图遍历扩展 → PPR 排序
         → 格式化为 XML 上下文 → 注入系统提示词
```

---

### 4️⃣ 会话结束时的知识固话

```python
from context_engine.skill_memory import rectification_and_standardization

# 会话结束时调用
await rectification_and_standardization(session_id="session_001")
```

**执行内容：**

1. **终审**
   - 将 EVENT 提升为 SKILL（若具有通用价值）
   - 补充缺失的跨节点关系
   - 标记过时节点（因新发现而失效）

2. **图谱维护**
   - 社区检测（Leiden 算法）
   - 生成社区摘要（LLM 描述 + 嵌入向量）
   - 全局 PageRank 更新
   - 节点去重与合并

3. **状态清理**
   - 清除会话运行时状态
   - 释放内存缓存

---

## 核心机制

Skill Memory 的核心创新在于三个自动化机制：经验向技能的自动升级、基于图谱的路由表、以及相似节点的自动合并。

---

### 经验 → 技能自动升级（Experience → Skill Promotion）

**核心思想：** 同一经验（EVENT）被验证多次后，自动升格为可复用的技能（SKILL）。

#### 升级流程

```
EVENT 首次出现 → validated_count = 1
     ↓
EVENT 再次匹配 → validated_count += 1
     ↓          （每被召回且确认一次 +1）
validated_count >= threshold（默认 3）
     ↓
LLM finalize 评审：
  - 判断该经验是否具有通用价值
  - 确认是否存在更优的已有 SKILL
  - 决定是否升级
     ↓
通过 → 节点 type 从 EVENT 改为 SKILL
     → 创建 SOLVED_BY 边（EVENT_old → SKILL_new）
     → instruction 字段记录具体操作步骤
```

**关键变量：**

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `validated_count` | 该节点被验证/匹配的次数 | 每次重复出现 +1 |
| `promotion_threshold` | 触发 LLM 终审的阈值 | 3 |
| `finalize` | 执行终审的 LLM 调用 | 在 `rectification_and_standardization` 中触发 |

**代码逻辑（extractor/core.py）：**

```python
# 节点提取时，如果名称已存在则累积 validated_count
def upsert_node(db, c, session_id) -> UpsertResult:
    name = normalize_name(c['name'])
    ex = find_by_name(db, name)
    
    if ex:
        # 已存在节点：增加验证次数
        count = ex.validated_count + 1
        # 合并来源会话
        sessions = list(set(ex.source_sessions + [session_id]))
        UPDATE gm_nodes SET validated_count=?, source_sessions=? WHERE id=?
    else:
        # 新节点，validated_count 默认为 1
        INSERT INTO gm_nodes VALUES (...)
```

```python
# 会话结束时，对 validated_count 达到阈值的 EVENT 执行终审
async def finalize(session_nodes, graph_summary) -> FinalizeResult:
    promoted_skills = []
    new_edges = []
    invalidations = []
    
    for node in session_nodes:
        if node.type == "EVENT" and node.validated_count >= promotion_threshold:
            # LLM 评审：此经验是否值得升级为 SKILL
            decision = await llm.ainvoke(
                FINALIZE_PROMPT.format(node=node, summary=graph_summary)
            )
            if decision.promote:
                promoted_skills.append(node)
                new_edges.append({
                    "from": node.id,
                    "to": decision.skill_id,
                    "type": "SOLVED_BY",
                    "instruction": decision.instruction
                })
    
    return FinalizeResult(
        promoted_skills=promoted_skills,
        new_edges=new_edges,
        invalidations=invalidations
    )
```

**设计要点：**
- 阈值触发 LLM 终审而非自动升级 — 确保质量
- validated_count 在每次节点匹配时累加（非仅提取时）
- 升级后保留原 EVENT 节点（标记为旧，不删除）— 保留历史记录
- `source_sessions` 记录每次出现的会话 ID，可追溯

---

### Skill 路由（Routing Table）

**核心思想：** 图谱中 SKILL 节点的入边（SOLVED_BY / USED_SKILL）天然构成路由表，无需独立的路由器模型。

#### 路由表结构

路由信息存储在 **边（edge）的 `instruction` 字段** 中，每条 SOLVED_BY 或 USED_SKILL 边记录了调用方式：

```python
# 路由表示例（基于图谱查询）
def build_routing_table(db, skill_name) -> list[dict]:
    """查询某个 SKILL 节点的所有路由条目"""
    cursor.execute("""
        SELECT e.type, e.instruction, e.condition,
               n_from.name AS from_name, n_from.type AS from_type
        FROM gm_edges e
        JOIN gm_nodes n_from ON e.from_id = n_from.id
        WHERE e.to_id = (SELECT id FROM gm_nodes WHERE name = ?)
        AND e.type IN ('SOLVED_BY', 'USED_SKILL')
    """, (skill_name,))
    return cursor.fetchall()
```

**路由信息格式（instruction 字段内容示例）：**

| 源节点 | 边类型 | instruction（调用方式） | condition（触发条件） |
|--------|--------|------------------------|-----------------------|
| EVENT:importerror-libgl1 | SOLVED_BY | `apt-get install libgl1-mesa-glx` | 遇到 ImportError: libGL.so.1 |
| TASK:deploy-bilibili-mcp | USED_SKILL | 步骤 3：使用 conda 创建 Python 3.10 环境 | 部署 Python 项目时 |
| SKILL:pip-install | REQUIRES | 前置步骤：确认 pip 已安装 | — |

#### 路由表是如何构建的？

无需额外步骤。每条边被创建时，`instruction` 字段就包含了调用信息：

```python
# 提取器创建 SOLVED_BY 边时自动写入路由信息
{
    "from": "n-event-001",           # EVENT 节点
    "to": "n-skill-001",             # SKILL 节点
    "type": "SOLVED_BY",
    "instruction": "conda install -c conda-forge libgl1-mesa-glx",  # 调用方式
    "condition": "ImportError: libGL.so.1"                          # 触发条件
}
```

#### 召回时使用路由

```python
# 召回结果附带的边天然就是路由表
result = await recaller.recall(query="Docker 部署报错")
for edge in result.edges:
    if edge.type in ("SOLVED_BY", "USED_SKILL"):
        # instruction 可以直接作为 LLM 的工具调用指令
        print(f"{edge.from_id} → {edge.to_id}: {edge.instruction}")
```

**设计要点：**
- 路由表是**图谱的副产品** — 不需要独立构建或维护
- instruction 字段是**结构化文本** — 可以直接被 LLM 消费
- 多跳路由 — 通过 REQUIRES 边可以构建多步骤链路

---

### 相似节点自动合并（Node Merging）

**核心思想：** 当两个节点的语义相似度超过阈值时，自动合并为一个节点，保留更完整的信息。

#### 触发时机

1. **每次提取时：** 新提取的节点与已有节点进行名称和向量比对
2. **会话结束时：** 批量检查本会话新产生的节点
3. **图谱维护时：** 定期全局去重扫描

#### 合并流程

```
节点 A（name="docker-deploy-error"）  ↔  节点 B（name="docker-deployment-error"）
     ↓
1. 向量相似度计算（cosine similarity）
2. 边连通性检查（是否已有边连接？）
     ↓
相似度 >= dedup_threshold（默认 0.90）
     ↓
合并：
  - 保留 name 较长或 validated_count 更高的节点
  - validated_count = A.count + B.count
  - content = 长度更长的内容
  - source_sessions = union(A.sessions, B.sessions)
  - 将 B 的所有边迁移到 A
  - 删除 B（被合并节点）
```

**代码逻辑：**

```python
def merge_nodes(db, keep_id, merge_id):
    keep = get_node(db, keep_id)
    merge = get_node(db, merge_id)
    
    # 1. 合并属性（保留较长内容，累积验证次数）
    sessions = list(set(keep.source_sessions + merge.source_sessions))
    count = keep.validated_count + merge.validated_count
    content = keep.content if len(keep.content) >= len(merge.content) else merge.content
    
    UPDATE gm_nodes 
    SET content=?, validated_count=?, source_sessions=? 
    WHERE id=?
    
    # 2. 迁移边关系
    UPDATE gm_edges SET from_id=? WHERE from_id=?
    UPDATE gm_edges SET to_id=? WHERE to_id=?
    
    # 3. 删除自环和重复边
    DELETE FROM gm_edges WHERE from_id = to_id
    DELETE FROM gm_edges 
    WHERE id NOT IN (SELECT MIN(id) GROUP BY from_id, to_id, type)
    
    # 4. 删除被合并节点
    delete_node(db, merge_id)
```

**合并条件策略：**

| 策略 | 条件 | 优先级 |
|------|------|--------|
| 名称标准化后一致 | `normalize_name(a) == normalize_name(b)` | 最高（100% 合并） |
| 向量相似度 ≥ 阈值 | `cosine(a.embed, b.embed) >= 0.90` | 高 |
| 相同社区 + 高相似度 | 同 community_id + 相似度 ≥ 0.85 | 中 |
| 仅向量相似度（无社区关联） | 相似度 ≥ 0.95 | 中 |

**设计要点：**
- 边迁移时检查**重复边**（两条边如果 from/to/type 都相同，只保留一条）
- validated_count 在合并时累加 — 频繁出现的知识自然拥有更高的权重
- 名称标准化规则：转小写、连字符替换空格、移除标点符号

---

## 核心组件

### 🔍 Extractor（提取器）

**位置：** `extractor/core.py`

**核心方法：**

```python
class Extractor:
    @staticmethod
    async def extract(messages, existing_names) -> ExtractionResult:
        """从对话中提取知识图谱"""
        
    @staticmethod
    async def finalize(session_nodes, graph_summary) -> FinalizeResult:
        """会话结束前的终审"""
```

**提取策略：**

- **宁滥勿缺** — 对所有对话内容（包括讨论、分析、比较）都尝试提取
- **纠错追踪** — 当用户纠正 AI 错误时，新旧两种方法都会被提取，通过 `PATCHES` 边连接
- **命名一致性** — 将已有节点名称提供给 LLM，确保同一事物重用同一名称

---

### 🎯 Recaller（召回器）

**位置：** `recaller/core.py`

**双路召回架构：**

```
用户查询
  ├─ 精确路径
  │   ├─ 向量搜索 / FTS5 → 种子节点
  │   ├─ 社区扩展（同社区节点）
  │   ├─ 图遍历（BFS 最大深度=2）
  │   └─ PPR 排序
  │
  └─ 泛化路径
      ├─ 社区向量搜索 → 匹配社区
      ├─ 获取社区代表节点
      ├─ 图遍历（BFS 最大深度=1）
      └─ PPR 排序
  
  ↓ 合并 & 去重
最终结果（节点 + 边）
```

**代码示例：**

```python
class Recaller:
    async def recall(self, query: str) -> RecallResult:
        # 两条路径并行执行
        precise = await self._recall_precise(query, limit)
        generalized = await self._recall_generalized(query, limit)
        
        # 合并 & 去重
        return self._merge_results(precise, generalized)
```

**重排序过滤：**

```python
# 召回后使用重排序器进行二次过滤
filter_contents = reranker_model.filter(
    query=query,
    candidates=[node.content for node in seeds],
    gap_score=0.85  # 阈值
)
```

---

### 🕸️ Graph Engine（图谱引擎）

#### 社区检测

**位置：** `graph/community.py`

**算法：** Leiden 算法（比 Louvain 更快更准确）

```python
def detect_communities(db: Connection) -> CommunityResult:
    # 1. 读取图谱结构
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
        
        # 生成社区嵌入向量
        embedding = await embed.aembed_query(embed_text)
        
        # 保存到 gm_communities 表
        upsert_community_summary(db, community_id, summary, len(member_ids), embedding)
```

**使用场景：**
- 召回时拉取整个社区的节点（更广的上下文覆盖）
- 泛化召回（当用户问"做过哪些工作"时返回领域概览）
- 组装时将同一社区的节点分组（上下文更连贯）

---

#### PageRank 计算

**位置：** `graph/pagerank.py`

**两种 PageRank：**

| 类型 | 计算时机 | 用途 | 跳跃策略 |
|------|---------|------|---------|
| **个性化 PPR** | 召回时实时计算 | 查询相关节点排序 | 返回种子节点 |
| **全局 PR** | 会话结束时批量更新 | 顶级节点兜底排序 | 均匀分布 |

**个性化 PageRank 核心逻辑：**

```python
def personalized_page_rank(db, seed_ids, candidate_ids, cfg):
    # teleport 向量：仅指向种子节点
    teleport_weight = 1.0 / len(valid_seeds)
    
    # 初始分数：集中在种子节点上
    rank = {node_id: teleport_weight if node_id in seed_set else 0.0}
    
    # 迭代传播
    for _ in range(iterations):
        new_rank = {}
        
        # teleport 分量：返回种子节点
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
- 数千节点：< 5ms
- 时间复杂度 O(迭代次数 × 边数)
- 图谱结构缓存 30 秒（避免每次召回都查询 SQL）

---

### 💾 Store（存储）

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
    validated_count INTEGER DEFAULT 1,  -- 验证次数（重复出现时累积）
    source_sessions TEXT,               -- JSON 格式的来源会话列表
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
        # 已存在：增加 validated_count，合并 source_sessions
        count = ex.validated_count + 1
        sessions = list(set(ex.source_sessions + [session_id]))
        UPDATE gm_nodes SET validated_count=?, source_sessions=? ...
    else:
        # 创建新节点
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
# 混合搜索（FTS5 + 兜底 LIKE）
def search_nodes(db, query, limit):
    if fts5_available(db):
        # 优先 FTS5
        sql = "SELECT n.*, rank FROM gm_nodes_fts MATCH ? ORDER BY rank LIMIT ?"
    else:
        # LIKE 兜底
        sql = "SELECT * FROM gm_nodes WHERE name LIKE ? OR content LIKE ? ..."
```

---

## 数据模型

### GmNode

```python
class GmNode(BaseModel):
    id: str                      # 唯一 ID，格式 "n-{timestamp}-{random}"
    type: Literal["TASK", "SKILL", "EVENT"]
    name: str                    # 标准化名称（小写、连字符分隔）
    description: str             # 触发场景的一行描述
    content: str                 # 纯文本知识内容
    validated_count: int = 1     # 验证次数（重复出现时累积）
    source_sessions: List[str]   # 来源会话列表
    community_id: Optional[str]  # 社区 ID
    pagerank: float = 0          # 全局 PageRank 分数
    created_at: int              # 创建时间戳（毫秒）
    updated_at: int              # 更新时间戳（毫秒）
```

### GmEdge

```python
class GmEdge(BaseModel):
    id: str                      # 唯一 ID，格式 "e-{timestamp}-{random}"
    from_id: str                 # 源节点 ID
    to_id: str                   # 目标节点 ID
    type: str                    # 边类型（5 种有效值）
    instruction: str             # 执行步骤 / 调用方法
    condition: Optional[str]     # 触发条件（SOLVED_BY 必需）
    session_id: str              # 来源会话
    created_at: int              # 创建时间戳
```

### GmConfig

```python
class GmConfig(BaseModel):
    db_path: str = "skill_memory.db"
    compact_turn_count: int = 6       # 社区维护间隔（轮次）
    recall_max_nodes: int = 6         # 最大召回节点数
    recall_max_depth: int = 2         # 最大图遍历深度
    fresh_tail_count: int = 10        # 新鲜尾部节点数
    dedup_threshold: float = 0.90     # 去重相似度阈值
    pagerank_damping: float = 0.85    # PageRank 阻尼因子
    pagerank_iterations: int = 20     # PageRank 迭代次数
    embedding: Embeddings             # 嵌入模型
    llm: BaseChatModel                # LLM 模型
```

---

## 召回机制

### 精确召回

**目标：** 找到与查询高度相关的特定三元组

**流水线：**

```python
async def _recall_precise(query, limit):
    # 1. 向量搜索种子节点
    vec = await embed.aembed_query(query)
    scored = vector_search_with_score(db, vec, ceil(limit/2))
    seeds = [s['node'] for s in scored]
    
    # 2. 不足时兜底 FTS5
    if len(seeds) < 2:
        fts_results = search_nodes(db, query, limit)
        seeds.extend([n for n in fts_results if n.id not in seen_ids])
    
    # 3. 重排序过滤
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

### 泛化召回

**目标：** 提供跨领域概览，覆盖精确路径可能遗漏的知识领域

**流水线：**

```python
async def _recall_generalized(query, limit):
    # 1. 社区向量搜索
    vec = await embed.aembed_query(query)
    scored_communities = community_vector_search(db, vec)
    
    if scored_communities:
        community_ids = [c['id'] for c in scored_communities]
        seeds = nodes_by_community_ids(db, community_ids, 3)
    
    # 2. 兜底：基于时间的社区代表
    if not seeds:
        seeds = community_representatives(db, 2)
    
    # 3. 重排序过滤
    filter_contents = reranker_model.filter(query, [s.content for s in seeds], gap_score=0.85)
    
    # 4. 浅层图遍历
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
    
    # 所有精确结果全部保留
    for n in precise['nodes']:
        node_map[n.id] = n
    for e in precise['edges']:
        edge_map[e.id] = e
    
    # 泛化结果去重后加入
    for n in generalized['nodes']:
        if n.id not in node_map:
            node_map[n.id] = n
    
    # 合并边：只保留两端都在最终节点集合中的边
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

### 定期维护

**触发条件：** 每 N 轮对话（默认：6 轮）

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

### 会话结束时维护

**触发条件：** 会话结束时调用 `rectification_and_standardization()`

**步骤：**

```python
async def rectification_and_standardization(session_id):
    # 1. 获取本次会话所有节点
    nodes = get_by_session(db, session_id)
    
    # 2. 构建图谱摘要（Top 20 节点）
    cursor.execute("SELECT name, type, validated_count, pagerank FROM gm_nodes ORDER BY pagerank DESC LIMIT 20")
    summary = ", ".join(f"{n['type']}:{n['name']}(v{n['validated_count']},pr{n['pagerank']})" for n in top_nodes)
    
    # 3. 终审
    fin = await extractor.finalize(session_nodes=nodes, graph_summary=summary)
    
    # 4. 处理提升的技能
    for nc in fin.promoted_skills:
        upsert_node(db, {"type": "SKILL", "name": nc.name, ...}, session_id)
    
    # 5. 处理新边
    for ec in fin.new_edges:
        upsert_edge(db, {...})
    
    # 6. 标记无效节点
    for node_id in fin.invalidations:
        delete_node(db, node_id)
    
    # 7. 执行图谱维护
    result = await run_maintenance(db, DEFAULT_CONFIG, DEFAULT_CONFIG.llm, embed)
    
    # 8. 清理会话状态
    msg_seq.pop(session_id, None)
    turn_counter.pop(session_id, None)
```

---

### 节点去重与合并

**策略：** 基于向量相似度 + 名称标准化

```python
def merge_nodes(db, keep_id, merge_id):
    # 1. 合并属性（保留较长内容，累积验证次数）
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

# === 1. 对话中 ===
for turn in conversation:
    # 保存消息
    ingest_message(session_id, user_message)
    ingest_message(session_id, ai_message)
    
    # 异步知识提取（后台任务）
    await after_turn(session_id, [user_message, ai_message])

# === 2. 发送请求前 ===
# 组装上下文（召回相关知识）
context = await assemble(
    user_text="如何用 Docker 部署应用？",
    messages=conversation_history
)

# 注入系统提示词
if "system_prompt_addition" in context:
    system_prompt += "\n\n" + context["system_prompt_addition"]

# === 3. 会话结束时 ===
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
    "pagerank_iterations": 30,     # 更多 PageRank 迭代次数
})
```

---

### 查询统计信息

```python
from context_engine.skill_memory.store import get_db, get_stats

db = get_db()
stats = get_stats(db)

print(f"总节点数：{stats['total_nodes']}")
print(f"按类型：{stats['by_type']}")
print(f"总边数：{stats['total_edges']}")
print(f"按边类型：{stats['by_edge_type']}")
print(f"社区数：{stats['communities']}")

# 示例输出：
# 总节点数：156
# 按类型：{'TASK': 45, 'SKILL': 89, 'EVENT': 22}
# 总边数：234
# 按边类型：{'USED_SKILL': 120, 'SOLVED_BY': 67, 'REQUIRES': 30, 'PATCHES': 12, 'CONFLICTS_WITH': 5}
# 社区数：12
```

---

## 性能优化

### 1. 异步任务队列

**位置：** `async_task_queue.py`

```python
# 嵌入生成不阻塞主流程
async_task_queue.add_task(recaller.sync_embed(node))
```

**优点：**
- 知识提取后立即返回
- 嵌入向量在后台异步生成
- 避免等待 LLM 响应

---

### 2. 图谱结构缓存

```python
_cached: Optional[GraphStructure] = None
CACHE_TTL = 30_000  # 30 秒

def load_graph(db):
    if _cached and (time.time() * 1000 - _cached['cached_at']) < CACHE_TTL:
        return _cached
    
    # 重新加载图谱结构
    ...
```

**优点：**
- 避免每次召回都查询 SQL
- 30 秒内共享同一图谱结构
- 压缩后自动失效

---

### 3. 向量哈希去重

```python
def sync_embed(node):
    content_hash = hashlib.md5(content.encode()).hexdigest()
    existing_hash = get_vector_hash(db, node.id)
    
    if existing_hash == hash_obj:
        return  # 跳过未变更的节点
```

**优点：**
- 避免冗余嵌入计算
- 节省 LLM 调用成本

---

### 4. FTS5 全文搜索

```python
# 优先 FTS5（快速）
if fts5_available(db):
    sql = "SELECT n.*, rank FROM gm_nodes_fts MATCH ? ORDER BY rank LIMIT ?"
else:
    # LIKE 兜底（慢速）
    sql = "SELECT * FROM gm_nodes WHERE name LIKE ? OR content LIKE ?"
```

**优点：**
- FTS5 比 LIKE 快 10-100 倍
- Trigram 分词支持中文
- 通过触发器自动维护索引

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

**优点：**
- 减少事务开销
- 提升写入性能

---

## 常见问题

### Q1: 如何调整召回的节点数量？

```python
from context_engine.skill_memory.core import DEFAULT_CONFIG

DEFAULT_CONFIG.recall_max_nodes = 10  # 默认为 6
```

---

### Q2: 如何禁用社区检测？

目前不支持完全禁用，但可以增大维护间隔：

```python
DEFAULT_CONFIG.compact_turn_count = 100  # 每 100 轮维护一次
```

---

### Q3: 如何查看已提取的知识？

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

# 删除特定节点（自动清理相关边和向量）
delete_node(db, "n-1234567890-abcde")
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| **数据库** | SQLite 3 + FTS5 |
| **向量存储** | SQLite JSON 字段 |
| **图算法** | igraph + Leiden 算法 |
| **PageRank** | 自定义实现（Python） |
| **嵌入模型** | BGE/BAAI 系列 |
| **LLM** | LangChain ChatModel |
| **异步框架** | asyncio |

---

## 许可证

本项目遵循 EMA AI Agent 开源许可证。

---

**作者：** MOYE  
**最后更新：** 2026-05-30
