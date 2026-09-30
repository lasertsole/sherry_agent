# 对标分析: Zcode 与 Sherry 差异

> **状态**: 待评估（本文为对标分析产出，非已批准的实施计划）
> **创建日期**: 2026-09-30
> **对标对象**: Zcode v3.14.4（Electron 桌面端 + CLI v0.16.9），本机 `/usr/bin/zcode`、`~/.zcode/`；Sherry（EMA AI Agent），`/home/honor/Desktop/project/sherry_agent`
> **目标**: 系统性记录双方在架构、机制、能力面上的差异，作为 Sherry 演进的决策依据
> **配套文档**: `对标计划_Zcode可借鉴能力.md`（只列 Zcode 优点与改造建议）。本文是完整差异清单，两文结论若有冲突以本文为准
> **取证方式**: Zcode 侧 = `~/.zcode/` 落盘物（3 个 rollout JSONL、2 个 SQLite、`v2/` 配置、插件树、exec 日志）+ 完整 TypeScript 源码树 `/home/honor/Desktop/project/ZCode`（非 minified，可精确到行号）；Sherry 侧 = 仓库实测（`config/features` 实际值 + 源码 + 实际调用返回值）
> **安全声明**: 本文含 Zcode 的安全缺陷（这是差异的一部分）。取证过程未输出任何私钥、API key、token 或 rollout 中的用户对话内容；涉及凭据处一律只述机制不述值

---

## 证据分级

全文每条结论标注可信度等级，**阅读时必须随文参考**：

| 等级 | 含义 | 可信度 |
| --- | --- | --- |
| **[落盘实测]** | 来自本机 `~/.zcode/` 真实落盘数据（SQLite 行数/聚合、JSONL 内容、配置文件） | 高 |
| **[源码确认]** | 在源码树中定位到确切 `文件:行号`，逻辑可读 | 高 |
| **[行为推断]** | 由 schema、命名、调用点推断，无直接运行验证 | 中 |
| **[未验证]** | 尝试取证但受限于未 minify 程度、需 root、或源码中确实不存在 | 低 |

---

## 〇、基线对比（修正后）

### 〇.1 必须先读的两条修正

本文修正了首轮对标的两处错误结论。这两条直接影响改造取舍，务必先确认：

#### 修正 1：Zcode 源码是可读的完整 TypeScript，不是闭源打包

首轮结论写的是「Zcode 闭源分发，无源码可核验内部实现」，并把 `/home/honor/Desktop/project/ZCode` 标为「已克隆 harness 源码，可能与发布构建存在偏差」。

**实际情况**：`/home/honor/Desktop/project/ZCode` 是一个完整的 TypeScript monorepo，**非 minified**，包含 `apps/zcode-cli/packages/{core,contracts,adapters,bootstrap,dynamic-workflow,dynamic-workflow-runtime}`、`packages/{shared,services,ui}` 等。核心逻辑可精确定位到行号，例如：

- `apps/zcode-cli/packages/core/src/context/builder.ts:82-223` — `ContextBuilder.build()`
- `apps/zcode-cli/packages/core/src/tool/edit-matchers.ts:41-69` — Edit 的 8 级匹配级联
- `apps/zcode-cli/packages/dynamic-workflow-runtime/src/child-source.ts:260-314` — workflow 的 `vm` 沙箱构造
- `apps/zcode-cli/packages/contracts/src/hooks/index.ts:7-15` — 7 个 hook 事件枚举

**影响**：首轮所有标 ⚠︎ 的结论可信度应上调一档；首轮「无法验证」的项目本轮基本全部落地。本文因此几乎不需要 ⚠︎ 标注。

#### 修正 2：Sherry 的 taskflow 可以在进程重启后恢复

首轮写的是「进程重启后无法从中间步骤恢复，只能整个 task 重跑」，并据此把「workflow resume 语义」列为 P1 最高价值项。

**实际情况** [源码确认]：taskflow 状态是 SQLite WAL 持久化的，不是内存态。

- `agent/tools/taskflow/registry/store_sqlite.py:1-12` — 「SQLite persistence layer for TaskFlow state (task_flows table)」
- 继承 `agent/tools/pub_base/sqlite_store.py::BaseSQLiteRepository`（与 todolist、subagent registry 共用骨架），每条连接都做 WAL 检查/切换 + 锁重试，避免 `database is locked`
- `get_active_flows_sync(session_id)` 从 SQL 侧直接取非终态 flow（`agent/prompt_data_provider.py:37-41`），进程重启后照样能读到
- `StepStatus` 7 态中 `dispatched` 是可重入的中间态（`agent/tools/taskflow/config.py:35-41`），不是终态

**影响**：「进程重启后恢复」不是差距，**撤回该 P1 项的原始理由**。真正的差距收窄为下面两条（见 §12.6）：Zcode 的 `paused` 是**显式用户意图**（主动挂起后被另一个 run 取代），Sherry 的 `waiting` 是**阻塞等待**（`set_waiting` / `wait_all` 语义）；以及 Zcode 的 checkpoint 是 **journal 重放**（已完成节点不重跑，见 §12.6），Sherry 是 **DAG 依赖重算**。

### 〇.2 量化基线

| 指标 | Zcode | Sherry | 差异定性 |
| --- | --- | --- | --- |
| 模型上下文窗口 | 1,000,000 [落盘实测] | 131,072 [实测] | Zcode 大 7.6× |
| 主 agent 工具数 | 34 [落盘实测] | 36（`build_main_tools()` 实测） | Sherry 略多 |
| 工具并发上限 | 10（`toolConcurrency.maxConcurrency`）[源码确认] | 25 槽位（main 12 / subagent 8 / nudge 4 / nested 1）[实测] | **模型不同**：Zcode 批内并发 vs Sherry 跨类别 lane |
| 迭代预算 | `error_max_turns` 终态 + 10 并发批 [源码确认] | main 90 / worker 60 / default 50 [实测] | 都有 |
| Token 硬预算 | `session_target.token_budget` [落盘实测] | **无** [实测] | **Zcode 独有** |
| Hook 扩展点 | 7 事件用户脚本 [源码确认] | **无** [实测] | **Zcode 独有** |
| 文件状态回滚 | 507 checkpoint [落盘实测] | **无** [实测] | **Zcode 独有** |
| 对话分支/回退 | append-only + branchGeneration [源码确认] | **无** [实测] | **Zcode 独有** |
| 代码智能 | 无内建索引 [源码确认] | AST + LSP + 语义 16 工具 [实测] | **Sherry 独有** |
| 多模态 | 图/视频/PDF [源码确认] | ITTT/VTTT/STT/TTI + 20MB 上限 [实测] | Sherry 更厚 |
| 语音 | 无 [未验证] | FunASR + 语音 daemon [实测] | **Sherry 独有** |
| Skill 生态 | SKILL.md + 渐进披露 [源码确认] | SKILL.md + SkillSpector 扫描 + curator [实测] | Sherry 更厚 |
| Workflow 编排 | 两代，9 工具，TS 脚本 [源码确认] | 14 工具，声明式 DAG [实测] | Zcode 更表达力 |
| 强制 token 预算（workflow） | **无**（`spent_tokens` 仅观察面）[源码确认] | 无 | 双方都无 |

---

## 一、Prompt 分层与注入

### 1.1 Zcode：三段 system message + 双注入目标

[源码确认] `apps/zcode-cli/packages/core/src/context/builder.ts::ContextBuilder.build()`

Zcode 把 prompt 拆成**三个独立的 system message**（为了 provider 的 prompt cache 分段），再加两条 meta_user 消息：

| 段 | 内容 | 缓存提示 | 实测字符数 | 估算 token |
| --- | --- | --- | --- | --- |
| `cli_prefix` | 极短 CLI 身份前缀 | `cacheControl: ephemeral` | 42 | ~14 |
| stable body | identity（含 `SECURITY_NOTICE` + `# Harness`）+ custom + workflow_actor | stable | 2,313 | ~771 |
| dynamic system | communicating + context_management + env_info + memory + system_context(git) | dynamic | 8,642 | ~2,881 |
| meta_user `context_prefix` | AGENTS.md + 记忆索引 + 日期 | — | 6,460 | ~2,153 |
| meta_user `skills_listing` | 全部 skill 的 name+description+路径 | — | **26,597** | ~8,866 |
| **合计** | | | **~43,854** | **~14,700** |

（token 估算用 `packages/shared/src/usage-stats.ts:9` 的除数 3，中文 ×2；tokenizer 名 `zcode.estimateTokens.v1`，`confidence: low`，`tokenMethod: estimated`）

**编排维度** [源码确认]：每个 section 有两个正交属性
- `injectionTarget`: `"system"` | `"meta_user"`
- `cacheHint`: `"stable"` | `"dynamic"`

`orderSectionsForInjection`（`builder.ts:310`）固定顺序：system-stable → system-dynamic → meta_user-stable → meta_user-dynamic。

**14 个 section** [源码确认]：`cli_prefix`、`identity`、`desktop_context`、`dynamic_behavior`、`session_guidance`、`memory`、`env_info`、`output_style`、`context_management`、`system_context`、`skills`、`request_user_context`、`current_date`、`custom`。`ContextSource` 枚举在 `context/types.ts:31-50`。

### 1.2 Sherry：7 个 block 组装 + 跨层 provider 协议

[实测] `workspace/prompt_builder.py`（324 行），组装函数：

```
_resolve_provider / _read_todos_sync / _build_todo_block / _build_boulder_block
_build_taskflow_block / _build_continuity_block / _build_knowledge_block
_read_static_files / build_system_prompt
```

**关键差异**：Sherry 的 prompt block **全部是动态注入**，且数据来自 agent/context_engine 两层。为此专门建了依赖倒置协议：

- `runtime/data_provider.py` — `PromptDataProvider` Protocol（`get_todos` / `get_active_flows` / `format_memory_for_system_prompt` / `build_continuity_prompt` / `build_system_prompt` 等 10 个方法）+ `SkillWriteProvider`（写侧 4 个方法）
- `agent/prompt_data_provider.py::AgentPromptDataProvider` — 逐方法 call-time import 的转发实现，`agent.core.init()` 时注册

**收益**：Zcode 的 persona 是一段静态文本（`AGENTS.md` 拼进 meta_user）；Sherry 的 persona（`workspace/{SOUL,AGENTS,USER,IDENTITY}.md`）之上叠了 todo 块、boulder 块、taskflow 进度块、跨会话连续性块、计划知识块——**这些是 Sherry 独有的**（见 §15）。

**代价**：Sherry 缺 Zcode 的 `stable`/`dynamic` 缓存分段，也无 `cacheHint` 概念。大 prompt 场景下 provider 侧 cache 命中率会低于 Zcode。**这是 Zcode 明确更优的一项。**

### 1.3 AGENTS.md 发现机制

[源码确认] `adapters/src/context/index.ts::DEFAULT_PRIORITY_FILES = ["AGENTS.md"]`

Zcode 只认 `AGENTS.md`（**不认 `CLAUDE.md`**），支持 `.zcode/AGENTS.md`、`.agents/AGENTS.md`、用户级 `~/.zcode/AGENTS.md`。

[实测] Sherry 同样只用 `AGENTS.md`（`workspace/AGENTS.md` + `workspace/template/{en,zh,ja,ko}/AGENTS.md` 四语言模板）。**此项无差异。**

### 1.4 system-reminder：Zcode 有 27 个注入源，Sherry 无对应机制

[源码确认] `core/src/system-reminder/source.ts`

Zcode 有一套结构化的 `<system-reminder>` 注入目录，**27 个 source** 分 3 组：

| 组 | 数量 | 成员 |
| --- | --- | --- |
| PREFIX | 2 | `context_prefix`、`skills_listing` |
| PERSISTED | 15 | `todo_reminder`、`task_status`、`tool_result_warning`、`resume_referenced_session_context`、`plan_file_reference`、`resume_goal_state`、`goal_state_change`、`plugin_reference`、`target_continuation`、`goal_completion_verification`、`rewind_notice`、`conversation_fork`、`selection_side_chat`、`queued_system_notification`、`shell_environment_change` |
| PER_REQUEST | 10 | `incoming_message`、`hook_context`、`runtime_mode`、`plan_mode_exit`、`output_style`、`date_change`、`referenced_session_context`、`model_anomaly`、`prompt_attachment`、`diagnostics` |

机制细节：
- 包装函数 `wrapSystemReminder`（`source.ts:203-212`）：`<system-reminder>\n{body}\n</system-reminder>`；**嵌套标签被拒绝/转义**（`:208`, `:233`）
- 每个 descriptor 带 3 个属性：`channel`、`lifecycle`、`isMeta`
- 6 个 channel：`request_prefix`、`current_turn`、`tool_result`、`history_continuity`、`mid_turn_event`、`real_user`
- `real_user` 的 `isMeta=false`（即 `target_continuation` 会作为真实用户输入呈现）

**触发阈值** [源码确认]：
- `todo_reminder`：距上次 `TodoWrite` 满 10 个 assistant turn **且** 距上次提醒满 10 turn（`TODO_REMINDER_CONFIG`）
- `runtime_mode`（plan mode 提示）：plan mode 激活时，每 5 个 human turn 一次，每第 5 次给全量版（`RUNTIME_MODE_REMINDER_CONFIG`）

**落盘实测**：`model-io-sess_bfd00704-*.jsonl` 中解码后只出现 3 种 reminder 实际内容——`context_prefix`、`skills_listing`、`todo_reminder`。其余 24 种在本机会话中未被触发。

**Sherry 对应**：`agent/middlewares/task_intent/`（E7）、`nudge/`、`todo_continuation_enforcer`（E3）等中间件承担类似职责，但**没有统一的注入目录/元数据描述**，每个中间件各自拼字符串。**Zcode 的 27 源目录化 + channel 分类是可借鉴的工程化做法**（见配套文档）。

---

## 二、Hooks 扩展点

### 2.1 Zcode：7 事件用户可配脚本钩子

[源码确认] `contracts/src/hooks/index.ts:7-15`

```ts
HookEventName = SessionStart | UserPromptSubmit | PreToolUse | PermissionRequest
              | PostToolUse | PostToolUseFailure | Stop
```

比 Claude Code 多一个 `PermissionRequest`。

