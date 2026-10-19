# 计划：llm_wiki 加显式图层 + 多模态 ingest + 图算法查询（对齐 graphify 常规 wiki 能力）

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- **硬约束**：**不破坏 llm_wiki 现有功能**——所有改动必须向后兼容，详见 §1
- 关联代码：
  - `skills/builtin/llm_wiki/SKILL.md`（LLM-driven wiki 主流程）
  - `skills/builtin/llm_wiki/scripts/core.py`（路径管理 + init_repo_wiki）
  - `skills/builtin/llm_wiki/scripts/ingest.py`（`save_source` 仅字符串）
  - `skills/builtin/llm_wiki/scripts/search.py`（`search_wiki` 是 grep glob）
  - `skills/builtin/core/multimodal_rag/scripts/graph_rag/core.py`（mineru parser 可复用）
  - `skills/builtin/core/image_to_text/scripts/core.py`（VLM 可复用）
  - `skills/builtin/core/multimodal_rag/scripts/graph_rag/base.py`（`_vision_model_func` 可复用）
- 关联文档：
  - `TODO/计划_GraphRAG_多跳路径查询缺口.md`（BFS 算法跨包呼应）
  - `TODO/计划_code_intel_能力暴露与graphify对齐.md`（code_intel 对齐，同系列）
  - `TODO/计划_GraphRAG_稀疏召回与重排调优.md`（graph_rag 对齐，同系列）
- 关联外部参考：
  - `D:/selfProj/graphify/graphify/extractors/markdown.py`（markdown extractor 正则模式可借鉴）
  - graphify README.md L37, L107（EXTRACTED/INFERRED confidence tag 语义）

---

## 0. 背景：llm_wiki 现状 vs graphify 常规 wiki 能力（已核实）

### 0.1 llm_wiki 当前能力（核实过代码）

| 能力 | 当前实现 | 缺什么 |
|---|---|---|
| 文本源 ingest | `save_source(content: str, category, filename)` 写 markdown 进 `raw/{articles,papers,transcripts,assets}` | **只能存字符串**，PDF/image/video/audio 不能 ingest |
| 搜索 | `search_wiki(keyword)` glob 所有 .md + grep 关键词 | **纯字符串 grep**，无图查询 |
| 健康检查 | `lint_wiki()` 检查孤立/断链/陈旧/矛盾/frontmatter | **已检查图不健康状态**，但没有显式图数据结构 |
| 持续沉淀 | `index.md` 内容目录 + `log.md` append-only 历史 | **比 graphify 强**——graphify 是快照式，llm_wiki 是持续 KB |
| 内容 schema | `SCHEMA.md` 定义 tag taxonomy / page thresholds / contradictions | **比 graphify 强**——graphify 无内容层 schema |

### 0.2 sherry 已有可复用能力（不用从零写）

| 内容类型 | graphify 怎么处理 | sherry 可复用 |
|---|---|---|
| **PDF** | LLM semantic pass | `multimodal_rag/scripts/graph_rag/core.py::get_rag_anything` 已用 mineru parser（布局感知） |
| **图片** | LLM semantic pass | `skills/builtin/core/image_to_text/scripts/core.py` + `multimodal_rag/scripts/graph_rag/base.py::_vision_model_func`（ITTT_model VLM） |
| **音频** | faster-whisper 转录 | sherry `REQUIRED_SKILLS` 包含 `speech_to_text`（multimedia chain，AGENTS.md 提过） |
| **视频** | faster-whisper + yt-dlp | sherry `REQUIRED_SKILLS` 包含 `video_text_to_text`（同上） |
| **office (.docx/.xlsx)** | python-docx / openpyxl | **真没有**，需新增（轻量依赖） |
| **markdown** | regex extractor 抽 link/wikilink/mention 进图 | **借鉴 graphify 的 `graphify/extractors/markdown.py` 正则模式** |

### 0.3 graphify 处理常规内容的真相（我之前漏说，现在补上）

