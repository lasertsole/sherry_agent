# 计划：语义检索可选化 + 可选 CodeEmbedder

- 建档日期：2026-10-18
- 状态：**计划（未开工）**
- 用户前提：**BGE（bge-m3）对纯代码不可靠**，因此语义层保持可选；BGE 保留，但不再作为自动启用语义层的理由。新增可选的 codeEmbedder；只有 codeEmbedder 就绪时，语义层才自动启用。
- 相关代码：
  - `agent/tools/code_intel/semantic/{search,indexer,chunker}.py`
  - `agent/tools/code_intel/tools.py::build_code_intel_tools`
  - `config/features/agent_side/code_intel_semantic.py`
  - `models/embed_model/core.py`
  - `docs/code-intel/README{,.zh,.ja,.ko}.md`

---

## 0. 结论先行

1. **语义层默认关闭，由 codeEmbedder 驱动。** `enabled="auto"` 时，只有配置了 codeEmbedder、模型文件存在、许可允许、探针通过，才注册 `semantic_code_search`；否则不注册、不下载、不建索引。BGE 单独存在**不会**触发自动启用。
2. **BGE 保留，但只是显式选项/回退。** `embedder="bge-m3"` 是显式 opt-in（多语言/有 docstring 的代码仍可用）；默认不把 BGE 当作纯代码的自动方案。`allow_bge_fallback` 默认 false，避免静默违背“BGE 不适合纯代码”的前提。
3. **一次只用一个 embedder。** 模型文件可以同时保留 BGE 和 codeEmbedder；**索引只保存当前 active model 的向量**。切换模型时按现有 `code_embeddings.model` 失效逻辑清空重建，不混向量、不维护双索引。
4. **不改全局 `build_embed_model()` 的默认值。** `context_engine/embeddings` 和 multimodal RAG 也调用它，必须继续用 BGE；codeEmbedder 走新的命名构建器（`build_named_embed_model(profile)`）。
5. **先做 P1（可选化，不加模型），再做 P2（codeEmbedder + A/B）。** P1 就能满足“语义层可选”；P2 只有在 benchmark 证明 codeEmbedder 在本仓库优于 BGE 时才继续，否则保留 BGE 作为显式选项、语义层默认关闭。

---

## 1. 现状与问题

当前行为：

- `build_code_intel_tools()` 对 `CODE_INTEL_ROLES`（researcher + librarian）无条件追加
  `semantic_code_search`；`SemanticSearch` 构造本身不加载模型，第一次调用才建索引/下载。
- `bge-m3` 本地 GGUF（606 MB）是唯一 embedder；`build_embed_model()` 还被
  `context_engine/embeddings/indexer.py`、`skills/.../graph_rag/base.py` 使用。
- `code_embeddings` 表已有 `model` 列，`_invalidate_on_model_change()` 在模型变化时失效重建，
  因此“切换模型”本身有基础，但**没有“是否启用语义层”的开关**。
- 近期的 docstring A/B（本机 bge-m3）：带 docstring hit@1 0.90 / hit@3 1.00 / MRR 0.950；
  去掉 docstring 为 0.70 / 0.80 / 0.789。BGE 在有 docstring 的代码上有效，但这不能证明
  它在**纯代码**上可靠；用户的前提成立时应让 BGE 退出自动路径。

问题：

1. 没有开关 → 无法在不需要语义检索的部署里省掉模型下载、索引和工具面。
2. 只有 BGE → 纯代码检索没有 code-specific 选项。
3. 若简单“存在模型就启用” → 会混用不同向量空间、可能启用 NC 许可模型、中文查询可能退化。

---

## 2. 设计

### 2.1 配置字段（扩展现有 `CODE_INTEL_SEMANTIC`，不新增模块）

```python
class CodeIntelSemanticConfig(TypedDict):
    # 保留：现有 BGE profile 名，兼容旧读取方/测试
    code_intel_semantic_model: str                    # "bge-m3"
    # 新增：语义层开关
    code_intel_semantic_enabled: str                  # "off" | "on" | "auto"（默认 "auto"）
    # 新增：选择哪个 profile；auto = 优先 code，失败不自动回落 BGE
    code_intel_semantic_embedder: str                 # "code" | "bge-m3" | "auto"（默认 "code"）
    # 新增：code profile 名（见 CODE_EMBEDDER_PROFILES）
    code_intel_semantic_code_model: str               # "qwen3-embedding-0.6b"（待 benchmark 确认）
    # 新增：只有显式 true 才允许 code 失败后回退 BGE
    code_intel_semantic_allow_bge_fallback: bool      # 默认 False
```

