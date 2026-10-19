# 计划：code_intel 能力暴露 + 对齐 graphify 核心能力改造

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- **硬约束**：**不破坏 code_intel 原有功能**——所有改动必须向后兼容，详见 §1
- 关联代码：
  - `agent/tools/code_intel/__init__.py`（对外接口）
  - `agent/tools/code_intel/extract.py`（tree-sitter 抽取层）
  - `agent/tools/code_intel/indexer.py`（SQLite 索引 + 两 pass call graph resolution）
  - `agent/tools/code_intel/query.py`（explore/callers/callees/impact 查询）
  - `agent/tools/code_intel/tools.py`（5 个 researcher_only LangChain 工具）
  - `agent/tools/code_intel/roots.py`（多根解析）
  - `skills/builtin/code_wiki/SKILL.md`（LLM-driven wiki 生成器）
  - `skills/builtin/code_wiki/scripts/core.py`（路径管理）
- 关联文档：`TODO/计划_GraphRAG_多跳路径查询缺口.md`（BFS 算法跨包呼应）
- 关联评测：`evals/graph_rag/suite.py`（code_intel 暂无独立 evals，需新建）

---

## 0. 背景：code_intel 已是 mini-graphify（已核实）

经过代码核查，sherry 的 `agent/tools/code_intel/` 已经具备 graphify 的核心能力雏形：

### 0.1 已有能力清单

| graphify 卖点 | sherry code_intel 现状 | 位置 |
|---|---|---|
| tree-sitter AST 解析 | `extract.py` 支持 Python/TypeScript/TSX/Rust/Go 5 种 | `extract.py:62, 71-85` |
| 真正的图数据库 | SQLite WAL + `symbols` / `call_edges` / `index_meta` 三表 | `indexer.py:27-71` |
| 跨文件 link 自动解析 | 两 pass call-graph resolution（first pass 抽 symbols，second pass resolve callee_id） | `indexer.py:118` |
| EXTRACTED vs INFERRED 标记 | `call_edges.resolved` 字段（0/1）—— **字段已存，只是没在输出暴露** | `indexer.py:50` / `query.py:51` |
| 最短路径 / 图遍历 | `impact()` 已实现 BFS，带 `depth` + `via`——**单向反向（找谁调用 X）** | `query.py:289-320` |
| 增量更新 | `mtime + size` 判断变化 | `indexer.py:144` |
| 查询能力 | explore / callers / callees / impact / semantic_code_search 五个 | `tools.py:1-13` |
| ast-grep 重写 | `ast_grep/` 子包完整 | `agent/tools/code_intel/ast_grep/` |
| LSP 集成 | `lsp/` 子包，带 tree-sitter fallback | `agent/tools/code_intel/lsp/` |
| 符号级 diff | `symbol_diff.py`（subagent merge 时的 interface 变更检测） | `agent/tools/code_intel/symbol_diff.py` |

### 0.2 比 graphify 缺什么（已核实）

| graphify 能力 | sherry 缺什么 | 差距性质 |
|---|---|---|
| `calls` / `imports` / `inherits` / `mixes_in` 四种边 | 只有 `call_edges`（calls） | 工程量（加 3 张新表 + 抽取逻辑） |
| `path A B` 双向最短路径 | `impact` 只单向反向 | 工程量（复用 BFS 改双向） |
| `EXTRACTED` / `INFERRED` confidence tag 在用户输出 | `resolved` 字段有，输出层没映射 | 工程量（半天，字符串映射） |
| Leiden 社区检测 / god nodes | 无 | 新依赖 + 新 cluster pass |
| `graph.html` 交互式可视化 | 无 | 视觉糖，低优先级 |
| 40 种语言 | 5 种 | 工程量（按需扩展） |
| `--wiki` markdown 导出 | code_wiki skill 是 LLM-driven，跟 code_intel **无耦合** | 范式差（要从 LLM 写改成 SQLite 导出） |
| 跨 20+ AI 助手集成 | 仅 sherry agent | 设计目标不同，不跟 |
| YC S26 商业化 | 无 | 设计目标不同，不跟 |

