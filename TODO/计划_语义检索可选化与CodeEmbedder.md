# 计划：code-intel 语义检索可选化（仅 CodeEmbedder）

- 建档日期：2026-10-18
- 状态：**计划（未开工；Qwen3 CodeEmbedder spike 已完成，未达标）**
- 用户前提（本计划的硬边界）：
  - **BGE 只用于文本语义**：`context_engine/embeddings`、multimodal RAG、通用文本检索继续用 BGE。
  - **code-intel 的语义层不使用 BGE**。没有 CodeEmbedder 时，语义层关闭；有 CodeEmbedder 且配置/许可/探针通过时才启用。
- 相关代码：
  - `agent/tools/code_intel/semantic/{search,indexer,chunker}.py`
  - `agent/tools/code_intel/tools.py::build_code_intel_tools`
  - `config/features/agent_side/code_intel_semantic.py`
  - `models/embed_model/core.py`
  - `docs/code-intel/README{,.zh,.ja,.ko}.md`

---

## 0. 结论先行

1. **code-intel 语义层只认 CodeEmbedder。** `bge-m3` 不是 code-intel 的可选项；没有 CodeEmbedder 就不注册 `semantic_code_search`，不加载模型、不建索引、不下载任何东西。
2. **BGE 保留在文本语义路径。** `build_embed_model()` 的默认值仍是 BGE，`context_engine/embeddings` 和 multimodal RAG 不受影响；code-intel 走新的命名构建器，只接受 CodeEmbedder profile。
3. **启用条件不是“文件存在”**，而是：配置选中 + profile 合法 + 许可允许（Apache-2.0/MIT）+ 探针通过。仅看到文件不启用。
4. **一次只用一个 CodeEmbedder。** 模型文件可以保留多个候选，但索引只保存当前 active CodeEmbedder 的向量；切换/升级时清空重建，不混向量。
5. **先做 P1（可选化，不加模型），再做 P2（CodeEmbedder + benchmark）。** P1 就能满足“没有 CodeEmbedder 时语义层关闭”；P2 只有在 benchmark 证明 code embedder 在本仓库优于 BGE 基线时才继续。

---

## 1. 现状与问题

当前行为：

- `build_code_intel_tools()` 对 `CODE_INTEL_ROLES`（researcher + librarian）无条件追加
  `semantic_code_search`；`SemanticSearch` 构造本身不加载模型，第一次调用才建索引/下载。
- `bge-m3` 本地 GGUF（606 MB）是唯一 embedder；`build_embed_model()` 还被
  `context_engine/embeddings/indexer.py`、`skills/.../graph_rag/base.py` 使用。
- `code_embeddings` 表已有 `model` 列，`_invalidate_on_model_change()` 在模型变化时失效重建，
  但**没有“是否启用语义层”的开关**，也没有“BGE 不得用于 code-intel”的约束。
- 近期 docstring A/B（bge-m3）：带 docstring hit@1 0.90 / hit@3 1.00 / MRR 0.950；
  去掉 docstring 为 0.70 / 0.80 / 0.789。这说明 BGE 在有 docstring 的代码上可用，
  但不改变“BGE 不作为纯代码语义层”的前提；code-intel 不采用 BGE。

问题：

1. 没有开关 → 不需要语义检索的部署也要承担工具、模型下载和索引。
2. code-intel 使用 BGE → 与“BGE 只服务文本语义”的边界冲突。
3. 若简单“存在模型就启用” → 会启用损坏/NC 许可/中文查询退化的模型。

---

## 2. 设计

### 2.1 配置字段（扩展现有 `CODE_INTEL_SEMANTIC`，不新增模块）

```python
class CodeIntelSemanticConfig(TypedDict):
    # 保留原键名，但语义收窄：它现在只表示 CodeEmbedder profile，
    # "bge-m3" 不再是合法值，启动时拒绝/迁移。
    code_intel_semantic_model: str                    # 默认 "qwen3-embedding-0.6b"
    # 语义层开关
    code_intel_semantic_enabled: str                  # "off" | "on" | "auto"（默认 "auto"）
    # 只允许 code；保留该字段是为了显式表达“不走 BGE”
    code_intel_semantic_embedder: str                 # "code" | "auto"（默认 "code"）
```

语义：

| enabled | 行为 |
| --- | --- |
| `off` | 不注册 `semantic_code_search`；不加载/下载模型；不建索引 |
| `on` | 必须找到合法 CodeEmbedder profile + 文件 + 探针通过；失败则不注册工具并记录原因 |
| `auto` | 只有 CodeEmbedder 配置齐全、文件存在、许可允许、探针通过才启用；否则关闭 |

