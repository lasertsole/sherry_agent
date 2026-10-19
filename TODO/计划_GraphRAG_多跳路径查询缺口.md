# 计划：GraphRAG 多跳路径查询缺口——BFS 显式遍历 + embedding-guided beam search

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- 关联代码：
  - `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/operate.py`（查询入口 `query_nodes_by_vector`、`query_edges_by_vector`）
  - `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/kg/snkv_graph_impl.py`（图存储）
  - `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/kg/snkv_vector_impl.py`（实体 embedding）
  - `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/lightrag.py`（查询模式分发）
- 关联文档：`TODO/计划_GraphRAG_稀疏召回与重排调优.md`（同属 GraphRAG 检索能力提升系列）
- 关联评测：`evals/graph_rag/suite.py`

---

## 0. 背景：Sherry graph_rag 没有路径算法

经过代码核查，确认 Sherry graph_rag 当前查询路径是**纯向量召回 + LLM 推理**，**没有任何路径算法**：

- `operate.py::query_nodes_by_vector`（`:4915-4919`）：对 query embedding 做一次向量搜索，取 top_k=40 实体
- `operate.py::query_edges_by_vector`（`:5180-5184`）：对 query embedding 做一次向量搜索，取 top_k=40 关系
- 没有 BFS、DFS、最短路径、随机游走、embedding-guided walk
- 所谓的"multi-hop"靠 LLM 在召回的 top_k 实体集合里找交集，推理出连接路径——**概率事件，不是保证事件**

### 0.1 能力分级

| 跳数 | 含义 | 当前能力 | 失败模式 |
|---|---|---|---|
| 1 跳 | A 直接连 B | 强 | — |
| 2 跳 | A → X → B | **不确定** | 中间实体 X 没进 top_k=40 召回就推理不出来 |
| 3 跳 | A → X → Y → B | 弱 | 召回概率更低 |
| 4+ 跳 | 深度路径 | 几乎不能 | top_k 召回覆盖不到整条桥接路径 |

### 0.2 与 DataGraphX_Learn 的反直觉对比

| 跳数 | Sherry graph_rag | DataGraphX_Learn |
|---|---|---|
| 1 跳 | 强 | 强（Neo4j `MATCH (n)-[r]-(m)`） |
| 2 跳 | 不确定 | **强**（`find_relevant_subgraph` BFS `max_depth=2` 保证） |
| 3 跳 | 弱 | 不能（`max_depth=2` 硬上限） |
| 4+ 跳 | 不能 | 不能 |

**2 跳上 DataGraphX 反而比 Sherry 稳**：Neo4j 的图遍历是算法保证；Sherry 的"多跳"是 LLM 推理的概率事件。这是 GraphRAG 范式为索引简单性牺牲路径查询能力的代价。

### 0.3 ✅ 现有接口盘点（已核实，工程量比初估小）

经过核查 LightRAG/SNKV 现有接口，**BFS 和 beam search 需要的所有底层原语都已齐备**，无需改动 `snkv_graph_impl.py` 存储层：

| 用途 | 现有接口 | 位置 |
|---|---|---|
| BFS 邻居查询（核心原语） | `BaseGraphStorage.get_node_edges(source_node_id)` | `base.py:510` / `snkv_graph_impl.py:218` |
| 批量邻居查询（性能优化） | `get_nodes_edges_batch(node_ids)` | `base.py:579` / `snkv_graph_impl.py:368` |
| 节点数据 | `get_node(node_id)` / `get_nodes_batch` | `base.py:487` / `:521` |
| 边数据 | `get_edge(src, tgt)` / `get_edges_batch` | `base.py:498` / `:563` |
| Beam search embedding 获取 | `SNKVVectorStorage.get_vectors_by_ids(ids)` | `snkv_vector_impl.py:620` |
| 实体名 → node_id 解析（fallback） | `query_nodes_by_vector(query, top_k)` | `operate.py:4919` |
| Query 实体名抽取（复用） | `extract_keywords_only(query, ...)` | `operate.py:3949` |

**结论**：BFS/beam search 都是**纯 Python 层算法实现 + 复用现有接口**，不动存储层。工程量从初估 6-9 天压到 3-5 天。

---

## 1. 任务一：BFS 显式路径查询（低成本，2-3 跳保证）

### 1.1 目标

新增 query mode `path`，专门处理"A 和 B 什么关系"类查询：从 query 抽 A、B 两个实体名 → 向量召回 top 3 实体作起点候选 → BFS 找 ≤3 跳路径 → 路径展开喂 LLM 推理。

