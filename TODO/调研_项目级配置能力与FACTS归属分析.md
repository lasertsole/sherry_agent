# 调研：其他 agent 的项目级配置能力 + FACTS.md 归属分析

- 建档日期：2026-10-22
- 状态：**调研（待规划）**
- **文档范围**：两部分——(1) 五个同类 agent 项目级配置能力调研，提取对 sherry 有价值的选项；(2) FACTS.md 放项目级还是全局的归属判断。
- 关联 TODO：
  - `计划_项目级指令文件读取.md`（AGENTS.md 读取——本文档是其扩展调研）
  - `计划_统一项目隐藏目录到sherry.md`（`.sherry/` 目录——项目级配置的未来容器）

---

## 1. 五个 agent 的项目级配置能力

### 1.1 总览

| 能力 | ZCode | claudeCode | codex | hermes | openclaw |
|---|---|---|---|---|---|
| 项目指令文件 | `.zcode/config.json` | `CLAUDE.md` | `AGENTS.md`（git root→cwd 拼接） | `.hermes.md`→`AGENTS.md`→`CLAUDE.md`→`.cursorrules` | `AGENTS.md` |
| 分层配置优先级 | — | ✅ 5 层（user legacy < user < project < local） | ✅ 10 层（packaged -10 ~ legacy 50） | — | — |
| 配置诊断（哪个层赢了哪个 key） | — | ✅ `ConfigInspection` | — | — | — |
| 生命周期 hooks | — | ✅ PreToolUse/PostToolUse/PostToolUseFailure | ✅ 12 事件（PreToolUse/PermissionRequest/PostToolUse/PreCompact/PostCompact/SessionStart/SessionEnd/UserPromptSubmit/SubagentStart/SubagentStop/Stop/Interrupt） | — | ✅ SessionStart（`.claude/settings.json` 里） |
| 项目 hook 信任/安全 | — | ✅ `strip_untrusted_project_hooks` | ✅ `trusted_hash` + `allow_managed_hooks_only` | — | — |
| 权限规则（allow/deny/ask 模式） | — | ✅ `permissions.defaultMode` + `allow[]`/`deny[]`/`ask[]` | ✅ permission profiles with `extends` inheritance | — | — |
| 项目级 skills | ✅ `.zcode/skills/` | — | ✅ `.codex/skills/` | — | ✅ `.agents/skills/`（42 个） |
| 项目级 commands | ✅ `.zcode/commands/` | — | — | — | — |
| 项目级 subagent 定义 | ✅ `.zcode/agents/` | — | ✅ `.codex/skills/*/agents/openai.yaml` | — | — |
| 项目级 plugins | ✅ `.zcode/plugins/` | ✅ `.claude-plugin/plugin.json` | — | ✅ `.hermes/plugins/`（opt-in） | ✅ `extensions/`（npm package） |
| 环境 setup/resume 脚本 | — | — | ✅ `.codex/environments/environment.toml` | — | ✅ `.agents/setup` + `.agents/resume` |
| 人格文件体系 | — | — | — | `SOUL.md`（全局） | ✅ `SOUL.md`/`IDENTITY.md`/`USER.md`/`BOOTSTRAP.md` |
| USER.md 带时效指令格式 | — | — | — | — | ✅ `<!-- observed: YYYY-MM-DD | status: active -->` + superseded 标记 |
| 配置兼容策略 | — | ✅ 废弃 key 一次性警告 | — | — | ✅ "只读当前 schema，破坏性改动需 doctor 迁移" |
| 能力层级政策（footprint ladder） | — | — | — | ✅ "extend→CLI+skill→service-gated tool→plugin→MCP→core tool" | — |

### 1.2 对 sherry 有价值的选项（按优先级排序）

#### Tier 1 — 高价值，可直接采纳

**① 项目级 lifecycle hooks（codex 的 12 事件 + claudeCode 的信任机制）**

codex 定义了 12 个 hook 事件，每个事件支持 `{matcher, hooks: [{type:"command", command, commandWindows, timeout}]}`。最有价值的事件：

| 事件 | sherry 对应时机 | 用途 |
|---|---|---|
| `SessionStart` | session 首次打开 | 跑 `pnpm install` / `uv sync` 恢复环境 |
| `PreToolUse` | 工具调用前（terminal/write_file 等） | 自定义审批规则、审计日志 |
| `PostToolUse` | 工具调用后 | 格式化 lint、跑测试 |
| `UserPromptSubmit` | 用户消息发送前 | 输入预处理、敏感词过滤 |
| `PreCompact` / `PostCompact` | 压缩前/后 | 压缩前保存上下文、压缩后恢复状态 |