graphify **不只做代码 wiki**——README 明确支持 docs/PDFs/images/video/audio 进同一个图。处理方式分两种：

- **代码**：tree-sitter AST，确定性，0 LLM
- **常规内容（docs/PDFs/images/video/audio）**：LLM semantic pass 抽 entities/concepts/relationships，进同一个 `graph.json`，跟代码节点互连，每条边标 EXTRACTED/INFERRED

graphify 的 markdown extractor（`graphify/extractors/markdown.py`）用正则解析三类 markdown 元素作为图边：
- `_MD_WIKILINK_RE`：`[[link]]` Obsidian 风格 wikilink
- `_MD_INLINE_LINK_RE`：`[text](url)` CommonMark 内联链接
- `_MD_BARE_MENTION_RE` / `_MD_PATH_MENTION_RE`：`` `code span` `` 里的符号提及（`Widget` / `pkg.Widget` / `src/pkg/mod.py::Widget::render`）

这意味着 **markdown 文档之间的 wikilink 关系在 graphify 里也是显式图边**——正是 llm_wiki 缺的那一层。

### 0.4 真正的差距（修正之前的判断）

llm_wiki 跟 graphify 处理常规内容**都是 LLM-driven**（非代码没法 AST）。差距不在"能不能 LLM 抽取"，而在**抽取结果的存储方式**：

- graphify：LLM 抽完 → **存进显式图数据结构**（节点 + 边 + confidence tag）→ 图算法查询
- llm_wiki：LLM 抽完 → **存进 markdown 文件**（自己写 `[[wikilinks]]`）→ 字符串 grep

**这个差距工程上可以补**——在 markdown 旁边维护显式图 SQLite，不替换 markdown。

---

## 1. ⚠️ 硬约束：不破坏 llm_wiki 现有功能

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **L1**：现有函数签名不变 | `save_source` / `search_wiki` / `init_wiki` / `lint_wiki` / `get_wiki_path` / `get_repo_wiki_path` 签名和返回值不变 | 现有 SKILL.md 调用路径全部继续可用 |
| **L2**：现有 `raw/` 目录结构不动 | `raw/{articles,papers,transcripts,assets}` 四个 category 保留；新增二进制 ingest 走独立子目录或扩展 category | 现有 markdown 文件继续可读 |
| **L3**：现有 SCHEMA.md / index.md / log.md 协议不变 | tag taxonomy / frontmatter 必填字段 / page thresholds / contradictions 机制不动；新增字段是可选 | 现有 wiki 实例升级后 SCHEMA.md 不需要重写 |
| **L4**：现有 frontmatter 字段不动 | `title` / `created` / `updated` / `type` / `tags` / `sources` / `confidence` / `contested` / `contradictions` 保留；新字段（如 `extracted_entities`）是可选 | 现有 markdown 文件不需要改 frontmatter 就能被新代码读 |
| **L5**：新依赖是 optional extras | `python-docx` / `openpyxl` / `networkx` / `python-louvain` 缺失时退化（不报错，跳过对应功能） | 卸载新依赖，core/ingest/search 仍正常工作 |
| **L6**：不破坏 agent 工具范围 | llm_wiki 不是 researcher_only 工具（它是 skill 内的脚本，不是 LangChain tool）；新查询函数也走 skill 脚本路径，不进 `_MAIN_TOOLS_BUILDERS` | `agent/tools/__init__.py` 不需要为 llm_wiki 改动 |
| **L7**：lint 行为不变 | `lint_wiki()` 现有 10 项检查（孤立/断链/陈旧/矛盾/frontmatter/page size/tag/log rotate）输出格式不变；新增检查追加到报告末尾 | 现有 lint 报告前 10 项 byte-identical |
| **L8**：增量更新行为不变 | markdown 文件改动后重建图是增量的（mtime 检查），不重扫整个 wiki | 大 wiki 重建 < 1s（如果只是改了一个文件） |

---

## 2. 任务一：多模态 ingest（docs/PDFs/images/video/audio）

### 2.1 目标