- **没有 `embedder="bge-m3"` 选项**，也没有 `allow_bge_fallback`。code-intel 的语义层永远不会回退 BGE。
- `enabled="auto"` 是默认值：没有 CodeEmbedder 时语义层关闭，符合“可选”的默认。
- `code_intel_semantic_model` 的旧值 `"bge-m3"`（如果存在）在加载配置时直接拒绝，并给出
  “code-intel semantic layer cannot use BGE; configure a code embedder” 的错误；不静默迁移到 BGE。

### 2.2 CodeEmbedder profile

profile 表作为 `CODE_INTEL_SEMANTIC` 的嵌套字段（或模块内私有 `_CODE_EMBEDDER_PROFILES`），
**不新增可导出的全大写 feature 实例**；注册表计数保持 49/27/76。若最终导出
`CODE_EMBEDDER_PROFILES`，计数变为 50/27/77，必须同步 AGENTS.md 与四语言文档。

```python
class CodeEmbedderProfile(TypedDict):
    name: str                 # "qwen3-embedding-0.6b"
    model_path: str           # models/embed_model/model_weight/...
    dim: int
    pooling: str              # "last" | "mean" | "cls"
    query_prefix: str
    document_prefix: str
    license: str              # 只允许 Apache-2.0 / MIT 进入 auto
    natural_languages: tuple[str, ...]
    max_context: int
    size_mb: int
```

首批候选（P2 只选一个做 benchmark）：

| 候选 | 许可 | 规模/格式 | 中文 NL | 结论 |
| --- | --- | --- | --- | --- |
| **Qwen3-Embedding-0.6B** | Apache-2.0 | 0.6B，1024d，32k；GGUF Q8 639 MB | 100+ 语言 | **首选**；代码检索强，last-token + instruction |
| Jina Embeddings v2 Base Code | Apache-2.0 | 161M，8k，30 编程语言 | 英文 NL | 轻量；中文查询不适用 |
| CodeRankEmbed | MIT | 137M，8k | 英文 query 前缀 | 轻量；无官方 GGUF |
| Jina Code 0.5B / 1.5B | CC-BY-NC-4.0 | 0.5B，896d，32k；GGUF 531 MB | 未声明 | 自动路径禁用（非商用） |
| Nomic Embed Code | Apache-2.0 | 7B；GGUF Q4 ≈4.1 GB | 英文 query | 太重，桌面端不采用 |
| SFR-Embedding-Code-400M_R | CC-BY-NC-4.0 | 400M，8k | 声称 multilingual | 自动路径禁用（非商用） |

- **BGE 不在本表**；它只存在于文本语义路径。离线 benchmark 可以把 BGE 当对照基线，
  但 code-intel 运行时不会加载/使用它。

### 2.3 加载器

- **不改** `build_embed_model()` 的默认行为（BGE），避免影响 context_engine / multimodal RAG。
- 新增 `build_named_embed_model(profile: CodeEmbedderProfile)`；code-intel 只走这个入口。
- `models/embed_model/core.py` 参数化：
  - `_GGUF_MODEL_PATH` → profile 的路径；
  - `Llama(..., pooling_type=...)`（last/mean/cls）：P2 先验证 `llama-cpp-python 0.3.36`
    是否暴露 last-token pooling；不支持则改用 `llama-server`/ONNX，不能假设换 GGUF 就行；
  - query/document 前缀：`SemanticSearch.search()` 加 query prefix，`build_chunk()` 加 document prefix；
  - 健康探针：加载后 embed 固定文本，失败则不启用。
- code-intel 不调用 `build_embed_model()`；`context_engine` / multimodal RAG 继续调用它（BGE）。

### 2.4 索引与旧 BGE 数据

- 保留 `code_embeddings` 表与 `model` 列；**同一时间只有 active CodeEmbedder 的向量**。
- `model` 值写 CodeEmbedder profile 名（如 `qwen3-embedding-0.6b`）。
- **升级迁移**：如果表里存在 `model = "bge-m3"` 的旧行，在语义层初始化时删除并记录；
  code-intel 不得读取/评分这些 BGE 向量。语义层关闭时可在惰性初始化时清理，避免遗留。
- 切换 CodeEmbedder 时按现有 `_invalidate_on_model_change()` 清空/重建；不维护双索引。
- 模型文件可以共存，但只有 active CodeEmbedder 被加载；`_embed_local` 每次调用加载/关闭，
  内存不叠加。

### 2.5 工具注册

`build_code_intel_tools()`：

1. 解析 `enabled`/profile；
2. 若关闭 → **不构造 `SemanticSearch`、不 append 工具**；只返回
   `explore / callers / callees / impact`，不触达模型/索引；
3. 若开启 → 构造语义工具，并把 CodeEmbedder profile 传给 `SemanticSearch`；
4. 构造失败/探针失败 → 记录 `logger.warning`，不 append 工具（不注册永远 degraded 的工具）。