**安全**：claudeCode 的 `strip_untrusted_project_hooks` 和 codex 的 `trusted_hash` 都要求项目级 hook 必须经过显式信任——防止恶意项目文件注入 hook。sherry 如果做 hooks 必须带这个。

**② 项目级权限规则（claudeCode 的 allow/deny/ask 模式）**

claudeCode 的权限系统比 sherry 当前的 HITL 更细粒度：

```json
{
  "permissions": {
    "defaultMode": "plan",          // read-only / workspace-write / plan / prompt / allow
    "allow": ["Read", "Bash(git *)"],
    "deny": ["Bash(rm -rf)"],
    "ask": ["Bash(npm publish)"]
  }
}
```

模式匹配（`Bash(git *)`）比 sherry 当前的 `detect_dangerous_command` 正则更灵活。但 sherry 已有 47+ 危险模式检测 + session/permanent allowlist + HITL 审批——覆盖面已经不小，差距主要在**项目级 allow/deny 规则**（当前 allowlist 是 per-session，不是 per-project）。

**③ 配置分层 + 诊断（codex + claudeCode）**

codex 的 10 层优先级（-10 ~ 50）和 claudeCode 的 5 层（user legacy < user < project < local）让用户清楚"哪个配置赢了"。claudeCode 的 `ConfigInspection` 报告每个 key 的来源、优先级、被谁覆盖——调试配置问题时极有价值。

sherry 当前的配置是扁平的（`.env` / `.models` / `.network` / `sherry.jsonc`），没有分层概念。如果要做项目级配置（`.sherry/config.json`），分层 + 诊断是配套能力。

**④ 项目级 skills/commands（ZCode + openclaw）**

ZCode 的 `.zcode/skills/` 和 `.zcode/commands/` 让项目自带技能和斜杠命令——clone 项目 = 得到该项目的专用技能。openclaw 有 42 个 workspace skills。

sherry 的 skills 当前全在 `SHERRY_HOME/skills/`（全局）。项目级 skills 的价值：一个 Python 项目可以自带 `run-migrations` skill，一个前端项目可以自带 `build-and-deploy` skill——不需要全局安装。

#### Tier 2 — 有价值，中等工程量

**⑤ 环境 setup/resume 脚本（openclaw `.agents/setup` + `.agents/resume`）**

openclaw 在 SessionStart hook 里跑 `pnpm install` 恢复环境；`.agents/resume` 验证 Node/pnpm 版本。codex 的 `.codex/environments/environment.toml` 声明命名动作（`Run` / `Build` / `Test`）+ 命令 + 图标。

对 sherry 的价值：用户打开项目时自动 `uv sync` / `pnpm install`，不用手动跑。但这跟 ① 的 hooks 系统是配套的——没有 hooks 就没有 SessionStart 事件触发 setup。

**⑥ 项目级 subagent 定义（ZCode + codex `agents/openai.yaml`）**

每个 skill 可以声明一个子代理接口（`interface.display_name` / `short_description` / `default_prompt`）。sherry 的 subagent 角色当前在 `SHERRY_HOME` 全局定义——项目级角色定义让"这个项目的研究员用这个 prompt"可配置。

**⑦ USER.md 带时效指令格式（openclaw）**

openclaw 的 USER.md 每条指令带 `<!-- observed: YYYY-MM-DD | status: active -->` 元数据，偏好变化时旧指令标记 `superseded`（不删不矛盾）。比 sherry 当前的 USER.md（纯文本追加）更结构化。

#### Tier 3 — 好实践，可做文档/政策

**⑧ 能力层级政策（hermes 的 footprint ladder）**

hermes 明确规定新能力的归属层次：extend 现有代码 → CLI+skill → service-gated tool → plugin → MCP server → core tool（最后手段）。防止每个新需求都往 core 里塞。

**⑨ 配置兼容策略（openclaw + claudeCode）**

openclaw："只读当前 schema，破坏性改动需 doctor 迁移"。claudeCode：废弃 key 一次性警告 + 自动迁移。

**⑩ skill provenance metadata（ZCode `metadata.json`）**

`version` / `organization` / `date` / `abstract` / `references[]`——跟踪 skill 来源。

### 1.3 sherry 当前缺什么

对照上表，sherry 在项目级配置上缺的主要是：

