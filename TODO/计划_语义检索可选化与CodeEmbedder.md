# 计划：code-intel 语义检索可选化（仅 CodeEmbedder；Jina v2 Base Code ONNX）

- 建档日期：2026-10-18
- 状态：**计划（未开工；Qwen3 已否决，Jina v2 Base Code ONNX spike 通过，待实现）**
- 用户前提（硬边界）：
  - **BGE 只用于文本语义**：`context_engine/embeddings`、multimodal RAG、通用文本检索继续用 BGE。
  - **code-intel 的语义层不使用 BGE**；没有 CodeEmbedder 时关闭；有 CodeEmbedder 且配置/许可/探针通过时才启用。
- 相关代码：
  - `agent/tools/code_intel/semantic/{search,indexer,chunker}.py`
  - `agent/tools/code_intel/tools.py::build_code_intel_tools`
  - `config/features/agent_side/code_intel_semantic.py`
  - `models/embed_model/core.py`
  - `docs/code-intel/README{,.zh,.ja,.ko}.md`

---

## 0. 结论先行

1. **选 Jina Embeddings v2 Base Code（ONNX）作为 codeEmbedder 候选。**
   Apache-2.0、161M、768 维、8k context、`onnx/model.onnx` 612 MB；在纯代码 corpus 上
   英文 hit@1 **0.80** / hit@3 0.90 / MRR 0.857，中文 hit@1 **0.70** / hit@3 0.80 /
   MRR 0.765，明显高于 BGE 和 Qwen3。
2. **Qwen3-Embedding-0.6B 否决。** 它是通用模型，不是 code-specific；per-text 纯代码
   en 0.30/0.60/0.493、zh 0.20/0.40/0.372，中文退化；batch 的 last-token pooling 还是
   padding 陷阱（160 个 embedding 49 分钟）。
3. **ONNX 路线打通了 transformers 5 的冲突。** `RobertaTokenizerFast` 在 transformers 5
   下可用（不需要 `trust_remote_code`），ONNX 输出与 torch 版逐条一致；只需加
   `onnxruntime`，**不降级 transformers**、不加载远程模型代码。
4. **code-intel 语义层仍然可选。** `enabled="auto"`：没有合法 CodeEmbedder 时不注册
   `semantic_code_search`、不下载模型、不建索引；BGE 只服务文本语义。
5. **一次只用一个 CodeEmbedder。** 旧 `code_embeddings.model = "bge-m3"` 行在升级时删除；
   切换 Jina/其它模型时清空重建，不混向量。

---

## 1. 现状与问题

当前行为：

- `build_code_intel_tools()` 对 `CODE_INTEL_ROLES`（researcher + librarian）无条件追加
  `semantic_code_search`；`SemanticSearch` 构造本身不加载模型，第一次调用才建索引/下载。
- `bge-m3` 本地 GGUF（606 MB）是唯一 embedder；`build_embed_model()` 还被
  `context_engine/embeddings/indexer.py`、`skills/.../graph_rag/base.py` 使用。
- `code_embeddings` 表已有 `model` 列，`_invalidate_on_model_change()` 在模型变化时失效重建，
  但没有“是否启用语义层”的开关，也没有“BGE 不得用于 code-intel”的约束。
- 本仓库此前的 docstring A/B 显示 BGE 在有 docstring 的代码上有效；但纯代码场景不可靠，
  且用户明确要求 code-intel 不用 BGE。

问题：

1. 没有开关 → 不需要语义检索的部署也要承担工具、模型下载和索引。
2. code-intel 使用 BGE → 与“BGE 只服务文本语义”的边界冲突。
3. Qwen3 不是 code-specific，不能回答“codeEmbedder 是否值得接入”。

---

## 2. 设计

### 2.1 配置字段（扩展现有 `CODE_INTEL_SEMANTIC`，不新增模块）

```python
class CodeIntelSemanticConfig(TypedDict):
    # 原键名保留，但语义收窄：只表示 CodeEmbedder profile；
    # "bge-m3" 不再是合法值，启动时拒绝/迁移。
    code_intel_semantic_model: str       # 默认 "jina-embeddings-v2-base-code"
    code_intel_semantic_enabled: str     # "off" | "on" | "auto"（默认 "auto"）
    code_intel_semantic_embedder: str    # "code" | "auto"（默认 "code"）
```

| enabled | 行为 |
| --- | --- |
| `off` | 不注册 `semantic_code_search`；不加载/下载模型；不建索引 |
| `on` | 必须找到合法 CodeEmbedder profile + 文件 + 探针通过；失败则不注册工具并记录原因 |
| `auto` | 只有 CodeEmbedder 配置齐全、文件存在、许可允许、探针通过才启用；否则关闭 |