扩展 `save_source` 支持**二进制内容**（PDF/image/audio/video bytes）+ 文件路径 ingest，走 sherry 已有的多模态能力抽取出 entities/relations，存进 `raw/` 对应 category + 同步进显式图（§3）。

### 2.2 改动清单（满足 L1 / L2 / L4）

- [ ] **2.2.1** `ingest.py` 新增 `save_binary_source(content_bytes: bytes, content_type: str, source_url: str = "", filename: str = None) -> dict`——不动 `save_source` 字符串路径：
  ```python
  def save_binary_source(content_bytes, content_type, source_url="", filename=None) -> dict:
      """
      content_type: 'pdf' | 'image' | 'audio' | 'video' | 'docx' | 'xlsx'
      走对应的 extractor 抽取 entities/relations，存进 raw/{papers,assets,transcripts} + 进图
      """
  ```
- [ ] **2.2.2** extractor 分发逻辑（复用 sherry 已有能力）：
  - `pdf` → 复用 `multimodal_rag/scripts/graph_rag/core.py::get_rag_anything(parser='mineru')` 的 mineru parser
  - `image` → 复用 `image_to_text` skill 或 `multimodal_rag` 的 `_vision_model_func`
  - `audio` → 复用 `speech_to_text` skill（faster-whisper）
  - `video` → 复用 `video_text_to_text` skill
  - `docx` → 新增 `python-docx`（optional extra，缺失时 L5 退化）
  - `xlsx` → 新增 `openpyxl`（optional extra）
  - `md` / `txt` → 复用 graphify markdown extractor 正则模式（§0.3）
- [ ] **2.2.3** 抽取出的 entities/relations 走 LLM semantic pass（用 sherry 的 `auxiliary_llm`）：
  - 输入：文档全文 / 图片描述 / 转录文本
  - LLM prompt：抽取 entities + relations，每条 relation 标 EXTRACTED（源文档明确陈述）或 INFERRED（LLM 推断）
  - 输出：JSON `{entities: [...], relations: [{src, dst, type, confidence, evidence}]}`
- [ ] **2.2.4** 抽取结果存两份：
  - markdown 进 `raw/{papers,assets,transcripts}/<filename>.md`（人类可读，保留 frontmatter 协议）
  - entities + relations 进显式图 SQLite（§3）
- [ ] **2.2.5** LLM ingest 同步：LLM 写 entity/concept/comparison 页面时，新 wikilinks 同步进显式图（带 confidence tag）

### 2.3 风险与约束

- **复用 multimodal_rag 的耦合**：multimodal_rag 是个独立 skill，llm_wiki 直接 import 可能违反 sherry 的 skill 隔离原则。**解决**：把 multimodal_rag 的 extractor 接口抽到 `runtime/data_provider.py` 或 `pub/` 共享层（类似 PromptDataProvider 模式），llm_wiki 通过 provider 调用，不直接 import。
- **LLM semantic pass 成本**：PDF/视频/图片抽取都要调 LLM，比 graphify 的 tree-sitter 慢且贵。这是常规内容处理的固有代价，graphify 也一样。
- **office 依赖**：`python-docx` / `openpyxl` 是 optional extra，L5 保证缺失时不报错。
- **faster-whisper 模型下载**：第一次跑音频/视频会下载模型（几百 MB），跟 multimodal_rag 的 mineru 模型下载一样。需要确保模型路径走 `MODELS_DIR`。

**验收**：L1 + L2 + L4 + L5。新建 `tests/skills/llm_wiki/test_binary_ingest.py`，每种 content_type 一个 fixture。

---

## 3. 任务二：显式图层（SQLite 边表）

### 3.1 目标

在 markdown 旁边维护显式图 SQLite DB，解析所有 markdown 的 wikilinks/links/mentions + 多模态 ingest 抽取的 entities/relations，作为图算法查询（§5）的基础。

### 3.2 改动清单（满足 L1 / L3 / L8）