### 0.3 关键事实：code_intel 是 RESEARCHER-only

**5 个工具都只在 RESEARCHER subagent 上注入，不进 `_MAIN_TOOLS_BUILDERS`**（`tools.py:1-13`）。每个工具带 `metadata={"scope": "researcher_only", "idempotent": True}`（`tools.py:40`）。

**这是改造的硬约束**：新增工具（`path` / `imports` / `inherits` / `communities`）也必须保持 `researcher_only` 范围，不能改变这个边界。

---

## 1. ⚠️ 硬约束：不破坏 code_intel 原有功能

**所有改动必须满足以下向后兼容性**：

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **C1**：`call_edges` 表 schema 不动 | 新增边类型用并行新表（`import_edges` / `inherit_edges` / `mixin_edges`），不修改 `call_edges` 列定义 | 现有索引文件（旧 SQLite DB）能直接被新版读取，不报 schema 错 |
| **C2**：`impact()` 行为不变 | 新增 `path()` 函数独立实现，不复用 `impact` 内部状态；`impact` 的签名、返回值、BFS 行为一字不改 | 现有调用 `impact()` 的代码（RESEARCHER subagent）输出 byte-identical |
| **C3**：`CalleeInfo.resolved` 字段语义不变 | 新增 `confidence: str` 字段（`"EXTRACTED"` / `"INFERRED"`），不修改 `resolved: bool` 的语义和默认值 | 现有读 `resolved` 字段的代码不报错；新增 `confidence` 是 derived field |
| **C4**：现有 5 个工具签名和输出格式不变 | `explore` / `callers` / `callees` / `impact` / `semantic_code_search` 的 input schema、output JSON shape 不动；新增工具走独立 input/output class | 现有 RESEARCHER subagent 调用旧工具不需要改任何 prompt 或 parser |
| **C5**：RESEARCHER-only 范围不变 | 新增工具也打 `scope: researcher_only`，不进 `_MAIN_TOOLS_BUILDERS`，不暴露给 main agent | `agent/tools/__init__.py` 不需要为 path/imports/inherits 新增注册 |
| **C6**：code_wiki 现有路径管理不动 | `init_repo_wiki` / `get_wiki_path` / `get_repo_wiki_path` / `get_modules_dir` / `get_diagrams_dir` 签名和实现不变；新增导出函数走独立入口 | 现有 SKILL.md 调用路径全部继续可用 |
| **C7**：不引入破坏性依赖 | 新依赖（如 `networkx` + 社区检测库）必须是 optional extras，缺失时退化而不是报错 | `import networkx` 失败 → 跳过社区检测，其他查询正常 |
| **C8**：增量索引行为不变 | `mtime + size` 判断逻辑不改；新边类型同样走增量路径 | 现有索引 DB 不需要重建就能继续使用 |

---

## 2. 任务一：暴露 code_intel 已有能力（高 ROI / 低改动，**必做**）

### 2.1 暴露 `resolved` 为 EXTRACTED/INFERRED confidence tag

**当前**：`CalleeInfo.resolved: bool` 已存在（`query.py:51`），`callees` 工具返回 JSON 里有这字段，但 LLM/用户看不到明确的 EXTRACTED/INFERRED 语义。

**改造**：

- [ ] **2.1.1** `query.py`：在 `CalleeInfo` dataclass 加 `confidence: str` 字段（derived，不动 `resolved: bool`）：
  ```python
  @dataclass(slots=True)
  class CalleeInfo:
      name: str
      target_file: str | None
      target_line: int | None
      call_line: int
      resolved: bool  # 不动
      confidence: str = ""  # 新增 derived："EXTRACTED" if resolved else "INFERRED"
  ```
