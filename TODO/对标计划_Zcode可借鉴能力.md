# 对标计划: Zcode 可借鉴能力

> **状态**: 待评估（本文为对标分析产出，非已批准的实施计划）
> **创建日期**: 2026-09-30（2026-09-30 第二轮源码核验后重写）
> **对标对象**: Zcode v3.14.4（Electron 桌面端 + `apps/zcode-cli`），本机 `/usr/bin/zcode`、`~/.zcode/`、源码树 `/home/honor/Desktop/project/ZCode`
> **目标**: 记录 Zcode 在长任务可靠性、扩展点、工具实现上优于 Sherry 的设计点，作为 Sherry 后续演进的候选 backlog
> **文档定位**: **本文只提炼「Zcode 更优、值得 Sherry 借鉴」的部分**（Sherry 领先侧与 Zcode 安全缺陷不在本文范围）。每条结论的证据就地标注，来源清单见文末「附：数据来源」——本文不依赖其他文档
> **证据分级**（本文统一使用）：

| 标记 | 含义 |
| --- | --- |
| `[源码确认]` | Zcode TypeScript 源码可精确定位到文件行号 |
| `[落盘实测]` | 来自 `~/.zcode/` 的 SQLite / JSONL / 配置文件实测 |
| `[实测]` | Sherry 侧仓库实测值（`config/features` 实际配置 + 源码 + 实际调用返回值） |
| `[行为推断]` | 逻辑推断，未执行验证 |
| `[未验证]` | 明确未确认 |

> **重要声明（已修正）**:
> 1. **Zcode 源码是可读的完整 TypeScript monorepo，非闭源打包**。本文所有 Zcode 结论均带源码路径或落盘路径，不再有「无法核验」的降级表述。首轮「闭源不可测」的说法已作废。
> 2. **本文不记录 Zcode 的安全缺陷**（Bash 无沙箱、插件 hook 无信任门、workflow `vm` 非安全边界、遥测等）——那是反向议题，需另文。但**部分缺陷会否决本文的照搬建议**（如 WebFetch 的 SSRF、workflow 脚本化），相关条目已就地标注，附数据来源表里也标了「否决照抄项」的源码位置。
> 3. **源码版本与发布版可能不一致**：源码树 `apps/zcode-cli` 版本为 0.16.9，发布版为 v3.14.4。唯一强交叉验证项是压缩阈值（966,000 与 `autoCompactThreshold` 精确吻合）。

---

## 〇、基线对比

先明确量级，避免后续讨论脱离事实：

| 指标 | Zcode | Sherry | 谁更优 |
| --- | --- | --- | --- |
| 模型上下文窗口 | 1,000,000 [落盘实测] | 131,072 [实测] | Zcode 大 7.6× |
| 主 agent 工具数 | 34 [落盘实测，rollout 工具数组] | 36（`build_main_tools()`）[实测] | 平 |
| 工具并发上限 | 10（批内，`toolConcurrency.maxConcurrency`）[源码确认] | 25 槽位（main 12 / subagent 8 / nudge 4 / nested 1）[实测] | **Sherry**（语义分 lane） |
| 会话级 token 硬预算 | ✅ `session_target.token_budget` [落盘实测] | ❌ [实测] | **Zcode** |
| 用户可配生命周期 hook | ✅ 7 事件 [源码确认] | ❌ [实测] | **Zcode** |
| 结构化上下文注入目录 | ✅ 27 个 `system-reminder` 源 [源码确认] | ❌（各中间件各自拼字符串）[实测] | **Zcode** |
| Prompt 缓存分段 | ✅ `stable` / `dynamic` + `cacheHint` [源码确认] | ❌（全动态注入）[实测] | **Zcode** |
| 文件状态回滚 | ✅ 509 条 workspace checkpoint [落盘实测] | ❌ [实测] | **Zcode** |
| 对话 rewind / 分支 | ✅ append-only + `branchGeneration` [源码确认] | ❌ [实测] | **Zcode** |
| turn 内联输入（`guide`） | ✅ 3 种投递语义 [源码确认] | ❌（2 种）[实测] | **Zcode** |
| per-agent 用量记账 | ✅ 3 张表，`model_usage` 5,880 行 [落盘实测] | ❌（进程内计数器不落库）[实测] | **Zcode** |
| 流恢复锚点 | ✅ 7 个流恢复事件 [源码确认] | ❌ [实测] | **Zcode** |
| Edit 模糊匹配 | ✅ 8 级级联 [源码确认] | ❌ [实测] | **Zcode** |
| Write 原子性 | ✅ temp+fsync+rename+`O_NOFOLLOW` [源码确认] | ⚠️ 未验证原子性 [实测] | **Zcode** |
| workflow 编排表达力 | 两代、9 工具、TS 脚本 [源码确认] | 14 工具、声明式 DAG [实测] | Zcode 更表达力 |
| workflow 失败级联 / 节点上限 | ❌ 无上限 [源码确认] | ✅ `failed` 依赖者保持 `blocked` [实测] | **Sherry** |
| 代码智能 | ❌ 无内建索引 [源码确认] | AST + LSP + 语义 16 工具 [实测] | **Sherry** |
| 多模态 / 语音 | 图/视频/PDF，无语音 [源码确认] | ITTT/VTTT/STT/TTI [实测] | **Sherry** |
| 记忆与知识 | Markdown 文件，agent 自维护 [落盘实测] | MesMemory + curator + 4 条经验提取路径 [实测] | **Sherry** |
| subagent 完成判定 | 纯程序化终态等待 [源码确认] | 4–6 层门（judge + evidence + drain）[实测] | **Sherry** |
| delegation 深度限制 | ❌ 无（`general-purpose` 可递归）[源码确认] | ✅ 默认 2 / 硬上限 2 [实测] | **Sherry** |
| 源码 / 测试规模 | 未见测试资产 | 157,469 / 111,684 LOC（5,368 用例）[实测] | **Sherry** |

**关键判断**：Zcode 是「通用编码 agent（Cursor/VS Code 级）」，Sherry 是「自托管个人 agent」。两种架构取向，不是版本差异。以下条目均须在此前提下评估——不是所有 Zcode 特性都该照搬。

---

## 一、长任务可靠性

### 1.1 ⭐ workflow journal 重放：已完成节点不重跑（首轮误判已修正）

**首轮写的**：「Sherry 进程重启后无法从中间步骤恢复，只能整个 task 重跑」——**这条是错的，已撤回**。

**实际情况** [源码确认 vs 实测]：Sherry taskflow 状态是 SQLite WAL 持久化的，`get_active_flows_sync(session_id)` 从 SQL 侧直接取非终态 flow（`agent/tools/taskflow/registry/store_sqlite.py` + `agent/prompt_data_provider.py:37-41`），进程重启后照样读得到。`StepStatus` 7 态中 `dispatched` 是可重入中间态，不是终态（`agent/tools/taskflow/config.py:35-41`）。

**真正的差距收窄为四条** [源码确认]：

| 维度 | Zcode 新一代 | Sherry taskflow |
| --- | --- | --- |
| ① 已完成步骤是否重跑 | ❌ **不重跑**——`AskScheduler.admitAsk`（`dynamic-workflow/src/engine/scheduler.ts:116-174`）按 `(siteId, ordinal)` 读 journal，`completed` / `failed` 节点**从 journal 短路**（`releaseCachedAsk`，**连 driver 都不调**），带 `inputHash` 防御性校验 | ⚠️ `depends_on` 满足即重注入；已 `done` 的 step 由 DAG 依赖判定跳过，但调度器仍被唤醒 |
| ② 显式挂起 | ✅ `paused`（legacy）/ `stopped(user)`（新），被后继 run 标记 `superseded` | ⚠️ `set_waiting` / `wait_all` 是**阻塞等待**语义，不是用户主动挂起 |
| ③ 血缘指针 | ✅ `dwf_run.resumed_from` + `superseded` 取代语义 | ❌ 无 |
| ④ 步骤级结果缓存 | ✅ `cached` 状态 + `input_hash` 命中 | ❌ 无 |

**改造建议**（按性价比排序）：
1. **① journal 短路**：给 taskflow step 增加 `input_hash` 列 + `cached` 态。`taskflow_resume` 时若依赖已满足且 `input_hash` 未变且 step 已 `done`，直接返回缓存的 step 产出，不重新 dispatch。**这一条能同时省 token 和缩短墙钟时间，是四条里唯一「省成本」的。**
2. **③ 血缘**：`task_flows` 加 `resumed_from` + `superseded_by` 两列，成本极低（一次 migration），换来可追溯的多版本演进与「旧 run 被谁取代」的 UI 能力。
3. **② 显式挂起**：新增 `paused` 终态，与现有 `set_waiting` 区分开——`waiting` = 等子步骤，`paused` = 用户主动挂起等后续 resume。语义上必须分，否则 UI 无法区分「在等」和「暂停了」。
4. **④ 步骤级缓存**：优先级最低，因为 Sherry 已有 evidence ledger 做了部分替代。

**风险**：①②④ 都需要先确认 taskflow step 的**重入幂等性**——当前 `dispatched` 可重入态是否真的能安全跳过（有无副作用工具已在执行）。这一条**必须先做原型验证，不能直接实现**。③ 无此风险。

**注意**：Sherry 在**失败级联语义**和**节点上限**上明确更完整——`failed` 的依赖者保持 `blocked`（绝不被失败解锁）、`failed`/`skipped` 级联 `skipped`（`config.py:24-28`），Zcode 只有 `skipped`。不要为了学 resume 语义而改坏这块。

