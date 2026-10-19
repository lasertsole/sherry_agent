# 计划：GraphRAG 节点召回精度提升——BGE-M3 稀疏输出 + 重排阈值调优

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- **未决项**：
  - §2.6.4：BGE-M3 官方 `weighted_score` 的 `weights_for_different_modes` 默认数值 **待定**——实施 §2 前必须按 §2.6.4.1 流程从 FlagEmbedding 源码核实后填入，代码不允许凭记忆硬编码权重。
  - §1：`models.build_embed_model()` 是否暴露 `weighted_score`（决定走 §2.6.3 路径 A 还是路径 B）待调研。
- 关联代码：`skills/builtin/core/multimodal_rag/scripts/graph_rag/base.py`、
  `skills/builtin/core/multimodal_rag/scripts/graph_rag/core.py`、
  `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/kg/snkv_vector_impl.py`、
  `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/utils.py`、
  `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/operate.py`、
  `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/lightrag.py`、
  `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/constants.py`
- 关联评测：`evals/graph_rag/suite.py`

---

## 0. 背景与当前实测状态

经过代码核查，确认 Sherry graph_rag 当前节点召回链路是**纯稠密单路 + LLM 抽关键词 + chunk 末段重排**：

| 召回阶段 | 实际机制 | 使用的存储 | 是否稀疏召回 |
|---|---|---|---|
| 实体检索 | LLM 抽 `hl_keywords`/`ll_keywords` → SNKVVectorStorage.query() 余弦搜索 | `SNKVVectorStorage`（dense, HNSW, cosine） | 否 |
| 关系检索 | 同上，对 `relationships_vdb` 余弦搜索 | `SNKVVectorStorage`（dense） | 否 |
| Chunk 检索 | 对 `chunks_vdb` 余弦搜索 → `process_chunks_unified` 走 rerank | `SNKVVectorStorage`（dense）+ `reranker_model` | 否（chunk 末段才有 rerank） |
| Rerank | **仅作用于 chunk**，**实体/关系不 rerank** | `reranker_model`（BGE-Reranker 类神经重排） | — |

### 0.1 关键事实清单（已核实）

- **SNKVVectorStorage 是 dense-only**：
  - `snkv_vector_impl.py:138`：`UsearchIndex(ndim=self._dim, metric="cos", dtype="f32")`——单 HNSW 索引，单一稠密向量。
  - `_cf_raw`（`vec_raw_{ns}`）只存 `float32` 稠密向量字节，无稀疏列族。
  - `query()`（`:357-400`）走单向量 cosine + 距离阈值 `1 - cosine_better_than_threshold`，无稀疏查询路径。
  - `EmbeddingFunc` 接口签名（`utils.py:473`）只返回单个 `np.ndarray`，无 dense/sparse 双通道概念。
- **`base.py:80` 用的就是 BGE-M3**：`embedding_dim=1024`、`max_token_size=8192`，对得上 BGE-M3 的 dense 输出。但 BGE-M3 原生支持 dense(1024) + sparse + multi-vector(ColBERT) 三路，**Sherry 只取了 dense 一路**——稀疏输出能力被空置。
- **Rerank 只对 chunk 生效**：
  - `utils.py:3318::process_chunks_unified` 是 `apply_rerank_if_enabled`（`:3234`）的唯一调用点。
  - `operate.py:4919`（`query_nodes_by_vector`）和 `:5184`（关系检索）做完余弦搜索直接返回，**实体/关系不经过任何 rerank**。
- **默认阈值非常宽松**（`constants.py` + `lightrag.py:192-216`）：

  | 参数 | 默认值 | 来源 | Sherry 是否覆盖 |
  |---|---|---|---|
  | `TOP_K`（实体/关系召回数） | **40** | `DEFAULT_TOP_K` | 否（`base.py` 未设） |
  | `CHUNK_TOP_K`（最终 chunk 数） | **20** | `DEFAULT_CHUNK_TOP_K` | 否 |
  | `COSINE_THRESHOLD`（余弦阈值） | **0.2** | `DEFAULT_COSINE_THRESHOLD` | 否 |
  | `MIN_RERANK_SCORE` | **0.0**（默认关） | `DEFAULT_MIN_RERANK_SCORE` | 否 |
  | `enable_rerank` | True | `RERANK_BY_DEFAULT=true` | 否 |
  | `RELATED_CHUNK_NUMBER` | 5（系统默认） | `DEFAULT_RELATED_CHUNK_NUMBER` | **是**（`base.py:18` 显式设 5） |

  `base.py:14-18` 只覆盖了 `MAX_SOURCE_IDS_PER_ENTITY=50` / `MAX_SOURCE_IDS_PER_RELATION=50` / `SOURCE_IDS_LIMIT_METHOD=FIFO` / `RELATED_CHUNK_NUMBER=5` 这四项，**所有召回/重排阈值都在跑 LightRAG 默认值**。