- [ ] **2.1.2** `_callees_for` 构造 `CalleeInfo` 时填 `confidence`（不改 `resolved` 赋值）。
- [ ] **2.1.3** `tools.py` 的 callees 工具 JSON 输出里把 `confidence` 字段带上（不改 `resolved` 字段）。
- [ ] **2.1.4** `CallerInfo` 同样加 `confidence`（callers 侧）：caller 一定 EXTRACTED（源码里实锤有这调用），但 INFERRED 边在跨文件 resolve 失败时出现——这里更准确的语义是"caller 关系都是 EXTRACTED，但 caller_id 是否解析到 symbol 决定 callee 侧是否 INFERRED"。**需在 §2.1.3 实施时确认 caller/callee 两端 confidence 语义不能反**。

**风险**：低。纯字段添加，不动现有字段语义。**验收 C3**。

### 2.2 暴露 `impact` 的 BFS 路径

**当前**：`ImpactResult.entries` 里每条 `ImpactEntry` 已带 `depth: int` + `via: str`（`query.py:81-86`）。BFS 路径已经返回，只是 LLM 可能没充分利用。

**改造**：

- [ ] **2.2.1** `tools.py` 的 impact 工具输出 schema 里**显式标注** `via` 是"上一跳的 callee_name"（即路径上的边标签），让 LLM 知道这是路径而非单点。
- [ ] **2.2.2** impact 工具 description 补充说明"返回的是 transitive callers 的 BFS 路径，每个 entry 的 `via` 是从根 symbol 出发到这个 caller 经过的上一跳 callee_name"——纯文档改动，不动逻辑。

**风险**：零。纯文档/schema 描述改动。

---

## 3. 任务二：对齐 graphify 核心能力（中 ROI / 中改动）

### 3.1 扩展边类型：imports / inherits / mixes_in

**目标**：在 `call_edges` 旁边并行加三张表，抽取 Python/TS/TSX/Rust/Go 的 imports/extends/implements 关系。

**改造清单**（满足 C1 / C8）：

- [ ] **3.1.1** `indexer.py` 加 schema：
  ```sql
  CREATE TABLE IF NOT EXISTS import_edges (
      id INTEGER PRIMARY KEY,
      importer_id INTEGER NOT NULL,
      imported_name TEXT NOT NULL,
      imported_id INTEGER,           -- resolved target symbol id (可空)
      file_path TEXT NOT NULL,
      line INTEGER NOT NULL,
      resolved INTEGER DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS inherit_edges (
      id INTEGER PRIMARY KEY,
      child_id INTEGER NOT NULL,
      parent_name TEXT NOT NULL,
      parent_id INTEGER,             -- resolved
      inherit_type TEXT NOT NULL,    -- 'class' / 'interface' / 'trait'
      file_path TEXT NOT NULL,
      line INTEGER NOT NULL,
      resolved INTEGER DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS mixin_edges (
      id INTEGER PRIMARY KEY,
      host_id INTEGER NOT NULL,
      mixin_name TEXT NOT NULL,
      mixin_id INTEGER,
      file_path TEXT NOT NULL,
      line INTEGER NOT NULL,
      resolved INTEGER DEFAULT 0
  );
  ```
  索引模式参考 `call_edges` 的 `idx_edges_*` 模式。
- [ ] **3.1.2** `extract.py` 加抽取逻辑（每语言）：
  - **Python**：`import_statement`（imported_name 从 `name`/`dotted_name` 字段抽）、`class_definition` 的 `superclasses`（inherit_type=`class`）
  - **TypeScript/TSX**：`import_declaration`（imported_name 从 `source`/`clause` 抽）、`interface_declaration` 的 `heritage`、`class_declaration` 的 `heritage`（区分 `extends` 是 class、`implements` 是 interface）
  - **Rust**：`use_declaration`、`impl_item`（区分 `trait` 是 mixin-like、`type` 是 inherit）
  - **Go**：`import_declaration`、`type_spec`（`struct_type` 嵌入字段是 mixin、`interface_type` 嵌入是 inherit）