语义：

| enabled | embedder | 行为 |
| --- | --- | --- |
| `off` | 任意 | 不注册 `semantic_code_search`；不加载/下载模型；不建索引 |
| `on` | `code` | 必须找到 code profile 且探针通过；失败则**不注册工具**并记录原因 |
| `on` | `bge-m3` | 显式启用 BGE（保留旧能力/多语言/有 docstring 的代码） |
| `auto` | `code` | 只有 code profile 配置齐全 + 文件存在 + 许可允许 + 探针通过才启用；否则关闭 |
| `auto` | `bge-m3` | **不自动启用**（BGE 不驱动 auto；避免“BGE 对纯代码不生效”被静默违背） |
| 任意 | 任意 | `allow_bge_fallback=true` 时，code 探针/加载失败可回退 BGE；默认 false |

- **“存在 codeEmbedder 就启用”落地为 `enabled="auto"` + `embedder="code"`**，但增加三个门：
  配置选中、许可允许、探针通过。仅“文件存在”不启用。
- `enabled="off"` 是真正的零成本路径：不下载 606 MB BGE，也不下载 codeEmbedder。

### 2.2 Embedder profile（放在 `code_intel_semantic.py`，仍是一个 config 模块）

profile 表作为 `CODE_INTEL_SEMANTIC` 的嵌套字段（或模块内私有 `_CODE_EMBEDDER_PROFILES`），
**不新增可导出的全大写 feature 实例**；因此 `config/features` 的注册表计数保持 49/27/76。
如果最终决定导出 `CODE_EMBEDDER_PROFILES`，计数会变为 50/27/77，必须同步
`AGENTS.md` 与四语言 long-running-tasks README（见 §5）。

```python
class CodeEmbedderProfile(TypedDict):
    name: str                 # "qwen3-embedding-0.6b"
    model_path: str           # models/embed_model/model_weight/...
    dim: int
    pooling: str              # "last" | "mean" | "cls"
    query_prefix: str
    document_prefix: str
    license: str              # 只允许 Apache-2.0 / MIT 进入 auto
    natural_languages: tuple[str, ...]   # 中文查询是否支持
    max_context: int
    size_mb: int
```

首批候选（P2 只选一个做 benchmark，不全部接入）：

| 候选 | 许可 | 规模/格式 | 中文 NL | 结论 |
| --- | --- | --- | --- | --- |
| **Qwen3-Embedding-0.6B** | Apache-2.0 | 0.6B，1024d，32k；GGUF Q8 639 MB | 100+ 语言 | **首选**；通用但代码检索强，last-token + instruction |
| Jina Embeddings v2 Base Code | Apache-2.0 | 161M，8k，30 编程语言 | 英文 NL | 轻量；中文查询不适用 |
| CodeRankEmbed | MIT | 137M，8k | 英文 query 前缀 | 轻量；无官方 GGUF |
| Jina Code 0.5B / 1.5B | **CC-BY-NC-4.0** | 0.5B，896d，32k；GGUF 531 MB | 未声明 | **自动路径禁用**（非商用） |
| Nomic Embed Code | Apache-2.0 | 7B；GGUF Q4 ≈4.1 GB | 英文 query | 太重，桌面端不采用 |
| SFR-Embedding-Code-400M_R | **CC-BY-NC-4.0** | 400M，8k | 声称 multilingual | **自动路径禁用**（非商用） |

- 许可门：`auto` 只接受 Apache-2.0/MIT；NC 模型必须显式配置且不自动下载。
- BGE 仍保留在 profile 表里（`bge-m3`），供 `embedder="bge-m3"` 显式选择。

### 2.3 加载器

- **不改** `build_embed_model()` 的默认行为（BGE），避免影响 context_engine / multimodal RAG。
- 新增 `build_named_embed_model(profile: CodeEmbedderProfile)`（或 `build_embed_model(profile=None)`，
  `None` 保持 BGE）；语义层只走命名构建器。
