# 代码检索框架 — Subagent 专属实现计划

> 基于对 5 个项目（oh-my-openagent-dev、opencode-dev、hermes-agent-main、deepagents-main、openclaw）的代码检索框架调研，为 Sherry 设计三阶段代码检索能力：tree-sitter 符号索引 + LSP 精确检索 + Embedding 语义搜索。
>
> **核心约束：仅限 subagent 可用，main agent 无法调用。**
>
> **前置依赖：[`TODO/subagent-role-migration.md`](subagent-role-migration.md) Phase 1（subagent 功能角色分工）。**
> 代码检索工具仅注入 `FunctionalRole.RESEARCHER` 的 subagent，Phase 1 的 `FunctionalRole` 枚举、角色定义加载器、`spawn_subagent_direct()` 的 `functional_role_hint` 参数传递必须先完成。
> 实施顺序：subagent-role-migration Phase 1 全部步骤 → 本计划 Phase 1。
>
> **实施策略：Phase 1 先行，评估后再决定 P2/P3。**

---

## 目录

1. [架构概览](#架构概览)
2. [设计决策](#设计决策)
3. [Phase 1 — AST 符号索引 + 调用图](#phase-1--ast-符号索引--调用图)
4. [Phase 2 — LSP 精确检索](#phase-2--lsp-精确检索)
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
| 文件遍历           | `agent/tools/file_tools/search_scan.py` → `bounded_walk` + `ScanState` | Phase 1 索引遍历     |
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
# 前置依赖：subagent-role-migration Phase 1 已完成
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

### 新增依赖

```toml
# pyproject.toml [project] dependencies — 新增
"tree-sitter>=0.25",
"tree-sitter-python>=0.23",
"tree-sitter-typescript>=0.23",
"tree-sitter-rust>=0.23",
"tree-sitter-go>=0.23",
```

### 新增文件（5 个）

#### 1. `config/features/agent_side/code_intel.py`

```python
"""Code intelligence configuration (tree-sitter symbol index + call graph)."""

from typing import TypedDict


class CodeIntelConfig(TypedDict):
    """Configuration for the subagent-only code intelligence tools."""
    code_intel_index_db_path: str
    code_intel_index_max_files: int
    code_intel_index_timeout_s: int
    code_intel_index_batch_size: int
    code_intel_explore_max_symbols: int
    code_intel_explore_max_source_chars: int
    code_intel_call_graph_max_depth: int
    code_intel_fuzzy_min_score: float
    code_intel_supported_extensions: list[str]
    code_intel_language_map: dict[str, str]
    code_intel_prune_dirs: list[str]
    code_intel_source_snippet_lines: int


CODE_INTEL: CodeIntelConfig = {
    "code_intel_index_db_path": "",  # set at runtime from config.path
    "code_intel_index_max_files": 5000,
    "code_intel_index_timeout_s": 60,
    "code_intel_index_batch_size": 100,
    "code_intel_explore_max_symbols": 10,
    "code_intel_explore_max_source_chars": 8000,
    "code_intel_call_graph_max_depth": 3,
    "code_intel_fuzzy_min_score": 0.3,
    "code_intel_supported_extensions": [
        ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
    ],
    "code_intel_language_map": {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "jsx",
        ".rs": "rust",
        ".go": "go",
    },
    "code_intel_prune_dirs": [
        ".git", "__pycache__", "node_modules", ".venv",
        "venv", "dist", "build", ".codeintel",
        ".codegraph", ".mypy_cache", ".ruff_cache",
    ],
    "code_intel_source_snippet_lines": 40,
}
```

**遵循约定：** 一个 TypedDict + 一个实例，re-export via `__init__.py`，NEVER bare dict。

#### 2. `agent/tools/code_intel/__init__.py`

```python
"""Code intelligence tools — subagent only (explore, callers, callees, impact)."""

from .tools import build_code_intel_tools

__all__ = ["build_code_intel_tools"]
```

#### 3. `agent/tools/code_intel/indexer.py` — tree-sitter 符号提取 + 调用图构建

**SQLite Schema:**

```sql
-- 符号表
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- function | class | method | variable
    file_path TEXT NOT NULL,         -- 相对路径
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    parent_id INTEGER,               -- 父符号 (类的方法)
    language TEXT NOT NULL,          -- python | typescript | rust | go
    source_snippet TEXT,             -- 源码截取（前 N 行）
    doc_string TEXT,                 -- docstring / comment（如可提取）
    FOREIGN KEY (parent_id) REFERENCES symbols(id)
);

-- 调用边
CREATE TABLE IF NOT EXISTS call_edges (
    id INTEGER PRIMARY KEY,
    caller_id INTEGER NOT NULL,
    callee_id INTEGER,               -- NULL = 未解析
    callee_name TEXT NOT NULL,       -- 调用点的文本名
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    resolved INTEGER DEFAULT 0,      -- 0=未解析, 1=已解析
    FOREIGN KEY (caller_id) REFERENCES symbols(id),
    FOREIGN KEY (callee_id) REFERENCES symbols(id)
);

-- 索引元数据（增量索引用）
CREATE TABLE IF NOT EXISTS index_meta (
    file_path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    language TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    symbol_count INTEGER,
    error TEXT
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON call_edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON call_edges(callee_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee_name ON call_edges(callee_name);
CREATE INDEX IF NOT EXISTS idx_edges_resolved ON call_edges(resolved);
```

**核心逻辑：**

```python
class CodeIndexer:
    """Tree-sitter based symbol indexer with call graph construction."""

    def __init__(self, db_path: str, config: CodeIntelConfig):
        self._db_path = db_path
        self._config = config
        self._parsers: dict[str, Parser] = {}  # language → Parser (lazy init)
        self._queries: dict[str, Query] = {}   # language → symbol/call queries

    def _get_parser(self, language: str) -> Parser:
        """Lazy-initialize tree-sitter parser for a language."""
        if language not in self._parsers:
            # Map language string to tree-sitter Language object
            lang_module = _LANGUAGE_MODULES[language]
            ts_lang = Language(lang_module.language())
            parser = Parser(ts_lang)
            self._parsers[language] = parser
        return self._parsers[language]

    def index_directory(self, root: Path, force: bool = False) -> IndexResult:
        """Index a directory tree — incremental by default (mtime check).

        Flow:
        1. Walk directory (reuse search_scan.py bounded_walk pattern)
        2. For each file: check mtime → skip unchanged
        3. tree-sitter parse → extract symbols + call sites
        4. Batch write to SQLite (transaction)
        5. Second pass: resolve cross-file call edges (name → symbol_id)
        """
        ...

    def index_file(self, path: Path, root: Path) -> FileIndexResult:
        """Index a single file.

        Flow:
        1. Detect language (extension → grammar)
        2. tree-sitter parse
        3. Query: function_definition, class_definition, method_definition, call
        4. Extract source_snippet (first N lines of symbol body)
        5. Write symbols + call_edges (delete old entries first)
        """
        ...

    def _resolve_call_edges(self, root: Path) -> int:
        """Second pass: resolve callee_name → symbol_id.

        Priority: exact name match in same file → same module → global.
        """
        ...
```

**Tree-sitter Query 定义（按语言）：**

```python
# Python
PYTHON_SYMBOL_QUERY = """
(function_definition name: (identifier) @function_name) @function
(class_definition name: (identifier) @class_name) @class
(call function: (identifier) @call_name)
(call function: (attribute attribute: (identifier) @attr_name) object: (identifier) @obj)
"""

# TypeScript
TS_SYMBOL_QUERY = """
(function_declaration name: (identifier) @function_name) @function
(class_declaration name: (type_identifier) @class_name) @class
(method_definition name: (property_identifier) @method_name) @method
(call_expression function: (identifier) @call_name)
(call_expression function: (member_expression property: (property_identifier) @prop_name))
"""

# Rust / Go — 类似结构，按各语言语法节点定义
```

**调用图解析策略（两遍）：**

1. **第一遍：** 提取所有符号定义 + 所有调用点（callee_name 记录文本名）
2. **第二遍：** 对每个 callee_name，按优先级搜索匹配的 `symbol.name`：
   - 同文件精确匹配
   - 同模块（同目录）精确匹配
   - 全局精确匹配
   - 模糊匹配（Levenshtein 距离 ≤ 2，仅同文件）
3. 匹配到的设 `resolved=1` 并填入 `callee_id`

#### 4. `agent/tools/code_intel/query.py` — 查询引擎

```python
class CodeQuery:
    """Query engine over the symbol index."""

    def __init__(self, db_path: str, config: CodeIntelConfig):
        self._db_path = db_path
        self._config = config

    def explore(self, query: str, root: Path) -> ExploreResult:
        """Fuzzy intent → symbol source + call paths.

        1. Extract keywords from query (tokenize + camelCase/snake_case split)
        2. Symbol name matching:
           - Exact match → prefix match → substring match → Levenshtein (dist ≤ 2)
        3. For top-N matches: get source + callers + callees (depth-limited)
        4. Format output (reference: codegraph_explore format)
        """
        ...

    def callers(self, symbol_name: str, root: Path) -> list[CallerInfo]:
        """Who calls this symbol?

        SELECT caller_id, file_path, line FROM call_edges
        WHERE callee_name = ? OR callee_id = (SELECT id FROM symbols WHERE name = ?)
        JOIN symbols for caller details
        """
        ...

    def callees(self, symbol_name: str, root: Path) -> list[CalleeInfo]:
        """What does this symbol call?

        SELECT callee_name, file_path, line FROM call_edges
        WHERE caller_id = (SELECT id FROM symbols WHERE name = ?)
        """
        ...

    def impact(self, symbol_name: str, root: Path) -> ImpactResult:
        """Blast radius of modifying this symbol.

        BFS/DFS traversal of call_edges (caller direction), depth-limited.
        Returns all transitive callers up to max_depth.
        """
        ...

    def _ensure_index(self, root: Path) -> None:
        """Ensure index exists and is up-to-date. Build if needed."""
        indexer = CodeIndexer(self._db_path, self._config)
        meta = indexer.get_index_meta(root)
        if meta is None or meta.is_stale:
            indexer.index_directory(root)
```

#### 5. `agent/tools/code_intel/tools.py` — LangChain 工具定义

```python
def build_code_intel_tools(session_id: str) -> list[BaseTool]:
    """Build 4 code intelligence tools — subagent only."""
    from config.features.agent_side import CODE_INTEL
    db_path = CODE_INTEL["code_intel_index_db_path"] or str(
        config.path.CODE_INTEL_DIR / "db.sqlite"
    )
    query_engine = CodeQuery(db_path, CODE_INTEL)

    return [
        ExploreTool(query_engine, session_id, CODE_INTEL),
        CallersTool(query_engine, session_id, CODE_INTEL),
        CalleesTool(query_engine, session_id, CODE_INTEL),
        ImpactTool(query_engine, session_id, CODE_INTEL),
    ]
```

**工具 description（参考 oh-my-openagent codegraph_explore）：**

```
# explore
"""Find code related to a concept, function name, or natural language query.
Returns source code + call paths for the most relevant symbols.
Use FIRST when you need to understand how something works, where it's defined,
or what calls it. Falls back to search_files if no matches found.
Example: explore("database connection pooling")"""

# callers
"""Find all functions/methods that call the given symbol.
Returns caller name, file, line, and source snippet.
Example: callers("built_agent")"""

# callees
"""Find all functions/methods called by the given symbol.
Returns callee name, file, line, and source snippet.
Example: callees("built_agent")"""

# impact
"""Analyze the blast radius of modifying the given symbol.
Traverses the call graph up to N levels deep, listing all affected callers.
Use before refactoring to understand what might break.
Example: impact("build_main_tools")"""
```

### 索引生命周期

| 触发点                      | 动作                                                            |
| --------------------------- | --------------------------------------------------------------- |
| subagent 首次调用 `explore` | 全量索引（如果 `.codeintel/db.sqlite` 不存在或过期）            |
| 后续调用                    | 增量索引（mtime 检查，只重索引变更文件）                        |
| 索引超时                    | 返回部分结果 + 提示 "index incomplete, retry for full coverage" |
| `.codeintel/`               | gitignored（类似 `.codegraph/`）                                |

### 增量索引策略

```python
def _should_reindex(self, file_path: Path, root: Path) -> bool:
    """Check if file needs reindexing based on mtime."""
    rel_path = str(file_path.relative_to(root))
    row = self._db.execute(
        "SELECT mtime FROM index_meta WHERE file_path = ?", (rel_path,)
    ).fetchone()
    if row is None:
        return True
    current_mtime = file_path.stat().st_mtime
    return current_mtime > row["mtime"]
```

---

## Phase 2 — LSP 精确检索

> **前置条件：Phase 1 完成且评估通过。**

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

### Phase 1 修改（4 个）

#### 1. `agent/tools/subagent/spawn/core.py` — `_build_child_agent()` (约 line 818)

> **前置依赖：** subagent-role-migration Phase 1 Step 1.4-1.5 已完成，`_build_child_agent` 签名中已增加 `functional_role` 和 `role_def` 参数。

**现有代码（Phase 1 完成后）：**

```python
base_tools = tools if tools is not None else build_main_tools()
filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

# ... LLM selection (Phase 1.5 已改为 role_def.model_tier 驱动) ...

child_agent = create_agent(
    ...
    tools=filtered_tools,
    ...
)
```

**修改后（本计划新增）：**

```python
base_tools = tools if tools is not None else build_main_tools()
filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

# Inject code intelligence tools — RESEARCHER role only, never available to main agent or other roles
final_tools = list(filtered_tools)
if functional_role == FunctionalRole.RESEARCHER:
    from agent.tools.code_intel import build_code_intel_tools
    code_intel_tools = build_code_intel_tools(session_id=run.child_session_key)
    final_tools = [*filtered_tools, *code_intel_tools]

# ... LLM selection (unchanged) ...

child_agent = create_agent(
    ...
    tools=final_tools,  # ← was filtered_tools
    ...
)
```

#### 2. `agent/tools/subagent/spawn/system_prompt.py` — 增加代码检索指导（仅 RESEARCHER）

在 `build_subagent_system_prompt()` 中，当 `functional_role == RESEARCHER` 时追加：

```python
# Code intelligence guidance (only for RESEARCHER role)
if functional_role == FunctionalRole.RESEARCHER:
    sections.append(
        "## Code Intelligence Tools\n"
        "You have code retrieval tools for fast repo navigation:\n"
        "- `explore` — fuzzy intent → symbol source + call paths. USE FIRST.\n"
        "- `callers` — who calls this symbol\n"
        "- `callees` — what does this symbol call\n"
        "- `impact` — blast radius of modifying a symbol\n"
        "Workflow: explore(query) → callers(symbol) for precision → "
        "search_files as keyword fallback.\n"
        "Index is built on first use; subsequent queries are fast."
    )
```

#### 3. `config/features/agent_side/__init__.py` — re-export

```python
from .code_intel import (
    CODE_INTEL as CODE_INTEL,
    CodeIntelConfig as CodeIntelConfig,
)
```

#### 4. `config/features/__init__.py` — 顶层 re-export

```python
from .agent_side import (
    # ... existing exports ...
    CODE_INTEL as CODE_INTEL,
    CodeIntelConfig as CodeIntelConfig,
)
```

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

| 文件                                               | 阶段 | 标记          | 覆盖点                                                                                                                                                |
| -------------------------------------------------- | ---- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/agent/tools/code_intel/conftest.py`         | P1   | —             | 共享 fixtures（tmp repo, mock config, test db）                                                                                                       |
| `tests/agent/tools/code_intel/test_indexer.py`     | P1   | `unit`        | tree-sitter 解析、符号提取（4 语言）、调用边构建、增量索引、mtime 跳过、prune_dirs 过滤、batch 事务                                                   |
| `tests/agent/tools/code_intel/test_query.py`       | P1   | `unit`        | 模糊匹配（精确/前缀/子串/Levenshtein）、callers/callees/impact 遍历、深度限制、空结果处理                                                             |
| `tests/agent/tools/code_intel/test_tools.py`       | P1   | `unit`        | explore/callers/callees/impact 工具 schema、session_id 注入、metadata scope 标签、description 正确性                                                  |
| `tests/agent/tools/code_intel/test_integration.py` | P1   | `module`      | `_build_child_agent` 注入（仅 RESEARCHER）、main agent 无 code_intel 工具、EXECUTOR/REVIEWER subagent 无 code_intel 工具、explore 降级到 search_files |
| `tests/agent/tools/code_intel/test_lsp_client.py`  | P2   | `unit`        | JSON-RPC 握手、请求/响应、超时、进程清理、didOpen 通知                                                                                                |
| `tests/agent/tools/code_intel/test_lsp_tools.py`   | P2   | `integration` | 4 个 LSP 工具端到端（需 LSP server 可用，skip if not）                                                                                                |
| `tests/agent/tools/code_intel/test_semantic.py`    | P3   | `integration` | 分块、embedding 存储、cosine 搜索、reranker 重排（需 embed model 可用）                                                                               |

### 关键测试用例

```python
# test_indexer.py
def test_python_symbol_extraction():
    """tree-sitter 正确提取 function/class/method 定义。"""

def test_call_edge_extraction():
    """调用边正确提取，callee_name 记录文本名。"""

def test_cross_file_call_resolution():
    """第二遍解析跨文件调用边，callee_id 正确填充。"""

def test_incremental_index_mtime_skip():
    """mtime 未变更的文件被跳过。"""

def test_prune_dirs_excluded():
    """prune_dirs 中的目录不被索引。"""

def test_unsupported_language_skipped():
    """不支持扩展名的文件被跳过（如 .vue）。"""

def test_index_batch_transaction():
    """批量索引写入为单事务，部分失败回滚。"""

# test_query.py
def test_explore_exact_match():
    """精确符号名匹配优先返回。"""

def test_explore_fuzzy_match():
    """Levenshtein 距离 ≤ 2 的模糊匹配。"""

def test_callers_returns_all_callers():
    """callers 返回所有调用者，含文件路径和行号。"""

def test_impact_depth_limit():
    """impact 遍历深度不超过 max_depth。"""

def test_explore_empty_result_suggests_search_files():
    """无匹配时提示使用 search_files。"""

# test_tools.py
def test_metadata_scope_subagent_only():
    """工具 metadata 包含 scope 标签。"""

def test_session_id_injection():
    """session_id 正确注入到工具实例。"""

# test_integration.py
async def test_main_agent_has_no_code_intel():
    """build_main_tools() 返回值中不含 explore/callers/callees/impact。"""

async def test_researcher_subagent_has_code_intel():
    """_build_child_agent(functional_role=RESEARCHER) 构建的工具列表中包含 4 个 code_intel 工具。"""

async def test_executor_subagent_has_no_code_intel():
    """_build_child_agent(functional_role=EXECUTOR) 构建的工具列表中不含 code_intel 工具。"""

async def test_reviewer_subagent_has_no_code_intel():
    """_build_child_agent(functional_role=REVIEWER) 构建的工具列表中不含 code_intel 工具。"""

async def test_code_intel_tools_not_in_main_builders():
    """_MAIN_TOOLS_BUILDERS 列表中无 code_intel builder。"""
```

---

## 实施步骤顺序

### Phase 1（约 15h）

| 步骤        | 内容                                                                         | 依赖   | 预估     |
| ----------- | ---------------------------------------------------------------------------- | ------ | -------- |
| 0           | **前置：完成 subagent-role-migration Phase 1 全部步骤**                      | 无     | —        |
| 1           | `pyproject.toml` 加 tree-sitter + 4 grammar 依赖                             | 步骤 0 | 0.25h    |
| 2           | `config/path.py` 加 `CODE_INTEL_DIR`                                         | 无     | 0.1h     |
| 3           | `.gitignore` 加 `.codeintel/`                                                | 无     | 0.1h     |
| 4           | `config/features/agent_side/code_intel.py` + re-exports（`__init__.py` × 2） | 2      | 0.5h     |
| 5           | `agent/tools/code_intel/__init__.py`                                         | 4      | 0.1h     |
| 6           | `agent/tools/code_intel/indexer.py` — tree-sitter 索引引擎                   | 4      | 4h       |
| 7           | `agent/tools/code_intel/query.py` — 查询引擎                                 | 6      | 3h       |
| 8           | `agent/tools/code_intel/tools.py` — LangChain 工具                           | 7      | 2h       |
| 9           | 修改 `spawn/core.py` — 注入 code_intel 工具                                  | 8      | 0.5h     |
| 10          | 修改 `spawn/system_prompt.py` — 代码检索指导                                 | 4      | 0.5h     |
| 11          | 编写 P1 测试（4 文件 + conftest）                                            | 9, 10  | 3h       |
| 12          | `uv run --with ruff ruff check . && ruff format --check .`                   | 11     | 0.5h     |
| 13          | `uv run --no-sync basedpyright agent/tools/code_intel/`                      | 12     | 0.5h     |
| 14          | `uv run pytest tests/agent/tools/code_intel -q`                              | 13     | 1h       |
| **P1 小计** |                                                                              |        | **~15h** |

> **注意：** P1 小计不含前置依赖 subagent-role-migration Phase 1 的工时。

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
| Phase 1  | ~15h     |
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
| **文件遍历限制**         | 复用 `search_scan.py` 的 `bounded_walk` + `prune_dirs`                                       | `indexer.py`                |
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