### 0.2 暴露的两个真实问题

1. **稀疏侧缺位**：实体名是精确字符串（论文 ID、型号、代码标识符、基因名）时，BGE-M3 稠密向量容易漂到语义近邻而漏掉精确匹配。LLM 抽关键词能补一部分，但 LLM 抽不到的词没有词法兜底。
2. **重排侧覆盖不全 + 阈值默认值过于宽松**：实体/关系召回阶段根本没有重排兜底，且 `cosine_threshold=0.2` 几乎放行一切，`top_k=40` 又把候选池拉到很大——实体召回阶段噪声大、信噪比低。

---

## 1. 任务一：BGE-M3 稀疏输出可行性确认（**先做调研，不直接动代码**）

### 1.1 目标

在不动 SNKVVectorStorage 架构的前提下，先确认两件事：

1. Sherry 现有的 `models.build_embed_model()`（`agent/tools/.../base.py:42-48` 调用的那个）返回的 embedding 模型**是否已经能产出 sparse 输出**？
2. 如果不能，**引入 BGE-M3 原生 sparse 输出**需要的依赖（`FlagEmbedding` / 模型服务端开关）成本多大？

### 1.2 调研步骤

- [ ] **1.2.1** 打开 `models/LLMs/embed.py`（或 `build_embed_model` 所在文件），看 embed 模型的 provider 配置：
  - 是本地 `FlagEmbedding` 实例？还是走 HTTP 服务？
  - 服务端是否暴露 sparse 字段（BGE-M3 的 sparse 是 token 级权重字典，HTTP 服务通常返回 `{dense: [...], sparse: {token_id: weight}}`）？
- [ ] **1.2.2** 翻 `.models.example`，看 embedding 配置项里有没有 sparse 相关字段。
- [ ] **1.2.3** 如果 Sherry 用的是 HTTP 服务且服务端支持 sparse，**几乎零成本**——只需在 `base.py::_embedding_func` 里多返回一个 sparse 字段，然后让上层 EmbeddingFunc 把 dense/sparse 分流。
- [ ] **1.2.4** 如果需要本地装 `FlagEmbedding`，评估：
  - 包体积（`flag_embedding` 拉 PyTorch + 模型权重，约几百 MiB）
  - 与现有 `models.build_embed_model()` 的并存方式（共用模型实例，还是开第二个实例）
  - 是否破坏 Sherry "桌面打包 ~1 GiB" 的体积红线（`AGENTS.md` 提过 backend tree ~1 GiB 上限）

### 1.3 验收标准

调研结论输出一份表，列出三种走向及对应的实现成本：

| 走向 | 实现成本 | 改动面 |
|---|---|---|
| A：现有 embed 服务已支持 sparse | 低 | `base.py::_embedding_func` 多返回一路 |
| B：本地装 `FlagEmbedding` 走 sparse | 中 | 增依赖 + 第二个模型实例 |
| C：现有模型不支持且不能加依赖 | 高 | 放弃 BGE-M3 sparse，回到经典 BM25 路线（见 §3 备选） |

> **决策点**：只有走向 A 或 B，才进入 §2 的 SNKV 改造；走向 C 则转入 §3 备选方案。

---

## 2. 任务二：SNKVVectorStorage 增加 sparse 双路召回（依赖 §1 调研结论为 A/B）

### 2.1 目标

在 `SNKVVectorStorage` 上增加**稀疏倒排索引**（term → entity_id 列表 + IDF/权重），与现有 dense HNSW 并列：

- `upsert`：dense 向量进 `vec_raw_{ns}`（不变），sparse 权重进新列族 `vec_sparse_{ns}`。
- `query`：dense 路走 HNSW cosine（不变），sparse 路走倒排表加权打分，两路结果融合。

### 2.2 改动清单

