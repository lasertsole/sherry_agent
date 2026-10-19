# GraphRAG 路径查询

`multimodal_rag` 技能的 LightRAG 引擎按向量相似度召回实体与关系，再把多跳拼接
留给 LLM。本页记录在该召回之上新增的显式路径层、随之落地的召回阈值调优，以及
关于稀疏召回的调研结论（附改动前后的实测数据）。

## 缺口

`vendored_lightrag` 没有任何路径算法：`query_nodes_by_vector` 与关系召回各自做
一次向量搜索（默认 `top_k=40`）后直接返回。中间桥接实体一旦落在 top-k 之外，
即使图里确实存在这条链路，"A 和 B 什么关系"也无法通过遍历回答——所谓"多跳"
只是 LLM 在候选集合上做推理的概率事件。

实体向量是纯稠密的（`BGE-M3`，1024 维）。vendored 引擎里没有稀疏通道，也没有
BM25 兜底。

## 路径层

`skills/builtin/core/multimodal_rag/scripts/graph_rag/path_search.py` 属于技能层
（vendored 树只在 `operate.py` 增加一个分发钩子）：

- **端点解析**——按成本从低到高三遍：图节点精确名 → 图标签包含匹配
  （`Kepler-7 launch pad` → `Kepler-7 Launch Pad`）→ 以
  `compute_mdhash_id(..., prefix="ent-")` 为键的实体向量召回。
- **BFS**——枚举解析出的端点之间的全部简单路径，最多 `max_depth=3` 跳，按
  `(hops, -average edge weight)` 排序并截断到 `top_k_paths=10`。路径内不重复节点；全局
  `max_visit_nodes` 预算与每层 `max_frontier` 上限约束稠密图的开销。
- **Beam 兜底**——仅在 BFS 一无所获时运行：双向、embedding 引导的 beam
  （`beam_width=5`、`beam_max_hops=5`），按"到对侧端点的余弦相似度 +
  `0.2 × edge weight`"给邻居打分，在第一个相遇节点停止。向量来自
  `SNKVVectorStorage.get_vectors_by_ids`。
- **格式化**——LLM 提示词拿到显式链路，外加每个节点、每条边的描述与所经边的
  源 chunk；结构化载荷（`entities` / `relationships` / `chunks` /
  `references`）合并进正常召回数据，并附带 `path_info` 元数据块。

两个入口会用到它：

- `mode="path"` 只跑路径搜索；找不到路径时退回 `hybrid` 召回，因此该模式不会
  用空上下文作答。
- 读起来像关系型问题的 `mix` / `hybrid` 查询（中、英、日、韩四语模式；
  `is_relationship_query`）会自动把路径段合并进上下文。

所有数值都放在
`config/features/agent_side/graph_rag_path.py::GRAPH_RAG_PATH`。

## 召回阈值

LightRAG 的默认值几乎放行一切：`TOP_K=40`、`CHUNK_TOP_K=20`、
`COSINE_THRESHOLD=0.2`，且 `MIN_RERANK_SCORE` 关闭。调优后的默认值是
`TOP_K=20`、`CHUNK_TOP_K=12`、`COSINE_THRESHOLD=0.4` 与
`MIN_RERANK_SCORE=0.5`，外加图规模上限
`MAX_SOURCE_IDS_PER_ENTITY=50`、`MAX_SOURCE_IDS_PER_RELATION=50`、
`SOURCE_IDS_LIMIT_METHOD=FIFO` 和 `RELATED_CHUNK_NUMBER=5`。

这些变量在 `graph_rag/__init__.py` 里、**先于** vendored 包导入时设置：
`QueryParam` 与 LightRAG dataclass 会在导入时把环境变量读成类级默认值，旧位置
（`base.py` 里自己的 LightRAG 导入之后）实际上从未生效。

## 稀疏召回：为什么没有实现

计划的稀疏通道被 embedding 层挡住。
`models/embed_model/text/core.py::CustomEmbedding` 只产出一个稠密向量——本地走
llama.cpp `create_embedding`，远程走 OpenAI 兼容的 `/embeddings` 端点。
`SNKVVectorStorage` 只有单路 HNSW 余弦索引，`EmbeddingFunc` 只返回一个
`ndarray`。要用 BGE-M3 原生稀疏输出，需要 `FlagEmbedding`（PyTorch 加几百
MiB）或一个支持稀疏的端点；两者都不适合桌面打包。因此稀有 ID/版本串的召回
仍然开放，SQLite FTS5/BM25 是记录在案的未来选项，而不是双路稀疏索引。

## 实测效果

`evals/graph_rag/multihop_bench.py` 索引一个带近似名干扰项的 20 文档语料
（102 个节点 / 106 条边），考察 6 个问题：4 个最短链路为 2-5 跳的关系问题，
外加 2 个单跳对照问题。两次运行查询的是**同一个**索引。

| 指标（多跳问题） | 改前 `mix` | 改后 `mix` | 改后 `mode="path"` |
| :--------------- | :--------- | :--------- | :----------------- |
| 上下文桥接召回   | 1.000      | 1.000      | 1.000              |
| 回答桥接召回     | 0.875      | 1.000      | 1.000              |
| 回答包含全部桥接 | 0.750      | 1.000      | 1.000              |
| 上下文总字符数   | 155,720    | 87,351     | 23,834             |
| 单跳对照准确率   | 1.000      | 1.000      | 1.000              |

上下文体积在 `mix` 下下降 `44%`、在 `mode="path"` 下下降 `85%`，同时回答桥接
召回从 `0.875` 升到 `1.000`。同一张图上的路径延迟为每次查询 `2-13 ms`
（BFS `2-5 ms`，beam `11-13 ms`），远低于 BFS `500 ms`、beam `2 s` 的目标。
beam 找到了 3 跳 BFS 找不到的 4、5 跳链路，因此它作为兜底默认开启。

单跳 RAGAS 套件（`evals/results/graph_rag/20261019_233415`）的 faithfulness、
context recall 与 context precision 保持 `1.000`；answer relevancy 为 `0.916`，
两次改前运行分别是 `0.963` 与 `0.934`——落在运行间波动范围内，正确性指标不变。

结论：优于改动前的流水线，两份 GraphRAG 计划文档已删除。

## 测试

```bash
uv run --no-sync pytest tests/skills/builtin/core/multimodal_rag/test_path_search.py \
  tests/skills/builtin/core/multimodal_rag/test_path_query_integration.py -q
uv run --no-sync python evals/graph_rag/multihop_bench.py --modes mix,path
uv run --group evals python evals/evals.py graph_rag
```

单元测试固定了跳数保证、防环、路径上限、标签与向量端点解析以及 beam 兜底；
集成测试在真实 SNKV 存储上、不调用 LLM 地跑 `mode="path"` 与 `mix` 自动合并。
RAGAS 套件仍是单跳回答质量的回归门。