**能力矩阵** [源码确认]（`contracts/src/hooks/index.ts:67-206`, `:163-194`, `:248-280`）：

| 事件 | 可注入上下文 | **可改 tool input** | **可否阻断** | 特有字段 |
| --- | --- | --- | --- | --- |
| `PreToolUse` | `additionalContext` | ✅ `updatedInput` | ✅ allow/ask/deny | `riskLevel`、`sideEffectScope`、`toolCallId`、`toolInput`、`toolName` |
| `PermissionRequest` | `additionalContext` | ✅ `updatedInput` | ✅ allow/deny | `permissionSuggestions`、`reason`、`requestId`、`permissionUpdates` |
| `PostToolUse` | `additionalContext` | ❌ | ❌ | `artifactRefs`、`toolResponse`、`toolResultPreview` |
| `PostToolUseFailure` | `additionalContext` | ❌ | ❌ | `error{message,type}`、`isInterrupt` |
| `UserPromptSubmit` | `additionalContext` | ❌ | ✅ 可阻断 | `attachmentsSummary`、`prompt` |
| `SessionStart` | `additionalContext` | ❌ | ❌ | `model`、`source`(startup/resume/clear/compact) |
| `Stop` | `additionalContext` | ❌ | ✅ `continue:true` 可**延长** | `responsePreview`、`stopHookActive`、`toolCallCount` |

**执行语义** [源码确认]：
- 超时默认 **60,000ms**（`DefaultHooksRuntimeConfig`，`contracts/src/hooks/index.ts:425`）；`maxOutputBytes` 32,768
- `AbortController` + `HOOK_TIMEOUT_ABORT_REASON`（`core/src/hooks/runner.ts:334-382`）
- `HookOutcome`: `success` / `blocked` / `failed` / `cancelled` / `timed_out`
- Matcher（`core/src/hooks/output.ts:98-112`）：`*` 或未指定 = 全匹配；`A|B` = 精确 alternation；否则按 `RegExp` 编译
- stderr/stdout preview ≤4,000 字符进 lifecycle event，**不进模型**

**配置来源** [源码确认]：
| 来源 | 路径 | 信任要求 |
| --- | --- | --- |
| 用户 | `~/.zcode/cli/config.json` 的 `hooks` 键 | 无 |
| 项目 | `zcode.json` 或 `.zcode/config.json` | **需 workspace trust**（digest-based trust coordinator，10 分钟 review 超时，未信任时 `diagnostic: config_project_hooks_pending_trust`） |
| 插件 | `hooks/hooks.json` 或 manifest `hooks` | **无条件执行**（`adapters/src/plugins/index.ts:366` 的 `canRunPluginHooks` 直接 `return true`） |
| 内部 | 源码内置 | — |

执行顺序：user → project → plugin → internal（`core/src/hooks/configured-runner.ts:174-192` 把 workspace hook 插到第一个非 user hook 之前）。

**Hook 类型**：`command`（shell 模式，可带 `async: true` 脱离本轮）和 `process`（argv 模式，无 async）。

### 2.2 Sherry：无用户可配 hook 点

[实测] Sherry 全文搜索 `hook` 命中 194 处，但**全部是内部机制**：

| Sherry 的 "hook" | 实际是什么 | 对应 Zcode 概念 |
| --- | --- | --- |
| `abefore_model` / `aafter_agent` | LangChain 中间件钩子 | 无对应（Zcode 无 middleware 概念） |
| `runtime/hooks.py` | 进程级回调注册表（auto-turn trigger / WS 任务表 / skill 扫描） | 无对应 |
| `agent/middlewares/humanInTheLoop/core.py:159-216` | `interrupt()` / `set_interrupt` / `clear_interrupt` | ≈ `PermissionRequest` 的硬编码内置版 |
| `wrap_tool_call` | 中间件包裹工具调用 | ≈ `PreToolUse` 的硬编码内置版 |

最接近 Zcode 扩展点的是 `skills/builtin/*/scripts/`（11 个目录），但那是 **skill 自带脚本，不是生命周期钩子**。

**定性**：这是 Zcode 明确更优的一项。Sherry 的 HITL/PathGuard/Redaction 全是编译期固定的 Python 中间件，用户无法在不 fork 源码的情况下插入自己的审计或策略逻辑。**但** Sherry 的固定中间件链比 Zcode 的用户脚本更可靠：Zcode 的插件 hook 是**无条件执行**的（见 §2.1 信任要求表），用户既不能审计也不能绕过；Sherry 的中间件内容与顺序由 `agent/middlewares/__init__.py` 编译期固定，不存在第三方注入面。

---

## 三、Turn 循环与流式事件

### 3.1 Zcode：显式状态机 + 约 90 个事件类型

[源码确认] `core/src/agent/turn-machine.ts::TurnMachineImpl` + `turn-state.ts`

**10 个 phase**（`turn-state.ts:25`）：
```
idle → processing_input → awaiting_model_response → streaming
     → scheduling_tools → executing_tools → aggregating_results
     → awaiting_permission → completing → error
```

**6 种终态**（`turn-state.ts:149`）：
```
success | cancelled | error_max_turns | error_max_budget
       | error_during_execution | error_max_tool_calls
```
注意 `cancelled` 是**正常终态**而非错误——这与 Sherry 把 HITL interrupt 当异常路径不同。

**主循环**（`core/src/runtime/methods/turn-loop.ts:43::runRegularTurnLoop`）：
```
while (true):
  microcompact → autoCompact → MCP init
  → 组装 provider 消息 → runModelBackedTurnStep
  → 若有 tool call 则执行 → continue
  → 无 tool call 且无 inline guide 则 break
```

**事件类型系统** [源码确认] `contracts/src/events/session.events.ts:83-176`，约 90 个 `SessionEventType`，分组：
- Session：`session_created` / `resumed` / `forked` / `compacted` / `title_updated` / `mode_changed` / `ended`
- Turn：`turn_started` / `turn_input_received` / `turn_complete` / `turn_error`
- **Steering（8 个）**：`turn_steer_queued` / `_delivery_changed` / `_dispatch_changed` / `_drained` / `_rejected` / `_discarded` / `_reordered` / `queue_auto_drain_changed`
- Message：`user_message` / `assistant_message` / `assistant_feedback_updated` / `system_message`
- Model：`model_request` / `model_selected` / `model_streaming` / `model_complete` / `model_error` / `model_network_status` / `model_anomaly_warning` / `network_request_status`
- **流恢复（7 个）**：`stream_recovery_anchor_created` / `_started` / `anchor_selected` / `tail_discarded` / `retry_started` / `_blocked` + `streaming_tool_ledger_updated`
- Tool（6 个）：`tool_call_scheduled` / `_started` / `_progress` / `_result` / `_error` + `tool_batch_complete`
- Permission（3+1）：`permission_requested` / `_resolved` / `_denied` / `user_input_auto_resolution_updated`
- 另有 hook / compaction / rewind / checkpoint / goal / subagent / background / interrupt / cancel / resume / error 组

**流式细粒度协议** [源码确认] `ModelStreamingKind`（`session.events.ts:696`）——**14 种 kind**：
```
start, text_start, text_delta, text_end,
reasoning_start, reasoning_delta, reasoning_end,
tool_input_start, tool_input_delta, tool_input_end,
tool_call, finish, error
```
payload（`:711`）：`{delta, done, kind, assistantMessageId, partId, toolCallId, toolName, input, providerExecuted}`

这意味着客户端能**分别**拿到 reasoning 流与正文流、增量 tool input JSON、以及 usage 遥测。发出方 `core/src/runtime/methods/model-streaming-event.ts`。

### 3.2 Sherry：LangGraph 图 + 18 个中间件

[实测] Sherry 无自定义 turn 状态机——状态由 LangGraph 图承载，相位等价物是 `agent/core.py` 的中间件注册顺序（18 个中间件，见 `AGENTS.md`）。

**关键结构差异** [源码确认 vs 实测]：
- Zcode：`after_model` 之外还有**显式的 `awaiting_permission` phase**，且 hook 可改 tool input
- Sherry：`humanInTheLoop/core.py:58` 的 `after_model` 拦截 tool call → `interrupt(HITLRequest(...))`，恢复契约是 `Command(resume={"decisions": [{"type": "approve"}]})`。HITL 的 resume 语义是 LangGraph 的，**不是自定义协议**
- Zcode 有 ~90 个事件类型 + 14 种流式 kind + 7 个流恢复事件；Sherry 的 WS 流由 `WS_STREAM` 配置约束（`max_flatten_depth = 5`、`max_continuation_retries = 4`、`max_reasoning_only_retries = 2`），**没有等价的流恢复锚点机制**

**流恢复是 Sherry 的明确空白** [源码确认]：Zcode 的 `stream_recovery_anchor_created` → `anchor_selected` → `tail_discarded` 是一套「流中途断线后从锚点续传并丢弃不确定尾部」的机制。Sherry 侧 `pub/func/message/` 有 retry，但无流级锚点。

### 3.3 Todo 呈现方式

[源码确认] Zcode **无专用 todo 流通道**。`TodoWrite`/`TodoRead` 是普通工具，落 `todo` 表（键 `(session_id, position)`）。`messageStreamShowTodos` 是纯 UI 开关（`packages/shared/src/validationAppSettings.ts:449`，默认 `false`；本机设置为 `False`），决定工具卡片是否内联渲染。todo 变化通过 system-reminder 注入模型上下文（`turn-loop.ts:138-156`）。

[实测] Sherry 同样无专用通道，todo 通过 `TODOLIST_INFRA` SQLite + `todowrite`/`todoread` 工具 + `knowledge` 工具。**此项无差异。**

---

## 四、输入队列与中断

### 4.1 Zcode：两层队列 + 三种投递语义

[源码确认]

**第一层：Runtime command queue**（`core/src/runtime/command-queue.ts`）
- 6 种 mode：`prompt`、`target-continuation`、`target-continuation-loop`、`task-notification`、`subagent-message`、`control-only-turn`
- 3 种 priority：`now` / `next` / `later`
- **无深度上限**；`dequeueNextBatch` 会把同优先级的 `task-notification` 合并
- `cancelPending` id 集合支持取消

**第二层：per-turn steer queue** — `activeTurn.pendingInputs`（`PendingTurnInput[]`）+ 持久化的 `session_input` 表

**投递模式**（`session_input.delivery` 的 DB check 约束）：

| 模式 | 语义 | 实现 |
| --- | --- | --- |
| `queue` | 下一个 turn 消费（turn 边界） | 正常入队 |
| `guide` | **当前 turn 内**在合法 model-step 边界注入，每边界最多 1 条 | `drainInlineGuideForNextRequest`（`turn-guide-drain.ts`）；若无 tool 边界则 `fallbackPendingGuidesToQueue` 降级为 queue |
| `startNow` | **抢占当前 turn** | 见下 |

**限制** [源码确认]：`MAX_TURN_STEER_INPUT_BYTES = 200,000`（`core/src/runtime/helpers/steering.ts:11`），preview 200 字符。

**拒绝原因**（`core/src/runtime/methods/steering.ts:57`）：`empty_input`、`input_too_large`、`no_active_turn`、`expected_turn_mismatch`、`turn_not_steerable`。

**落盘实测** [落盘实测]：`session_input` 表 214 行 = 107 `startNow`/promoted + 92 `queue`/promoted + 15 `queue`/cancelled。**本机从未用过 `guide` 模式。**

### 4.2 Zcode：中断链

[源码确认]
```
startNow 请求
  → acquireForegroundPromotionLease       (bootstrap/.../session-flow.ts)
  → preemptActiveTurnAndWait              (session-flow.ts:410)
  → stopActiveForegroundExecution          (core/.../runtime-command-queue.ts:446)
      → abort 活动 AbortController（带 reason）
  → waitForSessionIdle
```
abort signal 一路传到 tool executor：`call-runner.ts:157-162` 检测 `signal.aborted` → 返回 `ToolCancelled` 结果。`batch-runner.ts:92-128` 中任一批次返回 `turnControl.stopTurnAfterResult` → **剩余批次全部标 `ToolCancelled` 跳过**。

**已产生的部分工作保留，不回滚** [源码确认]（`turn-tools.ts` 注释）：assistant 的 `tool_use` 已持久化，被 abort 的工具仍产出 cancelled 结果。取消时活动 goal 转 paused，pending guide 降级为 queue（`turn.ts:731-762`）。

**准入层** [源码确认] `bootstrap/src/zcode-protocol-v4/command-inbox.ts`：**CommandInbox**
- 每命令幂等键闸 + per-session FIFO 准入闸，持有到 `settle`
- 已 settle 的 ACK 按 session LRU = **512**（`PROTOCOL_V4_LIMITS.idempotencyTablePerSession`，`packages/shared/src/zcode-protocol-v4/core.ts:82`）
- in-flight 输入被 pin，永不淘汰

### 4.3 Sherry：单队列 + lane 分发

[实测] `server/service/input_queue_service.py`

**准入逻辑**（`submit_user_input:314-415`）比 Zcode 更严谨：
1. **幂等优先**：`client_msg_id` 命中 ACTIVE 行 → `DEDUPED`（跨 session 查）
2. **busy 判定**：`detect_state(session_id)` **或**存在 `CLAIMED` 占位行（覆盖「已 dispatch 但未 raise detect_state 信号」的窗口）
3. busy → `enqueue`（返回 position）或 `QueueFullError` → `QUEUE_FULL`
4. idle → **先 resolve executor 再改状态**（缺注册时 fast-fail，不留孤儿占位）→ `insert_claimed`
5. **锁外 dispatch**：`asyncio.create_task(_run_executor(...))`，整个 turn 在一个 MAIN lane slot 内

**配置** [实测] `INPUT_QUEUE`：`batch_max_rows = 20`、`max_active_per_session = 20`、`expiry_seconds = 86400`、`busy_timeout_ms = 5000`、`lock_sweep_threshold = 256`。

**TurnExecutor 协议** [源码确认] `input_queue_service.py:142`：同时要求 `execute`（单消息）与 `execute_batch`（FIFO 多消息合并为**一轮**一次回复），`BatchTurnExecutor` 基类让不支持批量的执行器降级。

**中断** [实测]：HITL 用 `GraphInterrupt` + `Command(resume=...)`（`agent/middlewares/humanInTheLoop/core.py:17,178,284`）。`_sandbox_bypass_interrupt` 明确「`GraphInterrupt` 必须重抛，绝不吞掉」（`:287`）。

