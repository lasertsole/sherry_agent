# 代码检索框架 — 补充计划

> 基于 oh-my-openagent-dev 五层 LSP 安装基础设施和 ast-grep MCP 的深度调研，
> 补充 [`CODE_INTEL_SUBAGENT_PLAN.md`](CODE_INTEL_SUBAGENT_PLAN.md) 的五个缺口：
> LSP 二进制发现与自动安装、ast-grep 结构化搜索、LSP 语言/工具扩展、
> 文件事件自动同步、外部代码检索 subagent。
>
> **本文件是对原计划的修订与补充，不替代原计划。**
> 实施时原计划的 Phase 1 不变；Phase 2 由本文件 Phase 2S 替代；
> 新增 Phase 1A（必选，与 Phase 1 同级）、Phase 4 为独立阶段。
>
> **进度：Phase 1A / 2S / 2X 已落地（Phase 2S 同时承载原计划 Phase 2）；
> Phase 3 / 4 / 5 待派工。**
>
> **ast-grep 是核心必选组件**（对标 oh-my-openagent，在该项目中 ast-grep
> 与 LSP daemon 并列为无条件注册的核心组件，非 opt-in）。

---

## 目录

1. [缺口总览](#缺口总览)
2. [Phase 1A — ast-grep 结构化搜索（已落地）](#phase-1a--ast-grep-结构化搜索已落地)
3. [Phase 2S — LSP 二进制发现与自动安装（已落地，替代原 Phase 2）](#phase-2s--lsp-二进制发现与自动安装已落地替代原-phase-2)
4. [Phase 2X — LSP 语言与工具扩展（已落地）](#phase-2x--lsp-语言与工具扩展已落地)
5. [Phase 4 — 外部代码检索 subagent](#phase-4--外部代码检索-subagent)
6. [Phase 5 — 文件事件自动同步（可选）](#phase-5--文件事件自动同步可选)
7. [修订后的总工期](#修订后的总工期)
8. [修改文件清单（新增）](#修改文件清单新增)
9. [测试计划（补充）](#测试计划补充)
10. [安全清单（补充）](#安全清单补充)

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
| 7   | 外部代码检索（GitHub/npm/docs）                              | `librarian` subagent                                                    | Phase 4          |
| 8   | 文件事件自动同步                                             | CodeGraph 2s debounce 文件监听                                          | Phase 5 (可选)   |

---

## Phase 1A — ast-grep 结构化搜索（已落地）

> **状态：已落地。** 本节只保留落点、与提案的差异、实测摘要与平台覆盖；
> 规格细节已随实现移除。

### 落点

- `config/features/agent_side/ast_grep.py`：`AstGrepConfig` TypedDict + `AST_GREP` 实例，经
  `agent_side/__init__.py` 与 `config/features/__init__.py` **双级 re-export**。
- `agent/tools/code_intel/ast_grep/`：`resolver.py`（5 层发现：env → runtime → skill-bin →
  PATH → Homebrew，每层跑 `--version` 探针）、`provisioner.py`（SHA-256 校验的 GitHub
  release 下载 + stdlib `zipfile` 解压）、`install_hints.py`、`runner.py`
  （`ast_grep_search` / `ast_grep_rewrite`）、`__init__.py`。
- `skills/ast-grep/install.sh` + `install.ps1`：7 路包管理器 fallback
  （brew → npm → cargo → pip → nix → mise → GitHub ZIP）。
- `agent/tools/subagent/spawn/core.py::_build_child_agent()`：**所有 functional_role 的
  subagent 均注入** ast-grep；`ast_grep_*` 从不进入 `_MAIN_TOOLS_BUILDERS`，main agent 不可见。
- `.gitignore` 加 `skills/ast-grep/bin/`；运行时二进制写入
  `~/.sherry/runtime/ast-grep/<slug>/sg`，**不入库**。

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
  researcher 含 LSP + code_intel + ast-grep。
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

## Phase 4 — 外部代码检索 subagent

> **前置条件：subagent 功能角色分工（已落地）+ 本计划 Phase 1A 完成。**
> **定位：** 对标 oh-my-openagent 的 `librarian` subagent。
> 专门检索**外部代码库**（GitHub 仓库、npm 包、官方文档），
> 与 Phase 1-2 的**内部**代码检索互补。

### 架构

```
┌─ Librarian Subagent（RESEARCHER 角色，只读）──────────────────────┐
│                                                                    │
│  输入：用户问"某库怎么用？" / "某库怎么实现的？"                     │
│                                                                    │
│  工具链（复用现有 + 新增）：                                         │
│  ├─ web_search          已有 — 搜索官方文档 URL                      │
│  ├─ web_fetch           已有 — 抓取文档页面                           │
│  ├─ terminal (git/gh)   已有 — clone repo, git blame, gh search      │
│  ├─ search_files        已有 — 搜索 clone 下来的仓库                  │
│  ├─ read_file           已有 — 读取 clone 的源码                     │
│  └─ explore (Phase 1)   已有 — tree-sitter 索引 clone 的仓库         │
│                                                                    │
│  输出：                                                              │
│  ├─ 概念问题 → 官方文档摘要 + 链接                                   │
│  ├─ 实现问题 → 源码引用 + GitHub permalink                          │
│  └─ 上下文问题 → 相关 issues/PRs                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 设计决策

- **不新增工具** — librarian 复用现有的 `web_search`、`terminal`（git/gh）、`search_files`、`read_file`、`explore`
- **新增的是 subagent 角色定义** — 在 `FunctionalRole` 中定义 `RESEARCHER` 角色时，librarian 是一个预设的 RESEARCHER 实例
- **新增 `librarian` system prompt** — 类似 oh-my-openagent 的 librarian prompt，包含文档发现 → 仓库 clone → 源码搜索的工作流

### 新增文件（1 个）

#### `agent/tools/subagent/spawn/role_definitions/librarian.py`

```python
"""Librarian subagent role definition — external codebase retrieval.

Registered as a RESEARCHER-role subagent preset. The spawn system resolves
this definition when `agent_id="librarian"` is passed to sessions_spawn.
"""

from __future__ import annotations

LIBRARIAN_SYSTEM_PROMPT = """\
# THE LIBRARIAN

You are a specialized open-source codebase understanding agent.

## Your Mission

Answer questions about external libraries by finding EVIDENCE with
GitHub permalinks or official documentation links.

## Workflow

### TYPE A: Conceptual ("How do I use X?")
  1. web_search("library official documentation")
  2. web_fetch(specific doc page)
  3. Summarize with version-aware links

### TYPE B: Implementation ("How does X implement Y?")
  1. terminal: git clone --depth 1 to temp dir
  2. explore or search_files in cloned repo
  3. read_file for specific implementation
  4. Construct GitHub permalink: https://github.com/owner/repo/blob/<sha>/path#L10-L20

### TYPE C: Context ("Why was X changed?")
  1. terminal: gh search issues/prs
  2. terminal: git log --oneline -- path
  3. terminal: git blame -L start,end path

## Rules
- ALWAYS cite with permalinks (include commit SHA)
- Use --depth 1 for clones unless history is needed
- Clean up temp clones when done
- Read-only: never modify files in the target repo
"""

LIBRARIAN_TOOL_ALLOW = [
    "web_search", "web_fetch", "terminal", "read_file",
    "search_files", "explore", "callers", "callees", "impact",
    "ast_grep_search",
]

LIBRARIAN_TOOL_DENY = [
    "write", "edit", "patch_file", "task", "call_omo_agent",
    "sessions_spawn", "sessions_yield", "sessions_send",
]
```

### 注入点

在角色定义加载器中注册：

```python
# agent/tools/subagent/spawn/role_definitions/__init__.py
from .librarian import LIBRARIAN_SYSTEM_PROMPT, LIBRARIAN_TOOL_ALLOW, LIBRARIAN_TOOL_DENY

ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    # ... existing roles ...
    "librarian": RoleDefinition(
        system_prompt=LIBRARIAN_SYSTEM_PROMPT,
        tool_allow=LIBRARIAN_TOOL_ALLOW,
        tool_deny=LIBRARIAN_TOOL_DENY,
        functional_role=FunctionalRole.RESEARCHER,
    ),
}
```

### 工期

| 步骤     | 内容                                           | 预估      |
| -------- | ---------------------------------------------- | --------- |
| 1        | `role_definitions/librarian.py`                | 1h        |
| 2        | 注册到角色定义加载器                           | 0.3h      |
| 3        | 测试（spawn librarian subagent, 验证工具权限） | 1h        |
| **小计** |                                                | **~2.5h** |

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
| Phase 3  | Embedding 语义搜索 (原计划)                      | ~8h        | Phase 1         |
| Phase 4  | 外部代码检索 subagent (新增)                     | ~2.5h      | Phase 1A + 前置 |
| Phase 5  | 文件事件自动同步 (可选, 新增)                    | ~4h        | Phase 1         |
| **合计** |                                                  | **~65.5h** |                 |

> 原计划 ~34h → 修订后 ~65.5h（规划期估算）。增量 ~31.5h 主要来自 ast-grep 二进制 provision
> 基础设施（Phase 1A, +13h）和 LSP 基础设施（Phase 2S, +5h vs 原 Phase 2 的 ~11h → ~16h）。
>
> Phase 1A / 2S / 2X **已落地**；剩余 Phase 3 / 4 / 5 待派工。

### 推荐实施顺序

```
前置 (subagent 功能角色分工)
  ├→ Phase 1 (tree-sitter, 15h)
  └→ Phase 1A (ast-grep + provision) ← 已落地，与 Phase 1 并行
       ├→ Phase 2S (LSP 基础设施, 16h) ← ✅ 已落地（需 Phase 1 + 1A）
        │    └→ Phase 2X (LSP 扩展, 7h) ← ✅ 已落地
       ├→ Phase 3 (Embedding, 8h) ← 可与 Phase 2S 并行
       └→ Phase 4 (librarian, 2.5h) ← 可与 Phase 2S 并行
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
| `skills/ast-grep/install.sh`                       | 新建 — POSIX 安装脚本（7 路 fallback）        |
| `skills/ast-grep/install.ps1`                      | 新建 — Windows 安装脚本                       |
| `agent/tools/subagent/spawn/core.py`               | 注入 ast_grep（**所有 subagent**）            |
| `.gitignore`                                       | 加 `skills/ast-grep/bin/`                     |

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

### Phase 4 修改（2 个）

| 文件                                                       | 修改           |
| ---------------------------------------------------------- | -------------- |
| `agent/tools/subagent/spawn/role_definitions/librarian.py` | 新建           |
| `agent/tools/subagent/spawn/role_definitions/__init__.py`  | 注册 librarian |

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
| `tests/agent/tools/code_intel/ast_grep/test_resolver.py`      | P1A  | `unit`   | 5 层发现（env/runtime/skill-bin/PATH/homebrew）、--version 探针、缓存、Windows 后缀       |
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
| `tests/agent/tools/subagent/test_librarian_role.py`         | P4   | `integration` | librarian 角色定义加载、工具权限正确（有 explore/无 write）                             |
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
| **librarian 临时仓库清理**                 | clone 到 temp 目录，subagent 结束时清理                            | P4   |
| **librarian 只读约束**                     | tool_deny 包含 write/edit/patch_file，无法修改文件                 | P4   |
| **librarian 无 spawn 权限**                | tool_deny 包含 sessions_spawn/yield/send，无法创建子 subagent      | P4   |

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
| `context_engine/embeddings/store.py`    | Embedding BLOB 存储（Phase 3 复用，原计划已有）                    |
| `config/path.py` `ROOT_DIR`             | ast-grep skill bin cache 路径基础 (`ROOT_DIR/skills/ast-grep/bin`) |

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