- [ ] **3.2.1** 新增 `skills/builtin/llm_wiki/scripts/graph.py`（独立模块，不动 core/ingest/search）：
  ```sql
  CREATE TABLE IF NOT EXISTS wiki_nodes (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,           -- entity/concept name or file stem
      type TEXT NOT NULL,           -- 'entity' / 'concept' / 'source' / 'comparison'
      file_path TEXT,               -- 对应 markdown 文件路径（如果有）
      created_at REAL,
      updated_at REAL
  );
  CREATE TABLE IF NOT EXISTS wiki_edges (
      id INTEGER PRIMARY KEY,
      src_id INTEGER NOT NULL,
      dst_id INTEGER NOT NULL,
      edge_type TEXT NOT NULL,      -- 'wikilink' / 'inline_link' / 'mention' / 'semantic'（LLM 抽的）
      confidence TEXT NOT NULL,     -- 'EXTRACTED' / 'INFERRED'
      source_file TEXT NOT NULL,    -- 这条边来自哪个 markdown 文件
      line INTEGER,                 -- 行号（如果是 markdown 抽的）
      evidence TEXT                 -- LLM 抽取时的 evidence snippet（如果是 semantic）
  );
  CREATE TABLE IF NOT EXISTS wiki_graph_meta (
      file_path TEXT PRIMARY KEY,
      mtime REAL NOT NULL,
      sha256 TEXT,
      indexed_at REAL NOT NULL
  );
  ```
  索引：`idx_edges_src` / `idx_edges_dst` / `idx_edges_confidence` / `idx_nodes_name`
- [ ] **3.2.2** `build_wiki_graph(force=False)` 函数（增量，L8）：
  - 遍历 wiki 目录所有 .md 文件
  - 对每个文件：mtime 检查 → 解析 wikilinks/links/mentions → upsert 节点和边
  - markdown 解析正则**借鉴 graphify 的 `graphify/extractors/markdown.py`**：`_MD_WIKILINK_RE` / `_MD_INLINE_LINK_RE` / `_MD_BARE_MENTION_RE` / `_MD_PATH_MENTION_RE`
  - 删除的文件：从图里移除对应节点和边
- [ ] **3.2.3** confidence 自动判定：
  - `wikilink` / `inline_link` / `mention`（markdown 里直接有的）→ `EXTRACTED`
  - `semantic`（LLM semantic pass 抽的 relation）→ `INFERRED`（除非 LLM 显式标 EXTRACTED）
- [ ] **3.2.4** 跟 lint_wiki 集成：`lint_wiki()` 末尾调 `build_wiki_graph()`，新检查项追加到报告末尾（L7）。

### 3.3 风险与约束

- **markdown extractor 正则复杂度**：graphify 的 `_MD_INLINE_LINK_RE` 处理 `<bracketed>` 形式、CommonMark title、嵌套 paren——直接借鉴但要测试。
- **节点歧义**：`[[Apple]]` 可能指公司也可能指水果。第一版按 name 唯一建节点，歧义时 lint 报 warning。
- **图重建性能**：大 wiki（>1000 文件）重建要 <10s。mtime 增量是关键（L8）。

**验收**：L1 + L3 + L8。新建 `tests/skills/llm_wiki/test_graph_build.py`。

---

## 4. 任务三：confidence tag（EXTRACTED vs INFERRED）

### 4.1 定义（对齐 graphify 语义）

| 标签 | 定义 | llm_wiki 来源 |
|---|---|---|
| **EXTRACTED** | 源文档直接有的、明确陈述的 | markdown 里的 `[[wikilink]]` / `[inline link](url)` / `` `code mention` ``；源文档明确说"A 是 B 的成员" |
| **INFERRED** | LLM 推断的、源文档没直接说 | LLM semantic pass 从 PDF/图片/视频抽出的 entity relation；LLM 写 wikilink 时根据上下文推断的关系 |

### 4.2 改动清单（满足 L4）