### 4.4 差异定性

| 维度 | Zcode | Sherry | 谁更优 |
| --- | --- | --- | --- |
| 队列层级 | 2 层（command queue + per-turn steer queue） | 1 层（`UserInputQueue`） | Zcode |
| 投递语义 | 3 种（startNow 抢占 / guide 内联 / queue） | 2 种（STARTED / QUEUED） | **Zcode**（`guide` 是 Sherry 完全没有的能力） |
| 抢占安全 | foreground promotion lease + 幂等 LRU 512 | 幂等跨 session 查 + CLAIMED 占位 + 锁外 dispatch | **Sherry**（占位设计更严密） |
| 批量合并 | 无 `execute_batch` 等价 | `execute_batch` FIFO 合并为一轮 | **Sherry** |
| 幂等上限 | LRU 512/session 显式有界 | 无显式上限（`batch_max_rows=20` 是队列深度） | Sherry（更简） |
| 中断后工作 | 保留不回滚 | HITL 走 LangGraph interrupt/resume | 各有取舍 |
| 并发闸 | 工具批内 10 | 4 类 lane 共 25 + 启动校验 | **Sherry**（见 §13） |

**结论**：`guide`（turn 内联注入）是 Zcode 唯一明确领先且 Sherry 完全没有的输入语义。Sherry 的 `sessions_steer` 是 subagent 专用，不面向用户。

---

## 五、会话分叉与回退

### 5.1 Zcode：append-only 分支切断 + 文件检查点

这是 Zcode 最被低估的能力。

**文件级检查点** [落盘实测]：本机 `session_entry` 表 **507 条** `runtime/workspace_checkpoint` 记录。artifact 形状 [源码确认] `contracts/src/rewind/index.ts:100`：`workspace_file_before_change`，每文件带 `beforeContent` / `afterContent` / `structuredPatch`（jsdiff，3 context 行，5s timeout）。

**对话级 rewind** [源码确认] `core/src/runtime/methods/rewind-message.ts:404::rewindConversationToMessage`：
- 写 `setRevert`：`keptMessageIDs` / `branchCutAfterMessageID` / **`branchGeneration`**
- `branchGeneration` 递增作为 fencing token
- 取消被移除 turn 名下的后台任务
- **旧分支仍留在库里**，通过 `selectActiveConversationBranch`（`contracts/src/rewind/index.ts:176`）从活动分支隐藏

**维度矩阵** [源码确认]：
- scope：`conversation` | `workspace` | `both`
- strategy：`active_chain` | `file_only` | `fork_required` | `unavailable`
- target status：含 `covered_by_compact`（`:38-42`）——目标若在压缩边界之前，workspace rewind 仅在有 checkpoint 时可用，否则 `unavailable`

**6 条 fork 路径** [源码确认] `core/src/runtime/methods/session-fork.ts`：
| 函数 | 行 | 用途 |
| --- | --- | --- |
| `createSelectionSideConversation` | 804 | 选区旁聊 |
| `createForkedSession` | 851 | 基础 fork |
| `forkConversationFromMessage` | 895 | 旧版 workspace+checkpoint fork |
| `forkStableConversationAtMessage` | 1050 | V4 稳定 fork（纯转写本拷贝，原子 `commitForkBundle`） |
| `forkConversationBeforeMessage` | 1080 | 被 compact 覆盖的编辑 |

**fork 携带内容** [源码确认]：完整 message/part 转写本 + id 重映射（`cloneMessageForFork` / `clonePartForFork`）+ goal state 快照 + verifier ledger（`copyGoalStateForFork`）+ workspace fork 额外从 checkpoint 恢复文件。附 `session_fork` 时间线分隔 + 合成用户通知 + `SessionForked` 事件。

### 5.2 Sherry：无

[实测] 确认缺失：
- 无文件状态快照（`patch_file` / `write_file` 不可回滚）
- `TOOL_RESULT_EVICTION.eviction_subdir = 'evicted'` 只落盘**工具结果**，不是文件状态
- `workspace/` 在 `.gitignore` 中是锚定的（`/workspace/`），无 git 兜底
- checkpointer（`agent/checkpointer/thread_safe_checkpointer.py`）保存的是 **LangGraph 图状态**，不是文件快照，也不是对话分支树

**定性**：这是 Zcode 相对 Sherry **差距最大**的一项，且 Sherry 完全没有对应物。Zcode 的设计有两处特别值得学：
1. **append-only + branchGeneration**，而不是删除消息——与 Sherry 的 `context_engine/events/` append-only 事件日志哲学一致，可复用同一套思路
2. **checkpoint 与对话解耦**：文件快照独立于会话存在，scope 可选 `conversation`/`workspace`/`both`，避免了「回退对话必回退文件」的粗暴绑定

---

## 六、模型路由与计量

### 6.1 Zcode：per-agent × per-request × per-attempt 三维记账

[源码确认] `model_usage` 表约 40 列。关键维度：

| 列族 | 列 | 说明 |
| --- | --- | --- |
| 身份 | `logical_request_id`、`turn_id`、`trace_id`、`span_id`、`assistant_message_id`、`parent_user_message_id` | 完整链路可追 |
| **Agent 归因** | `agent` | **区分主/子 agent** |
| 任务类型 | `mode`、`task_type`、`query_source` | `task_type`：`interactive` / `subagent_child`；`query_source`：`main_turn` / `subagent` / `compact` / `session_title` |
| 路由 | `provider_id`、`model_id`、`variant` | |
| 重试 | `attempt_index`、`retry_count`、`retryable` | 每次尝试一行 |
| Token（7 列） | `input`、`output`、`reasoning`、`cache_creation_input`、`cache_read_input`、`provider_total`、`computed_total` | provider 报数与本地估算**并列存储** |
| 质量 | `finish_reason`、`tool_call_count`、`status`、`duration_ms`、`time_to_first_token_ms` | |
| 失败 | `cancelled_by_user`、`context_exceeded`、`error_type`、`error_code`、`error_message` | |
| 原始 | `raw_usage_json`、`provider_metadata_json` | |

**`agent` 列取值** [落盘实测]（`usage-observability.ts:93`：`agent: runtime.config.agentName ?? "zcode-agent"`）：
| agent | 调用数 | task_type |
| --- | --- | --- |
| `zcode-agent` | 5,662 | `interactive` |
| `zcode-general-purpose` | 124 | `subagent_child` |
| `zcode-Explore` | 76 | `subagent_child` |

**其他两张表** [源码确认]：
- `tool_usage`：每次 tool call 的 times / bytes / retry / approval / `side_effect_scope` / `read_only` / `destructive`
- `turn_usage`：每 turn 聚合（`model_request_count`、`retry_count`、`tool_call_count`、`error_count`、token 求和）

**落盘实测**：5,852 行 `model_usage`；provider 分布 deepseek-flash 5,285 / GLM-5.3-Flash 577；`cache_read_input_tokens` 合计 2,710,812,224；`cache_creation_input_tokens` 合计 **0**；`retry_count` 合计 114；`context_exceeded` **0**；`cancelled_by_user` 14。

### 6.2 Sherry：无 per-agent 计量，有 fallback chain

[实测] Sherry **没有** `model_usage` / `tool_usage` / `turn_usage` 任何一张计量表。`runtime/session/count_call_register.py` 是进程内计数器，不落 SQLite，不分 agent。

**Sherry 的模型侧能力** [实测]：
- `models/LLMs/main_llm.py:349::build_fallback_chain()` — 读 `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}`（i=1..），遇首个缺 `NAME` 停止；构造失败的候选**跳过并告警**，不让可选 fallback 拖垮启动；`temperature=0`、`max_retries=LLM_CLIENT_DEFAULTS["fallback_max_retries"]`
- `MODEL_BACKEND` 配置 provider
- `MAX_TOKENS_BOOST`：`base_max_tokens` / `default_max_tokens` = 8,192，`max_cap` = 32,768，`max_retries` = 3
- `REASONING_BUDGET`：anthropic 2,000 / 非 anthropic 4,096
- `LLM_RETRY`：`max_retries`、`base_delay`、`max_delay`、`jitter`、`stale_giveup_threshold`
- `MODEL_PRICING` 已有（可用于实时花费核算）
- `reasoning_normalizer.py` / `reasoning_openai.py` / `reasoning_payload.py` — 跨 provider 推理流归一化

**Zcode 的 `ListModels` 不切换 session model** [源码确认]（`tool/handlers/list-models.ts`）：它存在的唯一目的是让主 agent 为 workflow run 选 `subagent_model`。session model 在 **admission 时冻结**（`turn.ts:103::admittedModelSelection`），持久化为 `runtime/model_selection` session_entry（本机 12 行）。

**子 agent 模型选择** [源码确认]：来自 profile frontmatter 的 `modelSelection`（`subagent/profile-model-selection.ts`）+ `built-inModelSelectionOverrides`。

### 6.3 差异定性

| 维度 | Zcode | Sherry |
| --- | --- | --- |
| 用量落盘 | 3 张表，`model_usage` 约 40 列 | **无** |
| per-agent 归因 | ✅ `agent` 列 | ❌ |
| per-attempt 重试记账 | ✅ `attempt_index` | ❌ |
| provider 报数 vs 本地估算并列 | ✅ `provider_total` + `computed_total` | ❌ |
| cache token 单独记账 | ✅ 创建/读取分列 | ❌ |
| 上下文溢出标记 | ✅ `context_exceeded` | `TOKEN_GUARD` 有 128K 硬地板，但无逐请求记账 |
| TTFT 记账 | ✅ `time_to_first_token_ms` | ❌ |
| fallback chain | 有 | ✅ **Sherry 独有**（环境变量驱动的多候选链 + 构造失败跳过） |
| max_tokens 动态提升 | 无 | ✅ **Sherry 独有**（`MAX_TOKENS_BOOST` 3 次重试到 32K） |
| 跨 provider 推理归一化 | 有 | ✅ **Sherry 独有**（3 个 reasoning 模块） |

**结论**：**per-agent × per-attempt 用量记账是 Zcode 明确更优**。Sherry 已有 `MODEL_PRICING`，补一张计量表的成本很低，但价值在于：能回答「哪个 subagent 烧的 token」「哪次重试浪费了多少」这类问题，目前完全无法回答。

---

## 七、文件工具实现深度

这是 Zcode 工程成熟度最高的部分，也是 Sherry 最容易直接借鉴的部分。

### 7.1 Zcode Edit：8 级模糊匹配级联

[源码确认] `core/src/tool/edit-matchers.ts:41-69::findEditMatch()`

固定顺序尝试 8 种策略，命中即返回：

| # | 策略 | 作用 |
| --- | --- | --- |
| 1 | `exact` | 纯子串搜索（`:222-233`） |
| 2 | `quote_normalized` | 弯引号 → ASCII（`:358-364`） |
| 3 | `line_number_prefix_stripped` | 剥掉模型误抄的 `^\d+: ` / `^\d+\t` 读行号前缀（`:253-263`） |
| 4 | `escape_normalized` | 反转义 `\n \t \r \" \' \` \\ \$`（`:265-284`） |
| 5 | `unicode_escape_normalized` | `\uXXXX` → 真实字符（`:286-291`） |
| 6 | `line_trimmed` | 逐行 `.trim()` 比较（`:175-187`） |
| 7 | `indentation_flexible` | 剥离双方公共缩进后精确比较（`:189-202`） |
| 8 | `block_anchor` | **≥3 行；首尾行 trim 后必须匹配，中间行按平均 Levenshtein 行相似度 ≥ `BLOCK_ANCHOR_MIN_SIMILARITY = 0.8`**（`:30`, `:204-220`, `:323-356`） |

**歧义处理**（`toMatchResult`，`:132-144`）：多候选**值不同** → `status: "ambiguous"` / 错误 `AMBIGUOUS_REPLACE`；多候选**值相同** → 视为匹配，随后 handler 计数（`:224-230`），非 `replace_all` 下 >1 处则拒绝。

**`replace_all` 保护**（`:62`）：`BROAD_MATCHERS`（`line_trimmed` / `indentation_flexible` / `block_anchor`）在 `replace_all=true` 时**禁用**，防止过度替换。

**引号风格保留**（`:82-105`）：`preserveQuoteStyle` 在文件实际用弯引号时把弯引号套回替换文本。

### 7.2 Zcode Edit/Write：读-before-edit + 三要素 stale 检测 + 原子写

[源码确认]

**无 dry-run / preview 开关**：`EditInput` schema 只有 `file_path` / `old_string` / `new_string` / `replace_all`（`contracts/src/tools/edit.ts:22-41`）。diff 是**事后**算的（`createStructuredPatch`，jsdiff，3 context，5s timeout；`edit.ts:531-535`），作为 `structuredPatch` 返回给 UI/权限展示。

**原子性**：`newContent` 整体算完后才写（`edit.ts:508-520` → `writeTextFile({atomic: true, expectedRevision: read.revision})`）。adapter 侧（`adapters/src/fs/index.ts:700-755`）用 `O_EXCL|O_NOFOLLOW` 写临时文件 → fsync → `rename` 覆盖；失败清理临时文件并降级为 `O_NOFOLLOW` 截断写。**保留原文件 mode**（exec 位不丢）。**拒绝穿符号链接**（`SymlinkWriteRefusedError`）。

**stale 检测三要素**（`edit.ts:444-468`, `write.ts:306-332`）：
1. `mtime` 推进（**整数毫秒**比较，抑制误报）
2. `size` 变化
3. `revisionId` 不同 —— 形如 `mtime:<ms>:size:<bytes>`（`fs/index.ts:775-777`）

**假阳性豁免**（`edit.ts:434`, `write.ts:295`）：若上次是**全量读**且存储内容与当前内容相等，则**即使 mtime 推进也不算 stale**——容忍 linter/formatter 触碰文件但不改字节。

**写层乐观并发**（`fs/index.ts:473-483`）：`assertExpectedRevision` 不符 → `stale_write`。