- [ ] **2.2.1** `snkv_vector_impl.py`：新增 `_cf_sparse_name = f"vec_sparse_{ns}"` 列族，存倒排表。
- [ ] **2.2.2** `_open_store` / `_close_store`：加 sparse 列族的 open/close。新增 sparse 索引 sidecar `snkv_vec.{ns}.sparse.bin`（mmap 加载，避免每次启动扫全表重建）。
- [ ] **2.2.3** `upsert`：embedding_func 现在返回 `(dense, sparse)` 二元组——稀疏部分写入倒排表。**存储格式必须用 binary packed，不要用 JSON**：
  - Key：`token_id`（uint32，4 字节）
  - Value：packed bytes —— `count(uint32) + N × (entity_id_int64 + weight_float32)`，12 字节/项
  - 理由：JSON 包装会让单条倒排项从 12 字节膨胀到 50+ 字节，**白白丢掉 sparse 本该有的 3× 体积优势**（详见 §2.5 磁盘/内存预算）。**注意 backward compat**：老数据没有 sparse，需要降级路径（dense-only 查询）。
- [ ] **2.2.4** `query`：稠密路保留现状；新增稀疏路打分；两路结果按 **BGE-M3 官方 `weighted_score`** 融合，**不用 RRF**。理由见 §2.6：
  - 优先路径：直接调 `BGEM3FlagModel.weighted_score(weights_for_different_modes=OFFICIAL_WEIGHTS)`
  - 降级路径：手动做 weighted normalized sum（`α * dense_norm + β * sparse_norm`，权重见 §2.6.4 待定）
  - 稀疏路按需加载——只读 query 命中 token 的 posting list，稳态内存可比 dense 低。
- [ ] **2.2.5** `base.py::_embedding_func`：返回 `(np.ndarray_dense, dict_sparse)`，调整 `EmbeddingFunc` 的声明以支持双输出（可能要在 `EmbeddingFunc` 加 `sparse_dim` 或 `output_schema` 字段，**这一步会动到 vendored_lightrag/utils.py 的 EmbeddingFunc 类**——需要回归测试所有用到它的地方）。
- [ ] **2.2.6** `kg/__init__.py`：注册路径不变（SNKV 仍是原生注册），但要在 docstring 里更新 SNKVVectorStorage 的双路说明。

### 2.3 风险与约束

- **改动面跨 vendored 包**：`snkv_vector_impl.py`、`utils.py::EmbeddingFunc`、`base.py`、`operate.py`（query 调用点）四处。`vendored_*` 的下一次升级会冲突——在改动处加 `# SHERRY-PATCH: ...` 注释方便 rebase。
- **向后兼容**：老 `vec_raw_{ns}` 数据不能丢。新增 sparse 列族允许不存在（空索引 → sparse 路返回空 → 退化成纯 dense）。
- **EmbeddingFunc 接口变更**：影响所有 storage backend。需要回归 `tests/agent/tools/` 下涉及 embedding 的测试和 `evals/graph_rag/suite.py`。
- **倒排表膨胀**：BGE-M3 sparse 输出是 token 级权重，每个 entity 可能产生几百个非零 token。`MAX_SOURCE_IDS_PER_ENTITY=50` 已经控制了 entity 数量，但 sparse 表大小仍要监控——加 `MAX_SPARSE_TOKENS_PER_ENTITY` 上限（建议 256）。
- **Embedding 计算变重**：BGE-M3 输出 dense + sparse 双路比单 dense 慢 2~3 倍，**写入侧延迟**上升——这不是磁盘/内存问题，是索引阶段 CPU/GPU 开销问题。批量索引时尤其要观察。

### 2.4 验收标准

- [ ] `evals/graph_rag/suite.py` 跑前后对比，**pass rate 不下降**且精确实体名查询场景的召回率提升 ≥10%。
- [ ] **专有名词 fixture 按两类分别统计召回率**（关键：BGE-M3 sparse 对两类专有名词的解决能力不同，必须分开看）：
  - **A 类（训练分布内的常见专有名词）**：模型名（Falcon/BERT/GPT-4）、论文标题、作者名、多语言人名/角色名（橘雪莉/JuXueLi）、技术术语。**预期：召回率提升 ≥15%**，BGE-M3 sparse 在这类上有效。
  - **B 类（训练分布外的稀有 ID/版本串）**：自定义项目代号（`project_aurora`）、内部模型版本（`XJ-7B-v2.1`）、arXiv ID、commit hash、版本号（`v2.1.3-rc4`）。**预期：召回率提升 <5%，甚至无提升**——BGE-M3 sparse 对这类只能给 subword 级弱信号，IDF 自适应缺失。
- [ ] **决策点**：如果 B 类召回率 <30%，触发 §5 备选方案（BM25 via SQLite FTS5 作为退化兜底，仅覆盖正则识别出的 ID/版本串类查询，不做全量 BM25）。
- [ ] 双路查询的 p95 延迟相比单路 dense 增加不超过 30%（稀疏路走内存倒排表，应该很快）。
- [ ] 老数据（无 sparse）加载不报错，查询走 dense-only 退化路径。
- [ ] 索引阶段吞吐下降不超过 50%（BGE-M3 双路输出的预期代价）。