- `models/embed_model/core.py` 参数化：
  - `_GGUF_MODEL_PATH` → profile 的路径；
  - `Llama(..., pooling_type=...)`（last/mean/cls）：需在 P2 先验证 `llama-cpp-python 0.3.36`
    是否暴露 last-token pooling；若不支持，改用 `llama-server`/ONNX 后端，不能假设“换 GGUF 就行”；
  - query/document 前缀：`SemanticSearch.search()` 加 query prefix，`build_chunk()` 加 document prefix；
  - 健康探针：加载后 embed 一个固定文本，失败则不启用。
- 其它调用方不受影响：只有 `agent/tools/code_intel/semantic/*` 使用命名构建器。

### 2.4 索引与模型切换

- 保留现有 `code_embeddings` 表与 `model` 列；**同一时间只有 active model 的向量**。
- 切换 model 时按现有 `_invalidate_on_model_change()` 清空/重建；不维护双索引、不混向量。
- 索引 `model` 值写 profile 名（如 `bge-m3` / `qwen3-embedding-0.6b`），
  结果与日志可追踪。
- 模型文件可以共存（BGE 和 codeEmbedder 都留在 `models/embed_model/model_weight/`），
  但只有 active model 被加载；`_embed_local` 当前每次调用加载/关闭模型，内存不叠加。

### 2.5 工具注册

`build_code_intel_tools()`：

1. 解析 `enabled`/`embedder`/profile；
2. 若解析结果为关闭 → **不构造 `SemanticSearch`、不 append 工具**；只返回
   `explore / callers / callees / impact`；
3. 若开启 → 构造语义工具，并把 profile 传给 `SemanticSearch`；
4. 构造失败/探针失败 → 记录 `logger.warning`，不 append 工具（不注册一个永远 degraded 的工具，
   避免浪费工具面）。

`explore` 等四个工具永远不受影响；语义层仍是 researcher/librarian 专属，不加入主 agent。

### 2.6 可观测性

- 首次启用时日志：`active_embedder`、`model_path`、`dim`、`pooling`、`index_model`。
- `semantic_code_search` 结果增加 `embedder` 字段；失败/关闭时 `degraded` 写明原因
  （`semantic layer disabled` / `code embedder unavailable` / `probe failed`）。
- 不新增 HTTP 面板（本期不做 UI）；需要排查时看日志和工具返回。

---

## 3. 分阶段

### P1：语义层可选化（不引入 codeEmbedder，先落地）

- 扩展 `CODE_INTEL_SEMANTIC`：`enabled`、`embedder`、`allow_bge_fallback`（默认 `auto`/`code`/`false`）。
- `build_code_intel_tools()` 加门控：`off` / `auto` 且无 code profile → 不注册语义工具。
- 默认行为变化：**没有 codeEmbedder 时语义层关闭**；显式 `embedder="bge-m3"` 仍可启用 BGE。
- 文档：`docs/code-intel/README{,.zh,.ja,.ko}.md` 的 Layer 4 + 配置表；
  `AGENTS.md` 一句话说明语义层可选。
- 测试：配置矩阵、工具数量、关闭时零模型/索引访问、显式 BGE 仍可用。
- 预计 0.5–1 人日；无新依赖、无模型下载。

### P2：codeEmbedder（Qwen3 优先）+ benchmark

- 加 profile 表、命名加载器、pooling/prefix 支持、健康探针。
- 下载 Qwen3-Embedding-0.6B GGUF（639 MB，Apache-2.0）做本地 A/B；
  先验证 llama-cpp-python 的 last-token pooling；不行则评估 llama-server/ONNX。
- benchmark：20 条查询（中文/英文各半；高层/符号级各半），对比 BGE vs Qwen3：
  hit@1、hit@3、MRR、延迟、RSS、模型大小、索引重建时间。
- 接受门槛：**代码类查询相对 BGE 提升 ≥10%**，且中文查询不退化；否则不把 codeEmbedder
  设为默认，语义层保持关闭/BGE 显式可选。
- 测试：fake embedder 单测（不联网）、index model 失效、维度不匹配、探针失败、
  `allow_bge_fallback` 行为；真实模型 benchmark 是手动脚本，不进 CI。
- 预计 1–2 人日（含下载/验证）。

### P3（可选，暂不做）

- `enabled="auto"` 的自动探测/许可/回退完善；多索引共存；状态端点/UI；
  若 benchmark 显示 Qwen3 不适合，再评估 Jina v2 base code / CodeRankEmbed。