**错误分类**：`write_file_not_read`（未先读）、`write_file_stale`（读后已改）、`write_file_partial_view`（基于 token 截断的部分视图不可用于 Edit/Write）。Edit 另有 `MAX_EDIT_FILE_SIZE_BYTES = 1GB` 拒绝（`edit.ts:61,148`），`.ipynb` 引导改用 NotebookEdit（`:193-198`）。文件不存在时给 Levenshtein「Did you mean `<file>`?」建议（`edit-matchers.ts:369-397`）。

### 7.3 Zcode Read：token 感知的二分截断

[源码确认] `contracts/src/tools/read.ts:15-17`
| 常量 | 值 |
| --- | --- |
| `READ_MAX_FILE_SIZE_BYTES` | 256 KiB |
| `READ_MAX_OUTPUT_TOKENS` | 25,000 |
| `READ_DEFAULT_MAX_LINES` | 2,000 |

- **未变更重读**返回 stub：`"Wasted call — file unchanged since your last Read…"`（`read.ts:55,190-198`）——省 token 的显式设计
- 行号用 `cat -n` 风格 `N\t`（`read-text.ts:85-96`）
- **超限截断**用**二分搜索**取 ≤ **85%** token 上限的最大前缀（`read-text.ts:18,161-236`），返回 `partialViewNotice` + 续读 offset
- 部分视图标记 `isPartialView`，Edit/Write 拒绝基于它写入

### 7.4 Sherry 对应实现

[实测] `agent/tools/file_tools/`：`read_file.py`、`write_file.py`、`patch_file.py`、`search_files.py`

| 能力 | Zcode | Sherry |
| --- | --- | --- |
| 读分页 | ✅ 2000 行默认 + offset/limit | ✅ `read_file` 有 `truncated` + `hint`（`read_file.py:57-58,127,133-135`） |
| token 感知截断 | ✅ 二分到 85% 上限 | ⚠️ 有 `evict_threshold_chars` 但机制不同（见 §10.2） |
| 未变更重读省 token | ✅ 显式 stub | ❌ 无 |
| Edit 模糊匹配 | ✅ 8 级级联 + Levenshtein ≥0.8 | ❌ `patch_file` 无匹配降级策略 |
| 读-before-edit 强制 | ✅ 三态错误 | 部分（`PathGuard` 管路径，不管读序） |
| stale 检测 | ✅ mtime(ms)+size+revision+内容豁免 | ❌ 无 |
| 原子写 | ✅ temp+fsync+rename+O_NOFOLLOW | ❌ 未验证原子性 |
| 拒绝穿符号链接写 | ✅ `SymlinkWriteRefusedError` | 有 `PathGuard` 路径限制，但符号链接语义未验证 |
| diff 展示 | ✅ jsdiff structuredPatch | ❌ 无 |
| 并行安全 | ✅ `destructive` / `sideEffectScope` 标注驱动调度 | 靠 `ToolGuardrails` 运行时统计 |

**结论**：**Edit 的 8 级匹配级联 + stale 三要素检测 + 原子写，是 Sherry 最值得直接借鉴的三个工程细节**——它们不涉及架构变更，只改 `patch_file` / `write_file` 的实现。

---

## 八、搜索能力

### 8.1 Zcode：双层搜索，都无持久索引

[源码确认]

**层 A：Grep/Glob 工具**（Explore subagent / 非 embedded 模式）→ `adapters/src/fs/index.ts:452-574`
- 用 **`ripgrep` npm 包（WASI ripgrep）**，跑在 **Worker thread** 里（`fs/index.ts:79-113,1047-1134`）。文档化理由：WASM ripgrep 会阻塞 Node event loop，Worker 允许 abort/timeout 时 `terminate()`
- ripgrep 参数：`--no-config --hidden --color never --no-heading --with-filename --max-columns 500` + 显式排除 `.git/.svn/.hg/.bzr/.jj/.sl`；content 模式加 `--json`，计数模式加 `-c`，多行加 `-U --multiline-dotall`
- **ignore 策略**：依赖 ripgrep 自身 `.gitignore` 处理（`--hidden` 只含 dotfile，**不**关闭 ignore 文件）
- WASM 失败 → 降级进程内 JS 正则搜索（`searchTextWithJavaScript`，`:576-647`）
- **无持久文件索引**
- **无模糊匹配、无相关性排序**——结果仅按 **mtime 降序**再按路径（`:1272-1289`）

**层 B：Bash prelude 注入 shell 函数**（`adapters/src/exec/embedded-search-prelude.ts`）
- `find()` → **bfs**（`-S dfs -regextype findutils-default`）
- `grep()` → **ugrep**（`-G --ignore-files --hidden -I --exclude-dir=.git/.svn/.hg/.bzr/.jj/.sl`）；语义不同的 flag（`-z/-Z/--null/--null-data/…`）自动 bypass 回系统 grep
- `rg()` fallback 仅当 PATH 无 `rg`（`:142-169`）

**常量** [源码确认]：
| 常量 | 值 | 位置 |
| --- | --- | --- |
| `DEFAULT_GREP_HEAD_LIMIT` | 250 | `fs/index.ts:52` |
| `DEFAULT_RIPGREP_TIMEOUT_MS` | 30,000 | `fs/index.ts:53` |
| Grep 模型预算 | 20,000 bytes | `handlers/grep.ts:23` |
| `DEFAULT_GLOB_MAX_RESULTS` / `MAX_GLOB_RESULTS` | 100 / 100 | `fs/index.ts:51` |
| Glob 模型预算 | 100,000 bytes | `handlers/glob.ts:20-21` |
| `--max-columns` | 500 | `fs/index.ts:932-941` |

**二进制解析顺序** [源码确认] `packages/services/src/runtime-tools/runtimeToolResolver.ts:103-142`：
```
env ZCODE_{BFS,RG,UGREP}_BINARY
  → <ZCODE_SERVER_RUNTIME_ROOT>/tools/<dir>
  → process.resourcesPath/tools/<dir>     ← Electron 走这条到 /opt/ZCode/resources/tools
  → dev bundled-tools/<platform-arch>
  → PATH 查找
```
SEA CLI 解压到 `~/.zcode/cache/runtime_tools/<target>/` 并做 sha256 校验（`cli/src/sea-runtime-tools.ts`）。**缺失时优雅降级**到系统命令（`embedded-search-prelude.ts:179` 的 `command -v` 守卫）。

**`nativeSearchEnhancementsEnabled`** [源码确认]：默认 `true`（`packages/shared/src/validationAppSettings.ts:461`），UI 名「Enhanced Find and Grep」。**只控制 find/grep 的 bfs/ugrep 替换**，与 WebSearch 无关。

### 8.2 Sherry

[实测] `search_files`（`agent/tools/file_tools/search_files.py:176`）：一个 `BaseTool`，`target` 参数二分（`content` 正则 grep / `files` glob 找名），自动跳过 hidden / `__pycache__` / `node_modules` / `.venv`。

**能力矩阵**：
| 能力 | Zcode | Sherry |
| --- | --- | --- |
| 正则内容搜索 | ✅ ripgrep WASI in Worker | ✅ Python `re`（`bounded_walk` + `ScanState`） |
| glob 文件名搜索 | ✅ | ✅ |
| 分页 | ✅ offset/limit + head limit | ✅ `offset` / `limit`(≤200) |
| 上下文行 | n/a | ✅ `context`(0-5) |
| **双截断标记** | 单 `truncated` | ✅ **`page_truncated` + `scan_truncated`**（`search_scan.py:51,68,105-110`）——区分「结果页满」与「扫描预算耗尽」，比 Zcode 更精细 |
| 排序 | mtime 降序 | 未验证 |
| 模糊匹配 | ❌ | ❌ |
| 持久索引 | ❌ | ✅ **Sherry 独有**：`CODE_INTEL`（tree-sitter 符号索引，7 语言）、`CODE_INTEL_SEMANTIC`（bge-m3，`top_k` 5/20，候选池 40） |
| 结构化搜索 | ❌ | ✅ **Sherry 独有**：`AST_GREP`（23 语言，strictness 默认 smart，sha256 固定发行版） |
| LSP 精度检索 | ❌ | ✅ **Sherry 独有**：`LSP` 21 键配置 |
| 外部搜索增强 | ✅ bfs/ugrep 注入 shell | ❌ |
| ripgrep 级性能 | ✅ WASI SIMD | Python `re` |

**结论**：**搜索是 Sherry 明确领先**（AST + LSP + 语义索引三层是 Zcode 完全没有的）。Zcode 唯一可借鉴的是 **bfs/ugrep 注入 bash prelude**（让主 agent 的 shell 搜索更快）与 **WASM ripgrep in Worker** 的架构（但 Sherry 有语义索引，走的是另一条路）。

**一处 Sherry 领先细节**：`search_scan.py` 的双截断标记区分「结果页满」与「扫描预算耗尽」，让模型知道该不该继续翻页。Zcode 只有单一 `truncated`。**这是 Sherry 该保留并推广到其他工具的设计。**

---

## 九、Web 搜索与抓取

### 9.1 Zcode WebSearch = provider 原生 API 搜索，非本地爬虫

[源码确认] `core/src/tool/handlers/websearch.ts`
- 前置条件：`model.properties.supportsNativeWebSearch`
- 单次辅助 `model.streamText` 调用，挂一个 **provider 原生 `web_search` 工具契约**（`executionMode: "providerNative"`）
- `maxUses = DEFAULT_MAX_USES = 8`
- `maxOutputTokens = min(4096, model max)`
- timeout 60s
- 工具描述明写 **"US-only"**
- 结果 URL 去重，来源上限 `MAX_SOURCE_LINKS = 20`
- 预算：`maxInlineBytes = 10,000` / `maxModelBytes = 20,000`（`contracts/src/tools/websearch.ts:139-140`）
- **无本地爬虫、无本地索引**

### 9.2 Zcode WebFetch

[源码确认] `handlers/webfetch*.ts`

**流程**：GET（经 `HttpClientPort`/undici）→ **手写正则 HTML→Markdown 转换器**（`webfetch-content.ts:59-101`：剥注释/script/style/noscript，映射 h1-h6/a/li/br/block 标签，解码实体）→ **用辅助小模型回答 `prompt`**。仅当 URL 在预批准名单且 content-type 为 `text/markdown` 且 <100k 字符时直接返回 markdown。

**常量**（`webfetch-constants.ts:1-11`）：
| 常量 | 值 |
| --- | --- |
| `MAX_WEBFETCH_RESPONSE_BYTES` | 10 MiB |
| `MAX_MODEL_INPUT_CHARS` | 100,000（头截断 + 后缀标记） |
| `MAX_WEBFETCH_MODEL_BYTES` | 100,000 |
| `MAX_WEBFETCH_URL_CHARS` | 2,000 |
| `MAX_REDIRECTS` | 10 |
| 缓存 TTL / 上限 | 15 分钟 / 50 MiB |

**重定向策略**（`webfetch-url.ts:39-41,63-81`）：HTTP 升级为 HTTPS；**仅跟随同 host（允许 `www` 变体）/ 同协议 / 同端口**的重定向；**跨 host 重定向返回给模型而非跟随**。无 robots.txt 处理 [未验证：源码中未找到]。

**SSRF 防护是部分的、仅字面量** [源码确认]：
- `normalizeWebFetchUrl` / `getBlockedHostReason`（`webfetch-url.ts:103-135`）：拦 `localhost`、`*.localhost`、`*.local`；拦非公网 IP **字面量**（v4/v6）；拒 URL 内嵌凭据；拒单标签主机名
- `assertWebFetchLiteralEgress`（`webfetch-egress-guard.ts`）在每次真实 GET 前执行：用 `ipaddr.js` 要求 `range() === "unicast"`，显式**排除**基准测试段 `198.18.0.0/15`、IPv4-mapped IPv6、NAT64/DNS64 已知前缀 `64:ff9b::/96`（解包出内嵌 IPv4 再判）、特殊用途 IPv6 段（`64:ff9b:1::/48`、`100::/64`、`2001:2::/48`、`2001:10::/28`、`2001:20::/28`）
- **域名无 DNS 预检**（源码注释明说依赖 HTTP egress proxy 兜底解析后 IP）→ **不防 DNS rebinding**

### 9.3 Sherry

[实测] `agent/tools/web_search.py:33::build_web_search_tool()`
- Tavily API（`langchain_tavily.TavilySearch`，`max_results=5`）
- 需 `TAVILY_API_KEY`（在 gitignored `.env`，不落 `sherry.jsonc`）
- **无 key 时降级为解释性 stub**：`"Web search is currently unavailable. TAVILY_API_KEY is not configured…"`
- `_arun_with_retry`：`RETRY_MAX_ATTEMPTS` 次，指数退避 + jitter（`_backoff_delay`：`RETRY_BACKOFF_MIN * 2**attempt` + `random.uniform`，上限 `RETRY_BACKOFF_MAX`），`asyncio.wait_for(timeout=WEB_SEARCH_TIMEOUT)`
- 耗尽后返回可执行的降级建议（"try a more specific query or answer without web search"）

**无 WebFetch 等价物** [实测]：Sherry 没有 URL 抓取工具。

**差异定性**：
| 维度 | Zcode | Sherry |
| --- | --- | --- |
| 搜索后端 | provider 原生（无 key 成本，但 **US-only**） | Tavily（需 key，30s timeout） |
| 无凭据降级 | 硬前置（`supportsNativeWebSearch` 不满足则无此工具） | ✅ **Sherry 更优雅**（返回解释性 stub 而非消失） |
| 重试 | runner 层 | ✅ **Sherry 显式**（3 次 + 指数退避 + jitter） |
| URL 抓取 | ✅ WebFetch（HTML→MD→小模型回答） | ❌ 无 |
| SSRF 防护 | 部分（IP 字面量 + localhost，**无 DNS 预检**） | N/A |
| 结果预算 | inline 10K / model 20K bytes | 无显式（依赖 Tavily `max_results=5`） |

**结论**：**WebFetch 是 Zcode 独有的能力**。但它的 SSRF 防护只覆盖字面量，**Sherry 若要实现必须补 DNS 预检**（不能照抄）。Sherry 的 Tavily 无 key 降级比 Zcode 的「硬前置」更友好。

---

## 十、输出截断策略

### 10.1 Zcode：文件优先 + 双层预算

[源码确认]