> **BGE-M3 sparse 对专有名词的能力边界**（已核实）：
> - 训练时权重固化，无法感知语料时稀有度——这是和 BM25 的本质区别。
> - 对训练分布内的常见专有名词（含多语言）：有效。
> - 对训练分布外的稀有 ID/版本串：几乎无效，只能给 subword 级弱信号。
> - 估计覆盖 Sherry 实际查询 80%+ 的专有名词场景；剩余 20% 需要 BM25 兜底（§5）。

### 2.5 磁盘与内存预算

> **结论：sparse 不会激增磁盘/内存占用，前提是存储格式用 binary packed 而非 JSON。**

#### 2.5.1 单条记录体积对比

BGE-M3 一对输出的原始体积：

| 通道 | 单实体原始体积 | 说明 |
|---|---|---|
| Dense | **4 KiB** | 1024 × float32，固定长度 |
| Sparse | **~1.2 KiB** | 平均 ~150 个非零 token × (uint32 token_id + float32 weight) = 8 字节/项 |
| Multi-vector (ColBERT) | ~50 KiB | 暂不启用 |

单看原始数据，**sparse 比 dense 还小 3 倍**——dense 是 1024 维定长，sparse 只存非零项，BGE-M3 的学习权重会自动给停用词低权重，非零项数有界。

#### 2.5.2 总占用规模估算（按 Sherry 个人知识库实际规模）

每条倒排项 12 字节（int64 entity_id + float32 weight），总条目数 = `N_entities × avg_nonzero_per_entity`（按 150 估）：

| 实体规模 | Dense 磁盘 | Sparse 磁盘（binary packed） | Sparse 磁盘（**JSON 方案，错误**） | Sparse / Dense |
|---|---|---|---|---|
| 1k | 4 MB | 1.2 MB | ~4 MB | 0.3× / 1× |
| 10k | 40 MB | 12 MB | ~40 MB | 0.3× / 1× |
| 50k | 200 MB | 60 MB | ~200 MB | 0.3× / 1× |
| 100k（上限场景） | 400 MB | 120 MB | ~400 MB | 0.3× / 1× |

> JSON 方案会把体积从 **0.3× dense 膨胀到 1× dense**——白白丢掉 sparse 本该有的 3× 体积优势。这是 §2.2.3 强制 binary packed 的原因。

#### 2.5.3 内存对比

| 维度 | Dense（HNSW） | Sparse（倒排） |
|---|---|---|
| 启动加载 | **全量入内存**（`snkv_vector_impl.py:138` 的 `UsearchIndex` 一启动就把所有向量 + 图结构装进内存；sidecar `.usearch` 是 mmap 加速，但仍占虚拟地址空间） | **按需加载**（查询时只取 query 命中 token 的 posting list） |
| 稳态常驻 | ~dense 总量 + HNSW 图结构（约 1.2× dense） | 仅查询相关 token 的倒排链，几 KB ~ 几十 KB |
| 冷启动重建 | sidecar 命中则 <1s | 需要建 sparse sidecar（§2.2.2 的 `snkv_vec.{ns}.sparse.bin`），否则每次启动扫全表重建 |

**Sparse 稳态内存反而比 dense 低**——因为 dense 的 HNSW 必须全量驻留，sparse 倒排天然支持部分加载。

#### 2.5.4 真正的增长点（不是磁盘也不是内存）

1. **Embedding 计算吞吐下降 2~3 倍**：BGE-M3 输出 dense + sparse 双路比单 dense 慢。索引阶段开销，不在 §2.4 验收标准的磁盘/内存范围内，但要监控。
2. **Vocabulary 长尾的边角开销**：极罕见的 token 仍要占一个倒排链表头（哪怕只有一个 entity）。微乎其微，不是真问题。
3. **Sidecar 文件**：dense 有 `.usearch`，sparse 需要等价的 `.sparse.bin`。多一个文件，但体积和列族相当。

### 2.6 融合策略：BGE-M3 官方 weighted_score，不用 RRF

> **结论**：BGE-M3 dense + sparse 融合应该用官方推荐的 weighted sum（`BGEM3FlagModel.weighted_score`），RRF 作为降级兜底。

#### 2.6.1 为什么不用 RRF