- **没有 `bge-m3` 选项，也没有 BGE 回退**；code-intel 语义层永远不会回退 BGE。
- `code_intel_semantic_model = "bge-m3"` 在加载时直接拒绝，并给出
  “code-intel semantic layer cannot use BGE; configure a code embedder”。

### 2.2 CodeEmbedder profile

profile 表作为 `CODE_INTEL_SEMANTIC` 的嵌套字段（或模块内私有 `_CODE_EMBEDDER_PROFILES`），
不新增可导出的全大写 feature 实例；注册表计数保持 49/27/76。

```python
class CodeEmbedderProfile(TypedDict):
    name: str                 # "jina-embeddings-v2-base-code"
    model_path: str           # runtime download: .../jina-embeddings-v2-base-code.onnx
    dim: int                  # 768
    pooling: str              # "mean"
    query_prefix: str         # ""
    document_prefix: str       # ""
    license: str              # "Apache-2.0"
    natural_languages: tuple[str, ...]  # ("en", "zh")  # zh 为实测可用，非模型卡官方声明
    max_context: int          # 8192
    size_mb: int              # 612
```

已测候选：

| 候选 | 许可 | 规模/格式 | 纯代码 per-text（en / zh，70 chunks） | 结论 |
| --- | --- | --- | --- | --- |
| **Jina Embeddings v2 Base Code** | **Apache-2.0** | 161M，768d，8k；ONNX 612 MB | **0.80/0.90/0.857** / **0.70/0.80/0.765** | **采用** |
| Qwen3-Embedding-0.6B | Apache-2.0 | 0.6B，1024d，32k；GGUF 639 MB | 0.30/0.60/0.493 / 0.20/0.40/0.372 | 否决（通用、中文退化、batch 陷阱） |
| Jina Code 0.5B / 1.5B | CC-BY-NC-4.0 | 0.5B，896d；GGUF 531 MB | 未测 | 不采用（非商用） |
| Nomic Embed Code | Apache-2.0 | 7B；GGUF Q4 ≈4.1 GB | 未测 | 太重 |
| CodeRankEmbed | MIT | 137M，8k；无官方 GGUF | 未测 | 备选；需新后端 |
| SFR-Embedding-Code-400M_R | CC-BY-NC-4.0 | 400M，8k；ONNX | 未测 | 不采用（非商用） |

- Jina 的 ONNX 与 torch 输出逐条一致；`RobertaTokenizerFast` 在 transformers 5 下加载，
  **不触发 remote model code**。
- 中文查询是实测可用（0.70/0.80/0.765），但模型卡只声明 English + 30 programming
  languages；这是一条经验结论，需在更大查询集上复核。

### 2.3 加载器（codeEmbedder 走 ONNX，BGE 保持原样）

- **不改** `build_embed_model()` 的默认行为（BGE），避免影响 context_engine / multimodal
  RAG；`build_embed_model()` 继续返回 BGE。
- code-intel 新增命名构建器（如 `build_code_embed_model(profile)`），只接受 CodeEmbedder
  profile；内部：
  - `RobertaTokenizerFast.from_pretrained("jinaai/jina-embeddings-v2-base-code")`（transformers 5）；
  - `onnxruntime.InferenceSession(model.onnx, providers=["CPUExecutionProvider"])`；
  - mean pooling + L2（按模型卡）；
  - max_length 1024（chunk 上限 2000 字符；模型支持 8k）；
  - query/document 前缀为空；Jina 是 code retrieval 模型，不需要 Qwen3 的 Instruct/Query 模板。
- `onnxruntime` 作为运行时依赖（CPU，约 17 MB），**懒加载**：只有 codeEmbedder 启用时才 import；
  模型权重不随包分发，运行时从 HF 下载到 `models/embed_model/model_weight/`（gitignored），
  带大小/校验检查。
- 若目标平台没有可用的 onnxruntime wheel：语义层保持关闭并记录原因，不回退 BGE。

### 2.4 索引与旧 BGE 数据

- 保留 `code_embeddings` 表与 `model` 列；同一时间只有 active CodeEmbedder 的向量。
- `model` 写 `jina-embeddings-v2-base-code`（768 维）。
- 升级迁移：如果表里有 `model = "bge-m3"` 旧行，在语义层初始化时删除并记录；
  code-intel 不读取/评分 BGE 向量。语义层关闭时惰性清理。
- 切换 CodeEmbedder 时清空/重建；不维护双索引。
- 模型文件可共存，但只有 active CodeEmbedder 被加载。

### 2.5 工具注册