---

### 1.2 workflow run 级并发上限（可热调）

**Zcode 做法** [源码确认]：`Caps { maxConcurrency }` 在 run 提交时固定并持久化到 `dwf_run.caps_max_concurrency`，之后可通过 `WorkflowEngine.setMaxConcurrency`（`engine.ts:579-589`）**热调**。上限公式 `max(1, min(16, availableParallelism() - 2))`，外加按 `provider/model` 分桶的进程级 governor。

**Sherry 现状** [实测]：taskflow **无并发维度**——DAG 的可并行 step 是顺序 dispatch 的，只有 subagent 内部并发受 SUBAGENT lane（8）约束。

**差距**：taskflow 里两个互不依赖的 step 无法并行执行。

**改造建议**：在 taskflow 调度时识别「同层无依赖关系」的 step 集合，批量交给 MAIN lane 内的子任务并行执行；并发上限走 `TASKFLOW_INFRA` 新键，默认从 1 起步灰度。

**风险**：taskflow 的 progress / board / step judge 逻辑目前假设 step 逐个完成（`taskflow_resume` 是单步入口）。并行化需要先确认 judge 与 evidence 写入是并发安全的。**成本高于 1.1，建议排在 1.1 ①③ 之后。**

---

### 1.3 Session 级 token 预算封顶

**Zcode 做法** [落盘实测]：`session_target` 表 DDL 带 `check(status in ('active','paused','budget_limited','complete'))`，字段含 `objective` / `token_budget` / `tokens_used` / `time_used_seconds` / `summary_title` / `active_run_started_at`。预算耗尽自动转 `budget_limited` 而非无限烧 token。
> ⚠️ 本机该表 **0 行**（`[落盘实测]`）——只有 schema，无使用实例，行为未验证。

**Sherry 现状** [实测]：`TASKFLOW_INFRA` 有 `waiting_timeout_hours = 24`（最长挂起），`ITERATION_BUDGET` 有迭代次数上限，`COMPLETION_JUDGE.goal_max_turns = 5` 只管目标循环——**但没有 token 预算维度**。taskflow 可以跑满 24h × 90 迭代，token 花费无硬顶。

**改造建议**：
1. `runtime/session/state_register.py` 增加 `token_budget` / `tokens_used` 状态键（`state_keys.py` 走 Typed StateKey 注册，不写字面量 dict）
2. 预算耗尽转 `budget_limited` 态**暂停而非终止**，与 1.1② 的 `paused` 复用同一套语义
3. 复用已有的 `MODEL_PRICING` 配置做实时花费核算

**收益**：这是**最容易落地的一条**——只加状态键 + 一个 `before_model` 检查点，不动 taskflow 逻辑、不动数据库 schema。

**风险**：低。唯一要注意的是「预算耗尽」对用户必须是可见的（WS 事件 + 前端提示），否则表现为静默停机。

---

### 1.4 ⭐ 文件级检查点 + 对话 rewind（append-only 分支）

这是 Zcode 相对 Sherry **差距最大**的一项，Sherry 完全没有对应物。

**Zcode 做法**：

*文件检查点* [落盘实测]：`session_entry` 表 509 条 `runtime/workspace_checkpoint` 记录，payload 结构：
```
checkpointId / messageId / targetMessageId / toolMessageId
scope ("workspace") / snapshotRef / diffRef / fileCount
```
`snapshotRef` / `diffRef` 是 `zcode-artifact://<sessionId>/tool-result-<uuid>` URI，指向 artifact 目录里的文件快照与 jsdiff 结构化 diff（`contracts/src/rewind/index.ts:100`，每文件带 `beforeContent` / `afterContent` / `structuredPatch`，3 context 行，5s timeout）。

*对话 rewind* [源码确认]：`rewindConversationToMessage`（`core/src/runtime/methods/rewind-message.ts:404`）
- 写 `setRevert`：`keptMessageIDs` / `branchCutAfterMessageID` / **`branchGeneration`**
- `branchGeneration` 递增作为 **fencing token**（防止旧分支的迟到写入污染新分支）
- 取消被移除 turn 名下的后台任务
- **旧分支仍留在库里**，通过 `selectActiveConversationBranch`（`contracts/src/rewind/index.ts:176`）从活动分支隐藏——即 append-only，不删消息

*fork 路径* [源码确认]：`core/src/runtime/methods/session-fork.ts` 有 6 条（含 `createSelectionSideConversation` 选区旁聊、`forkStableConversationAtMessage` 原子 `commitForkBundle`），fork 携带完整 message/part 转写本 + id 重映射 + goal state 快照 + verifier ledger。

*维度解耦* [源码确认]：scope 可选 `conversation` / `workspace` / `both`；strategy 有 `active_chain` / `file_only` / `fork_required` / `unavailable`；target status 含 `covered_by_compact`——目标若在压缩边界之前，workspace rewind 仅在有 checkpoint 时可用，否则明确返回 `unavailable`（**不假装能回滚**）。

**Sherry 现状** [实测]：
- 无文件状态快照，`patch_file` / `write_file` 不可回滚
- `TOOL_RESULT_EVICTION.eviction_subdir = 'evicted'` 只落盘**工具结果**，不是文件状态
- `workspace/` 在 `.gitignore` 中是锚定的（`/workspace/`），**无 git 兜底**
- checkpointer（`agent/checkpointer/thread_safe_checkpointer.py`）保存的是 **LangGraph 图状态**，不是文件快照，也不是对话分支树

**改造建议**（按依赖顺序）：
1. **先做文件快照**：`patch_file` / `write_file` 写入前把旧内容快照到 artifact 目录（复用 `agent/tools/pub_base/` 的路径工具 + `evicted/` 的落盘模式），并在 session 状态里记 `messageId → checkpointId` 映射
2. **再做 `file_checkpoint_revert` 工具**，与 4.2 的 stale 检测配套（revert 也必须走 stale 检查，不能把别人的改动覆盖掉）
3. **最后做对话 rewind**：这一步可以**独立于** 1 和 2 上线（纯 append-only + `branchGeneration`），且与 Sherry 已有的 `context_engine/events/` append-only 事件日志哲学一致，可复用同一套思路

**收益**：agent 改坏文件后用户能一键撤销——这是目前用户投诉风险最高、而 Sherry 零防护的场景。

**风险**：磁盘占用。Zcode 的 `cli/artifacts/` 单会话已达数百文件 / 十余 MB。Sherry 可参照 `TODOLIST_INFRA.ttl_registry_max_entries` 做保留策略（`history_deleted_at` 式软删 + TTL 清理）。

---

### 1.5 流恢复锚点（stream recovery anchor）

**Zcode 做法** [源码确认]：7 个流恢复事件构成一套完整协议：
```
stream_recovery_anchor_created → stream_recovery_started → anchor_selected
→ tail_discarded → retry_started → retry_blocked
+ streaming_tool_ledger_updated
```
即「流中途断线后，从锚点续传并**丢弃不确定尾部**」。配套 14 种流式 kind（`ModelStreamingKind`：`start` / `text_*` / `reasoning_*` / `tool_input_*` / `tool_call` / `finish` / `error`），客户端能分别拿到 reasoning 流与正文流、增量 tool input JSON、usage 遥测。

**Sherry 现状** [实测]：`WS_STREAM` 有 `max_continuation_retries = 4` / `max_reasoning_only_retries = 2` / `drain_error_backoff_s = 1.0`，`pub/func/message/` 有 retry——**但没有流级锚点**，无法表达「这次重试从第 N 个 delta 继续、之前不确定的部分丢弃」。

**差距**：断线重连后要么整轮重跑（贵且可能重复副作用），要么盲目续接（可能把半截 tool call 当完整的执行）。

**改造建议**：先做低成本的 `streaming_tool_ledger_updated` 等价物——**已确认完整下发的 tool call 记为 committed，未完成的记为 pending**，重连时 pending 的一律按未执行处理。这一个 ledger 就能覆盖最危险的场景，不需要完整实现锚点协议。

**风险**：中。需要客户端协议配合（前端在 `client/`），属于前后端协同项。

---

### 1.6 压缩保真度（双向问题，不要照搬）

**Zcode 做法** [落盘实测 + 源码确认]：`autoCompactThreshold = 966,000`（与源码公式 `window − min(maxOut, 21000) − 13000` 精确吻合 = 1,000,000 − 21,000 − 13,000，这是本文最强的交叉验证项）。压缩后 1,041,285–1,096,583 → 22,580–32,084 token。但 `summarizedMessageCount = 2027` → `keptMessageCount = 1`——**2027 条消息压成 1 条**，激进的单次摘要。
另有 microcompact 预 pass：0.9 比例阈值触发，仅把旧工具结果清成 `[Old tool result content cleared]`，保留最近 5 条对话。

**Sherry 现状** [实测]：触发点 `window × 0.8 = 104,857`；保留窗口 `min/max_preserve_tokens = 2000/15000`，`preserve_ratio = 0.25`；分层保留 `critical_context_max_items = 3` / `key_decisions_max_items = 5` / `active_plan_notes_max_items = 20`。

**判断**：**Zcode 的触发点更合理**（966K vs 105K，因为窗口大 7.6 倍），但 **Sherry 的保真度策略明显更成熟**（分层保留 + 头尾双预览 vs 单次全压成 1 条）。