`explore` 等四个工具永远不受影响；语义层仍是 researcher/librarian 专属。

### 2.6 可观测性

- 首次启用时日志：`active_embedder`、`model_path`、`dim`、`pooling`、`index_model`。
- `semantic_code_search` 结果增加 `embedder` 字段；关闭/失败时 `degraded` 写明原因
  （`semantic layer disabled` / `code embedder unavailable` / `probe failed`）。
- 不新增 HTTP 面板（本期不做 UI）。

---

## 3. 分阶段

### P1：语义层可选化（不引入 CodeEmbedder，先落地）

- 扩展 `CODE_INTEL_SEMANTIC`：`enabled`（默认 `auto`）、`embedder`（只允许 `code`/`auto`）。
- `build_code_intel_tools()` 加门控：`off` / `auto` 且无合法 CodeEmbedder → 不注册语义工具。
- **默认行为变化：没有 CodeEmbedder 时语义层关闭**；`bge-m3` 不再是 code-intel 的选项。
- 迁移旧 `code_embeddings.model = "bge-m3"` 行（删除，不读取）。
- 文档：四语言 `docs/code-intel/README{,.zh,.ja,.ko}.md` 的 Layer 4 + 配置表；
  `AGENTS.md` 一句话说明 BGE 只用于文本语义、code-intel 仅 CodeEmbedder。
- 测试：配置矩阵、工具数量、关闭时零模型/索引访问、BGE 值被拒绝、旧行被清理。
- 预计 0.5–1 人日；无新依赖、无模型下载。

### P2：CodeEmbedder（Qwen3 优先）+ benchmark

- 加 profile 表、命名加载器、pooling/prefix 支持、健康探针。
- 下载 Qwen3-Embedding-0.6B GGUF（639 MB，Apache-2.0）做本地 A/B；
  先验证 llama-cpp-python 的 last-token pooling；不行则评估 llama-server/ONNX。
- benchmark：20 条查询（中文/英文各半；高层/符号级各半），对比 **BGE 作为离线基线** vs
  Qwen3；指标 hit@1、hit@3、MRR、延迟、RSS、模型大小、索引重建时间。
- 接受门槛：**代码类查询相对 BGE 基线提升 ≥10%**，且中文查询不退化；否则不启用
  CodeEmbedder，语义层保持关闭（BGE 仍只用于文本语义）。
- 测试：fake embedder 单测（不联网）、index model 失效、维度不匹配、探针失败；
  真实模型 benchmark 是手动脚本，不进 CI。
- 预计 1–2 人日（含下载/验证）。

#### P2 spike 结果（2026-10-18，本仓库实测）

对象：`Qwen3-Embedding-0.6B-Q8_0.gguf`（Apache-2.0，639 MB）vs 现有 bge-m3。
语料：70 个 chunk（10 个带 docstring 的目标函数 + 60 个同域干扰项）；纯代码对照使用
`without-doc`（docstring 从 header 和 body 都删掉）。查询：10 条英文 + 10 条中文（目标函数的
行为转述，不含函数名）。指标为 cosine 排序后的 hit@1 / hit@3 / MRR。

| 编码方式 | 模型 | en hit@1 / hit@3 / MRR | zh hit@1 / hit@3 / MRR |
| --- | --- | --- | --- |
| per-text（逐条，避免 last-token padding） | BGE | 0.30 / 0.70 / 0.470 | 0.30 / 0.60 / 0.431 |
| per-text | Qwen3 | 0.30 / 0.60 / 0.493 | **0.20 / 0.40 / 0.372** |
| batch（文档和查询都按批；不是生产 query 单条） | BGE | 0.70 / 0.90 / 0.800 | 0.60 / 0.70 / 0.720 |
| batch | Qwen3 | 0.30 / 0.60 / 0.460* | 0.40 / 0.60 / 0.506* |

样本只有 10 个目标、10 条英文 + 10 条中文查询，且 batch 行把 query 也批处理了（生产 query 是
单条），所以这些数字只用于方向性判断，不是生产环境的绝对指标；per-text 两行是同一协议下的
受控对比。

\* Qwen3 的 batch 结果不可作为质量结论：它要求 last-token pooling，批内不同长度文本的
padding 会污染向量；同一批数据改为 per-text 后质量明显变化。生产若用 Qwen3，必须逐条编码
或做左 padding。Qwen3 batch 的 160 个 embedding 还耗时 **49 分钟**（BGE 约 5 分钟），
per-text 70+20 也约 1.7× BGE。

**结论：Qwen3-Embedding-0.6B 未达到计划门槛（代码查询相对 BGE 提升 ≥10%）。**
- 纯代码 per-text：Qwen3 的 en MRR 略高（0.493 vs 0.470），但 zh 明显更低
  （0.372 vs 0.431），且两者 hit@1 都是 0.30；batch 下 BGE 明显更强。