| 缺失能力 | 对标 | 优先级 |
|---|---|---|
| 读 AGENTS.md（项目指令） | codex/hermes/openclaw | **已有 TODO**（`计划_项目级指令文件读取.md`） |
| 项目级 hooks | codex(12 事件) + claudeCode(信任) | 高 |
| 项目级 allow/deny 权限规则 | claudeCode(模式匹配) | 中（当前 HITL 覆盖面够，缺的是 per-project 规则） |
| 项目级 skills | ZCode(.zcode/skills/) + openclaw(.agents/skills/) | 中 |
| 配置分层 + 诊断 | codex(10 层) + claudeCode(ConfigInspection) | 中（做项目级配置时配套） |
| 项目级 commands | ZCode(.zcode/commands/) | 低 |
| 环境 setup/resume | openclaw + codex | 低（依赖 hooks） |

---

## 2. FACTS.md 归属分析

### 2.1 当前状态

| 维度 | 值 |
|---|---|
| 存储位置 | `WORKSPACE_DIR/memory/FACTS.md`（全局，所有项目共享一份） |
| 内容语义 | "broad, module-independent pitfalls and conventions that recur across plans/tasks" |
| 容量 | 1,375 字符，oldest-first 驱逐 |
| 写入方 | memory 工具（`target="facts"`）+ plan-extraction nudge + memory-review nudge |
| 读取方 | 系统提示冻结快照 + nudge 的 `format_live_content("facts")` + UI `GET /memory` |
| 注入位置 | 系统提示（`build_system_prompt` → `format_memory_for_system_prompt("facts")`） |
| per-project？ | **否**——`MEMORY_DIR` 固定在 `WORKSPACE_DIR/memory`，切项目不影响 |

### 2.2 内容是项目级的

FACTS.md 的实际内容示例（来自 e2e 测试）：

> "full-suite `python_repl` runs hit the 30s REPL timeout and truncate — split the suite into batches."

这是**特定项目**的测试套件陷阱。对另一个项目（比如一个 Nuxt 前端项目）完全无意义，甚至误导。

nudge 的路由规则（`nudges.py:282-287`）：
- **IN FACTS.md**：broad, cross-plan pitfalls/conventions——环境/工具链约束、跨模块陷阱
- **NOT in FACTS.md**：用户偏好（→ USER.md）、模块级教训（→ `<module>-notes` skill）、通用项目笔记（→ MEMORY.md）

"cross-plan" 的定义是"跨计划"（同一个项目的不同计划），不是"跨项目"。一个项目 A 的 pytest 超时陷阱对项目 B 的 Jest 测试无意义。

### 2.3 判断：FACTS.md 随项目——搬到 `.sherry/memory/FACTS.md`

**结论：FACTS.md 搬到项目目录 `.sherry/memory/FACTS.md`，MEMORY.md 和 USER.md 留全局。**

理由：

| 因素 | 分析 |
|---|---|
| 内容是项目级的 | ✅ "这个项目的 pytest 超时"对另一个项目无意义——跟着项目走才对 |
| 搬动不需要 per-project 状态存储 | FACTS.md 是**单个文件**，不像 sessions/tasks 需要 workspace hash 分组的架构——只需 `MemoryStore._path_for("facts")` 按 `session_workspace_root` 解析路径，fallback 到全局 |
| 容量小 | 1,375 字符——per-project 文件轻量，不需要 per-project 的 MesMemory/checkpoint 架构 |
| nudge 自动维护 | nudge 在 session 上下文里运行——通过 `session_id` 解析到当前项目的 FACTS.md 路径即可 |
| 跟着项目走 | clone 项目 = 得到这个项目的坑；团队共享教训 |

**MEMORY.md 和 USER.md 不搬**（详见 §2.5）。

详细的搬迁计划见 `计划_统一项目隐藏目录到sherry.md` §2.6。

### 2.4 为什么不需要等 per-project 状态存储

我之前的分析说"等 per-project 状态存储落地后再考虑"——这是错的。per-project 状态存储（workspace hash 分组 sessions/tasks/memory）是一个 ~500+ 行的架构变更，影响 `SESSIONS_DIR` 布局 + API + 前端。但 FACTS.md 搬到 `.sherry/memory/` 不需要这个架构——它只是 `MemoryStore` 的 `_path_for` 加一个 `session_workspace_root` 解析（~40 行），加上 per-session 快照加载（~30 行）。这两个是完全不同量级的改动。

per-project 状态存储是关于**全局存储按项目分组**（sessions/tasks/checkpoints 按 workspace hash 隔离）；FACTS.md 搬到项目目录是关于**一个文件从全局挪到项目目录**。前者是架构层，后者是路径层。