**可借鉴的两点**：
1. **microcompact 的独立通道**——Sherry 有 `preemptive_truncate_ratio = 0.7 → target 0.5`，思路相近但**没有「只清工具结果、保留对话」这条独立通道**。补上后可以在不触碰对话的情况下快速腾出空间。
2. **压缩失败熔断阈值对齐**——Zcode 连续 3 次失败即停止重试；Sherry 有 `max_recovery_attempts = 2` + `ineffective_threshold = 2`，机制已存在，阈值可对齐。

**不要照搬**：`keptMessageCount = 1`。Sherry 的分层保留应保留并强化。

---

### 1.7 Todo 跨会话持久化（无差距，仅记录结论）

**Zcode** [落盘实测]：`TodoRead` / `TodoWrite` 工具 + `todo` 表，本机 30 条（22 completed / 2 in_progress / 6 pending），主键 `(session_id, position)`。
**Sherry** [实测]：`todowrite` / `todoread` + `TODOLIST_INFRA` SQLite，有 `stagnation_max_stagnation = 3` 停滞熔断 + `stagnation_max_cooldown_s = 60` 冷却。

**结论**：**Sherry 更强**（多了停滞熔断与冷却）。不需要改造，记录以免重复讨论。

---

## 二、扩展点（Zcode 最值得学的工程化部分）

### 2.1 ⭐ 用户可配生命周期 Hook（7 事件）

**Zcode 做法** [源码确认] `contracts/src/hooks/index.ts:7-15`：
```
SessionStart | UserPromptSubmit | PreToolUse | PermissionRequest
| PostToolUse | PostToolUseFailure | Stop
```
比 Claude Code 多一个 `PermissionRequest`。

| 事件 | 可注入上下文 | **可改 tool input** | 可否阻断 | 特有字段 |
| --- | --- | --- | --- | --- |
| `PreToolUse` | `additionalContext` | ✅ `updatedInput` | ✅ allow/ask/deny | `riskLevel`、`sideEffectScope`、`toolCallId`、`toolName` |
| `PermissionRequest` | `additionalContext` | ✅ `updatedInput` | ✅ allow/deny | `permissionSuggestions`、`permissionUpdates` |
| `PostToolUse` | `additionalContext` | ❌ | ❌ | `artifactRefs`、`toolResultPreview` |
| `PostToolUseFailure` | `additionalContext` | ❌ | ❌ | `error{message,type}`、`isInterrupt` |
| `UserPromptSubmit` | `additionalContext` | ❌ | ✅ 可阻断 | `attachmentsSummary`、`prompt` |
| `SessionStart` | `additionalContext` | ❌ | ❌ | `model`、`source`(startup/resume/clear/compact) |
| `Stop` | `additionalContext` | ❌ | ✅ `continue: true` 可**延长本轮** | `responsePreview`、`stopHookActive`、`toolCallCount` |

**执行语义** [源码确认]：超时默认 60,000ms（`DefaultHooksRuntimeConfig`，`:425`），`maxOutputBytes` 32,768；`AbortController` + `HOOK_TIMEOUT_ABORT_REASON`（`core/src/hooks/runner.ts:334-382`）；5 种 `HookOutcome`（`success`/`blocked`/`failed`/`cancelled`/`timed_out`）；matcher 支持 `*` / `A|B` / RegExp；stderr/stdout preview ≤4,000 字符进 lifecycle event **但不进模型**。
**配置来源**：用户 `~/.zcode/cli/config.json`（无需信任）→ 项目 `zcode.json`（**需 workspace trust**，digest-based，10 分钟 review 超时）→ 插件 `hooks/hooks.json` → 内部。

**Sherry 现状** [实测]：全文搜索 `hook` 命中 194 处，**全部是内部机制**：
- `abefore_model` / `aafter_agent` — LangChain 中间件钩子
- `runtime/hooks.py` — 进程级回调注册表
- `agent/middlewares/humanInTheLoop/core.py:159-216` — `interrupt()` / `set_interrupt`，≈ `PermissionRequest` 的硬编码内置版
- `wrap_tool_call` — ≈ `PreToolUse` 的硬编码内置版

最接近的扩展点是 `skills/builtin/*/scripts/`（11 个目录），但那是 **skill 自带脚本，不是生命周期钩子**。

**差距**：用户无法在不 fork 源码的前提下插入自己的审计、计费、通知或策略逻辑。

**改造建议**：
1. **先做只读 + 通知型 hook**（`PostToolUse` / `PostToolUseFailure` / `Stop` 三个事件）：只允许发通知 / 写审计日志，**不允许阻断**。这一步无安全风险，能验证配置加载、进程调用、超时、输出截断四件事。
2. **再做 `PreToolUse` 的 allow/ask/deny**：这一步等于把用户策略放到关键路径上，**必须先有项目级信任门**——Zcode 的插件 hook 是无条件执行的（`adapters/src/plugins/index.ts:366`），这个设计不能抄。Sherry 已有 `SkillSpector` 扫描第三方 skill 的先例，hook 应复用同一套信任模型。
3. `updatedInput`（改写 tool input）**最后做**，且应限制为「路径规范化 / 参数补全」这类幂等变换，不允许注入新语义。

**收益**：这是 Sherry 作为**自托管**产品最缺的能力——用户想定制自己的策略，现在只能改 Python 源码。

**风险**：**高**。阻塞型 hook 是执行路径上的用户代码，安全设计必须从第一天就有信任门 + 超时 + 熔断 + 审计，不能像 Zcode 那样「先无条件执行再说」。

---

### 2.2 ⭐ system-reminder 目录化（27 个注入源）

**Zcode 做法** [源码确认] `core/src/system-reminder/source.ts`：一套结构化的 `<system-reminder>` 注入目录，**27 个 source** 分 3 组：

| 组 | 数量 | 成员 |
| --- | --- | --- |
| PREFIX | 2 | `context_prefix`、`skills_listing` |
| PERSISTED | 15 | `todo_reminder`、`task_status`、`tool_result_warning`、`resume_referenced_session_context`、`plan_file_reference`、`resume_goal_state`、`goal_state_change`、`plugin_reference`、`target_continuation`、`goal_completion_verification`、`rewind_notice`、`conversation_fork`、`selection_side_chat`、`queued_system_notification`、`shell_environment_change` |
| PER_REQUEST | 10 | `incoming_message`、`hook_context`、`runtime_mode`、`plan_mode_exit`、`output_style`、`date_change`、`referenced_session_context`、`model_anomaly`、`prompt_attachment`、`diagnostics` |

每个 descriptor 带 3 个正交属性：`channel`（6 个：`request_prefix` / `current_turn` / `tool_result` / `history_continuity` / `mid_turn_event` / `real_user`）、`lifecycle`、`isMeta`。
包装函数 `wrapSystemReminder`（`:203-212`）对**嵌套标签做拒绝/转义**（`:208`, `:233`）——防注入。
触发阈值可配：如 `todo_reminder` 距上次 `TodoWrite` 满 10 个 assistant turn **且**距上次提醒满 10 turn；`runtime_mode`（plan mode 提示）每 5 个 human turn 一次、每第 5 次给全量版。

**Sherry 现状** [实测]：`task_intent/`（E7）、`nudge/`、`todo_continuation_enforcer`（E3）等中间件承担类似职责，但**没有统一的注入目录/元数据描述**——每个中间件各自拼字符串，各自决定注入时机，各自处理嵌套。

**差距**：无法回答「当前 prompt 里有哪些注入、每条多大、什么条件触发、走了哪个 channel」；也无法统一做去重、预算与转义。

**改造建议**：不新建机制，**给现有中间件的注入内容加一层统一目录**：
- 每个注入点声明 `source_id` / `channel` / `budget_chars` / `trigger`，由一个 `reminder_catalog` 统一注册
- 统一走 `wrap_system_reminder()` 包装（带嵌套转义）
- 加一条 `before_model` 的注入预算检查：单轮注入总量超阈值时按 `isMeta` 优先级丢弃低价值项

**收益**：低风险高收益。**不动任何中间件的业务逻辑**，只加目录与包装层；同时顺手解决注入膨胀问题（`skills_listing` 单项就 26,597 字符，见 2.3）。

**风险**：低。需要注意 `channel = real_user` 的 `isMeta=false` 语义（`target_continuation` 会作为**真实用户输入**呈现给模型）——这是刻意的设计，迁移时不能统一当元信息过滤掉。

---

### 2.3 ⭐ Prompt 缓存分段（stable / dynamic）

**Zcode 做法** [源码确认] `core/src/context/builder.ts:82-223::ContextBuilder.build()`：把 prompt 拆成**三个独立 system message**（为 provider prompt cache 分段）+ 两条 meta_user 消息。每个 section 有两个正交属性：
- `injectionTarget`: `"system"` | `"meta_user"`
- `cacheHint`: `"stable"` | `"dynamic"`

`orderSectionsForInjection`（`builder.ts:310`）固定顺序：system-stable → system-dynamic → meta_user-stable → meta_user-dynamic。
14 个 section：`cli_prefix` / `identity` / `desktop_context` / `dynamic_behavior` / `session_guidance` / `memory` / `env_info` / `output_style` / `context_management` / `system_context` / `skills` / `request_user_context` / `current_date` / `custom`。

实测各段体积（`[落盘实测]`，`model-io-sess_bfd00704-*.jsonl`）：