`build_code_intel_tools()`：

1. 解析 `enabled`/profile；
2. 关闭 → 不构造 `SemanticSearch`、不 append 工具；只返回四个
   `explore / callers / callees / impact`；
3. 开启 → 构造语义工具，把 Jina profile 传给 `SemanticSearch`；
4. 构造失败/探针失败 → `logger.warning`，不 append 工具（不注册永远 degraded 的工具）。

`explore` 等四个工具不受影响；语义层仍是 researcher/librarian 专属。

### 2.6 可观测性

- 首次启用日志：`active_embedder`、`model_path`、`dim`、`pooling`、`index_model`。
- `semantic_code_search` 结果增加 `embedder` 字段；关闭/失败时 `degraded` 写明原因
  （`semantic layer disabled` / `code embedder unavailable` / `probe failed`）。
- 不新增 HTTP 面板（本期不做 UI）。

---

## 3. 分阶段

### P1：语义层可选化（不引入 CodeEmbedder，先落地）

- 扩展 `CODE_INTEL_SEMANTIC`：`enabled`（默认 `auto`）、`embedder`（只允许 `code`/`auto`）。
- `build_code_intel_tools()` 门控：`off` / `auto` 且无合法 CodeEmbedder → 不注册语义工具。
- **默认行为变化：没有 CodeEmbedder 时语义层关闭**；`bge-m3` 不再是 code-intel 选项。
- 迁移旧 `code_embeddings.model = "bge-m3"` 行（删除，不读取）。
- 文档：四语言 `docs/code-intel/README{,.zh,.ja,.ko}.md` 的 Layer 4 + 配置表；
  `AGENTS.md` 一句话说明 BGE 只用于文本语义、code-intel 仅 CodeEmbedder。
- 测试：配置矩阵、工具数量、关闭时零模型/索引访问、BGE 值被拒绝、旧行被清理。
- 预计 0.5–1 人日；无新依赖、无模型下载。

### P2：Jina v2 Base Code ONNX（已 spike 通过，待实现）

- 新增 ONNX backend + Jina profile + 命名构建器；`onnxruntime` 依赖、懒加载。
- 模型下载：`jinaai/jina-embeddings-v2-base-code` 的 `onnx/model.onnx`（612 MB）+
  tokenizer 文件；固定 revision/hash，下载后校验大小。
- 索引：`model=jina-embeddings-v2-base-code`，768 维；清空 BGE 行；切换重建。
- 测试：fake ONNX session/tokenizer 单测（mean pooling、L2、维度、批量/单条一致）；
  真实模型 benchmark 手动脚本，不进 CI。
- 文档同步；配置表；不降级 transformers。
- 预计 1–2 人日。

#### P2 spike 结果（2026-10-18，本仓库实测）

语料：70 个纯代码 chunk（10 个目标函数 + 60 个干扰项，docstring 从 header 和 body 删除）。
查询：10 条英文 + 10 条中文行为转述（不含函数名）。per-text 编码（同一协议）。

| 模型 | en hit@1 / hit@3 / MRR | zh hit@1 / hit@3 / MRR |
| --- | --- | --- |
| BGE（per-text） | 0.30 / 0.70 / 0.470 | 0.30 / 0.60 / 0.431 |
| Qwen3（per-text） | 0.30 / 0.60 / 0.493 | 0.20 / 0.40 / 0.372 |
| **Jina v2 Base Code（ONNX，per-text）** | **0.80 / 0.90 / 0.857** | **0.70 / 0.80 / 0.765** |

- ONNX 与 torch 版 Jina 的指标/逐条 rank 完全一致；tokenizer 用 `RobertaTokenizerFast`
  （transformers 5，无 remote code）；`onnx/model.onnx` 612 MB。
- Jina ONNX 90 个 embedding（70 docs + 20 queries）约 1m22s（含模型加载）；单次推理
  0.12s/2 条；比 Qwen3 per-text 的 2m51s 更快，质量更高。
- Qwen3 batch 结果（en 0.30/0.60/0.460，zh 0.40/0.60/0.506）不可作质量结论：
  last-token pooling 的 padding 陷阱；且 160 个 embedding 跑了 49 分钟。
- 样本只有 10 个目标、10 条英文 + 10 条中文查询，属于方向性验证；Jina 的领先幅度很大，
  但仍建议在接入后用真实 agent 查询再做一轮小规模复核。

### P3（可选，暂不做）

- `enabled="auto"` 的自动探测/许可完善；多索引共存；状态端点/UI；
  若 Jina 在更大查询集上不达标，再评估 CodeRankEmbed。

---

## 4. 测试计划

**P1（unit，全部 hermetic）**