### 1.2 改动清单（修订：不动存储层）

- [ ] **1.2.1** ~~`snkv_graph_impl.py`：新增 `find_neighbors` 接口~~ —— **撤销**，直接复用 `BaseGraphStorage.get_node_edges`（已存在）。
- [ ] **1.2.1**（修订）`operate.py` 或新增 `path_search.py`：实现纯 Python BFS `find_paths(a_id, b_id, max_depth=3, top_k_paths=10)`：
  - 调 `get_node_edges` 拿一跳邻居
  - BFS 扩展 + visited 防环
  - 路径 cap=10，按 `(路径长度, edge_weight 平均)` 排序
- [ ] **1.2.2** `operate.py`：新增 query mode `path`。检测条件：
  - query 含 "什么关系"/"关系如何"/"如何联系"/"之间" + LLM 抽到 ≥2 个实体名
  - 实体名抽取复用 `extract_keywords_only`（`operate.py:3949`）—— **不新增**
- [ ] **1.2.3** 实体名 → node_id 映射 + fallback：
  - 拿 LLM 抽出的实体名字符串
  - 调 `query_nodes_by_vector` 召回 top 3 实体作起点候选（**关键 fallback**，防止 LLM 抽错字符串导致起点错）
  - 每个候选跑 BFS，结果合并去重
- [ ] **1.2.4** 路径展开喂 LLM：
  - `get_edges_batch` 批量取边数据
  - `get_nodes_batch` 批量取节点数据
  - 模板：`A → X → Y → B` + 每跳的 edge type/description/weight + 节点 description
- [ ] **1.2.5** `lightrag.py`：mode 分发里加 `path`，优先级在 hybrid 之前（关系类查询先走 path）

### 1.3 风险与约束（修订版）

- **LLM 实体名抽取错误（最大坑）**：query "苹果和华为什么关系"，LLM 抽 ["苹果", "华为"]，但图里实体名可能是 "Apple Inc." / "华为技术"。直接用 LLM 抽出的字符串 → `compute_mdhash_id` → node_id 会拿错。**必须 fallback**：拿字符串去 `query_nodes_by_vector` 向量召回 top 3，每个候选作起点跑 BFS。
- **歧义实体**：图里可能有多个"苹果"实体（公司 vs 水果）。top 3 候选可能指向不同实体。处理：每个候选 BFS 跑完，结果按路径完全相同去重，路径有部分重叠的保留短的。
- **路径数量爆炸**：稠密图 A 到 B 的 ≤3 跳路径可能有几百条。cap=10，按 `(路径长度 ASC, edge_weight 平均 DESC)` 排序。
- **路径打分策略（先用便宜的）**：路径长度 + edge weight 平均作粗排，**不在路径打分阶段上 LLM**——LLM 推理时自己在 prompt 里精排。如果粗排不够，再考虑路径上节点/边向量与 query 相似度（贵一档）。
- **BFS 性能**：邻居查询走 `get_node_edges`（SNKV 列族读）。3 跳 + 每跳 50 邻居 = 125000 次 IO 量级，需要用 `get_nodes_edges_batch` 批量预读。
- **LLM 上下文长度**：路径展开后，每条路径文本可能 200-500 tokens，top_k_paths=10 可能占 5K tokens——和 LightRAG 现有 top_k=40 实体召回共用预算，要重新分配。

### 1.4 验收标准

- [ ] §0.1 的 2 跳场景召回率从"不确定"提升到 **保证**（路径找到即返回，不靠 LLM 推理运气）。
- [ ] 3 跳场景路径找到率 ≥70%（剩下 30% 是路径真的不存在或图太稀疏）。
- [ ] 单次 BFS 查询 p95 延迟 <500ms（3 跳，节点数 ≤10k 规模）。
- [ ] 路径数 cap=10 生效，稠密图不会卡死。
- [ ] LLM 实体名抽取错误时，vector fallback 召回能救回至少 60% 的失败 case。

---

## 2. 任务二：embedding-guided beam search（中成本，3-5 跳补充，**条件性有效**）

### 2.1 目标

任务一 BFS 保证 2-3 跳，但 BFS 在稠密图 4-5 跳会组合爆炸。embedding-guided beam search 用 embedding 相似度做剪枝，扩展到 3-5 跳还能用——**但有强前提，不是普适提升**。

### 2.2 ⚠️ 能力边界（先看这个，再决定要不要做）

#### 2.2.1 能提升的场景（图里有"embedding 梯度"）