| 段 | 缓存提示 | 实测字符数 |
| --- | --- | --- |
| `cli_prefix` | `cacheControl: ephemeral` | 42 |
| stable body（identity + custom + workflow_actor） | stable | 2,313 |
| dynamic system（communicating / context_management / env_info / memory / system_context） | dynamic | 8,642 |
| meta_user `context_prefix`（AGENTS.md + 记忆索引 + 日期） | — | 6,460 |
| meta_user `skills_listing` | — | **26,597** |
| **合计** | | **~43,854** |

**Sherry 现状** [实测]：`workspace/prompt_builder.py`（324 行）组装 7 个 block（`_build_todo_block` / `_build_boulder_block` / `_build_taskflow_block` / `_build_continuity_block` / `_build_knowledge_block` 等），**全部是动态注入**，且数据来自 agent / context_engine 两层（为此专门建了 `runtime/data_provider.py` 的 `PromptDataProvider` 协议做依赖倒置）。
**没有 `stable` / `dynamic` 概念**，大 prompt 场景下 provider 侧 cache 命中率低于 Zcode。

**差距**：Zcode 明确更优——**可缓存部分与不可缓存部分被显式分开**，静态身份段（2,313 字符）永远命中缓存。

**改造建议**：
1. 给 `prompt_builder.py` 的 7 个 block 各加一个 `cache_hint: Literal["stable", "dynamic"]` 标注——**`workspace/{SOUL,AGENTS,USER,IDENTITY}.md` 是 stable，todo / boulder / taskflow / continuity / knowledge 是 dynamic**
2. 返回值改为分组结构（stable 组 + dynamic 组），由 `agent/core.py` 装配成两条 message
3. 顺带处理 `skills_listing` 26,597 字符的问题：Zcode 也是全量注入，**这不是 Zcode 的优势而是双方共有的膨胀点**——但既然要动这块，Sherry 可以顺手做渐进披露（只注入当前 session 可见 scope 的 skill）

**风险**：中。`PromptDataProvider` 协议是跨层 seam（`runtime/data_provider.py`），改返回结构要同步 `agent/prompt_data_provider.py` 与 `context_engine` 侧实现，且 `tests/workspace/test_prompt_builder*.py` 需要更新。建议**独立成一个可回滚的改动**。

---

### 2.4 计划模式（Plan Mode）

**Zcode 做法** [落盘实测，34 工具列表]：`EnterPlanMode` / `ExitPlanMode` 一对工具 + `startPlanRecommendationDismissed` 设置项（启动时推荐使用）。配套两个 reminder source：`runtime_mode`（plan mode 激活时每 5 个 human turn 提醒一次）与 `plan_mode_exit`。
另：plan mode 工具对**所有**子 agent 强制 disallow（`subagent/tool-policy.ts`）。

**Sherry 现状** [实测]：`HITL_DEFAULTS` 有审批机制，`TaskIntentMiddleware` 有意图识别，但**无「先出方案、后执行」的显式模式切换**。

**改造建议**：`EnterPlanMode` 作为 `HumanInTheLoop` 的一种模式——进入后 `write_file` / `patch_file` / `terminal` 全部转为待批准，退出后恢复。复用现有 HITL 骨架，成本低。
子 agent 强制 disallow 这一条**直接抄**（Sherry 的 subagent 工具最小权限已有 `inherited_tool_policy.py` 实现，加一条 plan-mode 规则是同构改动）。

**风险**：低。注意 plan mode 下的批准会让 turn 变长（HITL interrupt 语义），需与 1.5 的 `Stop` hook 类「延长本轮」能力协调，避免用户批准后又被立刻打断。

---

### 2.5 运行时 workflow 脚本编排

**Zcode 做法** [源码确认]：**两代系统并存**，不要混淆：

| | Legacy "script workflow" | New "dynamic workflow" |
| --- | --- | --- |
| 工具 | `Workflow` / `/expert` | `CreateWorkflow`、`AmendWorkflow`、`SaveWorkflow`、`EvalWorkflowSnippet`、`ListWorkflowRuns`、`GetWorkflowRun`、`ResumeWorkflowRun`、`ResolveWorkflowQuestion`、`ListSavedWorkflows`（**9 个**） |
| 数据表 | `workflow_run`、`workflow_activity`、`workflow_event`、`workflow_definition` | `dwf_run`、`dwf_actor`、`dwf_node`、`dwf_event` |
| 隔离 | Node 子进程 + `new AsyncFunction` + 全局覆写 | Node 子进程 + **`vm.createContext` realm** |

`EvalWorkflowSnippet` 属新一代。官方 SKILL.md 明说 legacy `Workflow` 与 `/expert` 是「另一个更旧的、恰好同名的特性」。
执行管线：作者写 TS → 类型检查（对内嵌 `.d.ts` facade 查 `import`/`process`/`fetch`/`require`/`Date.now`/`Math.random`，编译失败）→ lowering（剥离类型 + 注入 site id，`planner.ask<Plan>(t)` → `__host.ask("ask#3", planner, t)`）→ 落盘 `.zcode/workflow-runs/<runId>.mjs` → 子进程执行（`--max-old-space-size=256`，NDJSON over stdio）。
宿主 API：`agent().ask<T>()`、`files.*`（只读）、`git.*`（固定 allowlist argv）、`world.run`（cmd 必须是编译期字符串字面量并进「已批准命令集」，**fail-closed**）、`report`、`artifact.*`。

**Sherry 现状** [实测]：taskflow 的 step 由 `taskflow_create` 声明式定义（`response_schema` / `judge_criteria` / `depends_on`），**编排逻辑是固定的 Python 代码**，不能运行时生成新逻辑。`skills/builtin/` 已有成熟 skill 体系可挂载。

**差距**：声明式 DAG 适合已知流程，遇到未预见的长任务形态就僵了。

**改造建议：分两步，不要一步到位**
1. **先做「step 类型扩展」**：允许 taskflow step 引用一个 skill，把「固定编排」变成「编排 + 可插拔行为」。**这一步能吃到 80% 的表达力收益，且零沙箱风险。**
2. **再考虑脚本化**：Zcode 的 `vm` 隔离**不是安全边界**（Node 官方明说 `vm` 非安全沙箱；其 sandbox 对象暴露了外部 realm 函数 `__send`，经典 `constructor("return process")()` 逃逸理论可达——`[行为推断]`，**未执行验证**）。若 Sherry 要做，必须换真隔离方案（子进程 + seccomp/landlock 或容器），不能照抄。

**强烈建议先做第 1 步**。第 2 步的安全边界设计与 Sherry 现有姿态（`PathGuard` 硬拒 `~/.ssh`、OS 原生沙箱、审批门）直接冲突，需要独立立项。

---

## 三、输入与并发语义

### 3.1 ⭐ `guide`：turn 内联输入

**Zcode 做法** [源码确认]：两层队列 + **3 种投递语义**：

| 模式 | 语义 | 实现 |
| --- | --- | --- |
| `queue` | 下一个 turn 消费（turn 边界） | 正常入队 |
| **`guide`** | **当前 turn 内**在合法 model-step 边界注入，每边界最多 1 条 | `drainInlineGuideForNextRequest`（`turn-guide-drain.ts`）；若无 tool 边界则降级为 `queue` |
| `startNow` | **抢占当前 turn** | foreground promotion lease → `preemptActiveTurnAndWait` → abort 活动 `AbortController` → `waitForSessionIdle` |

限制：`MAX_TURN_STEER_INPUT_BYTES = 200,000`；5 种拒绝原因（`empty_input` / `input_too_large` / `no_active_turn` / `expected_turn_mismatch` / `turn_not_steerable`）；abort 时**已产生的部分工作保留不回滚**，活动 goal 转 paused，pending guide 降级为 queue。

**Sherry 现状** [实测]：`UserInputQueue` 单队列，2 种语义（`STARTED` / `QUEUED`）。`sessions_steer` 是 **subagent 专用**，不面向用户。

**差距**：`guide` 是 Zcode 唯一明确领先且 Sherry **完全没有**的输入语义——用户在 agent 干活时插一句「顺便把 X 也改了」，不用等整轮结束，也不用抢占。

**改造建议**：在 `server/service/input_queue_service.py` 的 `submit_user_input` 增加第三种 `GUIDE` 模式：
- 在 `runRegularTurnLoop` 等价位置（Sherry 是 LangGraph 图，找 `before_model` 等价点）每轮 model-step 前检查一次 guide 队列，每边界最多注入 1 条
- 注入方式用 2.2 的 `system-reminder` 目录（新增一个 `user_guide` source，`channel = current_turn`），**不要裸拼进 user message**——否则会污染 `context_engine` 的持久化历史
- 无 tool 边界时降级为 `QUEUE`（与 Zcode 一致）

**收益**：交互体验的直接改善，且实现面很小（队列侧加一个 mode + 一个 before_model 检查点）。

**风险**：中。必须确认 guide 注入不会被 `MessagePersistenceMiddleware` 当成真实用户消息落盘（否则历史里会出现幽灵消息），且要控制单轮注入预算（200KB 上限不能照抄，Sherry 的窗口只有 Zcode 的 1/7.6）。

---

### 3.2 工具并行安全静态标注