- [ ] **4.2.1** wikilinks 语法扩展（向后兼容）：
  - 现有：`[[link]]`（不带 tag）→ 默认 `EXTRACTED`（markdown 里直接有的链接）
  - 新增：`[[link|EXTRACTED]]` / `[[link|INFERRED]]`（显式标）
  - 解析：`build_wiki_graph` 解析 wikilink 时检查 `|` 后的 tag，无 tag 默认 EXTRACTED
- [ ] **4.2.2** frontmatter 新增可选字段 `extracted_entities` / `extracted_relations`（L4 保证可选）：
  ```yaml
  ---
  title: ...
  extracted_entities: [entity-a, entity-b]    # 新增可选
  extracted_relations:                         # 新增可选
    - {src: entity-a, dst: entity-b, type: member_of, confidence: EXTRACTED}
  ---
  ```
- [ ] **4.2.3** LLM ingest prompt 模板更新（SKILL.md §"Core Operations > Ingest"）：
  - LLM 写 wikilink 时鼓励显式标 `|EXTRACTED` 或 `|INFERRED`
  - 多模态 ingest 抽 relation 时必须标 confidence
- [ ] **4.2.4** lint 加 confidence 分布统计（追加到报告末尾，L7）：
  - EXTRACTED vs INFERRED 比例
  - INFERRED 比例 >70% 的页面 warning（"该页主要靠推断，建议人工核实"）

### 4.3 风险与约束

- **现有 wikilinks 全是 EXTRACTED**：现有 wiki 升级后所有边默认 EXTRACTED，合理（markdown 里直接有的）。
- **LLM 标 tag 的诚实度**：LLM 可能误标（把推断的标成 EXTRACTED）。lint 只能统计分布，不能验证语义正确性。这是 LLM-driven 的固有局限，graphify 也一样（graphify 的 INFERRED 是 AST resolver 推断的，比 LLM 标更可靠）。
- **多模态 ingest 的 EXTRACTED 判定**：从 PDF 抽出的"A 是 B 的成员"如果 PDF 里直接写了，是 EXTRACTED；如果是 LLM 推断的，是 INFERRED。需要 LLM prompt 里明确区分。

**验收**：L4 + L7。新建 `tests/skills/llm_wiki/test_confidence_tag.py`。

---

## 5. 任务四：图算法查询（对齐 graphify `query` / `path` / `explain`）

### 5.1 目标

把 `search_wiki(keyword)` 从 grep 升级到图算法查询，对齐 graphify 的 `query` / `path` / `explain` 命令。

### 5.2 改动清单（满足 L1 / L6）

- [ ] **5.2.1** 新增 `skills/builtin/llm_wiki/scripts/graph_query.py`（独立模块）：
  - `path(a_name: str, b_name: str, max_depth: int = 5) -> PathResult`：双向 BFS 最短路径，返回 hops + 每跳的 edge_type + confidence
  - `god_nodes(limit: int = 10) -> list[NodeDegree]`：纯 SQL 度数排序（不需要 networkx）
  - `explain(node_name: str) -> NodeExplainResult`：节点的所有邻居 + 边类型 + confidence 分布 + 来源文件
  - `communities() -> list[Community]`：Leiden/Louvain 社区检测（依赖 networkx + python-louvain，L5 缺失时退化成按 god_nodes 聚类）
  - `query(question: str) -> SubgraphResult`：自然语言 → jieba 分词 → 节点召回（name LIKE + 嵌入相似度如果有） → 子图（含 1-2 跳邻居）
- [ ] **5.2.2** `PathResult` / `PathHop` / `NodeDegree` / `NodeExplainResult` / `Community` / `SubgraphResult` dataclass（参考 code_intel 的 query.py 模式）
- [ ] **5.2.3** BFS 算法跨包共享：**这是第四套图了**（graph_rag / code_intel / llm_wiki + 之前的 GraphRAG 多跳 TODO）。建议把 BFS util 抽到 `pub/func/graph/bfs.py` 或 `agent/tools/pub_base/graph_bfs.py`：
  - 抽象接口 `GraphBackend.get_neighbors(node_id) -> list[node_id]`
  - 三套存储（SNKV / code_intel SQLite / llm_wiki SQLite）各自实现这个接口
  - BFS 算法只写一遍，三套复用
  - **不强制**：第一版各自实现，等四套都做完再考虑抽取（避免过早抽象）