- **同语义簇内的多跳**：学术合作网络、产品供应链、人物关系网——中间节点在 embedding 空间里离 B 比 A 离 B 更近，greedy 朝目标走有效
- **桥接节点是"枢纽"**：embedding 离多个目标都近（比如"宁德时代"在电池产业链里是中心节点），walk 能找到
- **LLM 关键词召回漏掉的中间节点**：walk 通过 embedding 相似度能找到

#### 2.2.2 不能提升甚至会误导的场景（关键）

- **跨领域桥接**：A = "苹果公司"，B = "比亚迪电池"，中间节点 X = "宁德时代"——但 "宁德时代" 的 embedding 可能离 "比亚迪电池" **更远**（语义不同域），greedy 会卡住或走错
- **桥接节点是"边角"**：embedding 离两个端点都远的中间节点，walk 完全错过
- **图有循环**：greedy 在环里打转，必须有 visited 集合
- **多候选邻居都接近 B**：greedy 选最像 B 的，但真正能到 B 的可能是不那么像 B 的那个

#### 2.2.3 纯 greedy walk 弱，必须升级

纯 greedy（每跳选最像 B 的一个邻居）容易死胡同。要真有效，必须：

- **Beam search**：每跳保留 top-k 候选（建议 beam_width=5），不是 greedy 选一个
- **Visited 集合防环**：每条候选路径维护 visited，避免走回头路
- **最大跳数上限**（建议 max_hops=5）：避免指数爆炸
- **双向 walk**：从 A 和 B 同时出发，在中间相遇——比单向 walk 召回率高 2-3 倍（学术界经验值，需 Sherry 数据验证）

### 2.3 改动清单（仅当 §2.4 决策通过时执行，修订：不动存储层）

- [ ] **2.3.1** ~~`snkv_graph_impl.py`：在 §1.2.1 `find_neighbors` 基础上，新增 `find_neighbors_with_embeddings`~~ —— **撤销**，直接组合 `BaseGraphStorage.get_node_edges` + `SNKVVectorStorage.get_vectors_by_ids`（两者都已存在，§0.3 表已列）。
- [ ] **2.3.1**（修订）新增 `path_search.py`（或 `operate.py` 内）：`embedding_guided_beam_search(a_id, b_id, beam_width=5, max_hops=5) -> list[path]`：
  - 每跳：`get_node_edges` 拿邻居 → `get_vectors_by_ids` 批量取邻居 embedding
  - 候选打分：`score = α * cosine(neighbor_emb, b_emb) + β * edge_weight`
  - top-k（beam_width=5）候选保留，其余剪枝
  - 双向同时扩展，中间相遇停止
  - 每条候选路径维护 visited 集合防环
- [ ] **2.3.2** `operate.py`：query mode `path` 在 §1.2.2 基础上，如果 BFS 3 跳找不到，自动 fallback 到 beam search 5 跳。
- [ ] **2.3.3** Beam search 找到路径后，跟 BFS 一样展开喂 LLM（复用 §1.2.4 的展开逻辑）。

### 2.4 决策点：先评估图特性再决定是否实施

在动代码前，先跑一个离线评估：

- [ ] **2.4.1** 取 Sherry 现有知识库的图数据，统计：
  - 平均节点度数（稠密度）
  - 连通分量大小分布
  - 4-5 跳路径的数量级（决定 BFS 还能不能跑）
- [ ] **2.4.2** 构造 10 个已知 4-5 跳路径的 query fixture，跑三种方案对比：
  - A. 纯 BFS（看是否能跑出来）
  - B. embedding-guided greedy walk（看 baseline）
  - C. embedding-guided beam search（看是否显著优于 A 和 B）
- [ ] **2.4.3** 如果 C 比 A 提升 ≥20% 且比 B 提升 ≥30%，做 §2.3。否则跳过任务二，承认 Sherry 不支持 4+ 跳路径查询。

### 2.5 风险与约束

- **Embedding 梯度假设可能不成立**：§2.2.2 列的失败场景在 Sherry 数据上可能很常见——必须先做 §2.4 评估，不能盲做。
- **Beam search 计算量**：每跳 beam_width × avg_neighbors 次距离计算，5 跳约 5×5×50 = 1250 次余弦相似度。可接受。
- **双向 walk 同步**：两个方向同时扩展，相遇判定（同一边被两边走到）需要小心实现。
- **embedding 模型一致性**：walk 用的 embedding 必须和索引时一致（BGE-M3 dense 1024 维），不能换模型后 walk 失效。

### 2.6 验收标准