**Bash 特殊路径**：
- 子进程 stdout/stderr **直接以 fd 写入文件** `~/.zcode/cli/exec/<session>/<call-id>-stdout.log`，Node **从不驻留内存**
- 模型只看**头部** `BASH_MAX_OUTPUT_LENGTH` **默认 30,000 bytes**（上限 150,000）
- 持久化后包 `<persisted-output>` 信封：注明体积 + 落盘路径 + **2,000 字符预览**（`MODEL_RESULT_PREVIEW_CHARS`）
- 文件监听上限 `BASH_RUNTIME_OUTPUT_LIMIT_BYTES = 5 GiB`

**落盘实测**：存在一个 **12,078,225 bytes**（12 MB）的 stdout 日志——模型实际只看到 30 KB 头部。

**通用工具结果预算**（`executor/result-serialization.ts` + `result-content-projection.ts` + `result-persistence-format.ts`）：
```
if originalBytes > min(maxModelBytes, maxInlineBytes):
    if strategy == "artifact" and enabled:
        落盘 artifact，返回 <persisted-output> 信封 + 2,000 字符预览
    else:
        fitContentWithSuffix 截断（默认 head；Bash 用 tail）
```
字节切片用 **UTF-8 边界安全的 code point 二分**。

**逐工具预算表** [源码确认]：
| 工具 | maxInline | maxModel | 备注 |
|---|---|---|---|
| Bash | 30,000 | 30,000 | 默认值，可配到 150,000 |
| Read | — | 25,000 tokens | 256 KiB 文件上限 |
| Grep | — | 20,000 | head limit 250 |
| Glob | — | 100,000 | 100 结果 |
| Edit / Write | 1,000,000 | 100,000 | |
| WebFetch | — | 100,000 | 10 MiB 响应 |
| WebSearch | 10,000 | 20,000 | |
| TaskOutput | 32,000 chars | — | 上限 160,000，持久化阈值 100,000 chars，总预算 400,000 |
| MCP 工具 | — | 256 KiB | 部分 64 KiB / 50 KiB |
| 通用默认 | — | 100,000 | |

**通用落盘常量**：`DEFAULT_INLINE_OUTPUT_BYTES = 10 MiB`、`DEFAULT_MAX_PERSISTED_OUTPUT_BYTES = 50 MiB`。

**图片** [源码确认] `handlers/read-image.ts` + `contracts/src/tools/read.ts`：
| 常量 | 值 |
|---|---|
| `READ_IMAGE_MAX_DIMENSION` | 2,000 |
| `READ_IMAGE_MAX_BASE64_BYTES` | 5 MiB |
| 目标原始体积 | 3.75 MiB |
| token 比率 | 0.125 |
| `READ_IMAGE_MAX_INPUT_BYTES` | 20 MiB |

resize/compress 后只发 image block，尺寸保留在结构化输出里。[未验证：具体重采样算法——`imageProcessorPort` 实现未读]

### 10.2 Sherry

[实测]

**预算常量密度远高于 Zcode**（`SUMMARIZATION` 有 14 个独立 truncate 键）：
| 键 | 值 |
|---|---|
| `preemptive_truncate_ratio` | 0.70 |
| `target_truncate_ratio` | 0.5 |
| `min_output_chars_to_truncate` | 500 |
| `min_args_chars_to_truncate` | 500 |
| `truncate_budget_ratio` | 见配置 |
| `min_tool_result_tokens_to_truncate` | 见配置 |
| `truncatable_recent_skip` | 见配置 |
| `preemptive_truncate_max_chars` | 见配置 |
| `summary_total_max_chars` | 见配置 |
| `file_ops_list_max_chars` / `file_ops_section_max_chars` | 见配置 |
| `latest_user_request_max_chars` | 见配置 |

[实测] `TOOL_RESULT_EVICTION`（10 键）：`enabled`、`evict_threshold_chars`、`preview_head_lines`、`preview_tail_lines`、`eviction_subdir = 'evicted'`、`excluded_tools`

[实测] `MESSAGE_PIPELINE`：`slice_last_turn_token_max = 6,000`、`tool_output_dedup_default_protected_tools = frozenset()`

[实测] `CONTEXT_GUARD` + `TOKEN_ESTIMATION`：`chars_per_token`、`chars_per_token_cjk`、`chars_per_token_json`、`tokens_per_image_block`、`tokens_per_audio_block`、`tokens_per_video_block`（**多媒体分 token 估算**）

[实测] `PTC`：`ptc_max_stdout_bytes = 50,000`、`ptc_max_stderr_bytes = 10,000`、`ptc_max_tool_calls = 50`、`ptc_timeout_seconds = 120`

**能力矩阵**：
| 维度 | Zcode | Sherry |
| --- | --- | --- |
| 工具结果落盘 artifact | ✅ `<persisted-output>` 信封 | ✅ `TOOL_RESULT_EVICTION` → `evicted/` 目录 |
| 落盘预览 | ✅ 2,000 字符 | ✅ `preview_head_lines` + `preview_tail_lines`（**头尾双预览，比 Zcode 更清晰**） |
| head/tail 保留 | ✅（`fitContentWithSuffix`） | ✅（head + tail 双配额） |
| 子进程输出直写文件 | ✅ fd 直写，Node 不驻留 | ❌ 未验证 |
| 分层截断触发点 | 单一 ratio | ✅ **Sherry 明确更多**：4 个触发点 + 4 路由 overflow router + 抢占式 0.70→0.50 两段 |
| UTF-8 边界安全 | ✅ code point 二分 | 未验证 |
| 逐工具独立预算 | ✅ 11 个工具各有预算 | 部分（`excluded_tools` 机制存在） |
| 分页/续读提示 | ✅ `partialViewNotice` + offset | ✅ `read_file` 的 `truncated` + `hint` |
| 「未变更重读」省 token | ✅ 显式 stub | ❌ |
| 多媒体 token 估算 | 图片 0.125 比率 | ✅ **Sherry 更全**：图/音/视频各一个 |
| 消息级 tail clip | `MESSAGE_PIPELINE` 无 | ✅ `slice_last_turn_token_max = 6,000`（压缩前的无 LLM 尾裁） |

**结论**：**Sherry 的截断策略比 Zcode 更精细**（分层触发、头尾双预览、多媒体分项估算、消息级尾裁）。Zcode 唯一明确更优的是**子进程输出 fd 直写文件**（`bash-file-output.ts`）—— Sherry 的 `terminal` 工具如果把大输出驻留内存，是真实风险点。**建议核查 Sherry `terminal.py` 是否有大输出保护。**

---

## 十一、执行模型与沙箱

### 11.1 Zcode Bash：每调用一进程 + 沙箱已撤除

[源码确认]

**进程模型**：`adapters/src/exec/node-execution-adapter-*.ts`
```
spawn(shell, ["-c", "-l", command])   // 无快照时
spawn(shell, ["-c", command])          // 有快照时
```
- **无常驻 shell**：每次 bash 调用一个新进程
- shell-init 快照按 `(rootDir, dialect, shellPath)` 缓存创建，每次调用 source 一次（`bash-startup-script.ts`）
- 同时注入 embedded-search prelude 源码
- `detached: platform !== "win32"` 建进程组 → 支持进程树杀
- 超时 SIGTERM → SIGKILL，`FORCE_EXIT_AFTER_KILL_MS = 5,000`
- bash timeout 默认 120,000ms / 上限 600,000ms；adapter 默认 300,000ms

**沙箱：无** [源码确认]。这是明确结论：
- `ExecutionRequest` 契约里有 `sandbox` policy 字段，但 **Node adapter 从不消费它**（全仓库只有契约定义 + `node-execution-adapter-run.ts` 的一行注释）
- 注释原文：`"sandbox 撤除后"`（直译「沙箱撤除后」）
- `dangerouslyDisableSandbox` **只影响遥测字段** `sandboxed: input.dangerouslyDisableSandbox !== true`，实际永远无操作
- 找到的 AppArmor profile 是给 **Electron 二进制本身**的，且带 `flags=(unconfined)`
- **结论：bash 直接 exec，以用户权限运行，无沙箱**

**cwd 跨调用持久** [源码确认]：成功的前台 main-scope bash 退出码 0 → `pwd -P` 捕获 → 若在 workspace 内则保留为 `nextWorkingDirectory`；若在 workspace 外则重置为 `workspaceRoot` 并附 stderr 提示。

**后台 bash**：`run_in_background` 或自动后台（assistant mode / 超时）。stdout **每 1 秒 tail 4 KiB**（阈值 2s）作为 progress 事件；`BackgroundBashOutput` 上限 **8,192 bytes**；`TaskOutput` 工具 **100ms 轮询**，`block` 模式等待并带 timeout，完成通知作为新 turn 入队。

### 11.2 Sherry

[实测] `agent/tools/terminal.py` + `docs/sandbox/README.md`（Terminal & Python REPL confinement: env scrubbing, OS-native isolation, approval gate）+ `evals/sandbox.py`

**Sherry 明确更优的项**：
| 能力 | Zcode | Sherry |
| --- | --- | --- |
| 沙箱 | ❌ **已撤除**，直接 exec | ✅ env 清洗 + OS 原生隔离 + 审批门 |
| 常驻 shell | ❌ 每调用一进程 | ❌ |
| cwd 约束 | 越界仅「重置 + 提示」 | ✅ `PathGuard` 中间件**硬拒**越界路径 |
| 秘密处理 | 无特殊处理 | ✅ `REDACTION`（`tool_output_enabled` + `tool_output_tools` + `tool_output_prefixes`）+ `UNTRUSTED_OUTPUT` |

** Sherry 需核查项**：`agent/tools/terminal.py` 对大输出的处理。Zcode 的 fd 直写文件是其明确优势（`BASH_RUNTIME_OUTPUT_LIMIT_BYTES = 5 GiB` 兜底），若 Sherry 的 terminal 把输出读进内存再截断，是潜在内存风险点。`TOOLS_TIMEOUTS` 有 `TERMINAL_TIMEOUT = 30`（秒），未见独立输出字节上限配置键。

### 11.3 Zcode workflow 的 `vm` 沙箱（唯一的真隔离）

这是 Zcode 唯一做了真隔离的地方，详见 §12.3。但需注意一个**未验证的逃逸面** [行为推断]：

Node 官方文档明确 `vm` **不是**安全沙箱。Zcode 的 sandbox 对象暴露了一个**外部 realm 的函数** `sandbox.__send`（`dynamic-workflow-runtime/src/child-source.ts:271-273`）。经典 vm 逃逸形式 `__send.constructor("return process")()` 理论可达外部 realm。**未执行验证**。

实际威胁模型是「模型生成的代码 + 用户批准的图与命令集」，不是敌对第三方代码，因此这可能是已接受的风险。但**结论应表述为「containment，非加固的安全边界」**。

---

## 十二、Workflow 编排（Zcode 独有的最大差异）

### 12.1 首要发现：两代系统并存

[源码确认] 首轮对标把两代混为一谈。实际是**两套独立系统共存**：

| | **Legacy "script workflow"** | **New "dynamic workflow"** |
| --- | --- | --- |
| 工具 | `Workflow` / `/expert` | `CreateWorkflow`、`AmendWorkflow`、`SaveWorkflow`、`EvalWorkflowSnippet`、`ListWorkflowRuns`、`GetWorkflowRun`、`ResumeWorkflowRun`、`ResolveWorkflowQuestion`、`ListSavedWorkflows`（**9 个**） |
| 源码 | `bootstrap/src/app/script-workflow-*` | `dynamic-workflow/` + `dynamic-workflow-runtime/` + `bootstrap/src/app/dynamic-workflow-*` + `workflow-driver*` |
| 数据表 | `workflow_run`、`workflow_activity`、`workflow_event`、`workflow_definition` | `dwf_run`、`dwf_actor`、`dwf_node`、`dwf_event` |
| 沙箱 | Node 子进程 + `new AsyncFunction` + 全局覆写 | Node 子进程 + **`vm.createContext` realm** |
| 作者 API | `agent` / `parallel` / `pipeline` / `phase` / `log` / `args` / `budget` / `workflow` | `agent().ask<T>()`、`files.*`、`git.*`、`world.run`、`phase`、`log`、`report`、`artifact.*`、`args` |

`EvalWorkflowSnippet` 属**新**一代。`bundled-skills/skills/dynamic-workflows/SKILL.md` 明说：「`CreateWorkflow` 是 dynamic-workflow 工具……legacy `Workflow` 工具和 `/expert` 是另一个更旧的、恰好同名的特性」。

### 12.2 执行管线（新一代）[源码确认]

1. **作者写 TypeScript**
2. **类型检查**：对内嵌 `.d.ts` facade 检查（`types: []`、`lib: "ES2022"`）——`import`、`process`、`fetch`、`require`、`Date.now`、`Math.random` 编译失败
3. **lowering**（`dynamic-workflow/src/lowering/lower.ts`）：剥离类型 + 注入 site id → 输出唯一自由标识符为 `__host` 的 async 函数体。例：`planner.ask<Plan>(t)` → `__host.ask("ask#3", planner, t)`
4. **落盘**为 ESM 入口 `<cwd>/.zcode/workflow-runs/<runId>.mjs`（`child-entry-file.ts`，tmpdir 兜底）
5. **子进程执行**（`dynamic-workflow-runtime/src/harness.ts:261-276`）：
```
spawn(process.execPath, ["--max-old-space-size=256", entryPath],
      { cwd, stdio: ["pipe","pipe","pipe"],
        env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" } })
```

### 12.3 隔离边界 [源码确认]

`child-source.ts::childMain`（`:260-314`）：
```ts
deps.vm.createContext({ __argsJson }, { name: "workflow-sandbox" })
vm.runInContext(BOOTSTRAP, sandbox)        // 装 __host / __deliver / __execute
vm.runInContext("(async (__host) => {\n" + payload.lowered + "\n})", sandbox)
```

**三层隔离**：
1. **进程边界** — 独立 Node 进程，NDJSON over stdio 通信（`protocol.ts` 是线路契约）
2. **`vm` realm** — 脚本只见 ES intrinsics + 注入的 `__host`。**无** `process` / `require` / `Buffer` / `fetch` / `console`，无文件系统/网络。动态 `import()` 抛 `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`。`eval` / `Function` 绑定同一 context
3. **确定性禁令**（`:227-247`）— `Date.now()`、无参 `new Date()`、`Math.random()` 运行时抛错