- Qwen3 是“通用、代码能力较强”，不是 code-specific；本轮不能据此判断 Jina v2 base code /
  CodeRankEmbed 等真正的 code-specific 模型。
- 因此**不把 Qwen3 加入 auto**；语义层保持默认关闭，BGE 仍只服务文本语义。
- 若继续 P2，下一轮只测真正的 code-specific 候选（Jina v2 base code / CodeRankEmbed，
  英文 query；中文查询需额外验证），仍按同一 A/B 门槛；不达标就停止投入语义层。

### P3（可选，暂不做）

- `enabled="auto"` 的自动探测/许可完善；多索引共存；状态端点/UI；
  若 Qwen3 不合适，再评估 Jina v2 base code / CodeRankEmbed。

---

## 4. 测试计划

**P1（unit，全部 hermetic）**

- 配置解析矩阵：`off/on/auto` × CodeEmbedder profile 存在/缺失/探针通过/失败。
- `build_code_intel_tools()` 返回 4 个工具（关闭）或 5 个（开启）；关闭时 mock 断言
  `SemanticSearch` 构造/模型加载/索引 build 均未被调用。
- **`code_intel_semantic_model = "bge-m3"` 被拒绝**，错误信息明确；`embedder="bge-m3"` 不存在。
- `build_embed_model()` 仍返回 BGE，供 context_engine / multimodal RAG 使用（回归测试）。
- 旧 `code_embeddings` 中 `model = "bge-m3"` 的行被清理，且不会被评分。

**P2（unit + 手动 e2e）**

- profile 校验：未知模型、非法 pooling、缺少 license、负维度。
- 维度不匹配 → 清晰 `degraded`，不评分。
- 模型切换 → `code_embeddings` 只保留新 CodeEmbedder 行；旧行被清理。
- 探针失败 → 不注册工具（没有 BGE 回退）。
- fake embedder 的 query/document 前缀、pooling 参数传递。
- 真实 Qwen3 benchmark 脚本手动运行，结果写入计划/README，不进 pytest。

---

## 5. 文档与配置同步

- `docs/code-intel/README{,.zh,.ja,.ko}.md`：
  - Layer 4 改为“语义层可选、仅 CodeEmbedder、无 BGE”；
  - 配置表更新 `enabled` / `embedder` / `code_intel_semantic_model` 的语义；
  - 说明 `auto` 的四个条件（配置/文件/许可/探针），并明确 BGE 只用于文本语义。
- `AGENTS.md`：code-intel 段落补一句“BGE 只服务文本语义；code-intel 语义层仅 CodeEmbedder”。
- `config/features/agent_side/code_intel_semantic.py` 注释写清 BGE 禁止项。
- 不新增 config 模块；profile 表作为嵌套字段/私有表，注册表计数保持 49/27/76。
- 文档门：`check_docs_parity.py`、`check_doc_links.py`、ruff/basedpyright 照常。

---

## 6. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| llama.cpp 不支持 last-token pooling | P2 先做 10 行探针；不支持则用 llama-server/ONNX |
| CodeEmbedder 中文查询退化 | profile 标 `natural_languages`；默认不启用；benchmark 中文集作为门槛 |
| 自动启用 NC 模型 | auto 只允许 Apache-2.0/MIT；NC 必须显式选择，且不能自动下载 |
| 模型文件存在但损坏/不完整 | 探针 + 文件大小/校验；失败不注册工具 |
| 旧 BGE 向量被误用 | 升级时删除 `model="bge-m3"` 行；code-intel 启动校验 model 必须属于 CodeEmbedder profile |
| 双模型索引混向量 | 一次只保留 active CodeEmbedder 的 `code_embeddings` 行；切换清空重建 |
| 工具面浪费 | 关闭/失败时完全不注册语义工具 |
| 影响其它 embedding 调用 | `build_embed_model()` 默认仍 BGE；code-intel 不调用它 |
| 首次下载成本 | `enabled="off"`/`auto` 无 CodeEmbedder 时不下载任何模型；Qwen3 639 MB 可选 |

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
   不下载 BGE、不建索引、不加载模型。
2. code-intel 无法选择 `bge-m3`：旧值被拒绝/清理，错误信息明确；`semantic_code_search`
   永远不会用 BGE 评分。
3. 配置 CodeEmbedder 且探针通过时，语义工具出现，结果带 `embedder`，索引 `model` 正确；
   切换/升级会清空/重建，不混向量。
4. BGE 的文本语义功能（context_engine / multimodal RAG）保持原样，相关回归测试绿。
5. Qwen3 benchmark 有记录；若未达到门槛，记录“不启用 CodeEmbedder”的结论，语义层保持关闭。
6. 四语言 code-intel 文档与配置表同步，docs parity / link 门绿。