**Zcode 做法** [源码确认] `core/src/tool/scheduler.ts:85::canRunInParallel`：
```
destructive == true                    → 阻止
concurrentSafe == true                 → 允许
readOnly 或 sideEffectScope == "none"  → 允许
否则                                   → 阻止
```
`READ_ONLY_TOOLS`（`:233`）显式列出 9 个：`Read`、`Glob`、`Grep`、`WebSearch`、`WebFetch`、`TodoRead`、`TodoWrite`、`AskUserQuestion`、`Skill`。
调度：拓扑排序 → `groupByParallel` → 按 `maxConcurrency`（默认 10）切分并行组 → 顺序遍历各组并行执行；任一组返回 `stopTurnAfterResult` → 剩余组全部标 `ToolCancelled`。

**Sherry 现状** [实测]：`TOOL_GUARDRAILS` 有 15 键阈值（`exact_failure_warn_after` / `block_after` / `same_tool_failure_warn_after` / `halt_after`），但这是**运行时反应式**（同一工具连续失败 N 次才阻断），**不是调度前的并行安全判定**。

**差距**：Sherry 不知道哪些工具可以安全并行，只能靠 LLM 自觉串行调用。

**改造建议**：给工具定义加 3 个静态标注字段（`destructive` / `concurrent_safe` / `side_effect_scope`），在 `build_main_tools()` 注册时声明；并发调度前按标注分组。`READ_ONLY` 集合可直接照抄 Zcode 的 9 个 + Sherry 自己的 `message_search` / `ast_grep` / `lsp` 等。
标注是**纯声明式**的（不执行任何用户代码），安全风险为零。

**风险**：低。唯一的坑是标注必须准确——标错会导致并发执行产生竞态。建议首批只标「明确只读」的一批，保守起步。

---

## 四、工具实现细节（Sherry 最容易直接借鉴的部分）

这是 Zcode 工程成熟度最高的部分，也是**不涉及任何架构变更**、只改单个文件实现就能吃到收益的部分。

### 4.1 ⭐ Edit 8 级模糊匹配级联

**Zcode 做法** [源码确认] `core/src/tool/edit-matchers.ts:41-69::findEditMatch()`——固定顺序尝试 8 种策略，命中即返回：

| # | 策略 | 作用 |
| --- | --- | --- |
| 1 | `exact` | 纯子串搜索 |
| 2 | `quote_normalized` | 弯引号 → ASCII |
| 3 | `line_number_prefix_stripped` | 剥掉模型误抄的 `^\d+: ` / `^\d+\t` 读行号前缀 |
| 4 | `escape_normalized` | 反转义 `\n \t \r \" \' \` \\ \$` |
| 5 | `unicode_escape_normalized` | `\uXXXX` → 真实字符 |
| 6 | `line_trimmed` | 逐行 `.trim()` 比较 |
| 7 | `indentation_flexible` | 剥离双方公共缩进后精确比较 |
| 8 | `block_anchor` | **≥3 行；首尾行 trim 后必须匹配，中间行按平均 Levenshtein 行相似度 ≥ 0.8** |

三个关键保护：
- **歧义处理**（`:132-144`）：多候选**值不同** → `status: "ambiguous"` / `AMBIGUOUS_REPLACE` 拒绝；多候选**值相同** → 视为匹配，但非 `replace_all` 下 >1 处仍拒绝
- **`replace_all` 保护**（`:62`）：`BROAD_MATCHERS`（`line_trimmed` / `indentation_flexible` / `block_anchor`）在 `replace_all=true` 时**禁用**，防过度替换
- **引号风格保留**（`:82-105`）：文件实际用弯引号时把弯引号套回替换文本
- **不存在时给建议**（`:369-397`）：Levenshtein「Did you mean `<file>`?」

**Sherry 现状** [实测]：`patch_file` **无匹配降级策略**——`old_string` 对不上就失败。

**差距**：这是 LLM 编辑失败的最大单一来源。模型经常（a）把弯引号打成 ASCII、（b）把 `Read` 输出的行号一起抄进去、（c）缩进对不上。

**改造建议**：把 `findEditMatch()` 的 8 级级联移植到 `agent/tools/file_tools/patch_file.py`，**照抄全部三个保护**（歧义拒绝、`replace_all` 禁用 broad matcher、引号风格保留）。

**收益**：**最高性价比**。纯函数、无状态、无架构影响，可单测（8 级各有 fixture）。预计能显著降低「patch 失败 → 重试 → 再失败」的循环。

**风险**：低。唯一要注意 `block_anchor` 的 0.8 阈值需要用中文/CJK fixture 验证（CJK 的 Levenshtein 行相似度分布与英文不同）。

---

### 4.2 ⭐ Edit/Write：读-before-edit + stale 三要素 + 原子写

**Zcode 做法** [源码确认]：

*stale 检测三要素*（`core/src/tool/handlers/edit.ts:444-468`, `core/src/tool/handlers/write.ts:306-332`）：
1. `mtime` 推进（**整数毫秒**比较，抑制误报）
2. `size` 变化
3. `revisionId` 不同 —— 形如 `mtime:<ms>:size:<bytes>`（`fs/index.ts:775-777`）

*假阳性豁免*（`core/src/tool/handlers/edit.ts:434`, `core/src/tool/handlers/write.ts:295`）：若上次是**全量读**且存储内容与当前内容相等，则**即使 mtime 推进也不算 stale**——容忍 linter/formatter 触碰文件但不改字节。

*原子性*（`fs/index.ts:700-755`）：`O_EXCL|O_NOFOLLOW` 写临时文件 → `fsync` → `rename` 覆盖；失败清理临时文件并降级为 `O_NOFOLLOW` 截断写。**保留原文件 mode**（exec 位不丢）。**拒绝穿符号链接**（`SymlinkWriteRefusedError`）。

*错误分类*：`write_file_not_read`（未先读）/ `write_file_stale`（读后已改）/ `write_file_partial_view`（基于 token 截断的部分视图不可用于 Edit/Write）。Edit 另有 `MAX_EDIT_FILE_SIZE_BYTES = 1GB` 拒绝、`.ipynb` 引导改用 NotebookEdit。

*无 dry-run*：`EditInput` schema 只有 4 个字段，diff 是**事后**算的（`createStructuredPatch`，3 context，5s timeout）返回给 UI/权限展示。

**Sherry 现状** [实测]：
- 无 stale 检测（`patch_file` 读后被外部改过仍会写入 → 静默覆盖用户改动）
- 原子性未验证
- 符号链接语义未验证（`PathGuard` 管路径限制，但 symlink 穿透未确认）
- **`read_file` 的部分视图**（truncated）与 patch 之间**无关联保护**——模型可能基于截断视图 patch 出错误内容

**改造建议**：
1. **stale 三要素 + 内容豁免**（最高优先）：在 `patch_file` / `write_file` 记录读时的 `revision`（`mtime_ms:size`），写入前比对；内容相等则豁免。这一条**直接消除「静默覆盖用户改动」这个真实数据丢失场景**
2. **原子写 + 保留 mode + 拒绝 symlink**：改 `write_file` 的落盘实现
3. **部分视图保护**：把 `read_file` 的 `truncated` / `hint` 标记透传给 `patch_file`，基于部分视图的 patch 显式拒绝（照抄 `write_file_partial_view`）
4. diff 展示：照抄 `createStructuredPatch` 思路，在 patch 成功时返回结构化 diff 供前端展示

**收益**：数据安全性。三条都是纯实现改动，`patch_file` / `write_file` 两个文件。

**风险**：低-中。stale 检测会让「读之后文件被 formatter 改了」的场景从成功变失败——必须实现内容豁免，否则会引入大量假阳性。

---

### 4.3 ⭐ Read：token 感知的二分截断 + 未变更重读 stub

**Zcode 做法** [源码确认] `contracts/src/tools/read.ts:15-17`：
| 常量 | 值 |
| --- | --- |
| `READ_MAX_FILE_SIZE_BYTES` | 256 KiB |
| `READ_MAX_OUTPUT_TOKENS` | 25,000 |
| `READ_DEFAULT_MAX_LINES` | 2,000 |

- **token 感知二分**（`read-text.ts:161-236`）：超限时用**二分搜索**取 ≤ 85% token 上限的最大前缀，返回 `partialViewNotice` + 续读 offset。**UTF-8 边界安全**（按 code point 二分，不会切碎多字节字符）
- **未变更重读省 token**（`read.ts:55,190-198`）：文件未变则返回 stub `"Wasted call — file unchanged since your last Read…"`，不重复吐内容
- 行号用 `cat -n` 风格 `N\t`

**Sherry 现状** [实测]：`read_file` 有 2000 行默认 + `truncated` + `hint`，`search_files` 有**双截断标记**（`page_truncated` + `scan_truncated`，区分「结果页满」与「扫描预算耗尽」，比 Zcode 更精细）。
但 `read_file` 的截断是**行数 / 字符数**口径（`evict_threshold_chars`），**不是 token 口径**——CJK 场景下字符数与 token 数偏差大。

**改造建议**：
1. `read_file` 的截断改为 **token 口径 + 二分**（复用 `TOKEN_ESTIMATION` 的 `chars_per_token_cjk` / `chars_per_token_json`），并保证 UTF-8 code point 边界
2. 加「未变更重读」stub：记录上次读的 `revision`，未变则返回提示而非内容

**收益**：① 直接降低 CJK 大文件的 token 浪费（Sherry 的 CJK token 估算已经有专门配置，只是 `read_file` 没用上）；② 是纯粹的省 token 优化。