- [ ] 4 跳路径召回率比纯 BFS 提升 ≥20%（或决策点判定不做）。
- [ ] Beam search 单次查询 p95 延迟 <2s。
- [ ] Visited 集合生效，无死循环。

---

## 3. 融合策略：三层查询

最终查询路径分三层，按 query 类型路由：

| Query 类型 | 检测条件 | 路由 |
|---|---|---|
| 关系类（"A 和 B 什么关系"） | LLM 抽取到两个实体名 + 关系词 | §1 BFS（2-3 跳） → §2 beam search（4-5 跳，条件性） → LLM 推理 |
| 单实体多跳（"A 影响了谁"） | 单实体名 + 影响类动词 | §1 BFS（1-N 跳扩展） |
| 语义类（"什么是 X"） | 无实体对，纯概念 | LightRAG 现有 local/global/hybrid（不动） |

最终 prompt 上下文合并：

```
[BFS 路径结果（如有）]
A → X → Y → B
  edge: A-X "成员" (weight: 0.8)
  edge: X-Y "导师" (weight: 0.7)
  edge: Y-B "合作" (weight: 0.9)

[Beam search 路径结果（如有，且 BFS 没找到）]
A → ... → B (4 hops, similarity score: 0.65)

[LightRAG 现有召回]
top_k=40 实体 + 关系

→ LLM 推理回答
```

---

## 4. 工程量与执行顺序

```
§1 BFS（2-3 跳保证）       — 1~2 天，必做（底层接口齐备，纯算法层实现）
  │
  └─ §2.4 评估（1 天） → 决策点
        │
        ├─ 通过 → §2 beam search（4-5 跳补充） — 2~3 天（embedding 接口齐备）
        │
        └─ 不通过 → 承认 4+ 跳不支持，关掉任务二

§3 融合策略（含 §1/§2）   — 0.5 天（直接拼字符串模板）
```

**总工程量：3~5 天**（初估 6-9 天，因 §0.3 核查发现底层接口齐备，存储层无需改动，工程量压减 40-50%）。

**建议执行顺序**：
1. 先做 §1（BFS 2-3 跳保证）——立刻补上当前最大的能力缺口，工程量小
2. §1 完成后跑 §2.4 评估——决定是否值得做 beam search
3. §2 视评估结果决定
4. §3 融合策略收尾

---

## 5. 替代方案讨论：为什么不直接上 Neo4j

| | Sherry SNKV + 自建路径算法 | 切换到 Neo4j |
|---|---|---|
| 外部依赖 | 无（进程内） | 需要 Neo4j 服务器 |
| 符合 AGENTS.md 无外部 DB 原则 | **是** | **否** |
| 路径算法成熟度 | 自建（有 bug 风险） | 工业级（Cypher `MATCH path=(a)-[*..5]-(b)`） |
| 部署成本 | 零 | 高（用户需装 Neo4j） |
| 与 LightRAG 集成成本 | 改 `snkv_graph_impl.py` | 替换整个存储层 + 数据迁移 |

**结论**：Neo4j 的路径算法成熟度更高，但违反 Sherry 的核心设计原则（无外部 DB、桌面打包体积上限）。坚持 SNKV + 自建路径算法。

---

## 6. 与稀疏召回 TODO 的关系

| TODO | 解决的问题 | 独立性 |
|---|---|---|
| `计划_GraphRAG_稀疏召回与重排调优.md` | 节点召回精度（漏召回专有名词） | 独立 |
| `计划_GraphRAG_多跳路径查询缺口.md`（本文档） | 多跳路径发现（漏召回中间节点） | 独立 |

两者解决的是**不同层面的问题**：
- 稀疏召回解决"实体本身能不能被找到"
- 多跳路径解决"找到的实体之间能不能连起来"

可以并行实施，互不依赖。最终效果是乘法关系：召回精度 × 路径完整性 = 整体检索能力。

---

## 7. 关键参考

- LightRAG 查询入口：`vendored_lightrag/operate.py:4915-4919`（实体）、`:5180-5184`（关系）
- LightRAG 查询模式分发：`vendored_lightrag/lightrag.py:2005`（local/global/hybrid/mix/naive/bypass）
- SNKV 图存储：`vendored_lightrag/kg/snkv_graph_impl.py`（需要新增 `find_neighbors` / `find_paths` 接口）
- SNKV 向量存储：`vendored_lightrag/kg/snkv_vector_impl.py:620`（`get_vectors_by_ids` 已存在，可复用取 embedding）
- LightRAG top_k 默认值：`vendored_lightrag/constants.py:52`（`DEFAULT_TOP_K = 40`）