### 2.5 为什么 MEMORY.md / USER.md 不搬（留全局）

三个记忆文件的语义本质不同：

| 文件 | 语义 | 随项目？ | 理由 |
|---|---|---|---|
| **FACTS.md** | 项目级陷阱（"这个项目的 pytest 超时"、"迁移脚本要先跑 alembic upgrade"） | ✅ 随项目 | 内容绑定特定项目的技术栈/工具链/代码库；对另一个项目无意义甚至误导 |
| **MEMORY.md** | agent 的通用工作笔记（"uv 需要 --no-sync"、"ssh dev server 用 2222 端口"） | ❌ 全局 | agent 跨项目的通用知识——工具链经验、环境配置、通用教训，不绑定特定项目 |
| **USER.md** | 用户是谁（"Alice，高级后端，偏好简洁回答"） | ❌ 全局 | 用户身份和偏好跨项目不变——换项目不换人 |

**关键区分**：FACTS.md 的定义是"broad, module-independent pitfalls that recur across **plans/tasks**"——"跨计划"指的是同一个项目的不同计划，不是"跨项目"。nudge 在特定 session 上下文里提取教训——提取的是当前项目的工作经验。内容本质是项目级的，应该跟着项目走。

MEMORY.md 和 USER.md 没有这个项目绑定——它们是 agent 和用户的关系档案，跨项目通用。搬它们到项目目录反而有害：换个项目就丢失用户偏好和 agent 的通用经验。

| agent | 记忆存储 | per-project？ | 机制 |
|---|---|---|---|
| ZCode | `~/.zcode/cli/memories/projects/{workspaceId}/memory/MEMORY.md` | ✅ per-project | `memoryRoot` 按 workspace 路径哈希 |
| codex | `~/.codex/`（全局） | ❌ 全局 | — |
| hermes | `~/.hermes/memories/` + `SOUL.md` | ❌ 全局 | — |
| claudeCode | `~/.claw-analog/` | ❌ 全局 | — |
| openclaw | `~/.openclaw/workspace/` | ❌ 全局（workspace 级） | — |
| **sherry** | `WORKSPACE_DIR/memory/`（MEMORY.md + USER.md 全局）+ `.sherry/memory/FACTS.md`（per-project） | **部分** | FACTS.md 随项目（`MemoryStore._path_for` 按 `session_workspace_root` 解析）；MEMORY.md/USER.md 全局 |

ZCode 是唯一做全量 per-project 记忆的——因为它有 `memoryRoot` 按 workspace 哈希解析的基础设施。其他四个原来都是全局的。**sherry 是首个做混合策略的**：FACTS.md（项目级陷阱）随项目走，MEMORY.md（通用知识）和 USER.md（用户身份）留全局——因为三者的语义绑定不同。

---

## 3. 建议的后续 TODO 优先级

| TODO | 优先级 | 依赖 | 工程量 |
|---|---|---|---|
| `计划_项目级指令文件读取.md`（读 AGENTS.md） | **已规划** | 无 | ~65 行 |
| `计划_统一项目隐藏目录到sherry.md`（`.sherry/` 目录 + FACTS.md 搬迁） | **已规划** | 无 | ~141 行 |
| 项目级 lifecycle hooks | 高 | `.sherry/` 目录（hook 配置存这里） | ~300-500 行 |
| per-project 状态存储（workspace hash） | 高 | 无（独立架构变更） | ~500+ 行 |
| 项目级 skills（`.sherry/skills/`） | 中 | `.sherry/` 目录 + skills loader 改造 | ~200 行 |
| 项目级 allow/deny 权限规则 | 中 | HITL 改造（接 `计划_HITL终端命令session级缓存.md`） | ~150 行 |
| 配置分层 + 诊断 | 中 | 项目级配置文件（`.sherry/config.json`） | ~200 行 |

**关键路径**：AGENTS.md 读取 → `.sherry/` 目录 + FACTS.md 搬迁 → 项目级 hooks → per-project 状态存储

前两步已有 TODO，工程量小（~206 行合计）。第三步（hooks）是第一个需要后端新能力的大 feature。第四步（per-project 存储）是架构基础，解锁 per-project sessions/tasks/会话列表（但不影响 FACTS.md——FACTS.md 已在第二步搬到项目目录）。

**FACTS.md 不依赖 per-project 状态存储**：FACTS.md 是单个文件搬到 `.sherry/memory/`，只需 `MemoryStore._path_for` 按 `session_workspace_root` 解析路径（~40 行）+ per-session 快照（~30 行），不需要 workspace hash 分组的全局架构变更。