**注意**：`search_files` 的双截断标记是 **Sherry 领先 Zcode 的设计，应推广到 `read_file` / `web_search` 等其他有截断的工具**，不要在改造时丢掉。

---

### 4.4 子进程输出 fd 直写文件

**Zcode 做法** [源码确认] `bash-file-output.ts`：当输出超过内联预算（`maxInlineBytes` / `maxModelBytes`）时，**子进程 stdout 直接 fd 写到 artifact 文件**，Node 进程不驻留内容，只回传路径 + 预览。

**Sherry 现状** [实测]：**工具侧没有输出上限，上下文侧有**。`terminal.py` 的两个落点都用 `proc.communicate()` 把 stdout 一次性收进内存再解码（`_execute_sync` 与 `_arun`），命令输出多大就读多大，工具本身不截断；真正的保护在下游——`ContextEvictionMiddleware` 对超过 `TOOL_RESULT_EVICTION["evict_threshold_chars"]`（20,000 字符）的工具结果做落盘驱逐，只给模型留下 5 行头 + 5 行尾的预览（`config/features/agent_side/tool_result_eviction.py`）。

**结论**：`ContextEvictionMiddleware` 已经解决了「大输出灌爆上下文」，**剩下的是进程内存**——一条 `cat` 大文件仍然让整段输出驻留内存（外加驱逐文件落盘一份）。Zcode 的 fd 直写文件把这一段也省掉。

**改造建议**：优先级低于本文多数条目（上下文侧已受保护），但方向明确：输出超过内联预算时改为把子进程 stdout 直接写进 artifact 文件，只回传路径 + 预览。

**风险**：中——改动落在工具执行路径上，且要同时覆盖同步/异步与沙箱（`bwrap`）两条路径。

---

### 4.5 bfs / ugrep 注入 bash prelude

**Zcode 做法** [源码确认] `adapters/src/exec/embedded-search-prelude.ts`：向 Bash prelude 注入 shell 函数——`find()` → **bfs**（`-S dfs -regextype findutils-default`）、`grep()` → **ugrep**（`-G --ignore-files --hidden -I --exclude-dir=...`；语义不同的 flag（`-z`/`-Z`/`--null` 等）自动 bypass 回系统 grep）、`rg()` fallback 仅当 PATH 无 `rg`。二进制解析顺序 `ZCODE_{BFS,RG,UGREP}_BINARY` env → `<runtime root>/tools/` → Electron resources → dev bundle → PATH，缺失时优雅降级。

**Sherry 现状** [实测]：`search_files` 用 Python `re`（`bounded_walk` + `ScanState`），无外部搜索二进制；`terminal` 走系统 shell，无 prelude 注入。

**判断**：**收益有限，不建议优先做**。Sherry 已有 AST + LSP + 语义索引三层检索（Zcode 完全没有），主检索路径不走 grep。bfs/ugrep 注入只对「agent 在 Bash 里手写 `find`/`grep`」的场景有边际收益。

**风险**：注入 prelude 会改变 `terminal` 工具的 shell 行为（用户可能依赖 GNU grep 语义），且 ugrep 的 flag 不兼容需要 bypass 逻辑——复杂度不低。**排在最后。**

---

## 五、计量与可观测

### 5.1 ⭐ per-agent × per-attempt 用量记账

**Zcode 做法** [源码确认 + 落盘实测]：3 张表，`model_usage` 约 40 列。关键维度：

| 列族 | 列 | 说明 |
| --- | --- | --- |
| 身份 | `logical_request_id`、`turn_id`、`trace_id`、`span_id`、`assistant_message_id`、`parent_user_message_id` | 完整链路可追 |
| **Agent 归因** | `agent` | **区分主 / 子 agent** |
| 任务类型 | `mode`、`task_type`、`query_source` | `task_type`：`interactive` / `subagent_child`；`query_source`：`main_turn` / `subagent` / `compact` / `session_title` |
| 重试 | `attempt_index`、`retry_count`、`retryable` | **每次尝试一行** |
| Token（7 列） | `input`、`output`、`reasoning`、`cache_creation_input`、`cache_read_input`、`provider_total`、`computed_total` | provider 报数与本地估算**并列存储** |
| 质量 | `finish_reason`、`tool_call_count`、`status`、`duration_ms`、`time_to_first_token_ms` | |
| 失败 | `cancelled_by_user`、`context_exceeded`、`error_type`、`error_code`、`error_message` | |
| 原始 | `raw_usage_json`、`provider_metadata_json` | |

另两张表：`tool_usage`（每次 tool call 的 times / bytes / retry / approval / `side_effect_scope` / `read_only` / `destructive`，5,966 行）、`turn_usage`（每 turn 聚合，172 行）。
实测 `model_usage` 5,880 行，`agent` 列取值分布：`zcode-agent` 5,662（`interactive`）/ `zcode-general-purpose` 124（`subagent_child`）/ `zcode-Explore` 76（`subagent_child`）；`cache_read_input_tokens` 合计 2,710,812,224，`cache_creation_input_tokens` 合计 **0**，`retry_count` 合计 114，`context_exceeded` **0**，`cancelled_by_user` 14。

**Sherry 现状** [实测]：**没有**任何一张计量表。`runtime/session/count_call_register.py` 是进程内计数器，不落 SQLite、不分 agent、不分 attempt。
Sherry 已有可复用的基础：`MODEL_PRICING` 配置、`MAX_TOKENS_BOOST`、fallback chain（`models/LLMs/main_llm.py:349`）、`LLM_RETRY`、3 个跨 provider 推理归一化模块。

**差距**：以下问题目前**完全无法回答**：
- 哪个 subagent 烧的 token？（`zcode-Explore` 76 次 vs `zcode-general-purpose` 124 次一目了然）
- 哪次重试浪费了多少？（`attempt_index` + `retry_count`）
- cache 命中率多少？（Sherry 因无 `cacheHint` 分段本来就低，但没有记账就无法量化 2.3 的改造收益）
- 上下文溢出发生在哪一步？（`context_exceeded`）

**改造建议**：
1. 新增 `model_usage` 表，**先只实现 8 个必需列**：`logical_request_id` / `session_id` / `turn_id` / `agent` / `task_type` / `attempt_index` / `input` / `output` / `computed_total` / `status`。其余列按需再加
2. 写入点在模型调用返回处（`models/LLMs/main_llm.py` 的统一出口），**注意异步写入不能阻塞 turn**
3. `agent` 列从 subagent registry 取（Sherry 的 run 记录已有 agent 身份，`agent/tools/subagent/`）
4. **与 2.3 的 `cache_hint` 一起做**——没有分段就没有 cache token 可记，两者一起上才能量化收益
5. 复用 `MODEL_PRICING` 算成本，加一个只读 HTTP 端点（照 `GET /lane-status` 的形态）暴露聚合统计

**收益**：**成本最低的可观测性提升**。一张表 + 一个写入点，能回答「钱花在哪」「哪个子 agent 不划算」这类对自托管用户最重要的问题。Sherry 已有 `MODEL_PRICING` 却没用量数据，是明显的闭环缺口。

**风险**：低-中。注意 SQLite 写压力（每 attempt 一行，长会话会累积），需要考虑批量写或定期归档；以及不要把 prompt 内容写进表（只存 id 与计数）。

---

## 六、功能面

### 6.1 WebFetch（Zcode 独有，但不能照抄）

**Zcode 做法** [源码确认]：GET → **手写正则 HTML→Markdown 转换器**（`webfetch-content.ts:59-101`：剥注释/script/style/noscript，映射 h1-h6/a/li/br/block，解码实体）→ **用辅助小模型回答 `prompt`**。仅当 URL 在预批准名单且 content-type 为 `text/markdown` 且 <100k 字符时直接返回 markdown。
常量（`webfetch-constants.ts:1-11`）：`MAX_WEBFETCH_RESPONSE_BYTES` 10 MiB / `MAX_MODEL_INPUT_CHARS` 100,000（头截断 + 后缀标记）/ `MAX_WEBFETCH_MODEL_BYTES` 100,000 / `MAX_WEBFETCH_URL_CHARS` 2,000 / `MAX_REDIRECTS` 10 / 缓存 TTL 15 分钟 / 上限 50 MiB。
重定向策略：HTTP 升级 HTTPS；**仅跟随同 host（允许 `www` 变体）/ 同协议 / 同端口**；**跨 host 重定向返回给模型而非跟随**。

**Sherry 现状** [实测]：`web_search.py` 用 Tavily（`max_results=5`），有显式重试（3 次 + 指数退避 + jitter）+ `asyncio.wait_for(30s)` + **无 key 时返回解释性 stub**（比 Zcode 的「硬前置 `supportsNativeWebSearch`」更优雅）。**无 URL 抓取工具。**

**⚠️ 不可照抄的部分**：Zcode 的 SSRF 防护**只覆盖字面量**——拦 `localhost` / `*.local` / 非公网 IP 字面量 / URL 内嵌凭据 / 单标签主机名；每次 GET 前 `assertWebFetchLiteralEgress` 要求 `ipaddr.js` 的 `range() === "unicast"` 并排除基准测试段 / IPv4-mapped IPv6 / NAT64 前缀，**但域名无 DNS 预检**（源码注释明说依赖 HTTP egress proxy 兜底）→ **不防 DNS rebinding**。