**宿主 API**（`:163-173`）：`__host` = `args`（frozen JSON）、`createActor`、`ask`、`worldRead`、`publishArtifact`、`declareArtifact`、`report`、`log`、`enterPhase`

**作者侧能力映射**（`facade/dts.ts` + `lowering/lower.ts`）：
- `files.glob/read/grep` — **只读**、workspace 作用域、超限**拒绝而非截断**
- `git.changedFiles/diff/status/log` — **固定 allowlist argv 数组，从不 shell 字符串**
- `world.run(cmd, args?, opts?)` → `{exitCode, stdout, stderr}`。**cmd 必须是编译期字符串字面量**并被收集进该 run 的「已批准命令集」（`collectWorldRunCommands`）；`workflow-world-read.ts::worldRun` **fail-closed**——声明集缺失或 cmd 不在其中 → `DriverError`
- `ask` / `agent` — 沙箱内不执行任何东西，round-trip 回父 driver，由 driver 生成真实 agent session

### 12.4 限制与常量 [源码确认]

| 项 | 值 | 位置 |
|---|---|---|
| 堆上限 | `DEFAULT_MAX_OLD_SPACE_MB = 256` | `harness.ts:182` |
| **真实 run 的 wall-clock 超时** | **无**（`dynamic-workflow-run-launch.ts:325-368` 只传 `signal`/`control`，**不传 `timeoutMs`**）→ 跑到完成/停止/出错为止 | 同左 |
| `EvalWorkflowSnippet` 超时 | 必填 `timeoutMs` | `dynamic-workflow-snippet.port.ts:24` |
| `world.run` 默认超时 | 300,000 ms（无上限） | `WORLD_READ_CAPS.runDefaultTimeoutMs` |
| `world.run` 输出上限 | stdout / stderr 各 256 KB | — |
| 并发上限公式 | `max(1, min(16, availableParallelism() - 2))` | `workflow-concurrency-ceiling.ts` |
| 进程级 governor | 额外按 `provider/model` 桶限流 | — |
| report 上限 | ≤256 项/run，≤32 KB/项 | `facade/dts.ts:88-89` |
| artifact 上限 | 32 ids/run，16 版本/id，20 MiB/文件，256 KB/markdown | — |
| `MAX_ESCALATIONS_PER_ASK` | 3 | `workflow-driver-helpers.ts:84` |
| `REPAIR_ATTEMPTS` | 3 | `engine/types.ts:941-944` |
| `NUDGE_ATTEMPTS` | 1 | 同上 |
| Legacy `MAX_WORKFLOW_AGENT_CALLS` | 1000（唯一硬上限） | `script-workflow-runtime.ts:44,243` |

**⚠️ 重大注意**：真实 workflow run **没有 wall-clock 超时**，且 `spent_tokens` **不是预算**（见 §12.6）。即 Zcode 的 workflow 可以无限跑、无限烧 token。

### 12.5 状态机与持久化 [落盘实测 + 源码确认]

**legacy 物理状态**（`sqlite_master` DDL）：
- `workflow_run.status` check：`pending | running | paused | completed | failed | cancelled`；另有 `budget_total`、`budget_spent`、`current_phase`、`parent_session_id`
- `workflow_activity.status` check：`queued | running | completed | failed | skipped | cancelled | cached | lost`；有 `child_session_id REFERENCES session(id)`、`attempt`、`call_path`、`call_index`、`input_hash`

**新一代物理 vs 逻辑状态**：
- 物理（DDL）：`dwf_run.status` check `pending | running | completed | failed | cancelled`
- 逻辑（`engine/types.ts:458,466`）：`RunStatus = pending | running | completed | errored | stopped`；`RunStopReason = user | model | provider | interrupted | superseded`
- **映射**（`dwf-journal-codecs.ts`）：`stopped{reason,error}` → 物理 `cancelled`（信封在 `failure_json`）；`errored{error}` → 物理 `failed`。解码反向；物理 `failed` + code `Interrupted` → `stopped(interrupted)`。migration 0020 注释确认：「stopped / errored 共享物理 failed，靠 failure_json 的 code 在 SQL 里分清」
- `dwf_node.status` check：`running | completed | failed`；`kind ∈ ask | world-read | world-run | report | artifact`；`unique(run_id, site_id, ordinal)`

**所有 8 张 workflow 表本机 0 行** [落盘实测]。

### 12.6 关键机制对比：checkpoint 语义

**新一代 = journal 重放** [源码确认]：
- 节点在 **admission 时**写 `running`（带 `actorSeq` + `inputHash`），settle 时更新为 `completed`/`failed`（`NodeRecord` 文档，`types.ts:778-786`）
- resume 时 `AskScheduler.admitAsk`（`scheduler.ts:116-174`）按 `(siteId, ordinal)` 读节点：
  - `completed` / `failed` → **从 journal 短路**（`releaseCachedAsk`，**不调 driver**）。带防御性 `inputHash` 校验，不符则整个 run 失败
  - `running`（中途崩溃）→ 按记录的 `actorSeq` **重新 dispatch**
- actor session 从 store 重建（`run-launch.ts:412-437`）；`mintActorSessionId` 由 `(runId, actorRef)` 确定性推导
- `replayRunProgress`（`dynamic-workflow-run-replay.ts`）从 `dwf_event` 重建 UI 投影，**row 是终态权威**
- **Amend-resume** 用 `ImportedRunCache`（按 actor 名 + per-actor ask 序号 + `inputHash`）；一旦有 live subagent 写工作区（`askMutating`）或 live `world.run` 执行就**关闭导入缓存**，之后下游真跑

**legacy = 重跑脚本 + per-activity 缓存** [源码确认]：
- `ScriptWorkflowRuntime.resume`（`script-workflow-runtime.ts:192-212`）用 `resumeFromRunId` 重跑，脚本从头执行
- 每次 `agent()` 查 `findCachedScriptWorkflowActivity({runId, callPath, inputHash})`，命中则返回缓存信封并把 activity 标 `cached`

**与 Sherry 的对比** [源码确认 vs 实测]：

| 维度 | Zcode 新一代 | Sherry taskflow |
| --- | --- | --- |
| 状态持久化 | ✅ `dwf_*` SQLite | ✅ `task_flows` SQLite WAL（**修正首轮误判**） |
| 进程重启恢复 | ✅ journal 重放 | ✅ 读得到非终态 flow |
| 已完成步骤是否重跑 | ❌ **不重跑**（journal 短路） | ⚠️ step 是 `dispatched` 可重入态；`depends_on` 满足即重注入，**已 `done` 的 step 由 DAG 依赖判定跳过** |
| 显式挂起 | ✅ `paused`（legacy）/ `stopped(user)`（新）→ 被后继 run `superseded` | ⚠️ `set_waiting` / `wait_all` 是**阻塞等待**语义，非用户主动挂起 |
| 血缘指针 | ✅ `dwf_run.resumed_from` | ❌ 无 |
| 步骤级缓存 | ✅ `cached` 状态 + `input_hash` | ❌ 无 |
| 步骤级重试计数 | ✅ `activity.attempt` | ✅ `STEP_JUDGE.max_retries = 2`（但语义是 judge 重试，非执行重试） |
| 单步失败级联 | ✅ `skipped` | ✅ **Sherry 明确更完整**：`failed` 依赖者保持 `blocked`（**绝不被失败解锁**），`failed`/`skipped` 依赖级联 `skipped`（`config.py:24-28`） |
| 节点上限 | ❌ **无**（migration 0020 明说「没有节点上限 / token 预算列」） | ✅ **Sherry 独有**：`ITERATION_BUDGET` + `TASKFLOW_INFRA` |
| 花费预算 | ❌ **无强制**。`spent_tokens` 注释原文「观察面，不是控制面」；`dwf_run` 无预算列。legacy `budget.total/spent/remaining` 也是**建议性**，唯一硬上限是 `MAX_WORKFLOW_AGENT_CALLS = 1000` | ❌ 同样无（见 §0.2） |

**结论（修正后）**：Zcode workflow 的真正优势收窄为三点：
1. **journal 重放不重跑已完成步骤**（比「DAG 依赖跳过」更彻底——连 driver 都不调）
2. **`resumed_from` 血缘 + `superseded` 取代语义**（可追溯的多版本演进）
3. **`cached` + `input_hash` 的步骤级结果缓存**

而 Sherry 在**失败级联语义**和**节点上限**上明确更完整。

### 12.7 workflow 与 subagent 的关系 [源码确认]

**新一代**：`agent()` → `createActor` → driver 创建**持久 child AgentRuntime session**（真实 zcode session，`ensureSessionPersistedForExternalAction` + `session_task_link` role `workflow_actor`）。
- **关键安全洞**：沙箱只限制**编排脚本**；它生成的 subagent 跑**完整 agent 栈**，拥有全部常规工具（读/搜/编辑/执行命令），仅受 `PathGuard` 约束
- 同一 actor 上的 ask **FIFO 串行**；`Promise.all` 是真正的 join/barrier
- typed ask 要求子 agent 调 `submit_result` 工具，engine 校验（Tier-1 schema gate）
- `context_exceeded` 只失败该节点（可 catch）

**无文件锁** [源码确认]：多个 actor/subagent 可并发编辑同一工作区；workflow 与交互 agent 可并发。

### 12.8 失败处理 [源码确认]

| 情形 | 结果 | 可 resume |
|---|---|---|
| 脚本 throw | `errored` | ❌ 须 `AmendWorkflow` 编辑 |
| 宿主故障（沙箱崩溃 / 超时 / 协议损坏 / spawn 失败） | `stopped(interrupted)` | ✅ |
| 模型**瞬时**错误（限流/过载/网络/5xx/`invalid_model_response`/未知业务码） | runner 内**无上限重试**，退避表现为 `askWaiting{cause:"backoff"}` | ✅ |
| 模型**确定性**失败（auth / provider 未配置 / 模型不存在 / 400 / 422 / 配额码） | `stopped(provider)` | ✅ |
| typed ask schema 不符 | 同 turn `reject` 修复，≤3 次 | — |
| turn 结束未提交 | nudge，≤1 次 | — |
| `world.run` 非零退出 | **resolve**（是一个值） | — |
| `world.run` spawn 失败/超时/超限 | **reject，可 catch** | — |
| git 失败 | reject，可 catch（`DriverError`） | — |

**无回滚** [源码确认]：部分副作用（subagent 已改的文件、已执行的命令）**不回滚**。`facade/dts.ts` 的 `world.run` 文档明确：resume 是**崩溃恢复，不是重新验证**。

---

## 十三、并发与调度

### 13.1 Zcode：批内并发 + workflow run 级上限

[源码确认] `core/src/tool/scheduler.ts`

`DEFAULT_MAX_CONCURRENCY = 10`（`:48`），可配 `toolConcurrency.maxConcurrency`（`contracts/config/index.ts:342` 默认 10）。**本机 `setting.json` 未覆盖**。

**`canRunInParallel` 判定**（`:85`）：
```
destructive == true        → 阻止
concurrentSafe == true     → 允许
readOnly 或 sideEffectScope == "none" → 允许
否则                       → 阻止
```

`READ_ONLY_TOOLS`（`:233`）：`Read`、`Glob`、`Grep`、`WebSearch`、`WebFetch`、`TodoRead`、`TodoWrite`、`AskUserQuestion`、`Skill`

**调度**（`:187`）：拓扑排序 → `groupByParallel` → 按 `maxConcurrency` 切分并行组。

**执行**（`executor/batch-runner.ts`）：
- `executeToolBatch:24` — `Promise.all`，按 maxConcurrency 分块
- `executeToolSchedule:56` — **顺序**遍历 `parallelGroups`，发 `batch_start` / `batch_complete`；任一批返回 `turnControl.stopTurnAfterResult` → **剩余组全部 `ToolCancelled`**（`:92-128`）

**workflow run 级** [源码确认]：`Caps { maxConcurrency }` 提交时固定，存 `dwf_run.caps_max_concurrency`，可热调（`WorkflowEngine.setMaxConcurrency`，`engine.ts:579-589`）。上限公式 `max(1, min(16, availableParallelism()-2))` + per-provider 进程级 governor。

**未找到的能力** [源码确认]：
- **无 delegation depth 计数器**。`AgentProfile` 无 depth 参数；`general-purpose` 的 `tools: ["*"]` 意味着子 agent **理论上可再调 Agent 递归**，无 depth guard [行为推断]
- **无 per-session subagent 并发信号量**。并发只受工具批内 10 间接约束

### 13.2 Sherry：4 类 lane + 启动校验

[实测] `runtime/lane/`（`core.py`）：

| Lane | 约束对象 | 默认 | 配置键 |
|---|---|---|---|
| `MAIN` | 主 agent turn（`_run_executor`） | `min(16, max(8, CPU))`，clamp 上界 `SUBAGENT + NUDGE`（12）→ 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | 子 agent 执行（spawn + steer） | 8 | `subagent_max_concurrent` |
| `NUDGE` | nudge/persistence（3 处） | 4 | `nudge_max_concurrent` |
| `NESTED` | `sessions_send` 回复轮（串行） | 1 | `nested_max_concurrent` |

- 合计 25 槽位
- `validate_lane_config()` **启动时校验** `main >= subagent + nudge`，所有 limit ≥ 1
- `install_lane_lifecycle()`（`server/service/lane_lifecycle.py`）校验 + 预热 + 注册 `set_drain_check(is_gateway_draining)` + 有界退出 drain（`atexit`, `drain_all(timeout=0)`，绝不阻塞退出）
- **超限不拒绝，而是排队**：`asyncio.Semaphore` + active/queued 计数器，FIFO 等待
- `PENDING → RUNNING` 在 lane slot **内**转移，此时才 stamp `started_at`（排队等待不算运行时间）
- 观测：`GET /lane-status` → `{main|subagent|nudge|nested: {name, max_concurrent, active, queued}}`
- `LaneManager.set_concurrency` 热更新只影响新 acquire
- ⚠️ `asyncio.Semaphore` 绑定 event loop，跨 loop acquire 会告警并 rebind（新信号量扣除未完成槽位，绝不重复发 permit）

### 13.3 差异定性