- [ ] **3.1.3** `extract.py` 的 `ExtractionResult` dataclass 加三个字段：`imports: list[ImportSite]`、`inherits: list[InheritSite]`、`mixins: list[MixinSite]`——不动现有 `symbols`/`calls` 字段。
- [ ] **3.1.4** `indexer.py` 的 `_index_directory` 增量 walk 里追加三张表的写入——复用现有两 pass resolution 模式：first pass 抽 + 存原始 `imported_name` 等，second pass 跨文件 resolve `imported_id`/`parent_id`/`mixin_id`。
- [ ] **3.1.5** `query.py` 加三个查询方法（复用 `_callers_for` / `_callees_for` 的 SQL helper 模式）：`imports_of(symbol)` / `importers_of(symbol)` / `inherits_from(symbol)` / `subclasses_of(symbol)` / `mixins_of(symbol)` / `mixins_in_by(symbol)`。

**风险**：
- **新增表多**：3 张表 + 各自索引。schema 兼容性必须测——C1 要求旧 DB 读取不报错。`CREATE TABLE IF NOT EXISTS` 保证这点。
- **抽取逻辑复杂**：TS 的 `import { x, y } from './foo'` 解析 named imports；Rust `use foo::bar as baz`；Python `from a.b import c, d`。需要逐语法测试。
- **跨语言 inherit_type 语义统一**：TS `implements` vs Rust `impl Trait for Type` vs Go struct embedding——要做语义映射表，避免混乱。

**验收**：C1 + C8。新建 `tests/agent/tools/code_intel/test_extract_edges.py`，每种语言一个 fixture 文件，断言抽出的 imports/inherits 边数和 resolved 状态。

### 3.2 加双向最短路径查询 `path(symbol_a, symbol_b)`

**目标**：复用 `impact` 的 BFS 逻辑，改成双向任意两点的最短路径。**独立新函数，不改 `impact`**（C2）。

**改造清单**：

- [ ] **3.2.1** `query.py` 加 `PathResult` 和 `path()` 方法：
  ```python
  @dataclass(slots=True)
  class PathHop:
      symbol: SymbolMatch
      edge_type: str   # 'calls' / 'imports' / 'inherits' / 'mixes_in'
      edge_confidence: str  # 'EXTRACTED' / 'INFERRED'

  @dataclass(slots=True)
  class PathResult:
      root_a: str
      root_b: str
      hops: list[PathHop] = field(default_factory=list)
      found: bool = False
      truncated: bool = False
  ```
- [ ] **3.2.2** `path()` 实现：双向 BFS——从 A 出发正向扩展（calls/imports/inherits/mixes_in 出边），从 B 出发反向扩展（callers/importers/subclasses/mixins_by 入边），中间相遇停止。复用 `_reverse_callers`（`query.py:386`）的双向 id+name 匹配模式。
- [ ] **3.2.3** 跳数上限走 `code_intel_call_graph_max_depth` 配置（复用现有，不引入新配置）。
- [ ] **3.2.4** `tools.py` 加 `path` 工具，input 是 `{symbol_a: str, symbol_b: str}`，scope=researcher_only，跟其他工具一致。

**风险**：
- 双向 BFS 比单向复杂，相遇判定要小心（同一边被两边都走到）。
- 跨边类型路径（A → calls → X → inherits → B）要不要支持？第一版只支持单一类型路径（默认 calls），按 query 参数 `edge_types` 过滤。

**验收**：C2 + C4 + C5。新建 `tests/agent/tools/code_intel/test_path_query.py`，构造已知路径的 fixture，断言 `path(A, B)` 返回的 hops 序列正确。

### 3.3 加 god_nodes / communities 查询（可选，依赖新依赖）

**目标**：复用 graphify 的 Leiden 社区检测思路，给 code_intel 加子系统发现能力。

**改造清单**：

