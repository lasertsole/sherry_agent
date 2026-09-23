# 代码检索框架 — 补充计划

> 基于 oh-my-openagent-dev 五层 LSP 安装基础设施和 ast-grep MCP 的深度调研，
> 承载代码检索框架的缺口补强与收尾：LSP 二进制发现与自动安装、
> ast-grep 结构化搜索、LSP 语言/工具扩展、Embedding 语义搜索、
> 文件事件自动同步、外部代码检索 subagent。
>
> **本文件是代码检索框架的唯一在册计划。** Phase 1 / 1A / 2S / 2X / 3 / 4 已落地，
> 落点、与提案的差异与测试矩阵并入各「已落地」节；Phase 2 由本文件 Phase 2S 承载，
> Phase 5 为后续独立阶段。
>
> **进度：Phase 1 / 1A / 2S / 2X / 3 / 4 已落地；Phase 5 待派工。**
>
> **ast-grep 是核心必选组件**（对标 oh-my-openagent，在该项目中 ast-grep
> 与 LSP daemon 并列为无条件注册的核心组件，非 opt-in）。

---

## 目录

1. [缺口总览](#缺口总览)
2. [Phase 1A — ast-grep 结构化搜索（已落地）](#phase-1a--ast-grep-结构化搜索已落地)
3. [Phase 2S — LSP 二进制发现与自动安装（已落地，替代原 Phase 2）](#phase-2s--lsp-二进制发现与自动安装已落地替代原-phase-2)
4. [Phase 2X — LSP 语言与工具扩展（已落地）](#phase-2x--lsp-语言与工具扩展已落地)
5. [Phase 3 — Embedding 语义搜索（已落地）](#phase-3--embedding-语义搜索已落地)
6. [Phase 4 — 外部代码检索 subagent（已落地）](#phase-4--外部代码检索-subagent已落地)
7. [Phase 5 — 文件事件自动同步（可选）](#phase-5--文件事件自动同步可选)
8. [修订后的总工期](#修订后的总工期)
9. [修改文件清单（新增）](#修改文件清单新增)
10. [测试计划（补充）](#测试计划补充)
11. [安全清单（补充）](#安全清单补充)

---

## 缺口总览

| #   | 缺口                                                         | oh-my-openagent 对标                                                    | 本计划阶段       |
| --- | ------------------------------------------------------------ | ----------------------------------------------------------------------- | ---------------- |
| 1   | LSP 二进制发现（repo-local → PATH → 安装提示）               | `server-installation.ts` (218 行)                                       | Phase 2S（已落地） |
| 2   | LSP 自动安装 + 安装提示 + 用户决策                           | `server-definitions.ts` + `install-decision.ts`                         | Phase 2S（已落地） |
| 3   | LSP fallback 策略（缺失时降级到 tree-sitter / search_files） | `server-resolution.ts` not_installed 状态                               | Phase 2S（已落地） |
| 4   | ast-grep 结构化搜索/重写                                     | `ast-grep-mcp/` (25 语言, 5 级 strictness)                              | Phase 1A（已落地） |
| 5   | LSP 语言覆盖（4 → 10）                                       | `BUILTIN_SERVERS` (40+ 语言)                                            | Phase 2X（已落地） |
| 6   | LSP 工具覆盖（4 → 8）                                        | symbols/goto-def/refs/rename/diagnostics/format/status/install-decision | Phase 2X（已落地） |
| 7   | 外部代码检索（GitHub/npm/docs）                              | `librarian` subagent                                                    | Phase 4（已落地） |
| 8   | 文件事件自动同步                                             | CodeGraph 2s debounce 文件监听                                          | Phase 5 (可选)   |
| 9   | Embedding 语义搜索（概念 → 代码块）                          | CodeGraph 语义检索 / `semantic_code_search`                             | Phase 3（已落地） |

---

## Phase 1A — ast-grep 结构化搜索（已落地）

> **状态：已落地。** 本节只保留落点、与提案的差异、实测摘要与平台覆盖；
> 规格细节已随实现移除。

### 落点

- `config/features/agent_side/ast_grep.py`：`AstGrepConfig` TypedDict + `AST_GREP` 实例，经
  `agent_side/__init__.py` 与 `config/features/__init__.py` **双级 re-export**。
- `agent/tools/code_intel/ast_grep/`：`resolver.py`（5 层发现：env → runtime → code-intel bin →
  PATH → Homebrew，每层跑 `--version` 探针）、`provisioner.py`（SHA-256 校验的 GitHub
  release 下载 + stdlib `zipfile` 解压）、`install_hints.py`、`runner.py`
  （`ast_grep_search` / `ast_grep_rewrite`）、`__init__.py`。
- `agent/tools/code_intel/ast_grep/scripts/install.sh` + `install.ps1`：7 路包管理器 fallback
  （brew → npm → cargo → pip → nix → mise → GitHub ZIP）。
- `agent/tools/subagent/spawn/core.py::_build_child_agent()`：**所有 functional_role 的
  subagent 均注入** ast-grep；`ast_grep_*` 从不进入 `_MAIN_TOOLS_BUILDERS`，main agent 不可见。
- `.codeintel/`（gitignored）承载第 3 层 bin 缓存（`CODE_INTEL_DIR/ast-grep/bin`）；运行时二进制
  写入 `~/.sherry/runtime/ast-grep/<slug>/sg`，**均不入库**。

### 与提案的差异

- **提取真实二进制而非 `sg` 启动器**：0.43.0 的 release ZIP 同时含 51 MB 的 `ast-grep`
  真二进制与约 440 KB 的 `sg` 启动器；启动器按自身路径 re-exec，在受限 PRoot 沙箱中失败。
  provisioner 因此**始终优先提取 `ast-grep`**，再以 resolver 期望的 `sg` 名写入运行时目录。
- **rewrite 语义修正**：`sg run` 没有 `--dry-run` 选项。dry-run 用 `--json=stream`
  （只预览、不写盘，且与 `-U` 互斥）；apply 用 `--update-all`，其 "Applied N changes"
  输出走 **stderr**（解析 stdout + stderr 两路）。
- **路径安全自持**：子代理中间件链不注册 `PathGuard`，`runner.py` 因此自行用
  `resolve_project_path`（生产）/ `SHERRY_SG_ROOT`（沙箱）校验每条路径；遍历或越界一律拒绝，
  `dry_run=False` 也只能写项目根内。
- **未改 `spawn/system_prompt.py`**：提案文件清单列了「追加 ast-grep 使用指导」，实际未做
  （工具描述已自解释），属有意省略。
- **实测摘要与平台覆盖**：release 无 checksums 资产，故 6 平台 SHA-256 均由**官方 release
  资产实际下载后计算**，6 个值全部验证（darwin/win32/linux × arm64/x64）。本机平台为
  **linux-arm64**（非提案假定的 x86_64）。实测 linux-arm64 归档摘要
  `e706846148493967f3ab8011334817edd86ce5acbec10718b2a7b40799c640ff`（与配置一致）；
  provision 实测 2.7 s，产出 51,531,936 B / 0755 的 `sg`。

## Phase 2S — LSP 二进制发现与自动安装（已落地，替代原 Phase 2）

> **状态：已落地。** 本节只保留落点、与提案的差异、实测摘要与实际支持的语言服务器/发现矩阵；
> 规格细节已随实现移除。原计划 Phase 2 的 4 个 LSP 工具按原设计保留，并已随本阶段落地。

### 落点

- `config/features/agent_side/lsp.py`：`LspConfig` TypedDict + `LSP` 实例（原计划 7 字段 +
  2S 补充 7 字段 + 资源约束 5 字段），经 `agent_side/__init__.py` 与
  `config/features/__init__.py` **双级 re-export**。
- `agent/tools/code_intel/lsp/protocol.py`：URI/Position/Range 原语 + `detect_language`。
- `agent/tools/code_intel/lsp/resolver.py`：5 层发现（显式配置 → repo-local（marker-gated）→
  `~/.sherry/runtime/lsp/<slug>` → `PATH` → Homebrew 前缀），每进程缓存 +
  `_clear_cache_for_tests`。
- `agent/tools/code_intel/lsp/installer.py`：白名单命令自动安装（超时 60s；
  `lsp_auto_install` 默认 False），安装后清缓存重发现。
- `agent/tools/code_intel/lsp/fallback.py`：`check_lsp_availability`
  （available / not_installed / not_configured 三态）+ `build_fallback_message`。
- `agent/tools/code_intel/lsp/client.py`：stdio JSON-RPC 客户端（initialize 握手 / didOpen /
  请求 / 超时 / publishDiagnostics / shutdown；读取线程按 id 分发响应）。
- `agent/tools/code_intel/lsp/manager.py`：进程级管理器——**按需启动 + 并发上限（LRU 淘汰）+
  空闲自动关停 + 显式/`atexit` shutdown，绝不留孤儿**（J10 资源约束的实现载体）。
- `agent/tools/code_intel/lsp/tools.py`：4 个 researcher 专属工具
  `lsp_goto_definition` / `lsp_find_references` / `lsp_workspace_symbol` /
  `lsp_call_hierarchy`；每次执行前 `check_lsp_availability`，缺失/启动失败/超时一律 fail-open
  返回安装提示 + fallback 建议。
- 注入点 `agent/tools/subagent/spawn/core.py::_build_child_agent()`：**仅
  `FunctionalRole.RESEARCHER`**；`lsp_*` 永不进入 `_MAIN_TOOLS_BUILDERS`。
  `spawn/system_prompt.py` 的 RESEARCHER 段追加 LSP 工具指导。

### 与提案的差异

- **新增 `manager.py`（提案未列）**：语言服务器是重型常驻子进程。提案只在 client 内提到
  “超时 + 进程清理、按 session 隔离（subagent 结束时 shutdown）”；实现把资源约束集中到进程级
  管理器：按需启动、并发上限、空闲关停、显式/atexit 兜底。客户端按 `(language, cwd)` 进程级
  共享，而非 per-session——2S 的 `LSPClient(language, cwd)` 签名本身无 session_id。
- **发现层 3 → 5 层**：提案为显式 / repo-local / PATH；实现沿用 1A 范式补
  `~/.sherry/runtime/lsp/<slug>` 与 Homebrew 前缀。
- **不做 `--version` 探针**：与 ast-grep 不同，LSP 服务器无法可靠探针——
  `basedpyright-langserver --version` 直接抛错退出。解析层只做文件存在性判断；损坏但存在的
  二进制（如悬空 rustup 代理）在 initialize 握手阶段被发现并 fail-open 降级。
- **显式绝对路径缺失不短路**：提案在绝对路径不存在时直接返回 `None`；实现继续尝试更低层
  （fail-open 更彻底）。
- **安装不做摘要校验**：LSP 经包管理器安装，无下载归档，故不涉及 1A 的 SHA-256 校验；其余安全项
  （命令白名单、默认关闭、60s 超时、`scrub_env`）照做。

### 实际支持的语言服务器与发现矩阵

| 语言 | 服务器 | 本机发现结果 | 状态 |
| ---- | ------ | ------------ | ---- |
| python | `basedpyright-langserver` | `<repo>/.venv/bin`（repo-local，`pyproject.toml` marker） | ✅ 真实冒烟通过（definition / references / diagnostics） |
| typescript | `typescript-language-server` | `PATH`（`/usr/bin/typescript-language-server`） | 二进制存在（未做真实冒烟） |
| rust | `rust-analyzer` | `PATH`（`~/.cargo/bin/rust-analyzer`，rustup 代理） | ⚠️ 代理存在但工具链组件缺失，握手 fail-open 降级 |
| go | `gopls` | 未发现 | ❌ 需安装 |

**发现矩阵**（层 → 规则 → 命中示例）：

| 层 | 规则 | 命中示例 |
| -- | ---- | -------- |
| 1 显式配置 | `lsp_*_server` / `command[0]` 为绝对路径 | 测试注入的假服务器 |
| 2 repo-local | marker-gated：`pyproject.toml`→`.venv/bin`、`package.json`→`node_modules/.bin`、`Cargo.toml`→`target/{debug,release}`、`go.mod`→`bin`（向上走到 repo root） | 本仓库 python（`.venv/bin`） |
| 3 Sherry runtime | `~/.sherry/runtime/lsp/<slug>`（`SHERRY_LSP_RUNTIME_DIR` 覆盖） | 用户手动放置 |
| 4 PATH | 含 Windows `PATHEXT` 后缀匹配 | typescript / rust |
| 5 Homebrew | `/opt/homebrew/bin`、`/usr/local/bin`、`/home/linuxbrew/.linuxbrew/bin` | macOS / linuxbrew |

### 测试

`tests/agent/tools/code_intel/lsp/`：

- `test_resolver.py`（unit）— 5 层命中/未命中、marker-gating、缓存开关、Windows 后缀、
  安装提示、config 双级 re-export。
- `test_installer.py`（unit）— auto_install 关闭、无命令、超时、缺工具、非零退出、成功后重发现、
  env 清洗。
- `test_fallback.py`（unit）— 三态 + fallback 消息格式。
- `test_client.py`（unit）— 握手、定义请求、didOpen/diagnostics、超时、错误响应、
  shutdown/force_kill 进程回收。
- `test_manager.py`（unit）— 惰性启动、复用、并发上限 LRU 淘汰、空闲关停、shutdown_all、无孤儿。
- `test_lsp_tools.py`（integration）— 4 工具端到端 + 路径安全 +
  not_installed/error/timeout 降级。
- `test_role_isolation.py`（module）— main 不可见、general/executor/reviewer 不含、
  researcher 与 librarian 含 LSP + code_intel + ast-grep。
- `test_lsp_e2e.py`（module）— 假服务器 hermetic e2e：4 工具共享一个惰性启动的服务器，结束回收。
- `test_lsp_smoke.py`（integration）— **真实 `basedpyright-langserver`** 冒烟：
  definition / references / diagnostics + 进程回收。

---

## Phase 2X — LSP 语言与工具扩展（已落地）

> **状态：已落地。** 本节只保留落点、与提案的差异、实际语言/工具矩阵与本机真实覆盖情况；
> 规格细节已随实现移除。原计划 Phase 2 的 4 个 LSP 工具在 2S 已落地，本节补齐 4 个新工具。
> **覆盖规模：** 4 语言 + 4 工具 → **10 语言 + 8 工具**（提案标题写“12+”，但其自带新增表只列 6 个
> 语言，实际即 10；两者差异以表为准，如实记录）。

### 落点

- `config/features/agent_side/lsp.py`：`lsp_supported_servers` 追加 6 语言（cpp/java/ruby/bash/
  vue/yaml），`lsp_enabled_languages` 4 → 10，`lsp_install_hints` / `lsp_auto_install_commands`
  同步；`LspServerSpec` 新增 `language_id` 字段；新增 `lsp_diagnostics_timeout_s`；
  repo-local marker 规则补 ruby/bash/vue/yaml。
- `agent/tools/code_intel/lsp/protocol.py`：新增 `language_id`（config key → 协议 languageId）、
  `format_diagnostic`、`format_text_edit`、`normalize_workspace_edit`。
- `agent/tools/code_intel/lsp/client.py`：`didOpen` 发送协议 `languageId`；client capabilities
  广告 `rename` / `formatting` / `rangeFormatting`；新增 `cached_diagnostics`（不阻塞读取）。
- `agent/tools/code_intel/lsp/tools.py`：新增 `lsp_rename` / `lsp_diagnostics` / `lsp_format` /
  `lsp_status`，`build_lsp_tools` 返回 8 个 researcher 工具；新增 WorkspaceEdit/TextEdit 应用器
  （默认预览，落盘走既有项目根路径闸）。
- `agent/tools/code_intel/lsp/__init__.py`：导出新工具与协议原语。
- `agent/tools/subagent/spawn/system_prompt.py`：RESEARCHER 段追加 4 个新工具指导。
- `agent/tools/subagent/spawn/core.py`：无需改动（`build_lsp_tools` 返回集自动扩到 8；注释同步）。
- 测试：`tests/agent/tools/code_intel/lsp/test_protocol.py`（新建 unit）、`test_lsp_extended.py`
  （新建 integration）、`test_lsp_smoke.py`（追加真实工具冒烟）、`test_lsp_e2e.py`（扩到 8 工具 +
  多语言惰性启动）、`conftest.py`（假服务器支持 rename/formatting/nodiag）。

### 与提案的差异

- **配置键 ≠ 协议 languageId**：提案把“扩展名 → 语言”当作一步；实现区分服务器选择句柄与协议
  标识（Bash 配置为 `bash`、协议须为 `shellscript`），在 `LspServerSpec` 显式携带 `language_id`，
  `protocol.language_id()` 读取并逐语言断言。这是本阶段最易错处。
- **`lsp_format` 方法双路**：整文件走 `textDocument/formatting`，仅当传入 1-based range 时走提案
  指定的 `textDocument/rangeFormatting`；服务器返回 null/error 时如实报 `supported=false`，绝不
  伪造“已格式化”。
- **rename/format 默认预览**：`lsp_rename` 默认 `dry_run=true`、`lsp_format` 默认 `write=false`，
  与 ast-grep rewrite 的 dry_run 一致；只有显式开启才落盘，且每条 edit 的文件路径都过
  `resolve_project_path` / `SHERRY_LSP_ROOT` 闸，越界路径记入 `skipped`。
- **diagnostics 是异步通知**：`lsp_diagnostics` 打开文件后按 `lsp_diagnostics_timeout_s` 等待
  `publishDiagnostics`，再读缓存；超时如实返回 `timed_out=true`（`cached_diagnostics()` 区分
  “空诊断”与“未到达”）。
- **`lsp_status` 不启动服务器**：仅聚合 resolver 三态 + manager 活跃快照，逐语言给
  `available`/`not_installed`/`not_configured`；本机未装的 6 个语言如实标 `not_installed`。
- **enabled list 语义**：`lsp_enabled_languages` 4 → 10，但它仍是“愿望清单”而非预装步骤——
  是否可用完全由 `resolve_lsp_server()` 发现二进制决定（未预装任何 server）。cpp/java 无
  project-local bin 目录，故不设 repo-local 规则，只走 explicit/runtime/PATH/Homebrew。

### 实际语言与工具矩阵

| 语言 | 服务器 | 扩展名 | 协议 languageId | 本机发现结果 | 状态 |
| ---- | ------ | ------ | --------------- | ------------ | ---- |
| python | `basedpyright-langserver` | `.py .pyi` | `python` | `<repo>/.venv/bin` | ✅ 真实冒烟（definition/references/diagnostics/rename/status） |
| typescript | `typescript-language-server` | `.ts .tsx .js .jsx .mjs .cjs .mts .cts` | `typescript` | `PATH` `/usr/bin` | 二进制存在（未做真实冒烟） |
| rust | `rust-analyzer` | `.rs` | `rust` | `PATH`（rustup 代理） | ⚠️ 代理存在但组件缺失，握手 fail-open |
| go | `gopls` | `.go` | `go` | 未发现 | ❌ 需安装 |
| cpp | `clangd` | `.c .cpp .cc .cxx .h .hpp` | `cpp` | 未发现 | ❌ 需安装 |
| java | `jdtls` | `.java` | `java` | 未发现 | ❌ 需安装 |
| ruby | `ruby-lsp` | `.rb .rake` | `ruby` | 未发现 | ❌ 需安装 |
| bash | `bash-language-server` | `.sh .bash .zsh` | `shellscript` | 未发现 | ❌ 需安装 |
| vue | `vue-language-server` | `.vue` | `vue` | 未发现 | ❌ 需安装 |
| yaml | `yaml-language-server` | `.yaml .yml` | `yaml` | 未发现 | ❌ 需安装 |

**工具矩阵（8，全部 RESEARCHER-only）：**

| 工具 | LSP 方法 | 真实（basedpyright）验证 | 假服务器覆盖 |
| ---- | -------- | ------------------------ | ------------ |
| `lsp_goto_definition` | `textDocument/definition` | ✅ 真实冒烟 | fake |
| `lsp_find_references` | `textDocument/references` | ✅ 真实冒烟 | fake |
| `lsp_workspace_symbol` | `workspace/symbol` | —（仅 fake，basedpyright 未做） | fake |
| `lsp_call_hierarchy` | `callHierarchy/*` | —（仅 fake） | fake |
| `lsp_rename` | `textDocument/rename` | ✅ 真实冒烟（返回真实 WorkspaceEdit，预览不改文件） | fake（含 apply + 路径闸） |
| `lsp_diagnostics` | `textDocument/publishDiagnostics` | ✅ 真实冒烟（坏文件返回真实诊断） | fake（含超时窗口） |
| `lsp_format` | `textDocument/formatting` + `rangeFormatting` | ❌ 仅 unsupported 路径可测（basedpyright 不支持格式化，返回 error/null） | fake（supported/unsupported/write/range） |
| `lsp_status` | —（内部聚合） | ✅ 真实冒烟（10 语言如实列状态，不启动服务器） | fake（三态 + 门控） |

**本机真实覆盖诚实标注：** python 工具的 definition/references/diagnostics/rename/status 有真实
`basedpyright-langserver` 冒烟；`lsp_format` **没有**真实成功冒烟（basedpyright 不支持格式化，
只覆盖了 unsupported 分支）；workspace_symbol/call_hierarchy 沿用 2S 的 fake 覆盖。其余 9 个语言
本机均未安装，只有 resolver 层断言（各有安装提示）。

### 测试

`tests/agent/tools/code_intel/lsp/`：

- `test_protocol.py`（unit，新建）— 10 语言 extension → language → languageId 逐语言断言、
  diagnostics severity/range、WorkspaceEdit（changes / documentChanges）归一化。
- `test_resolver.py`（unit，扩展）— 新增 6 语言的 PATH/显式发现、marker-gated repo-local
  （bash/vue/yaml/ruby）、安装提示、enabled 集合 4 → 10。
- `test_lsp_extended.py`（integration，新建）— rename 预览/apply/空名/未装/路径闸、diagnostics
  聚合与超时、format 预览/写入/不支持/range、status 三态与门控。
- `test_lsp_smoke.py`（integration，扩展）— **真实 basedpyright**：`lsp_diagnostics` 真实诊断、
  `lsp_rename` 真实 WorkspaceEdit 预览（不改文件）、`lsp_status` 如实列出 + 进程回收。
- `test_lsp_e2e.py`（module，扩展）— 8 工具共享一个惰性服务器 + 多语言双服务器惰性启动。
- `test_role_isolation.py` / `test_lsp_tools.py`（module/integration）— 工具面 4 → 8 同步。

---

## Phase 3 — Embedding 语义搜索（已落地）

> **状态：已落地。** 本节承接原计划 Phase 3 并记录落点、与提案的差异、实测可用性与测试；
> 工具面新增 `semantic_code_search`，仍为 **RESEARCHER 专属**。

### 落点

- `config/features/agent_side/code_intel_semantic.py`：`CodeIntelSemanticConfig` +
  `CODE_INTEL_SEMANTIC` 实例（`model` / `default_top_k` / `max_top_k` / `candidate_pool` /
  `batch_size` / `max_chunks` / `max_chunks_per_file` / `max_chunk_chars`），经
  `agent_side/__init__.py` 与 `config/features/__init__.py` **双级 re-export**。
- `agent/tools/code_intel/semantic/chunker.py`：按 Phase 1 symbol 表分块
  （function/method/class）；每块 = 符号源码 + 文件路径 + 行号 + 符号名 + kind；
  纯函数、无磁盘/DB。
- `agent/tools/code_intel/semantic/indexer.py`：`SemanticIndexer.build()`——先复用
  `CodeIndexer` 刷新符号索引，删除孤儿 embedding（文件重索引后符号 id 变化），
  再只对缺失向量的符号分批 embed；`code_embeddings` 表（BLOB，复用
  `context_engine` 的 `array.array("d")` 打包）含 `model` / `dim` / `chunk_text` /
  `created_at` 列 + `idx_code_emb_symbol` 索引。
- `agent/tools/code_intel/semantic/search.py`：`SemanticSearch.search()`——自愈式增量刷新 →
  `embed_query` → 纯 Python cosine 排序 → 现有 reranker 重排 → top-K；`semantic_code_search`
  LangChain 工具亦在此。
- `agent/tools/code_intel/tools.py::build_code_intel_tools()`：追加 `semantic_code_search`，
  仍仅由 `spawn/core.py::_build_child_agent()` 在 `FunctionalRole.RESEARCHER` 时注入
  （`_MAIN_TOOLS_BUILDERS` 不可见）。
- `agent/tools/subagent/spawn/system_prompt.py`：RESEARCHER 段追加 `semantic_code_search` 指导。

### 与提案的差异

- **嵌入模型复用而非新增**：直接用现有 `models/embed_model/core.py::build_embed_model()`
  （本地 bge-m3 GGUF / 远端 API，由既有 `EMBEDDING_*` 决定），**不引入任何新 provider /
  环境变量 / 开关**。
- **存储/相似度复用既有模式**：BLOB 用 `array.array("d").tobytes()`（同
  `context_engine/embeddings/store.py`），相似度用纯 Python cosine（不新增 numpy 依赖）；
  schema 按提案落地（`symbol_id` PK、`embedding` BLOB、`model`、`dim`、`chunk_text`、
  `created_at` + `idx_code_emb_symbol`）。
- **增量不依赖 symbol id 稳定**：Phase 1 重索引文件会重建符号行（新 id），旧 embedding
  因此变为孤儿，由 `DELETE ... WHERE symbol_id NOT IN (SELECT id FROM symbols)` 清除；
  未变文件的行保留，二次 build 不再 embed（实测 `embedded=0`）。
- **模型/维度一致性明确处理**：模型名变化 → 全表 purge 重建；维度冲突 → purge 后**重启本轮**
  重新嵌入全部符号（不留混合维度）；搜索期维度不符 → 明确 `degraded`（拒绝混用，不静默出垃圾）。
- **reranker 始终尝试、fail-open**：不设开关。reranker 不可用（如云端未配置 API）时保持
  cosine 顺序并在结果里如实标 `reranked=false`（本机实测即为该路径）。
- **分批 + 有上限 + 逐批释放**：`batch_size` / `max_chunks` / `max_chunks_per_file` /
  `max_chunk_chars` 全部有界；每批 embed 后写库并释放向量，峰值内存由一批决定。
- **无 LLM、无网络（本地模式）**：仅 parse + 读取 + 向量化 + SQLite。

### 实测可用性

- 本机 `EMBEDDING_MODEL_LOCAL=true`，`bge-m3-q8_0.gguf`（634 MB）已在位；真实嵌入冒烟通过
  （fixture 仓库 `indexed=23`，concept query 命中 `helper` / `top_func`），进程峰值 RSS
  约 **1.85 GB**（模型加载前 0.60 GB → 加载并嵌入后 1.85 GB）。
- 本机 reranker 为云端模式但 `RERANKER_API_BASE` / `RERANKER_API_KEY` 为空 → 构造即失败，
  如实降级为 cosine 顺序（`reranked=false`），符合 fail-open 设计。
- CI 的 llm-e2e 作业以 `--no-install-package llama-cpp-python` 同步，本地嵌入运行时缺失；
  真实嵌入冒烟测试以「运行时 + 权重在位」为前提，缺失即 **skip**（不下载、不失败）。

### 测试

`tests/agent/tools/code_intel/semantic/`：

- `test_chunker.py`（unit）— 分块 header/边界/截断、空文件、kind 过滤、snippet 回退。
- `test_indexer.py`（unit）— 增量二次索引、变更文件重嵌、删除孤儿、模型/维度重建、坏块
  fail-open、`max_chunks` 截断、`batch_size`、语法错误文件跳过。
- `test_search.py`（unit）— cosine 排序、top-K 夹取、reranker 重排与降级、空查询/空索引/
  维度不符/查询嵌入失败降级、编辑后自愈。
- `test_semantic_e2e.py`（module）— 假嵌入 + 假 reranker 的 build→search 全链路（工具层）。
- `test_semantic_smoke.py`（integration）— 真实 bge-m3：build → concept search
  （运行时/权重缺失则 skip）。
- 既有 `test_tools.py`、`test_integration.py`（角色隔离三方向）、
  `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`、
  `tests/agent/tools/taskflow/test_role_synthesize_smoke.py` 的工具面 exact-list 已同步。

---

## Phase 4 — 外部代码检索 subagent（已落地）

> **状态：已落地。** 本节只保留落点、与提案的差异（含三处规格修正）、实测工具面与测试；
> 规格细节已随实现移除。librarian 对标 oh-my-openagent 的 librarian subagent，
> 专门检索**外部代码库**（GitHub 仓库、文档），与 Phase 1-2 的**内部**代码检索互补。

### 落点

- `agent/tools/subagent/types/functional_role.py`：`FunctionalRole` 新增第 5 个成员
  `LIBRARIAN = "librarian"`；新增单一命名来源
  `CODE_INTEL_ROLES = frozenset({RESEARCHER, LIBRARIAN})`。
- `agent/tools/subagent/roles/definitions/librarian/AGENTS.md`：新建 librarian 角色定义
  （frontmatter `name` / `description` / `model_tier: auxiliary` / `tools` + "THE LIBRARIAN" prompt body）。
- `agent/tools/subagent/spawn/core.py`：code-intel + LSP 注入条件由 `== RESEARCHER` 改为
  `in CODE_INTEL_ROLES`（单一来源，同时覆盖 researcher 与 librarian）。
- `agent/tools/subagent/spawn/system_prompt.py`：`## Code Intelligence Tools` 指引段同步改用
  `CODE_INTEL_ROLES`，工具面与提示词段不会漂移。
- `agent/tools/subagent/tools/sessions_spawn.py`：`functional_role` 描述补 librarian。
- 测试：`tests/agent/tools/subagent/test_librarian_role.py`（新建）、`roles/test_loader.py`、
  `types/test_functional_role.py`、`spawn/test_functional_role_integration.py`、
  `tests/agent/tools/code_intel/test_integration.py`、
  `tests/agent/tools/code_intel/lsp/test_role_isolation.py`、
  `tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py`、
  `tests/agent/tools/subagent/test_spawn_privilege_guard.py`（4→5 角色期望同步）。

### 与提案的差异（三处规格修正 + 工具面）

- **① 删除 `web_fetch`**：提案把 `web_fetch` 标为“已有”，但仓库从未暴露该工具
  （`agent/tools/web_search.py` 只暴露 `web_search`，其结果自带页面内容摘要）。文档抓取改由
  `web_search` 承担；librarian prompt 的 TYPE A 第 2 步改成用 `web_search` 检索具体文档页。
- **② 落点改为 `roles/definitions/librarian/AGENTS.md`**：提案的
  `agent/tools/subagent/spawn/role_definitions/librarian.py` 路径已随角色迁移失效；现行角色定义
  加载器只认 `agent/tools/subagent/roles/definitions/<name>/AGENTS.md`（+ workspace 覆盖
  `workspace/subagent_roles/librarian/AGENTS.md`，gitignored）。
- **③ `FunctionalRole` 加第 5 个成员**：提案假定 `agent_id="librarian"` 会被 spawn 解析，但
  `_resolve_functional_role()` 走 `FunctionalRole(agent_id)`，`librarian` 非枚举成员会 `ValueError`
  退回 `default_functional_role`。加 `LIBRARIAN` 成员后该路径自然解析（零机制成本）。
  **注**：全量 spawn 仍受 `runtime_isolation` 约束——`agent_id` 只能取 `main`/`subagent`，故选择
  librarian 的受支持入口是 `sessions_spawn(functional_role="librarian")` 提示；`agent_id`→角色
  解析在 resolver 层由测试锁住。
- **librarian 是代码检索角色**：与 researcher 一样拿到**完整** code-intel 套件——
  `explore` / `callers` / `callees` / `impact` / `semantic_code_search` + 8 个 LSP 工具 + ast-grep
  （ast-grep 本就是全角色注入）。角色白名单 `read_file` / `terminal` / `web_search` / `search_files`
  只读；无 write/patch/python_repl，无 spawn/yield/send。
- **不新增工具**：librarian 复用现有工具，新增的只是一个角色定义 + 一个枚举成员。
- **`search_files` 说明**：它是真实存在的工具构建器
  （`agent/tools/file_tools/search_files.py`，`name="search_files"`），但**未登记进
  `_MAIN_TOOLS_BUILDERS`**（既有现状）；因此 librarian 的该白名单项在候选集里解析为空，
  clone 后仓库检索实际由 `terminal`（rg/grep）与 code-intel `explore` 承担。此项不在本阶段改动之列
  （改主工具集会影响 main 与其它角色）。

### 工具面（librarian）

`read_file` / `terminal` / `web_search` / `search_files`（角色白名单）
+ `explore` / `callers` / `callees` / `impact` / `semantic_code_search`
+ `lsp_goto_definition` / `lsp_find_references` / `lsp_workspace_symbol` / `lsp_call_hierarchy` /
`lsp_rename` / `lsp_diagnostics` / `lsp_format` / `lsp_status`
+ `ast_grep_search` / `ast_grep_rewrite`。
与 researcher 的 code-intel/LSP 面逐项一致（角色白名单不同）。

### 测试

- `tests/agent/tools/subagent/test_librarian_role.py`（integration）— librarian 解析（hint + agent_id
  resolver 层）、system prompt 注入 THE LIBRARIAN + Code Intelligence 指引、child 工具面含完整
  code-intel、hermetic spawn e2e。
- `roles/test_loader.py` — librarian 定义加载 / workspace 覆盖 / 坏 frontmatter fail-open /
  tools 真实性（含 `web_fetch` 缺席断言）。
- `types/test_functional_role.py` — 5 成员 + `CODE_INTEL_ROLES`。
- `spawn/test_functional_role_integration.py`、`code_intel/test_integration.py`、
  `code_intel/lsp/test_role_isolation.py`、`code_intel/ast_grep/test_ast_grep_e2e.py`、
  `test_spawn_privilege_guard.py` — 四向 + main 角色隔离与新期望同步。

---

## Phase 5 — 文件事件自动同步（可选）

> **前置条件：Phase 1 完成。**
> **定位：** 对标 oh-my-openagent CodeGraph 的 2s debounce 文件监听。
> 当前 sherry 的 tree-sitter 索引仅在查询时按 mtime 增量更新，
> 无实时性。本阶段添加文件监听 + 后台增量重索引。

### 设计

```
┌─ 文件监听器（进程级，server 启动时启动）──────────────────────────┐
│                                                                    │
│  watchfiles / asyncio 监听 ROOT_DIR（排除 prune_dirs）              │
│  ├─ 文件变更事件 → debounce 2s                                     │
│  ├─ debounce 后 → 增量重索引变更文件（mtime 检查）                  │
│  └─ 写入 .codeintel/db.sqlite                                      │
│                                                                    │
│  与查询路径的关系：                                                  │
│  └─ explore/callers/callees/impact 查询时仍做 mtime 检查            │
│     （监听器可能落后，mtime 是最终一致性保障）                       │
└────────────────────────────────────────────────────────────────────┘
```

### 依赖

```toml
# pyproject.toml [project] dependencies — 新增
"watchfiles>=2.0",  # Rust-based file watcher (asyncio)
```

### 新增文件（1 个）

#### `agent/tools/code_intel/watcher.py`

```python
"""Background file watcher for incremental re-indexing.

Started by the server process (not by subagents). Watches ROOT_DIR,
debounces file events, and triggers incremental re-indexing of changed
files in the .codeintel/db.sqlite symbol index.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from watchfiles import awatch

from config.features import CODE_INTEL
from config.path import ROOT_DIR
from .indexer import CodeIndexer


async def start_index_watcher(stop_event: asyncio.Event) -> None:
    """Start background file watcher for code intel index.

    Called once at server startup. Runs until stop_event is set.
    """
    if not CODE_INTEL.get("code_intel_index_db_path"):
        return  # code intel not configured

    indexer = CodeIndexer(
        CODE_INTEL["code_intel_index_db_path"],
        CODE_INTEL,
    )
    prune_dirs = set(CODE_INTEL["code_intel_prune_dirs"])
    debounce_s = 2.0

    async for changes in awatch(
        str(ROOT_DIR),
        stop_event=stop_event,
        watch_filter=lambda change, path: not any(
            p in path for p in prune_dirs
        ),
    ):
        # Debounce: collect changes, wait 2s, then batch re-index
        await asyncio.sleep(debounce_s)
        changed_files = [
            Path(path) for _, path in changes
            if Path(path).suffix in CODE_INTEL["code_intel_supported_extensions"]
        ]
        if changed_files:
            try:
                indexer.index_files(changed_files, ROOT_DIR)
                logger.debug("Re-indexed {} files", len(changed_files))
            except Exception:
                logger.exception("Background re-index failed")
```

### 注入点

在 `server/__main__.py` 或 `server/service/` 中启动：

```python
# server startup
import asyncio
from agent.tools.code_intel.watcher import start_index_watcher

stop_event = asyncio.Event()
# Start watcher as background task
asyncio.create_task(start_index_watcher(stop_event))
# On shutdown: stop_event.set()
```

### 工期

| 步骤     | 内容                                  | 预估    |
| -------- | ------------------------------------- | ------- |
| 1        | `pyproject.toml` 加 `watchfiles` 依赖 | 0.1h    |
| 2        | `agent/tools/code_intel/watcher.py`   | 2h      |
| 3        | 修改 server 启动逻辑                  | 0.5h    |
| 4        | 测试                                  | 1.5h    |
| **小计** |                                       | **~4h** |

---

## 修订后的总工期

| 阶段     | 内容                                             | 预估       | 前置            |
| -------- | ------------------------------------------------ | ---------- | --------------- |
| 前置     | subagent 功能角色分工（已落地）                  | —          | —               |
| Phase 1  | tree-sitter 符号索引 + 调用图 (原计划)           | ~15h       | 前置            |
| Phase 1A | ast-grep 结构化搜索 + 二进制 provision (已落地)  | —          | 前置            |
| Phase 2S | LSP 二进制发现 + 自动安装 + fallback (替代原 P2) | ✅ 已落地   | Phase 1 + 1A    |
| Phase 2X | LSP 语言 + 工具扩展 (新增)                       | ✅ 已落地   | Phase 2S        |
| Phase 3  | Embedding 语义搜索 (原计划)                      | ✅ 已落地   | Phase 1         |
| Phase 4  | 外部代码检索 subagent (新增)                     | ✅ 已落地   | Phase 1A + 前置 |
| Phase 5  | 文件事件自动同步 (可选, 新增)                    | ~4h        | Phase 1         |
| **合计** |                                                  | **~65.5h** |                 |

> 原计划 ~34h → 修订后 ~65.5h（规划期估算）。增量 ~31.5h 主要来自 ast-grep 二进制 provision
> 基础设施（Phase 1A, +13h）和 LSP 基础设施（Phase 2S, +5h vs 原 Phase 2 的 ~11h → ~16h）。
>
> Phase 1 / 1A / 2S / 2X / 3 / 4 **已落地**；剩余 Phase 5 待派工。

### 推荐实施顺序

```
前置 (subagent 功能角色分工)
  ├→ Phase 1 (tree-sitter, 15h)
  └→ Phase 1A (ast-grep + provision) ← 已落地，与 Phase 1 并行
       ├→ Phase 2S (LSP 基础设施, 16h) ← ✅ 已落地（需 Phase 1 + 1A）
        │    └→ Phase 2X (LSP 扩展, 7h) ← ✅ 已落地
       ├→ Phase 3 (Embedding, 8h) ← ✅ 已落地
       └→ Phase 4 (librarian, 2.5h) ← ✅ 已落地（可与 Phase 2S 并行）
            Phase 5 (file watcher, 4h) ← 可选，最后
```

---

## 修改文件清单（新增）

### Phase 1A 修改（已落地）

| 文件                                               | 修改                                          |
| -------------------------------------------------- | --------------------------------------------- |
| `config/features/agent_side/ast_grep.py`           | 新建（含 6 平台 SHA-256 provision 配置）      |
| `config/features/agent_side/__init__.py`           | re-export `AST_GREP` / `AstGrepConfig`        |
| `config/features/__init__.py`                      | 顶层 re-export                                |
| `agent/tools/code_intel/ast_grep/__init__.py`      | 新建                                          |
| `agent/tools/code_intel/ast_grep/resolver.py`      | 新建 — 5 层二进制发现 + `--version` 探针      |
| `agent/tools/code_intel/ast_grep/provisioner.py`   | 新建 — SHA-256 校验下载 + 提取真二进制        |
| `agent/tools/code_intel/ast_grep/install_hints.py` | 新建 — 安装提示                               |
| `agent/tools/code_intel/ast_grep/runner.py`        | 新建 — `ast_grep_search` / `ast_grep_rewrite` |
| `agent/tools/code_intel/ast_grep/scripts/install.sh`  | 新建 — POSIX 安装脚本（7 路 fallback）        |
| `agent/tools/code_intel/ast_grep/scripts/install.ps1` | 新建 — Windows 安装脚本                       |
| `agent/tools/subagent/spawn/core.py`               | 注入 ast_grep（**所有 subagent**）            |
| `.gitignore`                                       | 无 ast-grep 专属条；`.codeintel/` 已覆盖 第 3 层缓存 |

> 提案清单中的 `agent/tools/subagent/spawn/system_prompt.py` 改动未执行（见「与提案的差异」）。

### Phase 2S 修改（已落地：新建 9 + 修改 4）

| 文件                                            | 修改                                                              |
| ----------------------------------------------- | ----------------------------------------------------------------- |
| `config/features/agent_side/lsp.py`             | 新建 — `LspConfig`（原计划 7 字段 + 2S 7 字段 + 资源约束 5 字段） |
| `config/features/agent_side/__init__.py`        | re-export `LSP` / `LspConfig`                                     |
| `config/features/__init__.py`                   | 顶层 re-export                                                    |
| `agent/tools/code_intel/lsp/__init__.py`        | 新建 — 模块导出                                                   |
| `agent/tools/code_intel/lsp/protocol.py`        | 新建 — URI/Position/Range + `detect_language`                     |
| `agent/tools/code_intel/lsp/resolver.py`        | 新建 — 5 层二进制发现 + 每进程缓存                                |
| `agent/tools/code_intel/lsp/installer.py`       | 新建 — 白名单自动安装                                             |
| `agent/tools/code_intel/lsp/fallback.py`        | 新建 — 可用性检查 + fallback 链                                   |
| `agent/tools/code_intel/lsp/client.py`          | 新建 — LSP JSON-RPC stdio 客户端                                  |
| `agent/tools/code_intel/lsp/manager.py`         | 新建 — 进程级管理（按需/并发上限/空闲关停/atexit）               |
| `agent/tools/code_intel/lsp/tools.py`           | 新建 — 4 个 researcher 工具                                       |
| `agent/tools/subagent/spawn/core.py`            | 仅 RESEARCHER 注入 4 个 LSP 工具                                  |
| `agent/tools/subagent/spawn/system_prompt.py`   | RESEARCHER 段追加 LSP 指导                                        |

### Phase 2X 修改（已落地）

| 文件                                                   | 修改                                                                 |
| ------------------------------------------------------ | -------------------------------------------------------------------- |
| `config/features/agent_side/lsp.py`                    | +6 语言、`language_id` 字段、`lsp_diagnostics_timeout_s`、enabled 4→10、安装提示/自动安装、repo-local 规则 |
| `agent/tools/code_intel/lsp/protocol.py`               | `language_id` / `format_diagnostic` / `format_text_edit` / `normalize_workspace_edit` |
| `agent/tools/code_intel/lsp/client.py`                 | 发协议 `languageId`、广告 rename/formatting、`cached_diagnostics`    |
| `agent/tools/code_intel/lsp/tools.py`                  | +`lsp_rename`/`lsp_diagnostics`/`lsp_format`/`lsp_status` + edit 应用器 |
| `agent/tools/code_intel/lsp/__init__.py`               | 导出新工具与协议原语                                                 |
| `agent/tools/subagent/spawn/core.py`                   | 注释同步（`build_lsp_tools` 返回集自动扩到 8）                       |
| `agent/tools/subagent/spawn/system_prompt.py`          | RESEARCHER 段追加 4 工具指导                                         |
| `tests/agent/tools/code_intel/lsp/test_protocol.py`    | 新建 unit                                                            |
| `tests/agent/tools/code_intel/lsp/test_lsp_extended.py`| 新建 integration                                                     |
| `tests/agent/tools/code_intel/lsp/{conftest,test_resolver,test_lsp_tools,test_lsp_e2e,test_lsp_smoke,test_role_isolation}.py` | 扩展/同步 |
| `docs/subagent/README{,4}` + `agent/tools/subagent/README{,4}` | researcher 工具面 4 → 8（四语一致） |

### Phase 3 修改（已落地：新建 5 + 修改 6）

| 文件                                                        | 修改                                                         |
| ----------------------------------------------------------- | ------------------------------------------------------------ |
| `config/features/agent_side/code_intel_semantic.py`         | 新建 — `CodeIntelSemanticConfig` + `CODE_INTEL_SEMANTIC`      |
| `config/features/agent_side/__init__.py`                    | re-export `CODE_INTEL_SEMANTIC` / `CodeIntelSemanticConfig`   |
| `config/features/__init__.py`                               | 顶层 re-export                                               |
| `agent/tools/code_intel/semantic/__init__.py`               | 新建 — 模块导出                                              |
| `agent/tools/code_intel/semantic/chunker.py`                | 新建 — 按符号分块                                            |
| `agent/tools/code_intel/semantic/indexer.py`                | 新建 — 增量嵌入 + `code_embeddings` 存储 + 模型/维度一致性   |
| `agent/tools/code_intel/semantic/search.py`                 | 新建 — cosine + reranker 重排 + `semantic_code_search` 工具  |
| `agent/tools/code_intel/tools.py`                           | `build_code_intel_tools` 追加 semantic（仍 RESEARCHER 专属） |
| `agent/tools/subagent/spawn/system_prompt.py`               | RESEARCHER 段追加 `semantic_code_search` 指导                |
| `docs/subagent/README{,4}` + `agent/tools/subagent/README{,4}` | researcher 工具面 +`semantic_code_search`（四语一致）        |

### Phase 4 修改（已落地：新增 2 + 修改 5 + 测试 8 + 文档 8）

| 文件                                                          | 修改                                                         |
| ------------------------------------------------------------- | ------------------------------------------------------------ |
| `agent/tools/subagent/roles/definitions/librarian/AGENTS.md`  | 新建 — librarian 角色定义（frontmatter + THE LIBRARIAN prompt） |
| `tests/agent/tools/subagent/test_librarian_role.py`           | 新建 — 解析 / prompt / 工具面 / hermetic e2e                  |
| `agent/tools/subagent/types/functional_role.py`               | +`LIBRARIAN` 成员 + `CODE_INTEL_ROLES` 单一来源                |
| `agent/tools/subagent/types/__init__.py`                      | re-export `CODE_INTEL_ROLES`                                  |
| `agent/tools/subagent/spawn/core.py`                          | 注入条件 `== RESEARCHER` → `in CODE_INTEL_ROLES`             |
| `agent/tools/subagent/spawn/system_prompt.py`                 | code-intel 指引段改用 `CODE_INTEL_ROLES`                     |
| `agent/tools/subagent/tools/sessions_spawn.py`                | `functional_role` 描述补 librarian                            |
| `roles/test_loader.py`、`types/test_functional_role.py`、`spawn/test_functional_role_integration.py`、`test_spawn_privilege_guard.py`、`tests/agent/tools/code_intel/{test_integration,lsp/test_role_isolation,ast_grep/test_ast_grep_e2e}.py` | 4→5 角色期望同步 + librarian 隔离断言 |
| `docs/subagent/README{,4}` + `agent/tools/subagent/README{,4}` + 根 `README{,4}` | librarian 角色行 / 枚举 / paragraph（四语一致） |

> 提案清单中的 `agent/tools/subagent/spawn/role_definitions/{librarian.py,__init__.py}`（已失效路径）
> 未执行；现行落点见「落点」与 Phase 4 节「与提案的差异」。

### Phase 5 修改（3 个）

| 文件                                      | 修改                  |
| ----------------------------------------- | --------------------- |
| `pyproject.toml`                          | 加 `watchfiles>=2.0`  |
| `agent/tools/code_intel/watcher.py`       | 新建                  |
| `server/__main__.py` 或 `server/service/` | 启动 watcher 后台任务 |

### 公共修改

| 文件             | 修改                                                       |
| ---------------- | ---------------------------------------------------------- |
| `config/path.py` | 加 `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"` (原计划已有) |
| `.gitignore`     | 加 `.codeintel/` (原计划已有)                              |

---

## 测试计划（补充）

| 文件                                                        | 阶段 | 标记          | 覆盖点                                                                                  |
| ----------------------------------------------------------- | ---- | ------------- | --------------------------------------------------------------------------------------- |
| `tests/agent/tools/code_intel/ast_grep/test_resolver.py`      | P1A  | `unit`   | 5 层发现（env/runtime/code-intel-bin/PATH/homebrew）、--version 探针、缓存、Windows 后缀   |
| `tests/agent/tools/code_intel/ast_grep/test_provisioner.py`   | P1A  | `unit`   | SHA-256 校验、优先提取 `ast-grep`、6 平台 asset、下载/校验/超时/解压失败                   |
| `tests/agent/tools/code_intel/ast_grep/test_install_hints.py` | P1A  | `unit`   | 平台安装提示、config 双级 re-export、TypedDict 键一致                                     |
| `tests/agent/tools/code_intel/ast_grep/test_runner.py`        | P1A  | `unit`   | sg 子进程、JSON 解析、退出码、超时、dry_run/apply rewrite、路径安全、SHERRY_SG_PATH       |
| `tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py`  | P1A  | `module` | 四个 functional_role 均获 ast-grep、main 不可见、假下载器 hermetic e2e（search+rewrite）  |
| `tests/agent/tools/code_intel/lsp/test_resolver.py`         | P2S ✅ | `unit`        | 5 层发现、marker-gating、缓存开关、Windows 后缀、安装提示、config 双级 re-export          |
| `tests/agent/tools/code_intel/lsp/test_installer.py`        | P2S ✅ | `unit`        | 自动安装命令、超时、缺工具、returncode、成功后重发现、env 清洗、auto_install=False 拒绝   |
| `tests/agent/tools/code_intel/lsp/test_fallback.py`         | P2S ✅ | `unit`        | available/not_installed/not_configured 三态、fallback 消息格式                            |
| `tests/agent/tools/code_intel/lsp/test_client.py`           | P2S ✅ | `unit`        | JSON-RPC 握手、请求/响应、超时、错误响应、didOpen/diagnostics、进程回收                   |
| `tests/agent/tools/code_intel/lsp/test_manager.py`          | P2S ✅ | `unit`        | 惰性启动、复用、并发上限 LRU 淘汰、空闲关停、shutdown_all、无孤儿                         |
| `tests/agent/tools/code_intel/lsp/test_lsp_tools.py`        | P2S ✅ | `integration` | 4 LSP 工具端到端 + 路径安全 + not_installed/error/timeout 降级路径                        |
| `tests/agent/tools/code_intel/lsp/test_role_isolation.py`   | P2S ✅ | `module`      | 角色隔离三方向（main / 非 RESEARCHER / RESEARCHER）                                       |
| `tests/agent/tools/code_intel/lsp/test_lsp_e2e.py`          | P2S ✅ | `module`      | hermetic e2e：4 工具共享惰性服务器 + 进程回收                                             |
| `tests/agent/tools/code_intel/lsp/test_lsp_smoke.py`        | P2S ✅ | `integration` | 真实 basedpyright：definition / references / diagnostics + 进程回收                       |
| `tests/agent/tools/code_intel/lsp/test_protocol.py`         | P2X ✅ | `unit`        | 10 语言 extension → language → languageId、diagnostics、WorkspaceEdit 归一化             |
| `tests/agent/tools/code_intel/lsp/test_lsp_extended.py`     | P2X ✅ | `integration` | lsp_rename / lsp_diagnostics / lsp_format / lsp_status                                  |
| `tests/agent/tools/code_intel/semantic/test_chunker.py`     | P3 ✅ | `unit`        | 分块 header/边界/截断、空文件、kind 过滤、snippet 回退                                  |
| `tests/agent/tools/code_intel/semantic/test_indexer.py`     | P3 ✅ | `unit`        | 增量二次索引、变更重嵌、删除孤儿、模型/维度重建、坏块 fail-open、max_chunks、batch_size  |
| `tests/agent/tools/code_intel/semantic/test_search.py`      | P3 ✅ | `unit`        | cosine 排序、top-K 夹取、reranker 重排/降级、空查询/空索引/维度不符/嵌入失败降级、自愈  |
| `tests/agent/tools/code_intel/semantic/test_semantic_e2e.py`| P3 ✅ | `module`      | 假嵌入 + 假 reranker 的 build→search 全链路（工具层）                                   |
| `tests/agent/tools/code_intel/semantic/test_semantic_smoke.py` | P3 ✅ | `integration` | 真实 bge-m3：build → concept search（运行时/权重缺失则 skip）                        |
| `tests/agent/tools/code_intel/test_tools.py`（+ semantic）  | P3 ✅ | `unit`        | 5 工具 exact-list、scope metadata、session_id、args schema                              |
| `tests/agent/tools/code_intel/test_integration.py`（+ semantic） | P3 ✅ | `module`  | 角色隔离三方向（main / 非 RESEARCHER / RESEARCHER 含 semantic_code_search）             |
| `tests/agent/tools/subagent/test_librarian_role.py`         | P4 ✅ | `integration` | librarian 角色定义加载、工具权限正确（有 explore/无 write）                             |
| `tests/agent/tools/code_intel/test_watcher.py`              | P5   | `integration` | 文件变更触发重索引、debounce 2s、prune_dirs 排除                                        |

### 关键测试用例

LSP 关键用例已随实现落地，规格样例已退役；覆盖点见上表与 Phase 2S 节「测试」清单
（发现层矩阵、marker-gating、缓存、安装、三态 fallback、client 握手/超时/回收、
manager 惰性/并发/空闲、4 工具降级、角色隔离、真实冒烟）。

---

## 安全清单（补充）

| 维度                                       | 措施                                                               | 阶段 |
| ------------------------------------------ | ------------------------------------------------------------------ | ---- |
| **ast-grep 二进制 provision SHA-256 校验** | 下载的 ZIP 经 SHA-256 校验后才解压，防止供应链篡改                 | P1A  |
| **ast-grep 二进制版本锁定**                | pinned 0.43.0，不从 PATH 接受任意版本，--version 探针验证          | P1A  |
| **ast-grep 子进程隔离**                    | sg CLI 通过 subprocess.run 启动，有超时（30s），不继承敏感环境变量 | P1A  |
| **ast-grep rewrite dry_run**               | 默认 dry_run=True，必须显式设置 false 才修改文件                   | P1A  |
| **LSP 子进程环境清洗**                     | LSP server 子进程环境经 `scrub_env()` 清洗，不泄漏 API keys        | P2S（已落地） |
| **LSP 自动安装用户授权**                   | `lsp_auto_install` 默认 False，需用户在配置中显式开启              | P2S（已落地） |
| **LSP 安装命令白名单**                     | 仅 `lsp_auto_install_commands` 中列出的命令可执行，不接受任意命令  | P2S（已落地） |
| **LSP 安装超时**                           | 60s 超时，防止安装命令挂起                                         | P2S（已落地） |
| **LSP 二进制路径可信**                     | resolve_local 仅在 marker 文件存在时信任 bin 目录，防止目录注入    | P2S（已落地） |
| **LSP 进程回收（J10）**                    | 按需启动 + 并发上限 + 空闲自动关停 + 显式/atexit shutdown，无孤儿  | P2S（已落地） |
| **LSP rename/format 默认预览**             | `lsp_rename` dry_run=true、`lsp_format` write=false；仅显式开启才写盘，且每条 edit 过项目根路径闸 | P2X（已落地） |
| **LSP format 不伪造成功**                  | 服务器不支持格式化（返回 null/error）时如实返回 `supported=false` + 原因，不报“已格式化” | P2X（已落地） |
| **LSP status 只读聚合**                    | `lsp_status` 不启动任何服务器，仅聚合解析三态与活跃快照            | P2X（已落地） |
| **文件监听排除敏感目录**                   | watcher 排除 prune_dirs（.git, .venv, node_modules, ...）          | P5   |
| **文件监听仅触发索引**                     | watcher 不执行代码，仅 parse + 写 SQLite                           | P5   |
| **librarian 临时仓库清理**                 | clone 到 temp 目录，subagent 结束时清理（prompt 约束）             | P4（已落地） |
| **librarian 只读约束**                     | 角色 `tools` 白名单仅 read_file/terminal/web_search/search_files，无 write/patch/python_repl | P4（已落地） |
| **librarian 无 spawn 权限**                | 白名单无 sessions_spawn/yield/send；深度角色门禁照常生效           | P4（已落地） |

---

## 关键参考文件

### Sherry 内部（复用）

| 文件                                    | 用途                                                               |
| --------------------------------------- | ------------------------------------------------------------------ |
| `agent/tools/pub_base/env_scrub.py`     | `scrub_env()` — LSP/sg 子进程环境清洗                              |
| `agent/tools/file_tools/search_scan.py` | `bounded_walk` + `ScanState` — 索引遍历复用                        |
| `agent/tools/web_search.py`             | `build_web_search_tool()` — librarian 复用                         |
| `agent/tools/terminal.py`               | terminal 工具 — librarian git/gh 操作复用                          |
| `agent/tools/file_tools/read_file.py`   | `build_read_file_tool()` — librarian 源码读取复用                  |
| `context_engine/embeddings/store.py`    | Embedding BLOB 存储模式（Phase 3 复用：`array.array("d")` 打包）    |
| `config/path.py` `CODE_INTEL_DIR`       | ast-grep 第 3 层 bin 缓存路径基础 (`CODE_INTEL_DIR/ast-grep/bin`)  |

### 外部参考

| 项目                  | 文件                                         | 参考内容                                                                      |
| --------------------- | -------------------------------------------- | ----------------------------------------------------------------------------- |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-installation.ts`    | 三层二进制发现（repo-local → PATH → Windows 后缀）                            |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-definitions.ts`     | `AUTO_INSTALLABLE_SERVERS` + `LSP_INSTALL_HINTS` + `LSP_LOCAL_INSTALL_HINTS`  |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-resolution.ts`      | `findServerForExtension()` not_installed 状态 + installHint 返回              |
| `oh-my-openagent-dev` | `lsp-core/src/tools/install-decision.ts`     | `lsp_install_decision` 工具（用户授权安装）                                   |
| `oh-my-openagent-dev` | `lsp-core/src/tools/status.ts`               | `lsp_status` 工具（服务器状态聚合）                                           |
| `oh-my-openagent-dev` | `ast-grep-mcp/src/tools/search.ts`           | sg CLI 参数构建 + 输出解析                                                    |
| `oh-my-openagent-dev` | `ast-grep-mcp/src/tools/rewrite.ts`          | dry_run rewrite 实现                                                          |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-resolver.ts`          | 5 层二进制发现（env/runtime/skill-bin/PATH/homebrew）+ --version 探针         |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-provisioner.ts`       | ast-grep 二进制 SHA-256 验证下载 + ZIP 解压                                   |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-candidates.ts`        | 5 层候选路径生成逻辑                                                          |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-install-hints.ts`     | 平台特定的安装提示（brew/npm/cargo/scoop/winget/choco）                       |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-manifest.ts`          | pinned 版本号 + 6 平台 SHA-256 校验值                                         |
| `oh-my-openagent-dev` | `utils/src/ast-grep/install-script.ts`       | install.sh / install.ps1 启动器（30s 超时, Windows pwsh→powershell fallback） |
| `oh-my-openagent-dev` | `shared-skills/skills/ast-grep/install.sh`   | 286 行 7 路 fallback 安装脚本（brew/npm/cargo/pip/nix/mise/github）           |
| `oh-my-openagent-dev` | `omo-senpi/src/components/ast-grep/index.ts` | ast-grep MCP server 注册（enabled: true, lifecycle: "lazy"）                  |
| `oh-my-openagent-dev` | `omo-senpi/src/extension/component-list.ts`  | ast-grep 无条件注册（与 LSP 并列）                                            |
| `oh-my-openagent-dev` | `agents/librarian.ts`                        | librarian system prompt + 工具权限                                            |
| `oh-my-openagent-dev` | `utils/src/codegraph/provision.ts`           | 二进制 auto-provisioning 模式（SHA-256 验证）                                 |
| `deepagents-main`     | `libs/code/deepagents_code/managed_tools.py` | managed ripgrep 自动安装 + SHA-256 验证模式                                   |