| 维度 | Zcode | Sherry | 谁更优 |
| --- | --- | --- | --- |
| 并发模型 | 单一数字（10），批内 | **4 类语义 lane**，跨类别 | **Sherry** |
| 超限行为 | 批内切块 | **排队不拒绝**（FIFO） | **Sherry** |
| 启动校验 | 无 | ✅ `main >= subagent + nudge` | **Sherry** |
| 排队时间与运行时间分离 | 无此概念 | ✅ `started_at` 在 lane slot 内 stamp | **Sherry** |
| 优雅退出 | 无 drain 概念 | ✅ `atexit` + `drain_all(timeout=0)` | **Sherry** |
| 运行时可观测 | 无 `/lane-status` 等价 | ✅ HTTP 端点 | **Sherry** |
| 单工具并行安全标注 | ✅ `destructive` / `concurrentSafe` / `sideEffectScope` 三元标注驱动调度 | ⚠️ 靠 `TOOL_GUARDRAILS` 运行时统计（15 键阈值），非静态标注 | **Zcode** |
| 并发上限热调 | ✅ `setMaxConcurrency` | ✅ `set_concurrency` | 平 |
| delegation 深度限制 | ❌ **无**（`general-purpose` 可递归） | ✅ 默认 2 / 硬上限 2 | **Sherry** |
| per-session subagent 信号量 | ❌ 无 | ✅ SUBAGENT lane | **Sherry** |
| workflow 内部并发上限 | ✅ 独立 `maxConcurrency` + 公式上限 + per-provider governor | 无对应（taskflow 无并发维度） | **Zcode** |

**结论**：**并发调度是 Sherry 明确领先**。Zcode 唯一领先的是**工具并行安全性的静态标注**（`destructive` / `concurrentSafe` / `sideEffectScope`）—— Sherry 的 `TOOL_GUARDRAILS` 是运行时反应式（同一工具连续失败 N 次才阻断），不是调度前判定。**把工具并行安全标注补上，是一个低成本高收益的借鉴项。**

---

## 十四、多 agent 协调

### 14.1 Zcode

[源码确认] `core/src/subagent/` + `core/src/runtime-task/`

**spawn 路径**：
```
Agent 工具 handler (tool/handlers/agent.ts)
  → context.subagentPort.launch (subagent/runner.ts:131 createExploreSubagentPort)
  → 分流：前台 port.run / 后台 port.start（依 run_in_background 或 profile.background）
  → createSubagentLifecycle
      childSessionId = createSessionId(`subagent_${agentId}`)
      临时输出目录 metadata.json / output.txt / task.output
  → child session 持久化，parentSessionId 指向主 session，task_type = subagent_child
  → runAgentToCompletion → options.runExploreAgent
      allowedTools = resolveAllowedTools(profile)
      sessionId = child, maxTurns = profile.maxTurns
      permissionMode, systemPrompt, registerMessageSink
```

**落盘实测**：`session.parent_id` 有 **7 个** `subagent_child` 会话，各指向一个主 `interactive` 会话。

**profile 定义**（`subagent/profile.ts`，frontmatter 解析见 `profile-model-selection.ts`）：
`name`、`description`、`modelSelection`、`color`、`permissionMode`、`maxTurns`、`memory`、`tools`、`disallowedTools`、`skills`、`background`、`injectAgentsMd`、`mcpServers`

**内置 profile 的工具集**：
- `Explore`：`Bash`、`Glob`、`Grep`、`Read`、`WebFetch`、`WebSearch`、`TodoWrite`（**无 Agent**，故不递归）
- `general-purpose`：`["*"]`（**含 Agent**，理论可递归，无 depth guard）
- plan mode 工具对**所有**子 agent 强制 disallow（`subagent/tool-policy.ts`）

**子 agent prompt 分层**（`subagent/context-builder.ts`）：CLI prefix + agent prompt + subagent notes + environment + skills

**结果返回**：
- 前台 → 直接作为 Agent 工具结果
- 后台 → `task-notification` 文本（`runtime-task/notification.ts`，`TASK_NOTIFICATION_MAX_CHARS = 120,000`）经 `enqueueParentTaskNotification` 入父 command queue（同优先级可批量合并）
- `AgentCompletedOutput { content[], totalToolUseCount, totalDurationMs, totalTokens, usage }`
- Agent 工具结果预算 `maxInlineBytes` / `maxModelBytes` = 120,000（`handlers/agent.ts`）

**协调机制** [源码确认] `runtime-task/registry.ts`：
- 内存 task store + 邮箱（`pendingMessages`、`queueMessage` / `drainMessages`、`waitForTerminal` / `waitForBackgroundRequest`）
- `TaskOutput` 读 registry / 输出文件；`TaskStop` 停止；`SendMessage` 用 `delivery: "guide"` steer（`message-steering.ts`，`no_active_turn` 时重试 20×10ms）或在下一个工具轮次排队，**可复活已终止的 agent 到后台**

**`delivery_compaction` 不存在** [源码确认]：源码中找不到该术语。最接近的机制是结果预算 `artifact` 策略 + preview head、120k 通知截断、tool_result artifact 持久化。首轮若引用过该术语，应视为误记。

### 14.2 Sherry

[实测] `agent/tools/subagent/`，7 个运行时工具：`sessions_spawn`、`sessions_yield`、`sessions_send`、`sessions_kill`、`sessions_steer`、`agents_list`、`subagents_list`

**Sherry 明确更优的项**：
| 能力 | Zcode | Sherry |
| --- | --- | --- |
| delegation 深度限制 | ❌ 无 | ✅ 默认 2 / 硬上限 2（MAIN → ORCHESTRATOR → LEAF） |
| 角色模型 | 单轴（profile） | ✅ **两轴**：MAIN/ORCHESTRATOR/LEAF + functional role（general/researcher/executor/reviewer/librarian） |
| 工具最小权限 | profile `tools` / `disallowedTools` | ✅ `inherited_tool_policy.py` + `privilege.py` + spawn-privilege 守卫 |
| 并发信号量 | ❌ 无 per-session 限制 | ✅ SUBAGENT lane（8）+ 每父准入 + 池满转 `PENDING` |
| 排队/运行时间分离 | 无 | ✅ `PENDING → RUNNING` 在 lane slot 内，`started_at` 在此 stamp |
| 崩溃恢复 | `RuntimeTaskRegistry` 内存态 | ✅ registry SQLite + sweeper 恢复孤儿 + `pending_orphaned` |
| 完成门 | `CompletionJudge` | ✅ **四层门**（见下） |
| 子 agent 能力注入 | profile frontmatter | ✅ 15 个 code_intel 工具 + ast_grep + LSP 可按角色下放 |

**Sherry 四层完成门** [实测] `AGENTS.md`：
1. Tier 1 schema gate — step 的 `response_schema` 在 `taskflow_resume` 校验，miss 是 retry 信号（`schema_error`），不花 judge 调用；未重试的 miss 标记 step failed
2. `StepJudge` — `step_judge.py`，`taskflow_resume` 对带 criteria 的 step（`max_retries = 2`）；Tier 1 pass 不跳过它；调用方声明的 `step_outcome` 高于两者
3. `CompletionJudge` goal loop — 每次 spawn，由 `COMPLETION_JUDGE["goal_max_turns"] = 5` 约束
4. Evidence ledger — 终态/`python_repl` 自动记录 + 文件编辑 stale 事件；读侧 `evidence_collector.py` 推导 staleness
5. `taskflow_finish` gates A–D — DAG 完整性、无未决 step（`blocked`/`failed`/`skipped`/`cancelled` 按 id + 原因上报）、无 FAIL/`[stale]` evidence、SisyphusVerifier（仅当有 todo + plan_path）
6. `SubagentCompletionDrainMiddleware` — 无条件程序化门

**对比 Zcode 的完成判定** [源码确认]：Zcode 侧对应物是 `subagent/completion-notification.ts` + `runtime-task/registry.ts` 的 `waitForTerminal`，即**纯程序化终态等待**（工具轮次耗尽 / `maxTurns` 达上限 / `stopReason`），**无 LLM judge**。typed ask 的 Tier-1 schema 校验是 workflow 侧独有，subagent 侧无。

**结论**：**subagent 完成判定是 Sherry 明确领先**（4–6 层门 vs Zcode 的程序化终态等待）。Zcode 领先的是**运行时协调原语**（`SendMessage` 可复活已终止 agent 到后台、`TaskOutput` 阻塞等待、内存邮箱队列）—— Sherry 的 `sessions_steer` + `sessions_send` + EventBus announce 覆盖了大部分，但**没有「复活已终止 agent」这一项**。

---

## 十五、Sherry 独有优势汇总（反向对标）

上一份文档只列 Zcode 优点，本节反向列出 Sherry 明确更强或 Zcode 完全没有的项，避免单向叙事导致误判。

### 15.1 代码智能（Zcode 完全没有）

[实测] Sherry 四层检索：

| 层 | 配置 | 能力 |
|---|---|---|
| 符号索引 | `CODE_INTEL` 13 键 | tree-sitter，7 语言（`.py` `.ts` `.tsx` `.js` `.jsx` `.rs` `.go`），`call_graph_max_depth = 3`，`explore_max_symbols = 10`，`fuzzy_min_score = 0.3`，`index_max_files = 5,000`，`prune_dirs` 11 项 |
| 语义检索 | `CODE_INTEL_SEMANTIC` 8 键 | `bge-m3`，`default_top_k = 5` / `max_top_k = 20`，候选池 40，`max_chunk_chars = 2,000`，`max_chunks = 1,000` |
| 结构化搜索 | `AST_GREP` 12 键 | 23 语言，strictness 默认 `smart`，**sha256 固定的 0.43.0 发行版**（6 平台），超时 30,000ms |
| LSP 精度 | `LSP` 21 键 | 4 语言 server + 请求/启动超时 |

Zcode 侧对应物：`~/.zcode/` 下有 `@colbymchenry/codegraph` 类的**项目级索引**（`.codegraph/codegraph.db`）但那是**本项目开发工具**，不是 Zcode 产品能力。Zcode 产品的搜索只有 ripgrep（正则）+ bfs/ugrep（文件名）。**无符号索引、无 AST、无 LSP、无语义检索。**

### 15.2 安全姿态（Zcode 明确更弱）

| 能力 | Zcode | Sherry |
|---|---|---|
| Bash 沙箱 | ❌ **已撤除**（注释「sandbox 撤除后」），直接 exec | ✅ env 清洗 + OS 原生隔离 + 审批门 |
| 路径约束 | cwd 越界仅「重置 + stderr 提示」 | ✅ `PathGuard` 硬拒越界 |
| 输出脱敏 | 无 | ✅ `REDACTION`（3 键）+ `UNTRUSTED_OUTPUT`（2 键，含 `advisory` 模式） |
| 重复输出检测 | 无 | ✅ `REPETITION_GUARD`（11 键：`max_identical_outputs`、`internal_repeat_ratio`、`internal_min_lines`） |
| 工具失败熔断 | 无 | ✅ `TOOL_GUARDRAILS` 15 键（`exact_failure_warn_after` / `block_after`、`same_tool_failure_warn_after` / `halt_after`） |
| 崩溃环检测 | 无 | ✅ `CRASH_LOOP`（300s 窗口 / 3 次熔断 / 1h 保留） |
| 上下文超限熔断 | 无（`context_exceeded` 仅记账） | ✅ `TOKEN_GUARD` 128K 硬地板（启动/build/spawn/env write 四处） |
| 心跳陈旧检测 | 无 | ✅ `HEARTBEAT_STALENESS`（`stale_cycles_idle = 7` / `in_tool = 20`） |
| 第三方 skill 安全扫描 | ❌ 插件 hook **无条件执行**（`canRunPluginHooks` 直接 `return true`） | ✅ SkillSpector（静态 YARA/规则 + 可选 LLM 语义分析） |
| 项目 hook 信任门 | ✅ digest-based trust coordinator | N/A（无用户 hook） |
| WS 流诊断头 | 8 个 `stream_diag_headers` | — |

**Zcode 侧的三处具体风险** [源码确认]：
1. **插件 hook 无信任门** — `adapters/src/plugins/index.ts:366` 的 `canRunPluginHooks` 无条件 `return true`，第三方插件的 `hooks/hooks.json` 以用户权限直接执行任意进程
2. **Bash 无沙箱** — `sandbox` policy 在契约中存在但 adapter 从不消费
3. **hook 以用户权限执行任意进程** — 经 `ExecutionPort.run`（shell/argv 两模式），stdin 灌 JSON，无沙箱

### 15.3 多模态与语音（Zcode 缺）

[实测] Sherry：
- ITTT（图像理解）/ VTTT（视频理解）/ STT（FunASR 本地语音识别）/ TTI（文生图）/ 视频转文本
- `MEDIA_PIPELINE`：`max_media_bytes = 20 MiB`、`multimodal_temp_retention_days = 7`、`main_llm_native_multimodal = 'auto'`、`main_llm_silent_degradation_detection = True`
- 文档解析：MinerU 多模态文档摄入进知识图谱 RAG
- `TOKEN_ESTIMATION`：图/音/视频**各有独立 token 换算**
- 语音 daemon（`SKILLS_TOOLING` 6 个语音相关键：host/port/ready_wait/liveness_timeout/http_timeout/server_host）

[源码确认] Zcode 有图/视频/PDF 输入（`Read` 工具路由 jpeg/png/gif/webp/mp4/mov/webm/pdf），但**无语音** [未验证：是否有 STT]。

### 15.4 记忆与知识（Zcode 薄）

| 维度 | Zcode | Sherry |
|---|---|---|
| 短期记忆 | SQLite `session` / `message` / `part` | ✅ MesMemory SQLite WAL + **FTS5**（含中文 **trigram 分词表**）+ 向量语义检索 |
| 事件日志 | 无 append-only 事件表 | ✅ `context_engine/events/` append-only + projector |
| 长期记忆 | Markdown 文件（`~/.zcode/cli/memories/projects/` 20 文件 + MEMORY.md 索引），agent 自维护 | ✅ MesMemory + `MEMORY_TOOL`（`memory_char_limit` / `user_char_limit` / `facts_char_limit`）+ curator 自动整理 |
| 自动整理 | 无 | ✅ `CURATOR_DEFAULTS` 9 键（`default_interval_hours = 120`、`stale_after_days = 30`、`archive_after_days = 90`、`default_consolidate`、`min_idle_hours = 2`、`interval_override_min/max_days`） |
| 经验提取 | 无 | ✅ **四条生命周期路径**：压缩时记忆复盘、todo 全完成时的计划抽取、压缩前记忆 flush、压缩后 todo fork |
| 知识图谱 RAG | 无 | ✅ LightRAG + RAG-Anything + `snkv` 向量存储 + 4 套 RAGAS 评测 |
| 跨会话连续性 | `resume_goal_state` reminder | ✅ `build_continuity_prompt` + `session_continuity` |