- [ ] **3.3.1** 在 `indexer.py` 加 cluster pass：index 完成后，把 call_edges + 新三张表喂给 networkx 图，跑 Leiden（或 Louvain，更轻量）。
- [ ] **3.3.2** 结果写回 SQLite 新表 `communities`（symbol_id → community_id）。
- [ ] **3.3.3** `query.py` 加 `god_nodes(limit=10)` 和 `community_of(symbol)` 查询。
- [ ] **3.3.4** 新依赖管理：
  - `networkx` 必装（轻量）
  - `python-louvain` 优先（比 `graspologic` 轻，只有 Louvain）
  - 两者都缺失时：cluster pass 跳过，`god_nodes` 退化成按 degree 排序（纯 SQL，不需要 networkx），`community_of` 返回 None——满足 **C7**
- [ ] **3.3.5** 桌面打包体积评估：`networkx` ~5 MB，`python-louvain` ~1 MB，加起来 <10 MB，远低于 Sherry 1 GiB 上限。

**风险**：
- Leiden 和 Louvain 结果不一样，社区划分会抖动。选定一个（建议 Louvain，依赖更轻）。
- 大图聚类慢：>10k symbols 的仓库可能要几秒。加 `max_symbols_for_clustering` 上限，超过跳过。

**验收**：C7。新建 `tests/agent/tools/code_intel/test_communities.py`，断言依赖缺失时不报错。

---

## 4. 任务三：code_wiki skill 改造（高 ROI / 中改动）

### 4.1 目标

把 code_wiki 从 **LLM-driven markdown 生成** 改成 **基于 code_intel SQLite 导出** 的混合模式：

- 主体内容（README/architecture/modules/diagrams）从 SQLite 导出，**确定性，0 LLM**
- 仍保留 LLM 路径作 fallback（code_intel 索引不可用或语言不支持时）
- **不破坏 code_wiki 现有路径管理**（C6）

### 4.2 改动清单（满足 C6）

- [ ] **4.2.1** `skills/builtin/code_wiki/scripts/core.py` 新增 `export_wiki_from_index(repo_name, db_path)` 函数——不动 `init_repo_wiki` 等现有函数：
  ```python
  def export_wiki_from_index(repo_name: str, db_path: str) -> dict:
      """
      从 code_intel SQLite DB 导出 markdown wiki。
      确定性，0 LLM 调用。
      替代 LLM-driven 路径，但保留 fallback（见 §4.2.4）。
      """
  ```
- [ ] **4.2.2** 导出逻辑：从 SQLite 读 symbols + 各类 edges + communities（如果 §3.3 完成），按模板生成：
  - `README.md`：god_nodes + communities + 入口符号列表
  - `architecture.md`：按 community 分组的 Mermaid 流程图（每 community 一个 subgraph）
  - `modules/<community>.md`：每个 community 一个文档，列出该 community 的 symbols + cross-community edges
  - `diagrams/class-diagram.md`：从 inherit_edges 生成 Mermaid classDiagram
  - `diagrams/sequences.md`：**这步保留 LLM**——序列图需要追踪运行时调用流，AST-only 做不到，LLM 跑 query/callees 后再写
- [ ] **4.2.3** 模板化（`skills/builtin/code_wiki/templates/`）：
  - 新增 `index_from_db.md.j2` / `architecture_from_db.md.j2` / `module_from_db.md.j2` / `class_diagram_from_db.md.j2`
  - 用 Jinja2（Sherry 已经有依赖，没有的话用 f-string）
  - 不替换现有模板（`README.md` / `architecture.md` / `module.md` 等）——LLM-driven 路径继续用
- [ ] **4.2.4** SKILL.md 更新流程：第 1 步先尝试 `export_wiki_from_index`，失败（DB 不存在/语言不支持/code_intel 未索引）再 fallback 到现有 LLM-driven 流程。**两条路径并存**。
- [ ] **4.2.5** `.codewiki-state.json` 新增字段 `"generator": "code_intel_export"` 或 `"generator": "llm_driven"`，标明这次是哪条路径生成的——下次更新能判断要不要重建索引。

### 4.3 风险与约束