RRF 是经典 BM25 + dense 混合检索的事实标准，因为那两路分数根本不可比（BM25 无界，dense cosine 在 [-1,1]）。但 BGE-M3 dense + sparse 不一样：

| 维度 | 经典 BM25 + Dense | BGE-M3 Dense + Sparse |
|---|---|---|
| 来源模型 | 两个独立模型/方法 | **同一个 BGE-M3 模型联合训练** |
| 分数标定 | 各自标定，不可比 | 联合训练，有相对意义 |
| 官方推荐融合方式 | RRF 或 weighted sum | **Weighted sum**（`BGEM3FlagModel.weighted_score`，作者调过权重） |
| 分数 magnitude 是否携带信息 | 弱 | **强**——稀疏 token 数 × 权重直接反映匹配强度 |

RRF 的 `1/(60+rank)` 把 BGE-M3 联合训练的标定信息丢掉了：一个实体命中 50 个高权重 sparse token vs 命中 3 个，RRF 只看到 rank——这俩 magnitude 差异被抹平。

#### 2.6.2 Sherry 用例的关键不对称

GraphRAG 查询分布是**不对称**的，RRF 的等权融合做不到 per-query 自适应：

- **非专有名词查询**（"解释 mixture-of-experts 原理"）：dense 已经够用，sparse 加进来是噪声——dense 应主导
- **专有名词查询**（"JuXueLi 和 YuanYe HanNa 的关系"）：sparse 是补丁——sparse 应主导

RRF 的"民主"反而稀释了 dense 在通用查询上的优势。官方 weighted sum 通过 α 偏向 dense，让 sparse 只在真正命中的时候起作用。

#### 2.6.3 推荐方案：直接走官方 `weighted_score`

**优先路径**：如果 §1 调研发现 `models.build_embed_model()` 返回的对象是 BGE-M3 原生封装（`BGEM3FlagModel` 或同 API），**直接调用官方的 `weighted_score` 方法**，把官方默认权重当参数传入：

```python
# 伪代码，实际 API 以 BGEM3FlagModel.weighted_score 签名为准
fused = embed_model.weighted_score(
    input_query,
    weights_for_different_modes=OFFICIAL_WEIGHTS,  # 见 §2.6.4
)
```

**降级路径**：如果是 HTTP 服务封装、接口不暴露 `weighted_score`，则拿三路输出**手动做 weighted normalized sum**：

```python
# Dense: cosine ∈ [-1, 1] → 归一化到 [0, 1]
dense_norm = (cosine + 1) / 2

# Sparse: 无界 → 用候选集中最大值归一
sparse_max = max(s.score for s in candidates) or 1.0
sparse_norm = s.score / sparse_max

# Weighted fusion（权重见 §2.6.4）
final_score = w_dense * dense_norm + w_sparse * sparse_norm
```

#### 2.6.4 ⚠️ 权重数值：**待定**——必须从官方源码核实后填入，不允许凭记忆硬编码

> **官方默认权重数值：`[待定]`**
>
> 实施时按 §2.6.4.1 流程从 FlagEmbedding 源码核实填入。在核实完成前，**代码里不允许出现任何具体权重数值**（包括本 TODO 草稿里曾经出现的 `dense=1.0, sparse=0.3, colbert=1.0` —— 那是从记忆里调的未核实值，已删除）。

**实施前置条件**：在动 §2 代码之前，必须完成以下核实：

- [ ] **2.6.4.1** 联网/本地拉取 FlagEmbedding 官方仓库或安装包源码（`pip show FlagEmbedding` 找到包路径），打开 `BGEM3FlagModel.weighted_score` 方法实现，**抄出 `weights_for_different_modes` 的官方默认值**——以源码为准。
- [ ] **2.6.4.2** 把核实后的权重数值写进代码，并在注释里写明来源（仓库 URL + 文件路径 + commit hash 或版本号 + 抄录时的日期）。
- [ ] **2.6.4.3** 如果 §1 调研发现 `models.build_embed_model()` 返回的对象直接暴露 `weighted_score`，**直接调官方 API**，跳过手动加权计算（路径 A）——此时权重作为参数由官方 API 内部使用，不需要硬编码。
- [ ] **2.6.4.4** 如果只能拿到三路原始输出（路径 B），手动加权时**用 §2.6.4.1 核实到的官方默认权重**，不要自己再调一遍——除非 evals 显示官方权重在 Sherry 数据分布上明显次优。

> **决策依据**：作者在 BGE-M3 论文和仓库 README 里调过的权重，应该比我们从零调参更可靠。除非有明确证据证明在 Sherry 数据分布上不合适，否则**不自行调参**，保持官方默认。

