# 🔎 Code Intel：面向子代理的四层代码检索

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 本文是[子代理系统 README](../../agent/tools/subagent/README.zh.md)与[子代理设计页](../subagent/README.zh.md)的设计层姊妹篇。前者是 spawn 流水线、角色工具策略与 `sessions_*` 工具的运行时与 API 参考；后者阐述两轴角色模型（深度角色 × 功能角色）。本页记录这些角色使用的代码检索框架：tree-sitter 符号索引、ast-grep 结构化搜索与重写、LSP 精确检索工具，以及基于嵌入的语义代码搜索。

事实来源：`agent/tools/code_intel/**`、`config/features/agent_side/code_intel.py`、`config/features/agent_side/code_intel_semantic.py`、`config/features/agent_side/ast_grep.py`、`config/features/agent_side/lsp.py`、`config/path.py`、`agent/tools/subagent/types/functional_role.py`、`agent/tools/subagent/spawn/core.py`、`agent/tools/subagent/spawn/system_prompt.py` 与 `agent/tools/subagent/roles/definitions/librarian/AGENTS.md`。下文每一条陈述均已对照该源码核实。

## 目录

- [总览](#-总览)
- [设计不变量](#-设计不变量)
- [第 1 层：Tree-sitter 符号索引](#-第-1-层tree-sitter-符号索引)
- [第 2 层：ast-grep 结构化搜索与重写](#-第-2-层ast-grep-结构化搜索与重写)
- [第 3 层：LSP 精确检索](#-第-3-层lsp-精确检索)
- [第 4 层：语义代码搜索](#-第-4-层语义代码搜索)
- [角色模型与工具面](#-角色模型与工具面)
- [配置](#-配置)
- [环境能力矩阵](#-环境能力矩阵)
- [限制与失败模式](#-限制与失败模式)
- [测试地图](#-测试地图)
- [相关文档](#-相关文档)

## 🎯 总览

Code Intel 用四个检索引擎回答子代理的代码理解问题，每个引擎对应一种问题形态：

| 层 | 回答的问题 | 工具 | 开放范围 |
|----|-----------|------|----------|
| **Tree-sitter 符号索引** | 符号定义在哪里，谁在调用它？ | `explore`、`callers`、`callees`、`impact` | `researcher` + `librarian` |
| **ast-grep** | 哪些代码具有该结构形态，如何重写？ | `ast_grep_search`、`ast_grep_rewrite` | 所有子代理 |
| **LSP** | 类型检查器对这个位置了解什么？ | `lsp_goto_definition`、`lsp_find_references`、`lsp_workspace_symbol`、`lsp_call_hierarchy`、`lsp_rename`、`lsp_diagnostics`、`lsp_format`、`lsp_status` | `researcher` + `librarian` |
| **语义搜索** | 哪个代码实现了这个概念？ | `semantic_code_search` | `researcher` + `librarian` |

四个引擎组合成一条工作流：`explore` 定位符号及其调用路径，`ast_grep_search` 跨语言查找结构模式，LSP 工具以类型感知方式解析定义与引用，`semantic_code_search` 在名称无法匹配时按概念检索。四层都在子代理解析组装时构建并注入。

```text
child agent assembly (spawn/core.py)
  base tools → role policy (allow / deny / main_only gate)
    ├─ role ∈ CODE_INTEL_ROLES?  → + explore/callers/callees/impact/semantic_code_search
    │                              + the eight lsp_* tools
    └─ always                    → + ast_grep_search / ast_grep_rewrite
```

## 🧱 设计不变量

1. **主智能体永远看不到这些工具。** 任何 code-intel 或 LSP 构建器都不注册进 `_MAIN_TOOLS_BUILDERS`；注入只发生在 `_build_child_agent` 内部、角色工具策略之后。
2. **工具面按角色开放。** tree-sitter 套件与全部八个 LSP 工具只给 `CODE_INTEL_ROLES`（`researcher`、`librarian`）；ast-grep 属于核心结构能力，因此所有功能角色都能拿到。
3. **fail-open 是契约。** 缺失的二进制、无法解析的文件、不可用的模型、超时或被拒绝的请求，都返回带可执行提示的 JSON 载荷；任何检索失败都不会把异常抛进子代理回合。
4. **一切都有上界。** 文件数、单文件字节数、构建时长、批次、结果数、路径数、模式大小与打开文件数都有上限；LSP 管理器另有 LRU 并发上限与空闲回收。
5. **写入默认关闭并做容器校验。** `ast_grep_rewrite` 除非 `dry_run=false` 否则只预览；`lsp_rename` 与 `lsp_format` 除非传入应用标志否则只预览；每条写入路径在落盘前都重新校验路径穿越与项目根包含关系。
6. **不引入新模型提供方。** 语义搜索复用既有的 `models/embed_model` 与 `models/reranker_model` 封装；唯一的新增存储是 SQLite 索引。
7. **根目录按调用解析。** `SHERRY_CODE_INTEL_ROOT`、`SHERRY_SG_ROOT` 与 `SHERRY_LSP_ROOT` 可为演练与测试覆盖工作根；未设置时使用进程 cwd 与共享的 `resolve_project_path` 门禁。

## 🌳 第 1 层：Tree-sitter 符号索引

索引由四个模块实现：`agent/tools/code_intel/extract.py`（纯字节 → 符号与调用点）、`agent/tools/code_intel/indexer.py`（SQLite 持久化与增量遍历）、`agent/tools/code_intel/query.py`（匹配与调用图查询）与 `agent/tools/code_intel/tools.py`（LangChain 封装）。

| 语法 | 语言键 | 扩展名 | 符号节点 |
|------|--------|--------|----------|
| Python | `python` | `.py` | `function_definition`、`class_definition`、`call` |
| TypeScript | `typescript`、`tsx` | `.ts`、`.tsx`、`.js`、`.jsx` | `function_declaration`、`class_declaration`、`method_definition`、`variable_declarator`（箭头与函数表达式）、`call_expression` |
| Rust | `rust` | `.rs` | `function_item`、`struct_item`、`enum_item`、`trait_item`、`impl_item`、`call_expression`、`macro_invocation` |
| Go | `go` | `.go` | `function_declaration`、`method_declaration`、`type_spec`（`struct_type` 与 `interface_type`）、`call_expression` |

节点名与语法版本均对照锁定包实测：`tree-sitter 0.26.0`、`tree-sitter-python 0.25.0`、`tree-sitter-typescript 0.23.2`、`tree-sitter-rust 0.24.2`、`tree-sitter-go 0.25.0`；解析器以 `Parser(Language(capsule))` 构造。`javascript` 别名映射到 `typescript` 语法，`jsx` 映射到 `tsx`。结果存入三张 SQLite 表：`symbols`、`call_edges` 与 `index_meta`。调用图在第二遍按优先级解析：同文件、同目录、任意全局匹配，最后是距离 ≤ 2 的 Levenshtein 模糊匹配。

增量与上界：每个文件的 `mtime` 与 `size` 都会与 `index_meta` 比较；只有变化的文件才重新解析，被删除的文件会连同其已解析的入边一起清理，写入以 100 个文件为一批提交。遍历在 `code_intel_index_max_files`（5000）或 `code_intel_index_timeout_s`（60）处停止并报告 `truncated`。fail-open 以文件为单位：语法错误、超大文件（超过 1 MB）、未知扩展名或读取失败都会写入一条 `index_meta` 记录后跳过——语法错误的文件绝不会被部分索引。变更调用由实例锁串行化，连接使用 WAL 并设置 30 秒 busy timeout。

查询在读取前先刷新增量索引，因此编辑后搜索不会返回陈旧符号，而目录未变化时只是一次廉价的空操作。四个工具如下：

| 工具 | 输入 | 输出 | 上界 |
|------|------|------|------|
| `explore` | 模糊概念、符号名或自然语言意图 | 匹配符号的源码、docstring、直接调用者与被调用者；无匹配时给出建议 | 10 个符号、8000 源码字符 |
| `callers` | 符号名 | 所有调用它的函数或方法，含调用点行号 | 精确名优先，模糊兜底 |
| `callees` | 符号名 | 该符号发出的所有调用，已知时附带解析到的目标文件与行号 | 重复调用点去重 |
| `impact` | 符号名 | 传递调用者（爆炸半径），含深度与经由的名字 | 深度 3，`truncated` 标志 |

## 🧬 第 2 层：ast-grep 结构化搜索与重写

ast-grep 把搜索模式当作代码而非正则，因此 `$NAME` 匹配一个 AST 节点，`$$$NAME` 匹配零个或多个节点。实现位于 `agent/tools/code_intel/ast_grep/`：`resolver.py`（二进制发现）、`provisioner.py`（校验下载）、`install_hints.py`（恢复提示）与 `runner.py`（两个工具）。独立兜底安装脚本是同目录下的 `scripts/install.sh` 与 `scripts/install.ps1`。

二进制发现分五层，每个候选必须是非空普通文件，并通过输出包含 `ast-grep` 的简短 `--version` 探测：

1. 显式覆盖——环境变量 `SHERRY_SG_PATH`。
2. 预置运行时——`~/.sherry/runtime/ast-grep/<platform>-<arch>/sg`。
3. code-intel bin 缓存——`.codeintel/ast-grep/bin`（先 `ast-grep` 后 `sg`）。
4. `PATH` 查找——先 `ast-grep` 后 `sg`，兼容 Windows `PATHEXT`。
5. Homebrew 与 Linuxbrew 前缀。

当所有层都未命中时，首次工具调用会自动预置锁定的 `0.43.0` 版本：各平台 URL 与 SHA-256 存放在 `config/features/agent_side/ast_grep.py`，下载受 60 秒超时约束，校验和不匹配即致命（fail-closed——绝不安装未经验证的字节），解压使用标准库 `zipfile`，因为宿主机可能没有 `unzip`。解压优先选择真正的 `ast-grep` 二进制而非 `sg` 启动器（后者相对自身路径重新执行，在部分沙箱中无法运行）；写盘是原子的并设置 755 权限。解析结果按进程缓存，文件仍存在即复用。

两个工具以子进程方式运行 `sg` CLI，环境经过擦洗，超时 30 秒：

| 工具 | 行为 | 安全默认 |
|------|------|----------|
| `ast_grep_search` | 默认以 `--strictness smart` 执行 `sg run --json=stream`；返回紧凑匹配（文件、1 基行号、文本、元变量） | 最多 50 条匹配、64 个路径、16 KiB 模式 |
| `ast_grep_rewrite` | 预览替换列表；仅当 `dry_run=false`（`--update-all`）时原地应用 | `dry_run` 默认为 `true` |

子进程启动前会先筛查每条路径：无覆盖时使用规范的 `resolve_project_path` 门禁；设置 `SHERRY_SG_ROOT` 时应用相同的穿越判定并校验对该根的包含关系——因此应用式重写只可能写入项目内部。

## 🛰️ 第 3 层：LSP 精确检索

LSP 层位于 `agent/tools/code_intel/lsp/`：`protocol.py`（URI、1 基与 0 基位置、结果格式化）、`resolver.py`（二进制发现）、`installer.py`（白名单自动安装）、`fallback.py`（可用性到可执行消息）、`client.py`（基于 stdio 的 JSON-RPC）、`manager.py`（进程生命周期）与 `tools.py`（八个工具）。工具以 1 基的 `line` 与 `character` 为输入，内部转换为 LSP 原生的 0 基位置。

| 工具 | 用途 | 默认行为 |
|------|------|----------|
| `lsp_goto_definition` | 类型感知地跳转到定义 | 只读 |
| `lsp_find_references` | 符号的全部引用 | 只读 |
| `lsp_workspace_symbol` | 模糊工作区符号搜索 | 只读，结果数有上限 |
| `lsp_call_hierarchy` | 传入调用者或传出被调用者 | 只读 |
| `lsp_rename` | 工作区重命名 | 预览 `WorkspaceEdit`；仅当 `dry_run=false` 时应用 |
| `lsp_diagnostics` | 文件的错误与警告 | 等待异步 `publishDiagnostics` 通知并报告 `timed_out` |
| `lsp_format` | 整文件或区间格式化 | 预览；仅当 `write=true` 时应用；不支持时报告 `supported=false` 而非假装成功 |
| `lsp_status` | 每个已配置服务器的诚实可用性 | 不启动任何进程 |

发现顺序与 ast-grep 各层一致，但加入 marker 门控：显式绝对路径；仅当同级存在对应 marker 文件时才信任的仓库本地 bin 目录（`pyproject.toml` 对应 `.venv/bin`、`package.json` 对应 `node_modules/.bin`、`Cargo.toml` 对应 `target/debug`、`go.mod` 对应 `bin`，逐级向上到仓库根）；然后是 `~/.sherry/runtime/lsp`；再是 `PATH`；最后是 Homebrew。解析结果按 `(cwd, command, platform)` 缓存。

语言服务器是重型常驻子进程，因此进程级管理器对其施加约束：服务器在首次请求某个 `(language, cwd)` 时惰性启动，同时存活的最多 `lsp_max_concurrent_servers`（2）个，超出时淘汰最久未使用者；空闲清扫线程回收超过 `lsp_idle_shutdown_s`（300 秒）未使用的服务器；`shutdown_all` 由 `atexit` 钩子执行——客户端绝不留下孤儿进程。每个客户端用 `Content-Length` 分帧 JSON-RPC，在读线程上分发响应，对服务器到客户端的请求回以空结果，所有等待都有超时，打开文件上限 32（关闭最旧者），并拒绝超过 1 MB 的文件。

可用性是三态报告：`available`、`not_installed`（已配置但没有二进制通过发现——返回安装提示与本地安装命令）、`not_configured`（语言不在 `lsp_enabled_languages` 中）。每条不可用路径都会返回工具名、安装提示，以及回退到 `explore` 或 `terminal`（rg/grep）的建议。自动安装默认关闭（`lsp_auto_install=False`）；启用后也只执行目标语言的白名单命令，并受 60 秒超时与擦洗环境约束。

## 🧠 第 4 层：语义代码搜索

语义搜索嵌入的是第 1 层的符号表，而不是切分任意行窗口，因此每个向量恰好描述一个函数、方法或类。`agent/tools/code_intel/semantic/chunker.py` 用一条标明符号及其位置的头行加上符号体构建分块（读取失败时回退到已存快照）；`agent/tools/code_intel/semantic/indexer.py` 持有 `code_embeddings` 表；`agent/tools/code_intel/semantic/search.py` 按余弦排序并可选重排。

索引天然增量：先刷新符号索引，删除符号已不存在的嵌入，并只为没有向量的符号生成嵌入。写入以 16 个分块为一批（可配置），单次构建最多嵌入 1000 个分块、每文件最多 60 个、每个最多 2000 字符。向量以 `array('d')` 二进制块存入 `code_embeddings` 表，该表同时保存 `model` 名与 `dim`；发现存储的模型变化或维度冲突时，会在同一次调用中清空索引并重建，未完成的索引可续跑，因为已嵌入的符号会被跳过。嵌入后端是既有的 `models.build_embed_model`（由 `EMBEDDING_*` 环境变量选择；默认本地 `bge-m3`）——不引入新的提供方。

搜索会自愈索引：嵌入查询、用纯 Python 余弦相似度对全部已存分块排序、保留 40 个候选池，并在配置了重排器时交给既有的 `models.build_reranker_model`。最终返回默认 `code_intel_semantic_default_top_k`（5）条，最多 20 条。失败开放状态在载荷中显式可见：没有任何已索引嵌入时提示先构建索引；查询模型不可用时返回 `degraded` 消息；未配置重排器时静默保持余弦顺序并令 `reranked=false`；索引与查询维度不匹配时要求重建，而不是对无意义的向量打分。

## 🧭 角色模型与工具面

`agent/tools/subagent/types/functional_role.py` 中的 `CODE_INTEL_ROLES` 是工具注入（`agent/tools/subagent/spawn/core.py`）与提示词指引（`agent/tools/subagent/spawn/system_prompt.py`）共同的唯一事实来源，因此工具面与其文档不会彼此漂移：

| 功能角色 | Tree-sitter 套件 | LSP 工具 | ast-grep |
|----------|------------------|----------|----------|
| `researcher` | 有 | 有 | 有 |
| `librarian` | 有 | 有 | 有 |
| `general` | 无 | 无 | 有 |
| `executor` | 无 | 无 | 有 |
| `reviewer` | 无 | 无 | 有 |

主智能体完全在这张表之外：构建器从不加入 `_MAIN_TOOLS_BUILDERS`，隔离测试断言 `build_lsp_tools` 既不在该列表中，也不在 `agent.tools` 命名空间里。`agent/tools/subagent/roles/definitions/librarian/AGENTS.md` 中的 `librarian` 定义记录了预期的外部仓库工作流——用 `terminal` 克隆，然后用 `explore` 与 `semantic_code_search` 建索引并检索，最后以永久链接作答。两轴角色模型本身（深度角色 × 功能角色）见[子代理设计页](../subagent/README.zh.md)，各角色的工具清单见[子代理系统 README](../../agent/tools/subagent/README.zh.md)。

## ⚙️ 配置

| 对象 | 模块 | 关键项（默认值） |
|------|------|------------------|
| `CODE_INTEL` | `config/features/agent_side/code_intel.py` | 最多 5000 文件、单文件 1 MB、构建预算 60 秒、批次 100、explore 10 符号与 8000 字符、调用深度 3、模糊分数 0.3 |
| `CODE_INTEL_SEMANTIC` | `config/features/agent_side/code_intel_semantic.py` | 模型 `bge-m3`、top-K 5（最大 20）、候选池 40、批次 16、单次构建 1000 分块、每文件 60 分块、每分块 2000 字符 |
| `AST_GREP` | `config/features/agent_side/ast_grep.py` | 锁定 `0.43.0`、50 条匹配、16 KiB 模式、30 秒运行超时、64 路径、60 秒预置超时、5 秒版本探测超时 |
| `LSP` | `config/features/agent_side/lsp.py` | 请求 10 秒、启动 15 秒、诊断 15 秒、并发服务器 2、空闲关停 300 秒、50 条结果、打开文件 32、单文件 1 MB、自动安装关闭 |
| `CODE_INTEL_ROLES` | `agent/tools/subagent/types/functional_role.py` | `researcher` 与 `librarian` |

索引数据库路径默认为 `CODE_INTEL_DIR / "index.db"`，其中 `config/path.py` 定义 `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"`；`SHERRY_CODE_INTEL_ROOT` 与 `SHERRY_CODE_INTEL_DB` 可在调用时覆盖根目录与数据库路径。

## 🖥️ 环境能力矩阵

发现不等于能力：下表是 2026-09-23 在开发主机上实测的状态，“已发现”仅表示解析器找到了候选二进制——并不证明服务器能启动或应答。`python` 是唯一有真实端到端冒烟的语言；`typescript` 仅完成发现；`rust` 解析到 rustup 垫片，但其 `rust-analyzer` 组件未安装，因此启动失败并降级为回退消息；其余七种语言均为 `not_installed`。

| 语言 | 服务器命令 | 本机是否发现 | 验证深度 |
|------|------------|--------------|----------|
| `python` | `basedpyright-langserver --stdio` | 是——仓库 `.venv/bin` | 真实冒烟：定义、引用、诊断、重命名预览与状态 |
| `typescript` | `typescript-language-server --stdio` | 是——`PATH` | 仅发现 |
| `rust` | `rust-analyzer` | 二进制存在——工具链组件缺失 | 启动即降级为回退 |
| `go` | `gopls` | 否 | 安装提示与回退 |
| `cpp` | `clangd` | 否 | 安装提示与回退 |
| `java` | `jdtls` | 否 | 安装提示与回退 |
| `ruby` | `ruby-lsp` | 否 | 安装提示与回退 |
| `bash` | `bash-language-server start` | 否 | 安装提示与回退 |
| `vue` | `vue-language-server --stdio` | 否 | 安装提示与回退 |
| `yaml` | `yaml-language-server --stdio` | 否 | 安装提示与回退 |

同一主机上的其余三个引擎：

| 引擎 | 状态 | 证据 |
|------|------|------|
| Tree-sitter 索引 | 四个语法已安装（`tree-sitter 0.26.0` 家族） | 完整 code-intel 测试套件通过（281 个测试） |
| ast-grep | 可从预置运行时层使用（`ast-grep 0.43.0`） | 基础结构搜索返回真实匹配 |
| 语义搜索 | 本地 `bge-m3` 后端可用 | 真实嵌入冒烟为概念查询返回相关符号 |

缺少这些能力的机器会降级而非失败：ast-grep 未解析时触发带校验的自动预置路径，嵌入后端不可用时 `semantic_code_search` 返回 `degraded` 消息并指回 `explore` 与 `terminal`。

## ⚠️ 限制与失败模式

- **索引由调用触发并自愈。** 没有后台索引器：首次查询构建索引，此后每次查询增量刷新。大仓库在第一次调用时承担构建成本，受文件与时间上限约束（会报告 `truncated`）。
- **文件事件自动同步未采纳。** 语义检索已在每次查询时自愈刷新索引，索引也按需重建，因此常驻的文件监听只会带来进程开销而收益边际。
- **语法错误、超大与未知扩展名的文件会被跳过**，各写入一条记录原因的 `index_meta` 行；它们绝不会被部分索引。
- **嵌入批次会重复加载模型。** 每次 `embed_documents` 调用都会加载后端（本地 GGUF 加载器按批调用），因此批大小是在加载时间与峰值内存之间取舍；单次构建与单文件上限使这一点保持有界。
- **未配置重排器只损失排序质量。** 没有重排器时结果保持余弦顺序，载荷中 `reranked=false`；配置了重排器但调用失败时同样回退到余弦顺序。
- **ast-grep 首次使用需要下载二进制。** 预置路径受 60 秒超时约束，校验和不匹配时拒绝安装；离线或该平台没有资产时，工具改为返回安装提示。
- **LSP 服务器很重。** 管理器限制最多 2 个并发、空闲 300 秒后回收，并淘汰最久未使用者；某种语言的首次请求要支付服务器启动成本，而服务器在窗口内没有任何发布时 `lsp_diagnostics` 可能返回 `timed_out`。
- **本机的 LSP 覆盖不完整。** 只有 `python` 与 `typescript` 可被发现；`rust` 能解析但缺少工具链组件无法启动；其余七种语言需要安装。自动安装默认关闭，因此缺失服务器绝不会触发包管理器运行。
- **librarian 的工具面是 `read_file` / `terminal` / `web_search` 加 code-intel 套件。** 它不含 `search_files` —— 该构建器不在 `_MAIN_TOOLS_BUILDERS` 中，因此外部仓库的关键词检索走 `terminal`（rg/grep）与 `explore`。
- **写入按设计分两步。** `ast_grep_rewrite`、`lsp_rename` 与 `lsp_format` 只有在被明确要求时才写入；预览是安全默认，已应用的编辑仍会经过容器校验。

## 🗺️ 测试地图

| 领域 | 测试 |
|------|------|
| 符号索引、调用图与端到端查询 | `tests/agent/tools/code_intel/test_indexer.py`、`tests/agent/tools/code_intel/test_query.py`、`tests/agent/tools/code_intel/test_e2e.py`、`tests/agent/tools/code_intel/test_integration.py` |
| 索引工具与角色隔离 | `tests/agent/tools/code_intel/test_tools.py`、`tests/agent/tools/code_intel/test_integration.py` |
| ast-grep 发现、预置与工具 | `tests/agent/tools/code_intel/ast_grep/test_resolver.py`、`tests/agent/tools/code_intel/ast_grep/test_provisioner.py`、`tests/agent/tools/code_intel/ast_grep/test_runner.py`、`tests/agent/tools/code_intel/ast_grep/test_install_hints.py`、`tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py` |
| LSP 协议、解析器、安装器、回退、客户端、管理器与工具 | `tests/agent/tools/code_intel/lsp/test_protocol.py`、`tests/agent/tools/code_intel/lsp/test_resolver.py`、`tests/agent/tools/code_intel/lsp/test_installer.py`、`tests/agent/tools/code_intel/lsp/test_fallback.py`、`tests/agent/tools/code_intel/lsp/test_client.py`、`tests/agent/tools/code_intel/lsp/test_manager.py`、`tests/agent/tools/code_intel/lsp/test_lsp_tools.py`、`tests/agent/tools/code_intel/lsp/test_lsp_extended.py` |
| LSP 角色隔离与真实冒烟 | `tests/agent/tools/code_intel/lsp/test_role_isolation.py`、`tests/agent/tools/code_intel/lsp/test_lsp_smoke.py`、`tests/agent/tools/code_intel/lsp/test_lsp_e2e.py` |
| 语义分块、索引、搜索与冒烟 | `tests/agent/tools/code_intel/semantic/test_chunker.py`、`tests/agent/tools/code_intel/semantic/test_indexer.py`、`tests/agent/tools/code_intel/semantic/test_search.py`、`tests/agent/tools/code_intel/semantic/test_semantic_e2e.py`、`tests/agent/tools/code_intel/semantic/test_semantic_smoke.py` |
| 角色接线与提示词小节 | `tests/agent/tools/subagent/types/test_functional_role.py`、`tests/agent/tools/subagent/roles/test_loader.py`、`tests/agent/tools/subagent/spawn/test_functional_role_integration.py`、`tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |

## 🔗 相关文档

| 页面 | 内容 |
|------|------|
| [子代理系统 README](../../agent/tools/subagent/README.zh.md) | 运行时与 API 参考：spawn 流水线、注册表与各角色工具清单 |
| [子代理设计](../subagent/README.zh.md) | 两轴角色模型（深度角色 × 功能角色）与 spawn 权限守卫 |
| [Context Engine README](../../context_engine/README.zh.md) | 语义层复用的嵌入存储与检索模式 |