- **code_intel 只支持 5 种语言**：非 Python/TS/TSX/Rust/Go 的代码库，`export_wiki_from_index` 返回 `{"fallback": True, "reason": "unsupported_language"}`，SKILL.md fallback 到 LLM-driven。
- **code_intel 索引必须已存在**：调用 `export_wiki_from_index` 前要先调 `CodeIndexer.index_directory(root)`。SKILL.md 第 1 步加这个检查。
- **Mermaid 图节点数**：稠密图 community 可能 >20 节点，Mermaid 渲染崩。按 graphify 的做法，每 community 单独一个 subgraph，整体 cap 在 50 节点。
- **sequences.md 必须留 LLM**：AST 只能告诉你静态 call 关系，不能告诉你运行时调用流（哪个入口触发哪条链）。这步保留 LLM-driven 是合理设计，不是 fallback 的失败。

**验收**：C6。新建 `tests/skills/code_wiki/test_export_from_index.py`，断言：
- 给定一个 Python fixture 仓库 + 已索引 SQLite DB，`export_wiki_from_index` 生成所有预期文件
- 不支持的语言返回 `fallback: True`
- 现有 `init_repo_wiki` 等函数行为不变

---

## 5. 明确不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| graph.html 交互式可视化 | 视觉糖，不影响 agent 检索能力，优先级低于功能对齐 |
| 跨 20+ AI 助手集成 | sherry 设计目标不同，graphify 是个独立 CLI 工具，sherry 是嵌入式 agent |
| Graphify Cloud 商业版对齐 | 设计目标不同 |
| 把 code_intel 改成 main agent 可用 | **违反 C5**，RESEARCHER-only 是有意设计——main agent 用 code_intel 会绕过 subagent 隔离 |
| 替换 `code_wiki` 现有 LLM-driven 路径 | **违反 C6**，保留作 fallback |
| 统一 code_intel + graph_rag + wiki 的图抽象 | 工程量太大，先各自做强再说。架构层统一图抽象另起 TODO |
| 扩展到 graphify 的 40 种语言 | 按需加，冷门语言（BYOND DreamMaker/OCaml/Common Lisp）sherry 用户场景不需要 |

---

## 6. 执行顺序与依赖

```
§2 暴露已有能力（高 ROI 低改动，~1 天）
  │
  ├─ §2.1 暴露 resolved → confidence  (0.5 天)
  └─ §2.2 暴露 impact BFS 路径文档     (0 天，纯文档)
  │
  ↓
§3 对齐 graphify 核心能力（中 ROI 中改动，~5-7 天）
  │
  ├─ §3.1 扩展边类型 imports/inherits/mixes_in  (2-3 天，独立)
  ├─ §3.2 加 path A B 双向最短路径              (1 天，依赖 §3.1 才有意义)
  └─ §3.3 Leiden/Louvain 社区检测                (2-3 天，独立，但依赖 §3.1 边才更准)
  │
  ↓
§4 code_wiki 改造（高 ROI 中改动，~3-5 天）
  │
  └─ 依赖 §3.1（imports/inherits）+ §3.3（communities）才有内容可导出
     如果 §3 没做，§4 导出内容会很贫瘠
```

**总工程量**：~9-13 天。建议分批：

1. **第一批（1 天）**：§2 全部——立刻让现有能力被 LLM 用起来
2. **第二批（2-3 天）**：§3.1（扩展边类型）——为后续打基础
3. **第三批（1 天）**：§3.2（path 查询）——可见的功能跃迁
4. **第四批（2-3 天）**：§3.3（社区检测）——可选，依赖新依赖
5. **第五批（3-5 天）**：§4（code_wiki 改造）——把前面成果导出成文档

---

## 7. 验收标准

### 7.1 硬约束验收（每项必须过）