#### 2.6.5 何时降级回 RRF

- 实测发现 BGE-M3 sparse 分数分布异常（magnitude 与真实相关性弱相关）——这时 RRF 反而更鲁棒
- 想零调参 ship-and-go，不在乎精度损失
- §1 调研结论是"模型完全不支持 sparse 输出"，只能走经典 BM25 路线（§5），那时 RRF 才是混合 BM25 + dense 的正确选择

#### 2.6.6 后续演进路径（仅在 evals 显示官方权重次优时考虑）

weighted sum 后面如果要做 **per-query 自适应**（比如检测到专有名词密集查询就降 w_dense、纯语义查询就拉满 w_dense=1.0），改造路径顺——直接根据 query 特征动态算权重。RRF 改自适应要变成 weighted RRF，工程上更别扭。但**这一节仅在 §2.4 evals 显示官方默认权重在 Sherry 数据分布上次优时才启动**，默认不动。

---

## 3. 任务三：重排阈值与 top_k 调优（**与 §1/§2 并行可做，不依赖稀疏召回**）

### 3.1 目标

在不动代码的前提下，先通过 `os.environ.setdefault` 调整 `base.py:14-18` 那块环境变量，验证以下假设：

1. `TOP_K=40` 对实体/关系召回**太大**，稀释信号，调到 **15~20** 后信噪比提升。
2. `COSINE_THRESHOLD=0.2` **太宽松**，调到 **0.4** 过滤掉近无关实体。
3. `MIN_RERANK_SCORE=0.0`（默认关）应该开启，设到 **0.5**——chunk 末段重排后过滤掉弱相关项。
4. `CHUNK_TOP_K=20` 可能偏大，调到 **12** 看上下文精简后回答质量是否反而提升。

### 3.2 改动清单

**第一阶段：纯环境变量调优（零代码改动）**

- [ ] **3.2.1** 修改 `base.py:14-18`，在现有 `os.environ.setdefault` 块里加四行：

  ```python
  os.environ.setdefault("MAX_SOURCE_IDS_PER_ENTITY", "50")
  os.environ.setdefault("MAX_SOURCE_IDS_PER_RELATION", "50")
  os.environ.setdefault("SOURCE_IDS_LIMIT_METHOD", "FIFO")
  os.environ.setdefault("RELATED_CHUNK_NUMBER", "5")
  # 新增 ↓
  os.environ.setdefault("TOP_K", "20")              # 默认 40 → 20
  os.environ.setdefault("CHUNK_TOP_K", "12")        # 默认 20 → 12
  os.environ.setdefault("COSINE_THRESHOLD", "0.4")  # 默认 0.2 → 0.4
  os.environ.setdefault("MIN_RERANK_SCORE", "0.5")  # 默认 0.0 → 0.5
  ```

- [ ] **3.2.2** 跑 `evals/graph_rag/suite.py` 基线（不改任何东西），记录 pass rate + 平均回答质量分。
- [ ] **3.2.3** 改环境变量后重跑 evals，对比。
- [ ] **3.2.4** 如果调优后某些 query 召回数过少（`<3`），考虑放宽 `COSINE_THRESHOLD` 到 0.3 或 `TOP_K` 到 25。

**第二阶段（可选，依赖第一阶段效果）：给实体/关系召回加 rerank**

第一阶段做完后，如果发现 `TOP_K=15` + `COSINE_THRESHOLD=0.4` 后实体召回精度仍不够，再考虑这个改动——给实体/关系也加一层 rerank 兜底。

- [ ] **3.2.5** 在 `operate.py:4919` 的 `query_nodes_by_vector` 返回后，加一层 `apply_rerank_if_enabled(query, entities, global_config, enable_rerank=query_param.enable_rerank, top_n=query_param.top_k // 2)`。
- [ ] **3.2.6** 同样给 `operate.py:5184` 的关系召回加 rerank。
- [ ] **3.2.7** 注意：实体/关系的"文档文本"是 `entity_name + description`，rerank 函数需要拿到合并后的文本——`utils.py:3270-3276` 的 fallback 链已经处理 `content`/`text`/`chunk_content` 字段，确认实体 dict 里有这些字段之一。

### 3.3 风险与约束