- 配置解析矩阵：`off/on/auto` × profile 存在/缺失/探针通过/失败。
- `build_code_intel_tools()` 返回 4 个工具（关闭）或 5 个（开启）；关闭时 mock 断言
  `SemanticSearch` 构造/模型加载/索引 build 均未被调用。
- `code_intel_semantic_model = "bge-m3"` 被拒绝；`embedder="bge-m3"` 不存在。
- `build_embed_model()` 仍返回 BGE，供 context_engine / multimodal RAG 使用（回归）。
- 旧 `code_embeddings.model = "bge-m3"` 行被清理且不评分。

**P2（unit + 手动 e2e）**

- fake ONNX session/tokenizer：mean pooling、L2、768 维、批量与单条一致。
- 模型文件缺失/大小不符/探针失败 → 不注册语义工具。
- 切换模型 → `code_embeddings` 只保留新 CodeEmbedder 行；BGE 行清理。
- `onnxruntime` 不可用 → 语义层关闭，不崩溃。
- 真实 Jina benchmark 脚本手动运行；不进 pytest。

---

## 5. 文档与配置同步

- `docs/code-intel/README{,.zh,.ja,.ko}.md`：
  - Layer 4 改为“语义层可选、仅 CodeEmbedder（Jina v2 Base Code ONNX）、无 BGE”；
  - 配置表更新 `enabled` / `embedder` / `code_intel_semantic_model`；
  - 说明 `auto` 的四个条件，并明确 BGE 只用于文本语义。
- `AGENTS.md`：code-intel 段落补“BGE 只服务文本语义；code-intel 语义层仅 CodeEmbedder”。
- `config/features/agent_side/code_intel_semantic.py` 注释写清 BGE 禁止项。
- 不新增 config 模块；profile 表作为嵌套字段/私有表，注册表计数保持 49/27/76。
- 文档门：`check_docs_parity.py`、`check_doc_links.py`、ruff/basedpyright 照常。

---

## 6. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| transformers 5 与 Jina remote model code 不兼容 | 已验证：只加载 ONNX + `RobertaTokenizerFast`，不加载 remote model code；不降级 transformers |
| onnxruntime 在某些平台没有 wheel | 懒加载；不可用则语义层关闭并记录；不回退 BGE |
| Jina 中文支持非官方声明 | 本仓库实测 0.70/0.80/0.765；接入后用真实查询复核；descriptor 标 `zh` 为 empirical |
| 模型文件 612 MB 下载失败/损坏 | 固定 revision/hash + 大小校验；失败不注册工具 |
| 旧 BGE 向量被误用 | 升级时删除 `model="bge-m3"` 行；启动校验 model 属于 CodeEmbedder profile |
| 双模型索引混向量 | 一次只保留 active CodeEmbedder 的 `code_embeddings` 行；切换清空重建 |
| 工具面浪费 | 关闭/失败时完全不注册语义工具 |
| 影响其它 embedding 调用 | `build_embed_model()` 默认仍 BGE；code-intel 不调用它 |
| 首次下载成本 | `enabled="off"`/`auto` 无 CodeEmbedder 时不下载；Jina 612 MB 仅在启用时下载 |

---

## 7. 非目标

- 不把 BGE 用作 code-intel 语义层；也不允许 BGE 回退。
- 不删除 BGE，不改 context_engine / multimodal RAG 的默认 embedder。
- 不同时维护 BGE 和 CodeEmbedder 两套 code-intel 索引（一次一个 active model）。
- 不自动下载 CC-BY-NC 或 4 GB 级模型。
- 不把 `semantic_code_search` 加到主 agent；仍限 researcher/librarian。
- 不动 code_wiki、graphify、知识图谱页面。
- 不做 UI/HTTP 状态端点（P3 再说）。

---

## 8. 验收标准

1. 未配置 CodeEmbedder 时，researcher/librarian 的工具列表**没有** `semantic_code_search`；
   不下载 BGE/CodeEmbedder、不建索引、不加载模型。
2. code-intel 无法选择 `bge-m3`：旧值被拒绝/清理，错误信息明确；语义工具永远不会用 BGE 评分。
3. 配置 Jina CodeEmbedder 且探针通过时，语义工具出现，结果带 `embedder`，索引 `model` 正确；
   切换/升级会清空/重建，不混向量。
4. BGE 的文本语义功能（context_engine / multimodal RAG）保持原样，相关回归测试绿。
5. Jina ONNX 的 benchmark 记录在案；接入后用真实 agent 查询复核，若明显退化则关闭语义层。
6. 四语言 code-intel 文档与配置表同步，docs parity / link 门绿。