---

## 4. 测试计划

**P1（unit，全部 hermetic）**

- 配置解析矩阵：`off/on/auto` × `code/bge-m3/auto` × profile 存在/缺失/探针通过/失败。
- `build_code_intel_tools()` 返回 4 个工具（关闭）或 5 个（开启）；关闭时 mock 断言
  `SemanticSearch` 构造/模型加载/索引 build 均未被调用。
- 显式 `embedder="bge-m3"` 仍返回 5 个工具。
- 旧配置（没有新字段）按默认 `auto`/`code` 解析，不崩溃。

**P2（unit + 手动 e2e）**

- profile 校验：未知模型、非法 pooling、缺少 license、负维度。
- 维度不匹配 → 清晰 `degraded`，不评分。
- 模型切换 → `code_embeddings` 只保留新 model 行；旧行被清理。
- 探针失败 → 不注册工具/回退行为符合 `allow_bge_fallback`。
- fake embedder 的 query/document 前缀、pooling 参数传递。
- 真实 Qwen3 benchmark 脚本手动运行，结果写入计划/README，不进 pytest。

---

## 5. 文档与配置同步

- `docs/code-intel/README{,.zh,.ja,.ko}.md`：
  - Layer 4 增加“语义层可选、默认由 codeEmbedder 驱动、BGE 显式可选”；
  - 配置表增加新字段；说明 `auto` 的四个条件（配置/文件/许可/探针）。
- `AGENTS.md`：code-intel 段落补一句语义层可选与 BGE/code 分工。
- `config/features/agent_side/code_intel_semantic.py` 的注释写清各字段语义。
- 不新增 config 模块；profile 表作为嵌套字段/私有表，不新增全大写导出实例 →
  注册表计数保持 49/27/76，无需改四语言计数。若最终导出 `CODE_EMBEDDER_PROFILES`，
  则必须同步为 50/27/77（`AGENTS.md` 三处 + 四语言 long-running-tasks README 四处）。
- 文档门：`check_docs_parity.py`、`check_doc_links.py`、ruff/basedpyright 照常。

---

## 6. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| llama.cpp 不支持 last-token pooling | P2 先做 10 行探针；不支持则用 llama-server/ONNX，不硬上 GGUF |
| codeEmbedder 中文查询退化 | profile 标 `natural_languages`；默认不自动回退；benchmark 中文集作为门槛 |
| 自动启用 NC 模型 | auto 只允许 Apache-2.0/MIT；NC 必须显式选择 |
| 模型文件存在但损坏/不完整 | 探针 + 文件大小/校验；失败不注册工具 |
| 双模型索引混向量 | 一次只保留 active model 的 `code_embeddings` 行；切换清空重建 |
| 工具面浪费 | 关闭/失败时完全不注册语义工具，不返回永远 degraded 的工具 |
| 影响其它 embedding 调用 | `build_embed_model()` 默认仍 BGE；只有语义层走命名构建器 |
| 首次下载成本 | `enabled="off"` / `auto` 无 code profile 时不下载任何模型；Qwen3 639 MB 可选 |

---

## 7. 非目标

- 不删除 BGE，不改 context_engine/multimodal RAG 的默认 embedder。
- 不同时维护 BGE 和 codeEmbedder 两套索引（一次一个 active model）。
- 不自动下载 CC-BY-NC 或 4 GB 级模型。
- 不把 `semantic_code_search` 加到主 agent；仍限 researcher/librarian。
- 不动 code_wiki、graphify、知识图谱页面。
- 不做 UI/HTTP 状态端点（P3 再说）。

---

## 8. 验收标准

1. 未配置 codeEmbedder 时，researcher/librarian 的工具列表**没有** `semantic_code_search`；
   不下载 BGE、不建索引、不加载模型。
2. 显式 `embedder="bge-m3"` 时，旧行为完全恢复（5 个工具、BGE 索引可用）。
3. 配置 codeEmbedder 且探针通过时，语义工具出现，结果带 `embedder`，索引 `model` 正确；
   切换回 BGE 会清空/重建，不混向量。
4. Qwen3 benchmark 有记录；若未达到门槛，记录“不启用 codeEmbedder”的结论，语义层保持关闭。
5. 四语言 code-intel 文档与配置表同步，docs parity / link 门绿。