- **`setdefault` 的覆盖语义**：如果 `.env` 或启动环境已经显式设了 `TOP_K`，`setdefault` 不会覆盖。这是对的——保留用户显式配置。但 evals 跑的时候要确认环境干净。
- **`MIN_RERANK_SCORE=0.5` 的实际行为**：`utils.py:3358` 的代码是 `global_config.get("min_rerank_score", 0.5)`——**键不存在时 fallback 是 0.5，但 LightRAG 默认值是 0.0**。一旦 `base.py` 把环境变量设了，`global_config["min_rerank_score"]` 就会读到 0.5，过滤启用。需要确认这个 fallback 不会产生意外行为。
- **chunk_top_k 调小风险**：如果回答依赖多个 chunk 拼接，调到 12 可能漏关键证据。第一阶段建议先调到 15，观察后再降到 12。
- **实体/关系加 rerank 的开销**：rerank 是 cross-encoder，每次 query 多两次 rerank 调用（实体 + 关系），延迟增加。`rerank_model_max_async` 控制并发，`default_rerank_timeout=30s` 是上限。

### 3.4 验收标准

- [ ] `evals/graph_rag/suite.py` pass rate 不下降。
- [ ] 人工抽 10 个 query，对比 top_k=40 vs top_k=20 的实体召回质量（_precision@10_）。
- [ ] 启用 `MIN_RERANK_SCORE=0.5` 后，回答中出现"无关上下文"的比例下降。
- [ ] p95 查询延迟不显著增加（第一阶段应该更快，因为候选池变小）。

---

## 4. 执行顺序与依赖

```
§1 调研（BGE-M3 sparse 可行性）
  │
  ├─ 走向 A/B → §2 SNKV 双路改造（大改动，2~3 天）
  │              │
  │              └─ 与 §3 并行
  │
  └─ 走向 C → §3 完成后，回到经典 BM25 路线评估（另起一个 TODO）

§3 重排阈值调优（零/小改动，0.5~1 天，**优先做，风险低收益快**）
```

**建议执行顺序**：

1. **先做 §3 第一阶段**（环境变量调优）——零代码改动，半天能出对比数据。
2. **并行做 §1 调研**——确认 BGE-M3 sparse 走向。
3. 根据 §1 结论决定是否做 §2（大改动）。
4. §3 第二阶段（实体/关系加 rerank）作为 §2 的兜底——如果 §2 完成后实体召回精度已经够，可以不做。

---

## 5. 备选方案：SQLite FTS5 倒排索引兜底（触发条件：§1 走向 C，或 §2.4 决策点判定需要）

> **型号已定：SQLite FTS5**。理由见 §5.1 选型对比。

### 5.1 选型对比

| 型号 | 外部依赖 | 内置 BM25 | 中文分词 | 跨平台 | 评估 |
|---|---|---|---|---|---|
| **SQLite FTS5** | 无（SQLite 已在用） | **是**（FTS5 默认 BM25 排序） | 需外挂 jieba | 是 | **首选** |
| `bm25s` / `rank_bm25` | 纯 Python 包 | 算法实现 | 需外挂 jieba | 是 | 备选，要自处理持久化 |
| Tantivy（`tantivy-py`） | Rust 编译依赖 | 是 | 需配置 | 是 | 性能好但打包噩梦，放弃 |
| Elasticsearch / OpenSearch | JVM + 外部服务 | 是 | 内置 IK | 是 | 违反无外部 DB 原则，放弃 |
| Whoosh | 纯 Python | 是 | 内置但弱 | 是 | 维护停滞，放弃 |
| Qdrant hybrid | 外部服务 | 是（sparse 模式） | — | 是 | 同上，违反原则 |

**FTS5 胜出关键**：SQLite 的扩展模块，Python 标准库 `sqlite3` 直接支持。Sherry 已在用 SQLite（`snkv_vec.db`、`snkv_kv` 都是 SQLite 衍生），加一个 FTS5 表**零新依赖**。

### 5.2 FTS5 关键细节

#### 5.2.1 BM25 不用自己实现

FTS5 默认排序就是 BM25，`bm25(table)` 函数调出分数：

```sql
SELECT rowid, bm25(fts_entity) AS score
FROM fts_entity
WHERE fts_entity MATCH ?
ORDER BY score ASC   -- 注意 FTS5 的 bm25 是越负越好，ASC 取最高相关
LIMIT ?;
```

#### 5.2.2 中文分词外挂 jieba（FTS5 自带 tokenizer 都不友好）

FTS5 自带的 `simple`（按空白切）/`unicode61`（按字符切，粒度太细）/`trigram`（三字滑窗，召回好但精度差）对中文都不合适。解法：