- [ ] **5.2.4** SKILL.md 新增"图查询"章节，文档化 path/god_nodes/explain/communities/query 用法。
- [ ] **5.2.5** `search_wiki(keyword)` 保留不动（L1），新增 `graph_query(question)` 作为推荐路径。SKILL.md 引导用户优先用 graph_query。

### 5.3 风险与约束

- **BFS 算法重复实现**：跟 GraphRAG 多跳 TODO + code_intel TODO 的 BFS 是同构的。建议跨包共享 util（§5.2.3），但不强制。
- **Leiden/Louvain 依赖**：`networkx` + `python-louvain`（比 `graspologic` 轻），缺失时 `communities()` 退化成按度数聚类（L5）。
- **`query(question)` 自然语言转图查询**：第一版用 jieba 分词 + name LIKE 召回，不上嵌入（避免引入 embedding 依赖）。第二版可考虑复用 graph_rag 的 BGE-M3 embedding（如果 §GraphRAG 稀疏召回 TODO 完成后）。
- **图查询性能**：单次 path 查询 p95 <500ms（<10k 节点规模）。

**验收**：L1 + L5 + L6。新建 `tests/skills/llm_wiki/test_graph_query.py`。

---

## 6. 任务五：lint 增强（图健康检查）

### 6.1 目标

现有 `lint_wiki()` 已经在做"图不健康状态检测"（孤立/断链/矛盾），但没显式图数据结构。§3 显式图建立后，lint 可以加真正的图指标。

### 6.2 改动清单（满足 L7）

- [ ] **6.2.1** lint 末尾追加（不动前 10 项）：
  - **图连通性**：连通分量数、最大分量大小、孤立子图列表（连通分量 = 1 是健康，>3 是 warning）
  - **confidence 分布**：全图 EXTRACTED vs INFERRED 比例、INFERRED 比例 >70% 的页面列表
  - **god nodes 检查**：度数 top 10 节点（哪些 entity/concept 是中枢）
  - **跨 community 断裂**：如果 §5 communities 完成，检查 community 之间的桥接边数（<3 是 warning，说明 wiki 内容分裂）
- [ ] **6.2.2** lint 报告 schema 扩展：现有报告字段保留，新字段追加（L7）。
- [ ] **6.2.3** `log.md` 自动追加 lint 行为不变（append-only 协议）。

**验收**：L7。新建 `tests/skills/llm_wiki/test_lint_graph.py`。

---

## 7. 明确不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 替换 markdown 为纯图存储 | 违反 llm_wiki 的"人类可读可编辑"设计原则；markdown 是 llm_wiki 比 graphify 强的地方 |
| 把 llm_wiki 改成 LangChain tool | llm_wiki 是 skill 内脚本，不是 tool（L6）；改 tool 会破坏 skill/tool 边界 |
| 上 tree-sitter 解析 markdown | graphify 用正则就够了（markdown 比代码结构简单），上 tree-sitter 是过度工程 |
| 跨 wiki 实例的统一图 | llm_wiki 设计上是单实例 KB，跨实例是 Graphify Cloud 的商业能力，不跟 |
| 把 llm_wiki 的图跟 code_intel/graph_rag 合并 | 这是架构层统一图抽象，优先级低于先把每套图各自做强（同 code_intel TODO §5） |
| 视频实时转录（流式） | 第一版只支持文件级转录（faster-whisper batch），实时流式是另一回事 |

---

## 8. 执行顺序与依赖