- [ ] **C1**：旧 SQLite DB（只有 call_edges）能被新版 code_intel 读取，不报 schema 错。
- [ ] **C2**：`impact(symbol)` 行为 byte-identical（同输入同输出）。
- [ ] **C3**：`CalleeInfo.resolved` 字段语义和默认值不变；`confidence` 是 derived。
- [ ] **C4**：现有 5 个工具的 input/output JSON shape 不变；新工具走独立 class。
- [ ] **C5**：新增工具都打 `scope: researcher_only`，`_MAIN_TOOLS_BUILDERS` 不变。
- [ ] **C6**：`init_repo_wiki` 等函数签名和实现不变；新函数 `export_wiki_from_index` 独立入口。
- [ ] **C7**：`networkx` / `python-louvain` 缺失时 cluster pass 跳过，其他查询正常。
- [ ] **C8**：`mtime + size` 增量逻辑不变；新边类型同样走增量。

### 7.2 功能验收

- [ ] `callees(symbol)` 返回的 JSON 里有 `confidence: "EXTRACTED"` / `"INFERRED"`。
- [ ] `path("UserService", "DatabasePool")` 返回 hops 序列正确（手工 fixture 验证）。
- [ ] `imports_of("module.foo")` 返回该模块的 imports 列表（Python/TS fixture 验证）。
- [ ] `god_nodes()` 返回度数最高的 top 10 符号（cluster 退化模式也工作）。
- [ ] `export_wiki_from_index("repo_name", db_path)` 生成 README/architecture/modules/diagrams 完整目录。
- [ ] 非支持语言（如 Java 仓库）`export_wiki_from_index` 返回 `fallback: True`，SKILL.md fallback 到 LLM 路径。

### 7.3 回归验收

- [ ] 现有 RESEARCHER subagent 在改完前后跑同一 query，`explore`/`callers`/`callees`/`impact`/`semantic_code_search` 输出 byte-identical（C4）。
- [ ] 现有 code_wiki skill 在改完前后跑同一 repo，LLM-driven 路径（fallback）产物结构不变（C6）。
- [ ] `evals/graph_rag/suite.py` 不受影响（code_intel 改动跟 graph_rag 无耦合）。

---

## 8. 与其他 TODO 的关系

| TODO | 解决的问题 | 与本 TODO 的关系 |
|---|---|---|
| `计划_GraphRAG_稀疏召回与重排调优.md` | graph_rag 节点召回精度（BGE-M3 sparse + 重排） | 独立，互不影响 |
| `计划_GraphRAG_多跳路径查询缺口.md` | graph_rag 多跳路径发现（BFS + beam search） | **BFS 算法跨包呼应**——本 TODO 的 `path()` 跟那边的 BFS 同构，但分别在 code_intel/graph_rag 两套存储上实现。两套都做完后，可考虑抽取共享 BFS util（不强制） |

---

## 9. 关键参考

- code_intel 工具入口：`agent/tools/code_intel/tools.py:1-13`（5 个 researcher_only 工具）
- code_intel 工具 scope 元数据：`agent/tools/code_intel/tools.py:40`（`scope: researcher_only, idempotent: True`）
- tree-sitter 抽取层：`agent/tools/code_intel/extract.py:62, 71-85`（5 种语言 grammar loader）
- SQLite schema：`agent/tools/code_intel/indexer.py:27-71`（symbols + call_edges + index_meta）
- 两 pass call-graph resolution：`agent/tools/code_intel/indexer.py:118`
- `resolved` 字段：`agent/tools/code_intel/indexer.py:50`（schema）+ `agent/tools/code_intel/query.py:51`（dataclass）
- `impact` BFS 实现：`agent/tools/code_intel/query.py:289-320`（单向反向 BFS）
- `CallerInfo` / `CalleeInfo` / `ImpactEntry` dataclass：`agent/tools/code_intel/query.py:33-97`
- code_wiki 路径管理：`skills/builtin/code_wiki/scripts/core.py:23-55`
- code_wiki 现有模板：`skills/builtin/code_wiki/templates/`（README.md / architecture.md / getting-started.md / module.md）
- graphify 边类型：calls / imports / inherits / mixes_in（README.md L119）
- graphify confidence tag：EXTRACTED（explicit in source）/ INFERRED（derived by resolution）（README.md L37, L107）
- graphify 查询模式：`graphify query` / `graphify path A B` / `graphify explain`（README.md L102-107）