- **建表**：用 `tokenize='simple'`（FTS5 内置），但实际存进去的是 jieba 分词后空格连接的字符串
- **写入**：`" ".join(jieba.cut(text))`，让 FTS5 看到的是"已经是英文形式"的 token 流
- **查询**：同样 jieba 分词后拼 MATCH 表达式
- **词典对齐**：`jieba.load_userdict(...)` 把已入库的实体名喂进去——不然 `JuXueLi` 会被切碎，BM25 反而失效

```python
import jieba

# 写入
text = "橘雪莉是 EMA Agent 的角色"
tokenized = " ".join(jieba.cut(text))   # "橘 雪莉 是 EMA Agent 的 角色"
cursor.execute(
    "INSERT INTO fts_entity(rowid, content) VALUES (?, ?)",
    (entity_int_id, tokenized),
)

# 查询
query_tokenized = " ".join(jieba.cut(user_query))
cursor.execute(
    "SELECT rowid, bm25(fts_entity) AS score FROM fts_entity "
    "WHERE fts_entity MATCH ? ORDER BY score ASC LIMIT ?",
    (query_tokenized, top_k),
)
```

#### 5.2.3 触发条件（不跑全量，按需走）

BM25 兜底**不每次查询都触发**，否则违背"Sparse 已经覆盖 80%+ 专有名词"的前提。只在 query 被正则识别为"ID/版本串类"时额外走一次：

```python
import re

ID_PATTERN = re.compile(
    r"(?:v\d+\.\d+[\.\d\-rc]*"     # v2.1.3-rc4
    r"|arxiv:\d{4}\.\d+"            # arXiv:2401.12345
    r"|0x[0-9a-f]{8,}"              # 0xabc123ef
    r"|[a-f0-9]{40,}"               # sha-like
    r"|[A-Z]{2,}-\d+[\.\d]*"        # XJ-7B-v2.1
    r")",
    re.IGNORECASE,
)

def should_trigger_bm25_fallback(query: str) -> bool:
    return bool(ID_PATTERN.search(query))
```

### 5.3 融合策略：第三路合并用 RRF（与 §2.6 不矛盾）

- **主召回内部**（BGE-M3 dense + sparse）：用 §2.6 的 `weighted_score`——同模型可比，不用 RRF。
- **BM25 兜底作为第三路**：BM25 分数无界，与 BGE-M3 `weighted_score` 不可比——**这时 RRF 是对的**。

```
主召回：weighted_score(dense, sparse) → 排序 → rank_main
兜底召回：FTS5 BM25 → 排序 → rank_bm25   (仅 §5.2.3 触发条件命中)
最终融合：score = 1/(60 + rank_main) + 1/(60 + rank_bm25)
```

> 这与 §2.6 说"不用 RRF"不矛盾：§2.6 是说 BGE-M3 内部两路不用 RRF；BM25 作为外部第三路，分数不可比，RRF 是正确选择。

### 5.4 工程量与验收

- **工程量**：~1 天（建 FTS5 表 + jieba 分词写入 + 触发条件 + RRF 合并），比 §2 BGE-M3 sparse 改造省事
- **效果预期**：通常不如 BGE-M3 的"学习型 sparse"对训练分布内专有名词好，但**对训练分布外的稀有 ID/版本串严格更强**（IDF 自适应）
- **验收**：§2.4 的 B 类 fixture（稀有 ID/版本串）召回率从 <30% 提升到 ≥60%

### 5.5 触发时机

**当前 TODO 不实施 §5**，触发条件：
1. §1 调研结论是"模型完全不支持 sparse 输出"（走向 C）—— 直接转 §5 为主路径
2. §2 实施后 evals 显示 B 类 fixture（稀有 ID）召回率 <30%——作为 §2 的补丁加上

单独立项时展开实施细节。

---

## 6. 参考链接

- LightRAG hybrid 模式定义：`vendored_lightrag/lightrag.py:2005`（local + global round-robin，**不是** vector + BM25）
- 实体检索入口：`vendored_lightrag/operate.py:4915-4919`
- 关系检索入口：`vendored_lightrag/operate.py:5180-5184`
- Chunk 重排入口：`vendored_lightrag/utils.py:3318-3385`
- Rerank 实现：`vendored_lightrag/utils.py:3234-3316`
- SNKV 向量存储：`vendored_lightrag/kg/snkv_vector_impl.py`
- EmbeddingFunc 类：`vendored_lightrag/utils.py:473-590`
- 默认常量：`vendored_lightrag/constants.py:52-63, 270-271`
- Sherry 接入点：`skills/builtin/core/multimodal_rag/scripts/graph_rag/base.py:14-18, 42-68, 71-94`