```
§2 多模态 ingest（高 ROI，3-5 天，复用 sherry 已有能力）
  │  ├─ PDF: 复用 multimodal_rag mineru
  │  ├─ image: 复用 image_to_text / VLM
  │  ├─ audio/video: 复用 speech_to_text / video_text_to_text
  │  └─ office: 新增 python-docx / openpyxl（optional）
  │
  ↓ （§2 抽取的 entities/relations 需要存储）
§3 显式图层 SQLite（基础设施，2-3 天）
  │  ├─ schema: wiki_nodes / wiki_edges / wiki_graph_meta
  │  ├─ build_wiki_graph() 增量解析 markdown
  │  └─ 借鉴 graphify markdown extractor 正则
  │
  ↓ （§3 有了图，才能加 confidence tag 和图算法）
§4 confidence tag（轻改动，1-2 天）
  │  ├─ wikilinks 语法扩展 [[link|EXTRACTED]]
  │  └─ lint 加 confidence 分布
  │
  ↓
§5 图算法查询（中改动，3-5 天）
  │  ├─ path A B 双向 BFS（跨包呼应 GraphRAG/code_intel TODO）
  │  ├─ god_nodes / explain
  │  ├─ communities（Leiden/Louvain，optional）
  │  └─ query 自然语言子图查询
  │
  ↓
§6 lint 增强（轻改动，1 天）
     ├─ 图连通性
     ├─ confidence 分布
     └─ god nodes / community 桥接检查
```

**总工程量**：~10-15 天。建议分批：

1. **第一批（2-3 天）**：§3 显式图层基础设施 + §4 confidence tag——先有图能查
2. **第二批（3-5 天）**：§5 图算法查询——可见的功能跃迁
3. **第三批（3-5 天）**：§2 多模态 ingest——扩展内容类型
4. **第四批（1 天）**：§6 lint 增强——把图指标纳入健康检查

**注意**：§2 和 §3 顺序可调换——如果先做多模态 ingest，抽取的 entities/relations 可以暂存 markdown，等 §3 显式图建好再回填。但建议先 §3 后 §2，避免多模态结果没地方存。

---

## 9. 验收标准

### 9.1 硬约束验收（每项必须过）

- [ ] **L1**：现有函数签名和返回值不变；新功能走新函数。
- [ ] **L2**：`raw/{articles,papers,transcripts,assets}` 结构保留；新增二进制 ingest 走扩展 category 或独立子目录。
- [ ] **L3**：SCHEMA.md / index.md / log.md 协议不变；新字段可选。
- [ ] **L4**：现有 frontmatter 字段不动；新字段（`extracted_entities` / `extracted_relations`）可选。
- [ ] **L5**：`python-docx` / `openpyxl` / `networkx` / `python-louvain` 缺失时对应功能退化而不是报错。
- [ ] **L6**：不进 `_MAIN_TOOLS_BUILDERS`；新查询函数走 skill 脚本路径。
- [ ] **L7**：lint 现有 10 项检查输出 byte-identical；新检查追加到报告末尾。
- [ ] **L8**：markdown 改动后图重建是增量的（mtime 检查），单文件改动重建 <1s。

### 9.2 功能验收

- [ ] `save_binary_source(pdf_bytes, 'pdf')` 能 ingest PDF，抽出的 entities/relations 进显式图。
- [ ] `build_wiki_graph()` 解析所有 markdown 的 wikilinks，图节点数 = entity/concept 页数 + source 文件数。
- [ ] wikilink `[[Apple|INFERRED]]` 解析出的边 confidence = "INFERRED"。
- [ ] `path("Apple", "Battery")` 返回最短路径 hops 序列正确（fixture 验证）。
- [ ] `god_nodes(10)` 返回度数 top 10 节点。
- [ ] `communities()` 在 networkx 缺失时退化（不报错）。
- [ ] `explain("Apple")` 返回节点的所有邻居 + confidence 分布 + 来源文件。
- [ ] `lint_wiki()` 报告末尾新增图连通性 / confidence 分布 / god nodes 三项。

### 9.3 回归验收

