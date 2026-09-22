# 代码检索框架 — Subagent 专属实现计划

> 基于对 5 个项目（oh-my-openagent-dev、opencode-dev、hermes-agent-main、deepagents-main、openclaw）的代码检索框架调研，为 Sherry 设计三阶段代码检索能力：tree-sitter 符号索引 + LSP 精确检索 + Embedding 语义搜索。
>
> **核心约束：仅限 subagent 可用，main agent 无法调用。**
>
> **前置依赖：subagent 功能角色分工（`FunctionalRole`，已落地）。**
> 代码检索工具仅注入 `FunctionalRole.RESEARCHER` 的 subagent，`FunctionalRole` 枚举、角色定义加载器、`spawn_subagent_direct()` 的 `functional_role_hint` 参数传递均已具备。
> 实施顺序：subagent 功能角色分工（已落地）→ 本计划 Phase 1（**✅ 已落地**）→ Phase 2（**由 SUPPLEMENT 的 Phase 2S 替代**）→ Phase 3。
>
> **实施策略：Phase 1 已落地并评估；P2/P3 待单独派工。**

---

## 目录

1. [架构概览](#架构概览)
2. [设计决策](#设计决策)
3. [Phase 1 — AST 符号索引 + 调用图](#phase-1--ast-符号索引--调用图) — ✅ 已落地
4. [Phase 2 — LSP 精确检索](#phase-2--lsp-精确检索) — 由 SUPPLEMENT 的 2S 替代
5. [Phase 3 — Embedding 语义搜索](#phase-3--embedding-语义搜索)
6. [修改文件](#修改文件)
7. [测试计划](#测试计划)
8. [实施步骤顺序](#实施步骤顺序)
9. [安全清单](#安全清单)
10. [关键参考文件](#关键参考文件)

---

## 架构概览

```
┌─ Subagent 工具层（注入于 _build_child_agent，仅 RESEARCHER 角色，main agent 不可见）──┐
│                                                                          │
│  Phase 1 — AST 符号索引（tree-sitter）                                    │
│  ├─ explore      模糊意图 → 符号源码 + 调用路径                             │
│  ├─ callers      谁调了这个符号                                            │
│  ├─ callees      这个符号调了谁                                            │
│  └─ impact       修改此符号的影响面                                        │
│                                                                          │
│  Phase 2 — LSP 精确检索                                                    │
│  ├─ lsp_goto_definition    跳转到定义                                      │
│  ├─ lsp_find_references    查找所有引用                                    │
│  ├─ lsp_workspace_symbol   工作区符号搜索                                  │
│  └─ lsp_call_hierarchy      调用层级（incoming/outgoing）                   │
│                                                                          │
│  Phase 3 — Embedding 语义搜索                                              │
│  └─ semantic_code_search   概念/意图 → 代码片段                            │
│                                                                          │
│  现有（所有 agent 可用）                                                   │
│  ├─ search_files  正则文本搜索（fallback）                                  │
│  └─ read_file     精确读取                                                │
└──────────────────────────────────────────────────────────────────────────┘

检索流程：模糊意图 → semantic_code_search(P3) / explore(P1)
       → 精确定位 → lsp_find_references(P2) / callers(P1)
       → 兜底 → search_files
```

### 可复用基础设施

| 设施               | 位置                                                                   | 复用场景             |
| ------------------ | ---------------------------------------------------------------------- | -------------------- |
| Embedding 模型     | `models/embed_model/core.py` → `CustomEmbedding`                       | Phase 3 代码块向量化 |
| Reranker           | `models/reranker_model/` → CrossEncoder                                | Phase 3 搜索结果重排 |
| Embedding 存储模式 | `context_engine/embeddings/store.py` → SQLite BLOB + cosine            | Phase 3 代码块存储   |
| SQLite             | 全项目通用（WAL 模式）                                                 | Phase 1 符号索引存储 |
| Subagent 注入点    | `spawn/core.py::_build_child_agent()`                                  | 所有 Phase 工具注入  |
| 文件遍历           | `agent/tools/file_tools/search_scan.py` → `bounded_walk` + `ScanState` | 未来检索遍历参考     |
| 环境清洗           | `agent/tools/pub_base/env_scrub.py` → `scrub_env()`                    | LSP 子进程环境       |

---

## 设计决策

### 为什么选 tree-sitter 而非 ctags / LSP-only

| 维度     | tree-sitter（选定）             | ctags                  | LSP-only               |
| -------- | ------------------------------- | ---------------------- | ---------------------- |
| 多语言   | 一套 API，按 grammar 扩展       | 独立解析器，格式不统一 | 每语言需独立 server    |
| 调用图   | 可提取 `call` 节点 → 跨文件解析 | 仅符号定义，无调用关系 | 需逐文件 hover/def，慢 |
| 离线索引 | 一次解析，持久化 SQLite         | 快但信息少             | 实时查询，无持久化     |
| 精确度   | AST 级，语法正确                | 正则匹配，有误报       | 最精确（语义级）       |
| 依赖     | tree-sitter + grammar pip 包    | ctags 二进制           | LSP server 进程        |

**结论：** Phase 1 用 tree-sitter 做离线全量索引（符号 + 调用图），Phase 2 用 LSP 做精确即时查询，两层互补。

### 为什么仅限 RESEARCHER 角色 subagent

- Main agent 的检索需求由 `search_files` + `read_file` 覆盖
- 代码索引构建开销大（首次全量扫描），不应由 main agent 触发
- 代码检索是只读探索能力，属于 RESEARCHER 功能角色（"read-only, codebase/web search"）
- EXECUTOR 是写入/执行角色，代码检索非其职责（EXECUTOR 应专注代码执行，检索由上游 RESEARCHER 完成）
- REVIEWER 是只读审计角色，侧重 diff 审查而非代码库探索
- 防止 main agent 利用 explore 绕过 middleware 链的推理约束

### Main agent 不可用 + 非 RESEARCHER 不可用的实现方式

代码检索工具**不加入** `_MAIN_TOOLS_BUILDERS`，只在 `_build_child_agent()` 中**当 `functional_role == RESEARCHER` 时**注入：

```python
# spawn/core.py::_build_child_agent — 仅 RESEARCHER 角色 subagent 路径
# 前置依赖：subagent 功能角色分工（已落地）
#   functional_role 和 role_def 已在 Phase 4.5 解析完毕，作为参数传入
base_tools = tools if tools is not None else build_main_tools()
filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

# ↓↓↓ 新增：仅 RESEARCHER 角色注入 code_intel 工具
final_tools = list(filtered_tools)
if functional_role == FunctionalRole.RESEARCHER:
    from agent.tools.code_intel import build_code_intel_tools
    code_intel_tools = build_code_intel_tools(session_id=run.child_session_key)
    final_tools = [*filtered_tools, *code_intel_tools]
```

- Main agent：`built_agent()` → `get_agent_tools()` → `_tools = build_main_tools()` — 无 code_intel 工具
- LEAF subagent + RESEARCHER 角色：获得 code_intel 工具
- LEAF subagent + EXECUTOR 角色：**无** code_intel 工具（写入/执行角色）
- LEAF subagent + REVIEWER 角色：**无** code_intel 工具（审计角色）
- LEAF subagent + GENERAL 角色：**无** code_intel 工具（GENERAL 默认回退，无专用检索需求）
- Orchestrator subagent：**无** code_intel 工具（OrCHESTRATOR 負责拆分任务，不直接检索）

### 支持语言

Phase 1 首批支持四种语言：

| 语言                    | tree-sitter grammar 包   | 文件扩展名                   |
| ----------------------- | ------------------------ | ---------------------------- |
| Python                  | `tree-sitter-python`     | `.py`                        |
| TypeScript / JavaScript | `tree-sitter-typescript` | `.ts`, `.tsx`, `.js`, `.jsx` |
| Rust                    | `tree-sitter-rust`       | `.rs`                        |
| Go                      | `tree-sitter-go`         | `.go`                        |

不支持的语言降级为 `search_files` 正则搜索。

---

## Phase 1 — AST 符号索引 + 调用图

### 已落地摘要

> **已落地**。落点：`config/features/agent_side/code_intel.py`、`config/path.py`（`CODE_INTEL_DIR`）、`.gitignore`（`.codeintel/`）、`agent/tools/code_intel/{__init__,extract,indexer,query,tools}.py`；注入点 `agent/tools/subagent/spawn/core.py::_build_child_agent()`（仅 `FunctionalRole.RESEARCHER`，永不加入 `_MAIN_TOOLS_BUILDERS`）+ `spawn/system_prompt.py`（RESEARCHER 检索指导）。

**实测事实（替代原规格中的猜测值）**

- **版本**：tree-sitter 0.26.0 / tree-sitter-python 0.25.0 / tree-sitter-typescript 0.23.2 / tree-sitter-rust 0.24.2 / tree-sitter-go 0.25.0。
- **API**：`Parser(Language(capsule))`；capsule 来自 `tree_sitter_python.language()` / `tree_sitter_typescript.language_typescript()`（tsx：`language_tsx()`）/ `tree_sitter_rust.language()` / `tree_sitter_go.language()`。不是旧式 `parser.set_language`。
- **实测节点名**（逐语法实探，非计划猜测）：
  - Python — `function_definition` / `class_definition` / `call`（callee：`function` 字段的 `identifier` 或 `attribute` 的 `attribute`）。
  - TypeScript — `function_declaration` / `class_declaration` / `method_definition` / `variable_declarator`（arrow/function 表达式）/ `call_expression`（`member_expression` 的 `property`）。
  - Rust — `function_item` / `struct_item` / `enum_item` / `trait_item` / `impl_item` / `call_expression` / `macro_invocation`。
  - Go — `function_declaration` / `method_declaration` / `type_spec` + `struct_type`/`interface_type` / `call_expression`（`selector_expression` 的 `field`）。

**与提案的差异**

- 提取层独立为 `code_intel/extract.py`（纯 bytes→符号/调用，无磁盘/DB）；`indexer.py` 只做增量遍历 + SQLite + 调用边解析；`query.py` 提供四个查询；`tools.py` 为 LangChain 包装。
- 增量判脏用 `index_meta(mtime, size)`（比计划的仅 mtime 更稳）。
- 新增 `code_intel_index_max_file_bytes`（默认 1MB）：超限文件跳过并记 warning；逐文件解析后即释放，流式/分批写库。
- fail-open：语法错误文件（`root_node.has_error`）、超大文件、未知扩展名、读/解析异常一律跳过并写 `index_meta.error`，工具调用不崩。
- 工具 `metadata={"scope": "researcher_only"}`；仅 RESEARCHER；main agent 与其它功能角色不可见。

（Phase 1 的完整规格细节已退役，历史实现见 git 记录。）

## Phase 2 — LSP 精确检索

> **本阶段由 [`CODE_INTEL_SUPPLEMENT.md`](CODE_INTEL_SUPPLEMENT.md) 的 Phase 2S 替代**（LSP 二进制发现 / 自动安装 / fallback 基础设施）；原计划的 4 个 LSP 工具保留不变。
>
> **前置条件：Phase 1 完成且评估通过（Phase 1 已落地）。**

### 架构

```
┌─ Subagent 进程 ──────────────────────────────────────┐
│                                                       │
│  lsp_goto_definition ─┐                               │
│  lsp_find_references  ├─→ LSPClient ──→ stdio ──→ LSP Server
│  lsp_workspace_symbol │     (JSON-RPC)                  │
│  lsp_call_hierarchy  ─┘                               │
│                                                       │
│  LSPClient:                                           │
│  ├─ 启动 LSP server 子进程                             │
│  ├─ initialize / initialized 握手                      │
│  ├─ textDocument/didOpen 通知                          │
│  ├─ 请求/响应 JSON-RPC over stdio                      │
│  ├─ 超时 + 进程清理                                     │
│  └─ 按 session 隔离（subagent 结束时 shutdown）         │
└───────────────────────────────────────────────────────┘
```

### LSP 服务器配置

| 语言          | LSP Server                   | 安装状态                         |
| ------------- | ---------------------------- | -------------------------------- |
| Python        | `basedpyright-langserver`    | 已安装（`basedpyright>=1.40.0`） |
| TypeScript/JS | `typescript-language-server` | 需安装                           |
| Rust          | `rust-analyzer`              | 需安装                           |
| Go            | `gopls`                      | 需安装                           |

### 新增文件（5 个）

| 文件                                     | 作用                                                          |
| ---------------------------------------- | ------------------------------------------------------------- |
| `config/features/agent_side/lsp.py`      | LSP 配置 TypedDict（server 路径、超时、启用语言）             |
| `agent/tools/code_intel/lsp/__init__.py` | 模块导出                                                      |
| `agent/tools/code_intel/lsp/protocol.py` | LSP 协议数据结构（Position/Range/Location/SymbolInfo）        |
| `agent/tools/code_intel/lsp/client.py`   | LSP JSON-RPC 客户端（stdio 传输，initialize/didOpen/request） |
| `agent/tools/code_intel/lsp/tools.py`    | 4 个 LSP LangChain 工具                                       |

### 工具

| 工具                   | LSP 方法                                                                                            | 输入                        | 说明           |
| ---------------------- | --------------------------------------------------------------------------------------------------- | --------------------------- | -------------- |
| `lsp_goto_definition`  | `textDocument/definition`                                                                           | file, line, char            | 跳转到符号定义 |
| `lsp_find_references`  | `textDocument/references`                                                                           | file, line, char            | 查找所有引用   |
| `lsp_workspace_symbol` | `workspace/symbol`                                                                                  | query (模糊字符串)          | 工作区符号搜索 |
| `lsp_call_hierarchy`   | `textDocument/prepareCallHierarchy` + `callHierarchy/incomingCalls` + `callHierarchy/outgoingCalls` | file, line, char, direction | 调用层级       |

### 配置

```python
class LspConfig(TypedDict):
    lsp_python_server: str          # "basedpyright-langserver"
    lsp_typescript_server: str      # "typescript-language-server"
    lsp_rust_server: str            # "rust-analyzer"
    lsp_go_server: str              # "gopls"
    lsp_request_timeout_s: float    # 单请求超时 (默认 10)
    lsp_server_start_timeout_s: float  # 服务器启动超时 (默认 15)
    lsp_enabled_languages: list[str]  # ["python", "typescript", "rust", "go"]

LSP: LspConfig = {
    "lsp_python_server": "basedpyright-langserver",
    "lsp_typescript_server": "typescript-language-server",
    "lsp_rust_server": "rust-analyzer",
    "lsp_go_server": "gopls",
    "lsp_request_timeout_s": 10.0,
    "lsp_server_start_timeout_s": 15.0,
    "lsp_enabled_languages": ["python", "typescript", "rust", "go"],
}
```

---

## Phase 3 — Embedding 语义搜索

> **前置条件：Phase 1 完成且评估通过。**

### 架构

```
┌─ 索引阶段 ───────────────────────────────────────────────────┐
│                                                               │
│  CodeChunker                                                  │
│  ├─ 按符号分块（复用 Phase 1 的 symbol 表）                    │
│  ├─ 每块 = 符号源码 + 文件路径 + 行号 + 符号名 + kind            │
│  └─ 输出 chunks: list[CodeChunk]                               │
│                                                               │
│  EmbeddingIndexer                                             │
│  ├─ embed_model.embed_documents(chunks)  ← 复用 build_embed_model│
│  ├─ 存入 SQLite code_embeddings 表 (BLOB，复用 context_engine 模式)│
│  └─ 增量：只 embed 新/变更的符号                                 │
│                                                               │
└───────────────────────────────────────────────────────────────┘

┌─ 搜索阶段 ───────────────────────────────────────────────────┐
│                                                               │
│  semantic_code_search(query)                                   │
│  1. embed_model.embed_query(query) → query_vector             │
│  2. SQLite 加载所有 code_embeddings                             │
│  3. cosine similarity 排序（复用 embeddings/search.py 模式）    │
│  4. reranker_model.rank(query, top_candidates) ← 可选重排       │
│  5. 返回 top-K 代码片段 + 文件路径 + 行号                       │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

### SQLite Schema（扩展 Phase 1 的 db.sqlite）

```sql
CREATE TABLE IF NOT EXISTS code_embeddings (
    symbol_id INTEGER PRIMARY KEY,    -- FK → symbols.id
    embedding BLOB NOT NULL,          -- array.array("d").tobytes()
    model TEXT NOT NULL,              -- 模型名
    dim INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,          -- 被嵌入的文本
    created_at REAL NOT NULL,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id)
);

CREATE INDEX IF NOT EXISTS idx_code_emb_symbol ON code_embeddings(symbol_id);
```

### 新增文件（4 个）

| 文件                                          | 作用                                               |
| --------------------------------------------- | -------------------------------------------------- |
| `agent/tools/code_intel/semantic/__init__.py` | 模块导出                                           |
| `agent/tools/code_intel/semantic/chunker.py`  | 按符号分块（复用 Phase 1 symbol 表）               |
| `agent/tools/code_intel/semantic/indexer.py`  | Embedding 生成 + 存储（复用 `build_embed_model`）  |
| `agent/tools/code_intel/semantic/search.py`   | Cosine 相似度搜索 + reranker 重排 + LangChain 工具 |

### 工具

```
# semantic_code_search
"""Search code by concept or natural language intent.
Returns the most relevant code snippets ranked by semantic similarity.
Use when explore (symbol name match) returns nothing, or when the query
is a concept ('how does authentication work') rather than a symbol name.
Examples: 'database connection pooling', 'error handling for websockets',
'session cleanup on disconnect'"""
```

### 复用点

| 复用                  | 来源                                  | 用途                              |
| --------------------- | ------------------------------------- | --------------------------------- |
| `build_embed_model()` | `models/embed_model/core.py`          | 代码块向量化                      |
| `reranker_model`      | `models/reranker_model/`              | 搜索结果重排                      |
| Embedding 存储模式    | `context_engine/embeddings/store.py`  | `_pack()`/`_unpack()` BLOB 序列化 |
| Cosine 搜索模式       | `context_engine/embeddings/search.py` | 向量相似度计算                    |
| SQLite 连接模式       | `context_engine/store/db.py`          | WAL 模式 + retry on lock          |

---

## 修改文件

### Phase 1 修改（4 个）— ✅ 已落地

> 完整 diff 见 git 记录；落点摘要：
>
> 1. `agent/tools/subagent/spawn/core.py::_build_child_agent()` — 新增 `session_id` 参数；在 `apply_tool_policy` 之后仅当 `functional_role == FunctionalRole.RESEARCHER` 注入 4 个 code_intel 工具，`create_agent(tools=final_tools)`。
> 2. `agent/tools/subagent/spawn/system_prompt.py::build_subagent_system_prompt()` — RESEARCHER 追加 `## Code Intelligence Tools` 指导段。
> 3. `config/features/agent_side/__init__.py` — re-export `CodeIntelConfig` / `CODE_INTEL`。
> 4. `config/features/__init__.py` — 顶层 re-export 同两项。

### Phase 2 修改（2 个）

- `config/features/agent_side/__init__.py` — re-export `LSP` / `LspConfig`
- `config/features/__init__.py` — 顶层 re-export

### Phase 3 修改（1 个）

- `agent/tools/subagent/spawn/system_prompt.py` — 追加 semantic_code_search 指导（仅 RESEARCHER）

### 公共修改

#### `config/path.py`

```python
CODE_INTEL_DIR = ROOT_DIR / ".codeintel"
```

#### `.gitignore`

```
# Code intelligence index
.codeintel/
```

---

## 测试计划

| 文件                                                           | 阶段  | 标记        | 覆盖点                                                                                                    |
| -------------------------------------------------------------- | ----- | ----------- | --------------------------------------------------------------------------------------------------------- |
| `tests/agent/tools/code_intel/conftest.py`                     | P1 ✅ | —           | 共享 fixtures（fixture repo、isolated db、env 隔离）                                                      |
| `tests/agent/tools/code_intel/test_indexer.py`                 | P1 ✅ | `unit`      | 4 语言符号提取、调用边/跨文件解析、增量/删除、prune/超大/语法错误、batch 事务                             |
| `tests/agent/tools/code_intel/test_query.py`                   | P1 ✅ | `unit`      | 精确/模糊匹配、callers/callees/impact 遍历、深度限制、空结果                                              |
| `tests/agent/tools/code_intel/test_tools.py`                   | P1 ✅ | `unit`      | 4 工具 schema、session_id、scope metadata、JSON 输出                                                      |
| `tests/agent/tools/code_intel/test_integration.py`             | P1 ✅ | `module`    | 角色隔离三方向（main / 非 RESEARCHER / RESEARCHER）                                                       |
| `tests/agent/tools/code_intel/test_e2e.py`                     | P1 ✅ | `module`    | hermetic e2e：4 语言 fixture 工程 + explore/callers/callees/impact + DB 复用                              |
| `tests/agent/tools/subagent/test_code_intel_researcher_e2e.py` | P1 ✅ | `llm_e2e`   | 真实 LLM：RESEARCHER 子代理实际调用 `explore`（CI 手动 llm-e2e 作业）                                     |
| `tests/agent/tools/code_intel/test_lsp_client.py`              | P2    | `unit`      | JSON-RPC 握手、请求/响应、超时、进程清理、didOpen 通知                                                    |
| `tests/agent/tools/code_intel/test_lsp_tools.py`               | P2    | `integration` | 4 个 LSP 工具端到端（需 LSP server 可用，skip if not）                                                  |
| `tests/agent/tools/code_intel/test_semantic.py`                | P3    | `integration` | 分块、embedding 存储、cosine 搜索、reranker 重排（需 embed model 可用）                                 |

> Phase 1 的关键用例已全部落地（见上表与 `tests/agent/tools/code_intel/`）；P2/P3 用例待派工。

---

## 实施步骤顺序

### Phase 1 — ✅ 已落地

依赖、`config/path.py`、`.gitignore`、config + 两级 re-export、5 个实现文件、RESEARCHER 注入点、P1 测试（含 hermetic e2e）与 `ruff` / `basedpyright` / `pytest` 门禁均已完成。

### Phase 2（约 13h） — Phase 1 评估通过后

| 步骤        | 内容                                                                  | 依赖 | 预估     |
| ----------- | --------------------------------------------------------------------- | ---- | -------- |
| 15          | `config/features/agent_side/lsp.py` + re-exports                      | 无   | 0.5h     |
| 16          | `agent/tools/code_intel/lsp/protocol.py`                              | 15   | 1h       |
| 17          | `agent/tools/code_intel/lsp/client.py`                                | 16   | 3h       |
| 18          | `agent/tools/code_intel/lsp/tools.py`                                 | 17   | 2h       |
| 19          | 修改 `spawn/core.py` + `system_prompt.py` — 注入 LSP（仅 RESEARCHER） | 18   | 0.5h     |
| 20          | 编写 P2 测试                                                          | 19   | 3h       |
| 21          | ruff + basedpyright + pytest                                          | 20   | 1h       |
| **P2 小计** |                                                                       |      | **~11h** |

### Phase 3（约 9h） — Phase 1 评估通过后

| 步骤        | 内容                                                       | 依赖 | 预估    |
| ----------- | ---------------------------------------------------------- | ---- | ------- |
| 22          | `agent/tools/code_intel/semantic/chunker.py`               | P1   | 1h      |
| 23          | `agent/tools/code_intel/semantic/indexer.py`               | 22   | 2h      |
| 24          | `agent/tools/code_intel/semantic/search.py`                | 23   | 2h      |
| 25          | 修改 `spawn/core.py` + `system_prompt.py`（仅 RESEARCHER） | 24   | 0.5h    |
| 26          | 编写 P3 测试                                               | 25   | 2h      |
| 27          | ruff + basedpyright + pytest                               | 26   | 0.5h    |
| **P3 小计** |                                                            |      | **~8h** |

### 总计

| 阶段     | 预估     |
| -------- | -------- |
| Phase 1  | ✅ 已落地 |
| Phase 2  | ~11h     |
| Phase 3  | ~8h      |
| **合计** | **~34h** |

---

## 安全清单

| 维度                     | 措施                                                                                         | 状态                        |
| ------------------------ | -------------------------------------------------------------------------------------------- | --------------------------- |
| **main agent 不可用**    | 不加入 `_MAIN_TOOLS_BUILDERS`，仅 `_build_child_agent` 注入                                  | `spawn/core.py`             |
| **非 RESEARCHER 不可用** | code_intel 工具仅在 `functional_role == RESEARCHER` 时注入，EXECUTOR/REVIEWER/GENERAL 不可用 | `spawn/core.py`             |
| **索引隔离**             | `.codeintel/` gitignored，SQLite WAL 模式                                                    | `.gitignore` + `indexer.py` |
| **索引超时**             | 索引构建有超时保护（默认 60s），部分结果可返回                                               | `indexer.py`                |
| **文件遍历限制**         | `os.walk` + `should_skip_dir` + 配置 `prune_dirs`，并受 `max_files` / 超时约束                | `indexer.py`                |
| **查询限制**             | explore 最大返回 N 个符号（默认 10），源码截断（默认 8000 chars）                            | `query.py`                  |
| **调用图深度**           | impact 遍历有深度限制（默认 3 层）                                                           | `query.py`                  |
| **LSP 进程隔离**         | subagent session 结束时 shutdown + kill LSP 进程                                             | P2 `client.py`              |
| **LSP 超时**             | 单请求超时（默认 10s），服务器启动超时（默认 15s）                                           | P2 `client.py`              |
| **embedding 存储**       | BLOB 序列化，不泄漏敏感路径                                                                  | P3 `indexer.py`             |
| **无网络调用**           | tree-sitter 索引纯本地，不发起网络请求                                                       | `indexer.py`                |
| **无代码执行**           | 索引仅 parse + 读文件，不执行任何代码                                                        | `indexer.py`                |
| **db 路径可控**          | SQLite 路径由配置控制，不在临时目录                                                          | `code_intel.py`             |

---

## 关键参考文件

### Sherry 内部

| 文件                                                  | 用途                                                 |
| ----------------------------------------------------- | ---------------------------------------------------- |
| `agent/tools/__init__.py`                             | `_MAIN_TOOLS_BUILDERS` 列表 — code_intel 不加入此处  |
| `agent/tools/subagent/spawn/core.py:818`              | `_build_child_agent` — code_intel 注入点             |
| `agent/tools/subagent/spawn/inherited_tool_policy.py` | `apply_tool_policy` — 工具过滤逻辑                   |
| `agent/tools/subagent/spawn/system_prompt.py`         | subagent 系统提示 — 代码检索指导注入点               |
| `agent/tools/file_tools/search_files.py`              | 现有纯 Python regex 搜索 — fallback 层               |
| `agent/tools/file_tools/search_scan.py`               | `bounded_walk` + `ScanState` — 索引遍历参考          |
| `agent/tools/python_repl.py`                          | 现有 sandboxed subprocess — 进程管理参考             |
| `agent/tools/pub_base/env_scrub.py`                   | `scrub_env()` — LSP 子进程环境清洗（P2）             |
| `config/features/agent_side/tools_timeouts.py`        | 工具超时配置模式参考                                 |
| `config/features/agent_side/subagent_infra.py`        | TypedDict 配置模式参考                               |
| `config/features/agent_side/__init__.py`              | agent_side re-export 聚合                            |
| `config/features/__init__.py`                         | 顶层 re-export 聚合                                  |
| `config/path.py`                                      | 文件系统路径配置 — 增加 `CODE_INTEL_DIR`             |
| `agent/core.py:175`                                   | `built_agent` — main agent 工具来源（无 code_intel） |
| `pyproject.toml`                                      | 依赖管理 — 加 tree-sitter                            |
| `context_engine/embeddings/store.py`                  | Embedding BLOB 存储模式（P3 复用）                   |
| `context_engine/embeddings/search.py`                 | Cosine 相似度搜索模式（P3 复用）                     |
| `context_engine/embeddings/indexer.py`                | Embedding 增量索引模式（P3 复用）                    |
| `context_engine/store/db.py`                          | SQLite WAL + retry on lock 模式（P1/P3 复用）        |
| `models/embed_model/core.py`                          | `build_embed_model()` → `CustomEmbedding`（P3 复用） |
| `models/reranker_model/__init__.py`                   | `reranker_model` CrossEncoder（P3 复用）             |

### 外部参考

| 项目                  | 文件                                | 参考内容                                            |
| --------------------- | ----------------------------------- | --------------------------------------------------- |
| `oh-my-openagent-dev` | `.mcp.json`                         | CodeGraph MCP 配置 — 8 工具设计参考                 |
| `oh-my-openagent-dev` | CodeGraph 实现                      | AST 知识图谱 + explore/callers/impact 工具格式      |
| `opencode-dev`        | `packages/opencode/src/tool/lsp.ts` | 9 操作 LSP 工具（含 workspaceSymbol/callHierarchy） |
| `opencode-dev`        | ripgrep 集成                        | 正则搜索 fallback 设计参考                          |
| `hermes-agent-main`   | `tools/code_execution_tool.py`      | 子进程管理模式（P2 LSP 进程管理参考）               |
| `openclaw`            | LSP 工具                            | hover/def/refs 简洁实现参考                         |