### 15.5 记忆 flush（Zcode 无）

[实测] `MEMORY_FLUSH`：`enabled = True`、`soft_threshold_tokens = 8,000`、`force_flush_chars = 50,000`、`output_max_tokens = 2,048`、`timeout_seconds = 30`、`model` 可指定。

Zcode 无「压缩前主动把记忆落盘」的机制——压缩即摘要，摘要即丢失。

### 15.6 可靠性与运维（Zcode 缺）

[实测] Sherry：
| 配置 | 键与值 |
|---|---|
| `LLM_RETRY` | `max_retries`、`base_delay`、`max_delay`、`jitter`、`stale_giveup_threshold` |
| `RETRY_BACKOFF` | 6 键（jittered base/max/jitter + backoff_floor + **adaptive** base/max） |
| `PERIODIC_BACKOFF` | `factor`、`max_interval_s`、`max_consecutive_failures` |
| `CRON_SERVICE` | 7 键（`min_every_ms`、`max_run_history = 20`、`degraded_threshold`、`disabled_threshold = 10`、双层退避 base/max） |
| `HEARTBEAT_SERVICE` | 8 键（含 `backoff_factor`、`backoff_max_interval_s`） |
| `WS_STREAM` | `max_continuation_retries = 4`、`max_reasoning_only_retries = 2`、`drain_error_backoff_s = 1.0` |

Zcode 侧对应：`RETRY_BACKOFF`/`PERIODIC_BACKOFF`/`CRASH_LOOP`/`HEARTBEAT_STALENESS` 均无。Zcode 的 hook `failed` 状态与 `modelRetryBudget` 是局部机制，**无系统级退避熔断**。

### 15.7 集成面（Sherry 更宽）

[实测] Sherry：
- `CHANNELS` + `plugins/channels/qq/`（QQ 机器人适配器）+ `bus/core.py` 消息总线
- `GATEWAY`：`allowed_origins` **6 项精确枚举**（tauri 3 种 + localhost 3 种），`require_token = False`（本机）
- `HTTP_UPLOAD`：图/音/视频各一个独立上限
- `INPUT_QUEUE`：见 §4
- 多语言 persona 模板（en/zh/ja/ko）+ `WORKSPACE_TEMPLATE_LANG` 懒同步（`file_sync.py`，只补缺失不覆盖用户编辑）
- 四语言 README parity gate（`scripts/check_docs_parity.py`）
- 导入契约强制（`lint-imports` 6 条跨包禁令 + 2 个叶子模块 seam）

[源码确认] Zcode 有 Feishu 轮询字符串证据（`pollFeishuAppRegistration` / `botDeliveryTarget`）但**本机未配置** [落盘实测：bot 表 0 行]。

### 15.8 工程质量（Sherry 独有）

[实测] Sherry：157,469 LOC 源码 / 111,684 LOC 测试（460 文件 / 5,368 用例）/ 5,368 用例；`run_tests_split.py` 三进程分组 runner；`evals/` 6 套评测（graph_rag / subagent / long_running_task / session_memory / nudge_extraction / 沙箱写入重定向）；pre-push 跑 basedpyright 全 diff；pre-commit 强制 snake_case + commitlint angular。

Zcode 侧无可见测试资产（闭源分发，源码树中未见对应测试目录）。

---

## 十六、不确定性声明

1. **无法运行 Zcode**：`zcode --help` 因 `/opt/ZCode/chrome-sandbox` SUID 配置错误直接 abort（需 root 修复）。**所有 Zcode 行为结论均为静态取证，无运行时验证。** 精确报错：
   ```
   [8154:0930/215022.500097:FATAL:sandbox/linux/suid/client/setuid_sandbox_host.cc:166]
   The SUID sandbox helper binary was found, but is not configured correctly.
   Rather than run without sandboxing I'm aborting now. You need to make sure
   that /opt/ZCode/chrome-sandbox is owned by root and has mode 4755.
   ```
2. **源码版本与发布版可能不一致**：`/home/honor/Desktop/project/ZCode` 的 `apps/zcode-cli` 版本为 **0.16.9**，而运行的是 Zcode **v3.14.4**。源码树可读且行号精确，但**不能保证与发布构建逐行一致**。交叉验证：压缩阈值 966,000 与落盘 `autoCompactThreshold` 精确吻合（1,000,000 − 21,000 − 13,000），说明源码常量与发布行为一致；但这是唯一一处有强交叉验证的项。
3. **workflow / bot / off-peak 表本机 0 行** [落盘实测]：8 张 workflow 表、bot 表、off_peak 表全部 0 行。能力存在于源码与 schema，**实际运行行为未验证**。§12 的所有结论基于源码逻辑推断。
4. **`session_input` 无 `guide` 记录** [落盘实测]：本机 214 行只用 `startNow` 与 `queue`，`guide` 模式代码存在但从未在本机触发，其真实行为未验证。
5. **`vm` 逃逸未验证** [行为推断]：§11.3 的 `__send.constructor("return process")()` 是**理论推断，未执行**。不应作为已确认漏洞，仅作为「vm 非安全边界」的已知风险提示。
6. **图片重采样算法未知** [未验证]：`READ_IMAGE_*` 常量已确认，但 `imageProcessorPort` 的具体实现（重采样算法、质量参数）未读。
7. **WebFetch 无 robots.txt** [未验证]：源码中未找到相关处理，但不能排除在其他层实现。
8. **SSRF 无 DNS 预检** [源码确认]：源码注释明说依赖 HTTP egress proxy 兜底。该 proxy 的实现不在本 checkout 内，其配置强度未验证。
9. **Bots 语义仅由字符串推断**：`pollFeishuAppRegistration` / `botDeliveryTarget` 是唯一证据，无配置实例。
10. **Sherry 侧全部为仓库实测**：`config/features` 实际值 + 源码 + 实际调用返回值，无估计成分。唯一未验证项是 `terminal.py` 的大输出处理（§11.2 标注为待核查）。
11. **本文未评估 Zcode 的遥测/隐私行为**：`config.isTelemetryEnabled` 等设置项存在，但本 checkout 中遥测实现不完整，未做结论。

---

## 附：数据来源

### Zcode 侧

| 来源 | 用途 | 分级 |
| --- | --- | --- |
| `/home/honor/Desktop/project/ZCode/apps/zcode-cli/packages/core/src/context/builder.ts` | prompt 三段组装、14 个 section、注入排序 | 源码确认 |
| `.../core/src/context/types.ts:31-50` | `ContextSource` 枚举 | 源码确认 |
| `.../core/src/system-reminder/source.ts` | 27 个 reminder 源、3 组分类、wrap 逻辑 | 源码确认 |
| `.../core/src/runtime/methods/turn-loop.ts` | 主循环、todo reminder 注入点 | 源码确认 |
| `.../core/src/agent/turn-machine.ts` + `turn-state.ts` | 10 phase、6 终态、转移表 | 源码确认 |
| `.../contracts/src/events/session.events.ts:83-176,696-711` | ~90 事件类型、14 种流式 kind、payload | 源码确认 |
| `.../core/src/runtime/command-queue.ts` | 6 种 mode、3 种 priority、batch 合并 | 源码确认 |
| `.../core/src/runtime/helpers/steering.ts:11` | `MAX_TURN_STEER_INPUT_BYTES = 200_000` | 源码确认 |
| `.../core/src/runtime/methods/steering.ts:57` | 5 种拒绝原因 | 源码确认 |
| `.../bootstrap/src/zcode-protocol-v4/command-inbox.ts` | 幂等闸 + FIFO 准入 | 源码确认 |
| `.../packages/shared/src/zcode-protocol-v4/core.ts:82` | `idempotencyTablePerSession = 512` | 源码确认 |
| `.../core/src/runtime/methods/rewind-message.ts:404` | append-only 分支切断、branchGeneration | 源码确认 |
| `.../core/src/runtime/methods/session-fork.ts` | 6 条 fork 路径 | 源码确认 |
| `.../contracts/src/rewind/index.ts:100,176` | checkpoint artifact 形状、scope/strategy 枚举 | 源码确认 |
| `.../core/src/tool/edit-matchers.ts:30-397` | 8 级匹配级联、Levenshtein ≥0.8、歧义处理 | 源码确认 |
| `.../core/src/tool/handlers/edit.ts:203-535` | read-before-edit、stale 三要素、原子写 | 源码确认 |
| `.../adapters/src/fs/index.ts:452-647,700-777,932-941,1272-1289` | WASM ripgrep、原子写、revision、ignore 策略、mtime 排序 | 源码确认 |
| `.../adapters/src/exec/node-execution-adapter-*.ts` | 每调用一进程、**无沙箱**、进程组杀 | 源码确认 |
| `.../adapters/src/exec/embedded-search-prelude.ts` | bfs/ugrep/rg 注入 | 源码确认 |
| `.../packages/services/src/runtime-tools/runtimeToolResolver.ts:103-142` | 二进制解析顺序 | 源码确认 |
| `.../core/src/tool/handlers/websearch.ts` / `webfetch*.ts` | provider 原生搜索、SSRF 字面量拦截、重定向策略 | 源码确认 |
| `.../core/src/tool/executor/result-serialization.ts` 等 | 通用结果预算、artifact 信封、UTF-8 二分 | 源码确认 |
| `.../core/src/tool/scheduler.ts:48,85,187,233` | 并发 10、并行判定、只读工具集、拓扑分组 | 源码确认 |
| `.../core/src/tool/executor/batch-runner.ts:24-128` | 批执行、stopTurnAfterResult 跳过剩余组 | 源码确认 |
| `.../core/src/subagent/{runner,profile,tool-policy,context-builder}.ts` | spawn 路径、profile 字段、工具策略 | 源码确认 |
| `.../core/src/runtime-task/{registry,notification}.ts` | 内存 task store、邮箱、120k 通知上限 | 源码确认 |
| `.../contracts/src/hooks/index.ts:7-425` | 7 事件、能力矩阵、超时、matcher | 源码确认 |
| `.../core/src/hooks/{runner,output,configured-runner}.ts` | 执行、阻断、顺序 | 源码确认 |
| `.../adapters/src/plugins/index.ts:366` | **插件 hook 无条件执行** | 源码确认 |
| `.../dynamic-workflow-runtime/src/{harness,child-source}.ts` | 256MB 堆、vm realm、确定性禁令、逃逸面 | 源码确认 |
| `.../dynamic-workflow/src/{facade/dts,lowering/lower,engine/types}.ts` | 作者 API、lowering、Caps、状态枚举、REPAIR/NUDGE | 源码确认 |
| `.../bootstrap/src/app/{workflow-world-read,workflow-concurrency-ceiling,dynamic-workflow-run-launch}.ts` | fail-closed 命令集、并发公式、无 wall-clock 超时 | 源码确认 |
| `.../adapters/src/model/workflow-model-failure-policy.ts` | 无上限重试 vs 确定性停止 | 源码确认 |
| `.../adapters/src/storage/session-store/{migrations.ts,repositories/dwf-journal-codecs.ts}` | 物理/逻辑状态映射、无预算列注释 | 源码确认 |
| `.../bundled-skills/skills/dynamic-workflows/SKILL.md` | 两代命名冲突的官方说明 | 源码确认 |
| `~/.zcode/cli/db/db.sqlite` | 全部表 DDL、行数、聚合统计 | 落盘实测 |
| `~/.zcode/cli/rollout/model-io-sess_*.jsonl` | prompt 分段实测字符数、reminder 实际内容、压缩事件 | 落盘实测 |
| `~/.zcode/cli/exec/` | 12 MB stdout 日志（验证落盘 > 内联） | 落盘实测 |
| `~/.zcode/v2/{setting.json,tasks-index.sqlite}` | 设置项、`forked_from_task_id` 列 | 落盘实测 |
| `~/.zcode/cli/{agents,memories,plugins}/` | 子 agent profile、记忆结构、插件树 | 落盘实测 |
| `/opt/ZCode/resources/tools/` | ripgrep / bfs / ugrep 二进制存在性 | 落盘实测 |

### Sherry 侧

| 来源 | 用途 |
| --- | --- |
| `config/features/agent_side/*.py`（33 项）+ `infra_side/*.py`（20 项） | 全部 53 项配置实测值 |
| `agent/core.py` | 中间件注册顺序、`main_llm_context_window` 参数 |
| `agent/tools/__init__.py::build_main_tools()` 实际调用 | 36 主工具真值 |
| `agent/tools/file_tools/{read_file,write_file,patch_file,search_files,search_scan}.py` | 文件工具实现与截断标记 |
| `agent/tools/taskflow/{config,registry/store_sqlite}.py` | StepStatus 7 态、SQLite WAL 持久化（修正首轮误判） |
| `agent/tools/subagent/`（`spawn/core.py`、`types/functional_role.py`、`tools/`） | spawn 路径、角色模型、7 工具 |
| `agent/middlewares/humanInTheLoop/core.py:159-287` | interrupt / resume 契约、GraphInterrupt 重抛 |
| `workspace/prompt_builder.py`（324 行）+ `workspace/template/{en,zh,ja,ko}/` | 7 个 block 组装、persona 分层 |
| `runtime/data_provider.py` + `agent/prompt_data_provider.py` | 依赖倒置协议 |
| `runtime/lane/core.py` + `server/service/lane_lifecycle.py` | 4 类 lane、启动校验、drain |
| `server/service/input_queue_service.py:142-415` | TurnExecutor 协议、幂等、CLAIMED 占位、锁外 dispatch |
| `models/LLMs/main_llm.py:349` | `build_fallback_chain()` |
| `agent/tools/web_search.py:33-89` | Tavily 集成、无 key 降级、重试退避 |
| `config/features/agent_side/{summarization,tool_result_eviction,message_pipeline}.py` | 截断与 evict 常量 |