**改造建议**：WebFetch 是真实的能力缺口（Sherry 只能搜、不能读），但**必须独立做 SSRF 设计**：
1. 复用到本仓库已有的防御方案（见 `TODO/防御计划_CSRF-SSRF-XSS补齐方案.md`），**不要照抄 Zcode 的字面量检查**
2. 强制**解析后 IP 校验**（resolve → 校验 → 连接该 IP，而非校验域名），关闭 DNS rebinding
3. 重定向策略照抄 Zcode 的同 host 限制（这条是好的）
4. HTML→Markdown 建议**用现成库**而非手写正则——Zcode 手写转换器是可维护性负担
5. 复用 `agent/tools/web_search.py` 已有的「无凭据降级 + 重试退避」模式

**风险**：**高（安全）**。这是本文中唯一「不补齐 SSRF 就不能做」的条目。

### 6.2 定时任务工具族

**Zcode** [落盘实测]：`CronCreate` / `CronList` / `CronUpdate` / `CronDelete` + `OffPeakCreate` / `OffPeakList`（6 个工具在 34 工具列表内）。off-peak 是「低谷期跑」的独立概念。
**Sherry** [实测]：`CRON_SERVICE` 7 键配置齐全（`degrade_backoff_base_ms`、`disabled_threshold = 10`、`max_run_history = 20`、双层退避 base/max）+ `改造计划_Cron_Skill绑定.md` 在推进 cron 与 skill 绑定。

**结论**：**Sherry 服务端能力更强**（降级背压 + 禁用阈值 + skill 绑定计划），Zcode 是工具侧暴露。
**可借鉴的只有一点**：把 cron 的触发暴露为 agent 工具（Zcode 侧是 agent 主动管理 cron），让模型能「检查自己的定时任务状态」。`OffPeak` 的低谷期概念 Sherry 暂无对应，可作为低优先候选。

### 6.3 会话上下文回读

**Zcode** [落盘实测]：`ReadSessionContext` 独立工具（34 工具列表内）。
**Sherry** [实测]：`message_search` 工具 + `message_search_checkpoint_fallback_enabled = True`（检查点回退）+ `message_search_max_session_chars = 100,000`。

**结论**：**Sherry 更完整**。不需要改造。

### 6.4 大窗口利用（1M 上下文）

**Zcode** [落盘实测]：实际使用 1M 窗口，压缩触发点 966K，配 `modelIoFullRetentionEnabled` 控制模型 I/O 留存。
**Sherry** [实测]：`config/schema.py` 的 `context_window_tokens` 默认 65,536，`MIN_REQUIRED_MAX_TOKEN = 131072`，`models/LLMs/main_llm.py::max_tokens = 131,072`。

**结论**：**模型选择问题，不是架构问题**。Sherry 的压缩机制能正确处理更大窗口。
**改造建议**：验证 `main_llm_context_window` 参数化是否已贯通——`agent/core.py:146` 已接受该参数，压缩触发按 `window × 0.8` 自动算。**换模型即生效，零代码改动。** 但需实测 1M 窗口下的压缩行为（`SUMMARIZATION.max_preserve_tokens = 15000` 在 1M 窗口下是否过小、`critical/key_decisions/active_plan_notes` 三个 max_items 是否够用）。

---

## 七、不建议照搬

| 项 | 原因 |
| --- | --- |
| 桌面端专属能力 | `closeToTrayOnWindows`、`keepAwakeWhileRunning`、`desktopChromiumHardwareAccelerationEnabled`、`embeddedBrowserViewportPreference`（393×852 移动视口）、`proactiveSuggestionsEnabled`、`nativeSearchEnhancementsEnabled`、Bots（IM 接入）——Sherry 的定位是自托管服务端，桌面端是客户端而非 agent 能力 |
| CUA 电脑操作 | `computerUseComposerEntryHidden` 对应的能力会引入新的安全边界，与现有 `PathGuard` 姿态冲突，需独立评估 |
| workflow `vm` 沙箱 | Node 官方明说 `vm` 非安全沙箱；Zcode 自身暴露了外部 realm 函数。**这是 containment，不是加固的安全边界**。若要脚本化必须换真隔离方案（见 2.5 第 2 步） |
| 插件 hook 无条件执行 | `canRunPluginHooks` 无条件 `return true`，第三方插件以用户权限执行任意进程。**Sherry 已有 SkillSpector，hook 必须复用同一信任模型**（见 2.1） |
| `keptMessageCount = 1` 的激进压缩 | 2027 条消息压成 1 条。Sherry 的分层保留更成熟，应保留并强化（见 1.6） |

---

## 八、优先级与投入产出

按「收益 / 成本」排序。**成本**列的估计基于 Sherry 现有代码结构，未做实现前评估。

| 优先级 | 条目 | 收益 | 成本 | 依赖 / 前置 |
| --- | --- | --- | --- | --- |
| **P0** | 4.2 stale 三要素 + 原子写 | **高**（消除静默覆盖用户改动） | 低（2 个文件） | 无 |
| **P0** | 4.1 Edit 8 级匹配级联 | **高**（降低 patch 失败循环） | 低（纯函数，可单测） | 无 |
| **P0** | 6.4 大窗口利用 | 高 | **零代码**（换模型） | 实测 1M 下压缩行为 |
| **P0** | 1.3 Session token 预算封顶 | 高 | 低（加状态键 + 检查点） | 无 |
| **P0** | 5.1 per-agent 用量记账 | 高 | 低（1 表 + 1 写入点） | 与 2.3 一起做收益最大 |
| **P1** | 4.4 核查 terminal 大输出 | **未知，可能高** | **先核查** | 依赖 4.4 核查结论 |
| **P1** | 2.2 system-reminder 目录化 | 中-高 | 低（只加目录与包装层） | 2.3 可并行 |
| **P1** | 1.4①③ journal 短路 + 血缘 | 高 | 中（migration + 幂等性验证） | **必须先验证 step 重入幂等** |
| **P1** | 2.3 Prompt cache 分段 | 中-高 | 中（跨层 seam 改动） | 需更新 `tests/workspace/` |
| **P1** | 4.3 Read token 感知二分 | 中 | 低-中 | 无 |
| **P1** | 2.5① step 挂载 skill | 高 | 中（复用现有 skill 体系） | 无 |
| **P2** | 1.4 文件快照 + revert 工具 | 中-高 | 中（磁盘占用策略） | 依赖 4.2 stale 检测 |
| **P2** | 3.1 `guide` 输入语义 | 中 | 中（队列 + 注入点） | 依赖 2.2 目录化 |
| **P2** | 3.2 工具并行安全标注 | 中 | 低（纯声明式） | 无 |
| **P2** | 2.4 计划模式 | 中 | 低（复用 HITL 骨架） | 与 1.5 协调 |
| **P2** | 1.5 流恢复 tool ledger | 中 | 中（前后端协同） | 无 |
| **P2** | 6.1 WebFetch | 中 | **高（SSRF 必须重做）** | 独立安全设计 |
| **P3** | 1.1②④ 显式挂起 + 步骤缓存 | 低-中 | 中 | 依赖 1.1① |
| **P3** | 1.2 taskflow 并行执行 | 中 | 高（judge/evidence 并发安全） | 依赖 1.1 |
| **P3** | 1.6 microcompact 独立通道 | 低 | 低 | 无 |
| **P3** | 6.2 OffPeak 低谷期 | 低 | 低 | 无 |
| **P3** | 4.5 bfs/ugrep prelude | 低 | 中（shell 语义变更） | 排在最后 |
| **P3** | 2.1 Hooks（分三步） | 高 | **高（安全设计）** | 2.1① 只读型可先上 |
| ❌ | 桌面端 / CUA / workflow 脚本化 | — | 很高 | 见第七节 |

**建议起手顺序**：
1. **第一批（零风险、可并行）**：4.1 + 4.2 + 1.3 + 3.2 —— 全部是单文件实现改动，不碰架构，可在一个批次内验证
2. **先核查再决策**：4.4（terminal 大输出）—— 这是本文唯一结论未知的项，核查成本极低但潜在收益可能超过上面所有条目
3. **第二批（需设计）**：5.1 + 2.3 一起做（用量记账与缓存分段是同一个闭环，分开做都看不到收益）；2.2 目录化作为 3.1 的前置
4. **第三批（需原型验证）**：1.4①③ —— 价值最高但依赖「step 重入幂等性」这个**未经证实的假设**，必须先做原型
5. **独立立项**：2.1 Hooks 与 6.1 WebFetch 都涉及新的安全设计，不应混在常规迭代里

---

## 九、已确认无需改造的项

对标后确认 Sherry 已具备或更优，记录以免重复讨论：