- [ ] 现有 markdown wiki 实例升级后，`search_wiki(keyword)` 行为不变（L1）。
- [ ] 现有 SCHEMA.md 不需要重写就能被新代码读（L3）。
- [ ] 现有 frontmatter 不需要改字段就能被新代码读（L4）。
- [ ] 卸载 `networkx` / `python-louvain` / `python-docx` / `openpyxl` 后，core/ingest/search 仍正常工作（L5）。
- [ ] 现有 lint_wiki 报告前 10 项 byte-identical（L7）。

---

## 10. 跟其他 TODO 的关系：四套图改造系列

这是 GraphRAG/code_intel 系列的第四个 TODO。sherry 现在有四套独立的"图"在多跳查询上都缺能力：

| TODO | 图存储 | 当前查询能力 | 改造目标 |
|---|---|---|---|
| `计划_GraphRAG_稀疏召回与重排调优.md` | graph_rag SNKV | 向量召回 + LLM 推理 | 加 BGE-M3 sparse + 重排 |
| `计划_GraphRAG_多跳路径查询缺口.md` | graph_rag SNKV | 无 BFS | 加 BFS + beam search |
| `计划_code_intel_能力暴露与graphify对齐.md` | code_intel SQLite | impact 单向 BFS | 加 path/imports/inherits/communities |
| **本文档** | **llm_wiki SQLite（新建）** | **search_wiki grep** | **加显式图 + 多模态 + path/communities** |

**架构层洞察（重要）**：

四套图各自实现 BFS / path 查询，算法同构。**建议在四套都做完后，抽取共享 BFS util 到 `pub/func/graph/bfs.py` 或 `agent/tools/pub_base/graph_bfs.py`**：

- 抽象接口 `GraphBackend.get_neighbors(node_id) -> list[node_id]`
- 四套存储（SNKV / code_intel SQLite / llm_wiki SQLite / GraphRAG SNKV 多跳）各自实现这个接口
- BFS / beam search / path 算法只写一遍，四套复用

但这是**架构层统一图抽象**，优先级低于先把每套图各自做强。本文档 §5.2.3 提到这个建议，不强制本 TODO 实施。

---

## 11. 关键参考

### sherry 内部（复用）

- llm_wiki 当前 ingest：`skills/builtin/llm_wiki/scripts/ingest.py:14`（`save_source` 仅字符串）
- llm_wiki 当前 search：`skills/builtin/llm_wiki/scripts/search.py:21`（`search_wiki` 是 grep glob）
- llm_wiki 当前 lint：`skills/builtin/llm_wiki/scripts/__init__.py`（10 项检查）
- multimodal_rag mineru parser：`skills/builtin/core/multimodal_rag/scripts/graph_rag/core.py:162`（`get_rag_anything`）
- multimodal_rag VLM：`skills/builtin/core/multimodal_rag/scripts/graph_rag/core.py:31`（`_vision_model_func`）
- image_to_text skill：`skills/builtin/core/image_to_text/scripts/core.py`
- REQUIRED_SKILLS（multimedia chain）：`skills/loader.py:244`

### graphify（借鉴）

- markdown extractor 正则：`D:/selfProj/graphify/graphify/extractors/markdown.py`（`_MD_WIKILINK_RE` / `_MD_INLINE_LINK_RE` / `_MD_BARE_MENTION_RE` / `_MD_PATH_MENTION_RE`）
- EXTRACTED / INFERRED confidence tag 语义：graphify README.md L37, L107
- graphify query/path/explain 命令：graphify README.md L102-107
- graphify 多模态 extras：README.md L266-290（`pdf` / `office` / `google` / `video`）

### 跨包呼应

- BFS 算法：`TODO/计划_GraphRAG_多跳路径查询缺口.md` §1.2（graph_rag 上 BFS）+ `TODO/计划_code_intel_能力暴露与graphify对齐.md` §3.2（code_intel 上 path）——本文档 §5 是第三处 BFS 实现，建议抽取共享 util
- 硬约束模式：参考 `TODO/计划_code_intel_能力暴露与graphify对齐.md` §1 的 C1-C8 模式，本文档 L1-L8 同构