| 能力 | Zcode | Sherry 结论 |
| --- | --- | --- |
| Todo 持久化 | `todo` 表 30 条 [落盘实测] | **Sherry 更强**（停滞熔断 + 冷却） |
| Cron 服务端 | 6 个工具侧 | **Sherry 更强**（降级背压 + 禁用阈值 + skill 绑定计划） |
| 会话上下文回读 | `ReadSessionContext` | **Sherry 更完整**（checkpoint fallback + 100K 字符上限） |
| 压缩有效性检测 | 连续 3 次失败熔断 | **Sherry 已有**（`max_recovery_attempts=2` + `ineffective_threshold=2`） |
| 并发显式管理 | 批内 10 | **Sherry 更明确**（四类 lane + 启动校验 + 排队不拒绝 + `/lane-status`） |
| delegation 深度限制 | ❌ 无 | **Sherry 独有**（默认 2 / 硬上限 2） |
| subagent 完成判定 | 程序化终态等待 | **Sherry 明确领先**（4–6 层门：schema gate / StepJudge / CompletionJudge / evidence ledger / finish gates A–D / drain） |
| 代码智能 | ❌ 无内建索引 | **Sherry 明确领先**（tree-sitter 符号索引 + ast-grep + LSP + 语义检索） |
| 搜索分层 | ripgrep + bfs/ugrep | **Sherry 更厚**（且 `search_files` 双截断标记比 Zcode 精细） |
| 输出截断策略 | 单一 ratio | **Sherry 更精细**（4 触发点 + overflow router + 头尾双预览 + 多媒体分项估算） |
| 记忆系统 | Markdown 文件，agent 自维护 | **Sherry 更厚**（MesMemory + FTS5 trigram + curator + 4 条经验提取路径 + 记忆 flush） |
| 多模态 / 语音 | 图/视频/PDF，无语音 | **Sherry 独有**（ITTT/VTTT/STT/TTI + 20MB 上限 + 多媒体分 token 估算） |
| 模型路由韧性 | 单一模型 | **Sherry 独有**（fallback chain + `MAX_TOKENS_BOOST` + 跨 provider 推理归一化） |
| 可靠性与运维 | 局部 hook failed 状态 | **Sherry 更厚**（`LLM_RETRY` / `RETRY_BACKOFF` / `PERIODIC_BACKOFF` / `CRASH_LOOP` / `HEARTBEAT_STALENESS`） |
| 搜索降级体验 | 硬前置，工具直接消失 | **Sherry 更优雅**（无 key 时返回解释性 stub） |

---

## 十、不确定性声明

结论的可信度边界，必须随本文一并阅读：

1. **无法运行 Zcode**：`zcode --help` 因 `/opt/ZCode/chrome-sandbox` SUID 配置错误（需 root 修复为 root:4755）直接 abort。**所有 Zcode 行为结论均为静态取证，无运行时验证。** 精确报错：
   ```
   [8154:0930/215022.500097:FATAL:sandbox/linux/suid/client/setuid_sandbox_host.cc:166]
   The SUID sandbox helper binary was found, but is not configured correctly.
   Rather than run without sandboxing I'm aborting now. You need to make sure
   that /opt/ZCode/chrome-sandbox is owned by root and has mode 4755.
   ```
2. **源码版本与发布版可能不一致**：源码树 `apps/zcode-cli` 版本 **0.16.9**，发布版 **v3.14.4**。源码可读且行号精确，但**不能保证与发布构建逐行一致**。唯一强交叉验证项：压缩阈值 966,000 与落盘 `autoCompactThreshold` 精确吻合（1,000,000 − 21,000 − 13,000）。
3. **`session_target` 表本机 0 行** [落盘实测]：1.3 的 token 预算能力只有 schema 证据，**行为完全未验证**（预算耗尽是否真转 `budget_limited`、是否真暂停）。
4. **workflow / bot / off-peak 8 张表本机 0 行** [落盘实测]：第 1.1、2.5 节的 workflow 结论基于源码逻辑推断，**实际运行行为未验证**。
5. **`guide` 模式本机从未使用** [落盘实测]：`session_input` 214 行只用 `startNow`（107）与 `queue`（92 + 15 cancelled），`guide` 代码存在但真实行为未验证。3.1 的设计判断基于源码。
6. **`vm` 逃逸未验证** [行为推断]：`__send.constructor("return process")()` 是理论推断，**未执行**。不应作为已确认漏洞，仅作为「vm 非安全边界」的风险提示。
7. **落盘计数是快照且库为活库**：`~/.zcode/cli/db/db.sqlite` 仍在写入，本文所有行数（509 checkpoint / 5,880 model_usage / 30 todo）为 2026-09-30 快照值，会随使用增长。**趋势可信，绝对值会变。**
8. **Sherry 侧唯一未验证项已核查**：`terminal.py` 的大输出处理（4.4）——工具侧无上限、上下文侧由 20k 字符驱逐兜底，结论已就地改写。
9. **本文未评估 Zcode 的遥测/隐私行为**：`config.isTelemetryEnabled` 等设置项存在，但该 checkout 中遥测实现不完整，未做结论。
10. **本文未记录 Zcode 的安全缺陷**（反向议题）。部分缺陷已就地标注为「不可照抄」（附数据来源表里标了源码位置），但完整安全评估需另文。

---

## 附：数据来源

本文每一节的结论都在正文就地标注证据，来源清单如下（路径以本机源码树 `/home/honor/Desktop/project/ZCode` 为根；`.../` 承接上一行的前缀）：

### Zcode 侧

| 来源 | 用途 | 分级 |
| --- | --- | --- |
| `/home/honor/Desktop/project/ZCode/apps/zcode-cli/packages/core/src/context/builder.ts:82-310` | Prompt 三段组装、14 section、注入排序 | 源码确认 |
| `.../core/src/system-reminder/source.ts` | 27 reminder 源、3 组、6 channel、嵌套转义 | 源码确认 |
| `.../contracts/src/hooks/index.ts:7-425` | 7 事件、能力矩阵、超时、matcher | 源码确认 |
| `.../core/src/hooks/{runner,output,configured-runner}.ts` | hook 执行、阻断、顺序 | 源码确认 |
| `.../adapters/src/plugins/index.ts:366` | 插件 hook 无条件执行（否决照抄项） | 源码确认 |
| `.../core/src/agent/turn-machine.ts` + `turn-state.ts:25,149` | 10 phase、6 终态 | 源码确认 |
| `.../contracts/src/events/session.events.ts:83-176,696-711` | ~90 事件、14 流式 kind、7 流恢复事件 | 源码确认 |
| `.../core/src/runtime/command-queue.ts` + `helpers/steering.ts:11` | 6 mode、3 priority、200KB 上限、5 拒绝原因 | 源码确认 |
| `.../core/src/runtime/methods/turn-guide-drain.ts` | `guide` 注入与降级 | 源码确认 |
| `.../core/src/runtime/methods/rewind-message.ts:404` + `contracts/src/rewind/index.ts:100,176` | append-only 分支、`branchGeneration`、scope/strategy | 源码确认 |
| `.../core/src/runtime/methods/session-fork.ts` | 6 条 fork 路径 | 源码确认 |
| `.../core/src/tool/edit-matchers.ts:30-397` | 8 级匹配、Levenshtein ≥0.8、歧义处理 | 源码确认 |
| `.../core/src/tool/handlers/{edit,write}.ts` | read-before-edit、stale 三要素、原子写 | 源码确认 |
| `.../adapters/src/fs/index.ts:452-647,700-777` | WASM ripgrep、原子写、revision | 源码确认 |
| `.../contracts/src/tools/read.ts:15-17` + `read-text.ts:161-236` | token 二分截断、85% 上限、未变更 stub | 源码确认 |
| `.../core/src/tool/scheduler.ts:48,85,187,233` | 并发 10、并行判定、只读工具集 | 源码确认 |
| `.../dynamic-workflow/src/engine/types.ts` + `.../dynamic-workflow-runtime/src/child-source.ts:260-314` | workflow 状态机、vm realm、确定性禁令 | 源码确认 |
| `.../adapters/src/storage/session-store/{migrations.ts,repositories/dwf-journal-codecs.ts}` | journal 短路、状态映射、无预算列 | 源码确认 |
| `~/.zcode/cli/db/db.sqlite` | 全部表 DDL、行数、聚合统计 | 落盘实测 |
| `~/.zcode/cli/rollout/model-io-*.jsonl` | 34 工具列表、prompt 分段实测、压缩事件 | 落盘实测 |
| `~/.zcode/v2/setting.json` | 桌面/任务/记忆设置项 | 落盘实测 |

### Sherry 侧

| 来源 | 用途 |
| --- | --- |
| `config/features/agent_side/*.py`（33 项）+ `infra_side/*.py`（20 项） | 全部 53 项配置实测值 |
| `agent/core.py:146` | `main_llm_context_window` 参数 |
| `agent/tools/__init__.py::build_main_tools()` 实际调用 | 36 主工具真值 |
| `agent/tools/file_tools/{read_file,write_file,patch_file,search_files,search_scan}.py` | 文件工具实现与双截断标记 |
| `agent/tools/taskflow/{config,registry/store_sqlite}.py` | StepStatus 7 态、SQLite WAL 持久化（修正首轮误判） |
| `agent/tools/subagent/` | spawn 路径、两轴角色模型、完成门 |
| `agent/middlewares/humanInTheLoop/core.py:159-287` | interrupt / resume 契约、GraphInterrupt 重抛 |
| `workspace/prompt_builder.py`（324 行）+ `tests/workspace/test_prompt_builder*.py` | 7 个 block 组装、待改动的测试面 |
| `runtime/data_provider.py` + `agent/prompt_data_provider.py` | Prompt 跨层 provider 协议 |
| `runtime/lane/core.py` + `server/service/lane_lifecycle.py` | 4 类 lane、启动校验、drain |
| `server/service/input_queue_service.py:142-415` | TurnExecutor 协议、幂等、CLAIMED 占位、锁外 dispatch |
| `models/LLMs/main_llm.py:349` | `build_fallback_chain()` |
| `agent/tools/web_search.py:33-89` | Tavily 集成、无 key 降级、重试退避 |
| `agent/tools/pub_base/sqlite_store.py` | 1.3 / 5.1 新表可复用的仓储骨架 |
