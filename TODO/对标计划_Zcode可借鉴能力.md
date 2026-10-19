# 对标计划: Zcode 可借鉴能力

> **状态**: 持续更新（2026-10-17 第四轮：**1.2 taskflow 并行执行全量落地**（含三个并发缺陷修复与血缘指针）、**2.1 Hooks Step 1 落地**（只读三事件，Step 2/3 待做）；此前 2026-10-14 第三轮核验：6 项已完成、2 项部分完成、7 项经评估不需要做、1 项已合并）
> **创建日期**: 2026-09-30（2026-09-30 第二轮源码核验后重写）
> **最近更新**: 2026-10-14 — §1.1 四条差距逐一评估（①②④ 不做、③ 合并）；新增 §1.6/§1.5 完整协议/§2.3 续/§6.2 四项不做（附理由）；§6.4 重定位为非 ZCode 特性
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
> 2. **本文不记录 Zcode 的安全缺陷**（Bash 无沙箱、插件 hook 无信任门、遥测等）——那是反向议题，需另文。但**部分缺陷会否决本文的照搬建议**（如 WebFetch 的 SSRF），相关条目已就地标注，附数据来源表里也标了「否决照抄项」的源码位置。
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
| 用户可配生命周期 hook | ✅ 7 事件 [源码确认] | 🔨 Step 1 已落地（PostToolUse / PostToolUseFailure / Stop 只读派发，总开关默认关）[实测，2026-10-17 更新] | **Zcode 仍领先**（可阻断 / 可改 input / 更多事件） |
| 结构化上下文注入目录 | ✅ 27 个 `system-reminder` 源 [源码确认] | ✅ `reminder_catalog` 统一目录 + 30K 预算 [实测，2026-10-14 新增] | **平**（Sherry 已补齐） |
| Prompt 缓存分段 | ✅ `stable` / `dynamic` + `cacheHint` [源码确认] | 🔨 stable/dynamic 已分离（cached system prompt vs per-turn reminders），未拆独立 message [实测，2026-10-14 更新] | **Zcode 仍领先**（wire 层分段） |
| turn 内联输入（`guide`） | ✅ 3 种投递语义 [源码确认] | ❌（2 种）[实测] | **Zcode** |
| per-agent 用量记账 | ✅ 3 张表，`model_usage` 5,880 行 [落盘实测] | ❌（进程内计数器不落库；trajectory ledger 已有但不记 token）[实测] | **Zcode** |
| 流恢复锚点 | ✅ 7 个流恢复事件 [源码确认] | ✅ 快照续传 (`resume_stream` + `seq` 水位) + trajectory ledger [实测，2026-10-18 更新] | **核心目标已追平**（协议不同：快照 vs 事件锚点；reasoning/tool-input 细粒度仍逊于 Zcode） |
| Edit 模糊匹配 | ✅ 8 级级联 [源码确认] | ✅ 11 级级联 [实测，2026-10-14 新增] | **Sherry**（多 3 级） |
| Write 原子性 | ✅ temp+fsync+rename+`O_NOFOLLOW` [源码确认] | ✅ temp+fsync+rename+`O_NOFOLLOW` + 跨进程 flock [实测] | **Sherry**（多了跨进程锁） |
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

### 1.1 workflow journal 重放：四条差距逐一评估，仅血缘指针保留（首轮误判已修正）

> **2026-10-14 深度评估后更新**：原四条差距经逐一分析，①②④ 不需要做（详见下文），③ 血缘指针已合并到 `计划_taskflow并行执行.md` §8。原独立的 `计划_workflow_journal重放.md` 已删除。
> **2026-10-15 完成**：`计划_taskflow并行执行.md` 全量落地（Phase 0 并发缺陷修复、Phase 1-2 有界并发派发、Phase 3 血缘指针），计划文件已删除；设计文档见 `docs/long-running-tasks/taskflow/README.md`（Flow Lineage 一节）。

**首轮写的**：「Sherry 进程重启后无法从中间步骤恢复，只能整个 task 重跑」——**这条是错的，已撤回**。

**实际情况** [源码确认 vs 实测]：Sherry taskflow 状态是 SQLite WAL 持久化的，`get_active_flows_sync(session_id)` 从 SQL 侧直接取非终态 flow（`agent/tools/taskflow/registry/store_sqlite.py` + `agent/prompt_data_provider.py:37-41`），进程重启后照样读得到。`StepStatus` 7 态中 `dispatched` 是可重入中间态，不是终态（`agent/tools/taskflow/config.py:35-41`）。

**四条差距逐一评估** [源码确认 + 实测，2026-10-14]：

| # | 维度 | Zcode | Sherry 现状 | 评估 |
|---|---|---|---|---|
| ① | 已完成步骤是否重跑 | journal 短路（`settleCachedAsk`，不调 driver）+ `inputHash` 校验 | DONE 硬拒绝 re-dispatch + DONE 不可变 + replan 不重置 DONE + result 从 `state["results"]` 复用 | **不需要做**——DAG 结构已防止重跑 |
| ② | 显式挂起 | `stop("user")` 实际 abort in-flight ask | `taskflow_cancel` 杀子 agent + `set_waiting` 暂停 flow | **不需要做**——PAUSED 不杀子 agent 无实际效果 |
| ③ | 血缘指针 | `dwf_run.resumed_from` + `superseded_by` | `task_flows.resumed_from` + `superseded_by` | **✅ 已完成**（2026-10-15，`taskflow_create(resumed_from=…)` 写入，`docs/long-running-tasks/taskflow/` Flow Lineage） |
| ④ | 步骤级结果缓存 | `cached` 事件标志 + `input_hash` 命中 | result 已在 `state["results"]` 被下游 `build_task_with_dep_results` 复用 | **不需要做**——result 复用已工作 |

**① 不做的理由**：ZCode 是确定性脚本重放引擎——每次 resume 从头重跑脚本，journal 短路是唯一跳过已完成节点的方式。Sherry 是事件驱动 DAG——step 只在依赖满足时被 dispatch，DONE step 永远不会被再次 dispatch。四层已有防护：`taskflow_dispatch` 的 `dispatchable` 硬拒绝（`:92-98`）、`taskflow_update_steps` 的 DONE 不可变（`:123-133`）、`taskflow_replan` 只 re-block 非 DONE step（`:75-96`）、下游 `build_task_with_dep_results` 已从 `state["results"]` 拉取 DONE step 的 result（`_shared.py:174-214`）。`input_hash` 要短路的是"DONE step 被尝试 re-dispatch 且 input 没变"——但这个场景在 Sherry 里不会发生。

**② 不做的理由**：PAUSED 只改 step 状态标记不杀子 agent——子 agent 照跑、result 照注入、下游照 unlock。ZCode 的 `stop("user")` 有效是因为它实际 `abortInFlight`（取消在途 LLM 调用、reject deferred、emit cancelled）。Sherry 已有 `taskflow_cancel`（杀子 agent + 取消 step）和 `taskflow_set_waiting`（暂停整个 flow）覆盖这个需求。

**④ 不做的理由**：result 已在 flow 的 `state["results"]` 列表里，下游 step 的 `build_task_with_dep_results` 直接拉取，不需要额外缓存机制。

**注意**：Sherry 在**失败级联语义**和**节点上限**上明确更完整——`failed` 的依赖者保持 `blocked`（绝不被失败解锁）、`failed`/`skipped` 级联 `skipped`（`config.py:24-28`），Zcode 只有 `skipped`。不要为了学 resume 语义而改坏这块。

---

### 1.2 ✅ taskflow 并行执行（**已落地，2026-10-16**）

**Zcode 做法** [源码确认]：`Caps { maxConcurrency }` 在 run 提交时固定并持久化到 `dwf_run.caps_max_concurrency`，之后可通过 `WorkflowEngine.setMaxConcurrency`（`engine.ts:579-589`）**热调**。上限公式 `max(1, min(16, availableParallelism() - 2))`，外加按 `provider/model` 分桶的进程级 governor。

**落地结果** [实测，2026-10-16]：三个 dispatch 入口全部改为 `asyncio.gather` 并发 spawn，由
`TASKFLOW_INFRA["max_parallel_dispatch"]`（默认 **2**，灰度起步；设为 1 精确还原顺序行为）的信号量限流；
SUBAGENT lane（8）仍是全局背压。部分失败不再中止兄弟步骤：成功的全部持久化（一次
`update_flow_with_conflict_retry`）、失败的点名报告。下面的三个并发缺陷同批修复，血缘指针
（1.1③）随本项一起落地。

**落地位置**：`agent/tools/taskflow/tools/{taskflow_dispatch,taskflow_resume,taskflow_wait_all}.py`、
`config/features/agent_side/taskflow_infra.py`、`agent/tools/pub_base/evidence_ledger.py`；
测试 `tests/agent/tools/taskflow/{test_concurrent_resume,test_concurrent_dispatch,test_no_double_dispatch,test_parallel_dispatch_failure,test_evidence_concurrent,test_lineage}.py`；
设计文档 `docs/long-running-tasks/taskflow/`（四语 "Auto-Dispatch Wave" + "Flow Lineage" 两节）。

**与原计划的偏差（均已写进 docs）**：血缘指针的写入点是 **create 路径**
（`taskflow_create(resumed_from=…)` 盖前驱的 `superseded_by`），因为 `taskflow_replan` 是原地重写、从不分叉新流。
计划文件 `计划_taskflow并行执行.md` 与 `计划_证据链修正.md` 均已退休；下方「并发缺陷 / 实施计划 /
测试」三块是**落地前的设计原文**，保留作设计依据（全部已执行）。

**原差距（存档）**：taskflow 里两个互不依赖的 step 无法并行 spawn。

#### 前置：并发缺陷（**已修，2026-10-16**；下方为当时的核验原文）

[源码确认 + 实测，2026-10-14 深度核验]

**缺陷 1（致命）：`taskflow_resume` 的正常完成路径无 conflict retry**
- 位置：`taskflow_resume.py:383-397`
- 正常路径（step DONE）直接调 `store_sqlite.update_flow`，失败返回 `conflict_error(exc)` 字符串
- redispatch 路径才用 `update_flow_with_conflict_retry`
- **后果**：两个并行 step 同时完成 → 两个 `taskflow_resume` 同时写同一 flow → 一个赢、一个 `FlowConflictError` 无 retry → **输掉的那个 result 被静默丢弃，不重试**
- `delivery.py::_try_auto_resume_taskflow` 对返回值只 log 不检查（line 404 `return True` 无条件），所以错误被完全吞掉

**缺陷 2（致命）：`_auto_dispatch_ready` 读改写竞态 + 无 conflict retry**
- 位置：`taskflow_resume.py:430-501`
- 第 451 行读 steps（第一次读），第 488 行再读 `latest`（第二次读，拿 revision），但第 491 行写的是 `{**dict(latest["state"]), "steps": steps}` —— **steps 用的是第一次读的旧列表**，期间其他 writer 的修改被覆盖
- 第 497-500 行 bare `try/except Exception: logger.exception(...)` —— **已 spawn 的子 agent 的 `child_session_key` 未持久化**（这是 `update_flow_with_conflict_retry` 正好解决的场景，但这里没用它）
- **后果**：两个并行 resume 都 unlock + dispatch → 重复 dispatch 同一 step、丢失已 spawn child 记录、互相覆盖 step 修改

**缺陷 3（中）：evidence ledger 无文件锁**
- 位置：`agent/tools/pub_base/evidence_ledger.py` — 追加写 JSONL（`open(path, "a")`），无 `file_write_lock` / `cross_process_lock`
- `read_all()` 无 per-line `try/except`，partial line 会让 `json.loads` 崩 → `collect_evidence_summary` 崩 → `taskflow_resume` 崩 → `_try_auto_resume_taskflow` 静默吞掉
- 仓库已有 `pub_base/file_lock.py`（flock / msvcrt.locking）但 evidence ledger 没用

**缺陷 4（低）：`_try_auto_resume_taskflow` 无条件返回 True**
- 位置：`delivery.py:366-407`
- `taskflow_resume` 返回 success 字符串或 `Error:` 字符串，`_try_auto_resume_taskflow` 都 `return True`
- error 被 log 但不传播 → announce pipeline 认为成功了

#### 实施计划（**已全部执行完毕**，下方为当时的计划原文，保留作设计依据）

**Phase 0：修复并发缺陷（不做这步就并行 = 丢数据）**

| 步骤 | 文件 | 改动 |
|---|---|---|
| 0a | `taskflow_resume.py:383-397` | 正常完成路径改用 `update_flow_with_conflict_retry`（和 redispatch 路径一致），`build_state` callback 重新应用 step-done + result-injection + unlock 逻辑到 fresh state 上 |
| 0b | `taskflow_resume.py:430-501` (`_auto_dispatch_ready`) | 改用 `update_flow_with_conflict_retry`；写前用 `latest["state"]["steps"]` 而非第一次读的旧 steps；`build_state` callback 重新应用 dispatched-step payloads 到 fresh steps（复用 `apply_dispatched_steps`） |
| 0c | `agent/tools/pub_base/evidence_ledger.py` | 写路径加 `file_write_lock`（复用 `pub_base/file_lock.py`）；`read_all` 加 per-line `try/except JSONDecodeError`（skip partial lines，log warning） |
| 0d | `delivery.py:366-407` | 检查 `taskflow_resume` 返回值是否以 `Error:` 开头，是则 log + return False（不阻断 announce 但留下可追溯的错误状态） |

**Phase 1：并行化 spawn 循环**

| 步骤 | 文件 | 改动 |
|---|---|---|
| 1a | `taskflow_dispatch.py:116-144` | `for sid in requested: await dispatch_child(...)` → `asyncio.gather(*[dispatch_child(...) for sid in requested], return_exceptions=True)`；收集成功/失败，持久化成功的（一次 `update_flow_with_conflict_retry`）；批量失败时不 break，而是记录哪些成功 |
| 1b | `taskflow_resume.py::_auto_dispatch_ready:456-485` | 同上模式，`newly_ready` 的 dispatch 改 `asyncio.gather` |
| 1c | `taskflow_wait_all.py:143-169` | `_retry_settled_steps` 的 retry 循环改 `asyncio.gather` |

**Phase 2：配置 + 背压**

| 步骤 | 文件 | 改动 |
|---|---|---|
| 2a | `config/features/agent_side/taskflow_infra.py` | 新增 `max_parallel_dispatch: int`（默认 2，灰度起步；上限不超过 `subagent_max_concurrent`） |
| 2b | dispatch 入口 | 加 `asyncio.Semaphore(max_parallel_dispatch)` 限制单次 dispatch 调用的并发 spawn 数（lane 系统已提供全局背压，但 per-dispatch-call 信号量防止单次 dispatch 炸 50 个子 agent） |

**Phase 3：测试**（已落地，文件名见上方「落地位置」）

| 测试 | 场景 |
|---|---|
| `test_concurrent_resume` | 两个 step 同时完成 → 两个 `taskflow_resume` 并发 → 两个 result 都成功注入（conflict retry 验证） |
| `test_concurrent_auto_dispatch` | 一个 step 完成后 unlock 3 个 ready → 3 个并发 spawn → 3 个 child_session_key 都持久化 |
| `test_no_double_dispatch` | 同一 step 被两个并发 resume 触发 dispatch → 只 spawn 一次（CAS 验证） |
| `test_evidence_concurrent_write` | 并发写 evidence ledger → 无 partial line 崩溃 |
| `test_parallel_dispatch_failure` | 5 个 step 并发 dispatch，2 个失败 → 3 个成功的持久化 + 2 个失败的报告 |

#### 已确认安全的组件（无需改动）

| 组件 | 原因 |
|---|---|
| `StepJudge` | 无状态纯函数，per-step 隔离，不读/写共享可变状态 |
| `CompletionJudge` | 无状态纯函数，per-run 隔离 |
| `push_taskflow_progress` | 只读同步 + best-effort WS push，并发安全（冗余帧无害） |
| SQLite store（WAL + busy_timeout） | 引擎层串行化写入，CAS 拒绝 stale 写 |
| `update_flow_with_conflict_retry` | 本身就是为并发设计的（CAS + bounded retry），Phase 0 只是让其他路径也用它 |
| `dispatch_child` → `spawn_subagent_direct` | 返回后子 agent 是后台 asyncio task，lane 系统已管理并发（8 SUBAGENT slot） |

**风险**：Phase 0 是硬前置——不修缺陷 1/2 就并行化 = 必然丢数据。Phase 0 本身无新风险（只是把已有的 conflict-retry 机制扩展到两条遗漏路径）。Phase 1 的风险在 `build_state` callback 的正确性——它必须能将 step-done + result-injection + unlock 逻辑重新应用到任意 fresh state 上（当前 `apply_dispatched_steps` 已为 redispatch 路径做了这件事，模式相同）。

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

### 1.5 ✅ 流恢复锚点（核心目标已完成：snapshot + seq；ZCode 7 事件协议不做）

> **2026-10-18 核验：已完成。** 实现方式不是 ZCode 的事件级锚点，而是**快照续传**：
> 断线重连后不重跑、不重放、不重复，文本从服务端的权威快照继续。
>
> - `server/service/stream_dispatch.py::active_turn_snapshot` 读取运行中 turn 的 `ai_text`
>   和当前 `seq`；没有运行中的 turn 时返回 `None`。
> - `server/trigger/ws/messages.py::_handle_resume_stream` 只向请求 socket 回复
>   `stream_resume`，状态为 `ok` / `ended` / `gone` / `unknown`；不广播、不重放历史帧。
> - 客户端 `agent-socket.ts` 在断线且本轮已收到过 chunk 时发送 `resume_stream`；
>   `agent-socket-frames.ts` 记录快照的 `seq` 水位，`use-chat-stream.ts` 用快照文本
>   **替换**当前 AI 行（先 flush buffered deltas），并丢弃 `seq` 以下的所有后续帧。
> - 测试：`tests/server/test_stream_resume.py`（广播恰好一次、死 socket 剔除、快照只回请求方、
>   跨 session 隔离）与 `client/app/composables/__tests__/agent-socket-resume.test.ts`。
>
> **未覆盖**（因此仍不是 ZCode 的完整 7 事件协议）：reasoning 增量、tool input JSON 增量、
> usage 遥测的 delta 级续传；这些场景下客户端拿不到断线期间的细粒度流。已 committed 的
> tool call 仍可在 trajectory ledger（`server/service/trajectory_store.py`，
> `GET /sessions/:session_id/trajectory`）里查到 start/end。
>
> **结论**：以“断线恢复、不重放、不重复”为目标，1.5 已完成；ZCode 的 7 事件锚点协议
> 作为事件粒度更细的另一种实现，不再作为 backlog。

**Zcode 做法** [源码确认]：7 个流恢复事件构成一套完整协议：
```
stream_recovery_anchor_created → stream_recovery_started → anchor_selected
→ tail_discarded → retry_started → retry_blocked
+ streaming_tool_ledger_updated
```
即「流中途断线后，从锚点续传并**丢弃不确定尾部**」。配套 14 种流式 kind（`ModelStreamingKind`：`start` / `text_*` / `reasoning_*` / `tool_input_*` / `tool_call` / `finish` / `error`），客户端能分别拿到 reasoning 流与正文流、增量 tool input JSON、usage 遥测。

**Sherry 对照**：核心目标（不重跑、不重放、文本续上、丢弃不确定尾部）由上述 snapshot + seq 方案完成；事件粒度的 reasoning/tool-input 续传不做。trajectory ledger 保留为已 committed tool call 的查询面。

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

## 二、扩展点（Zcode 最值得学的工程化部分）

### 2.1 🔨 用户可配生命周期 Hook（**Step 1 已落地 2026-10-16**；Step 2 / Step 3 待做）

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

**Step 1 落地结果** [实测，2026-10-16]：`agent/middlewares/user_hooks/` 派发
`PostToolUse` / `PostToolUseFailure`（`toolName` / `toolCallId` / 有界结果·错误预览 / `durationMs`）与
`Stop`（`responsePreview` / `toolCallCount`）——只读，不能阻断 / 改 input / 注入上下文。配置在
`sherry.jsonc` 的 `hooks` 段（`config/sherry_settings.py::load_sherry_hooks` 校验），总开关
`hooks.enabled` **默认关**（入库的配置不得自行让 agent 执行命令——这是对原计划「列了就跑」的安全修正）；
子进程 `shlex` 拆分不经 shell、环境擦洗、每 hook 超时并 kill、stdout 有上限且按可选 JSON 解析
（非 JSON = 成功）、载荷只带预览、失败只记日志不阻断回合。注册在 `ToolGuardrails` 之后且
`GATEABLE`（预设-中间件可关）。测试 `tests/agent/middlewares/test_user_hooks.py`（20 例）。

**Step 2 / Step 3 未做，阻塞点**：Step 2 的信任门依赖仓库里**不存在**的 workspace-trust 概念；
Step 3 的「幂等变换」约束没有判定机制。两者连同既有设计留在 `计划_2.1_用户可配生命周期Hook.md`
（该计划**未删**，Step 1 部分标为已落地）。

---

### 2.2 ✅ system-reminder 目录化（已完成）

> **2026-10-14 核验：已完成。** `agent/middlewares/reminder_catalog/`（core.py / dynamic.py /
> sources.py）实现了统一目录：`DynamicReminderMiddleware` 在每轮首次 model call 注入
> `<system-reminder source="…">` 消息，catalogue 决定注入什么、优先级、cooldown 和
> 30 000 字符总预算（超预算按优先级丢弃最低价值项）。包装走
> `pub/func/message/system_reminder.py`（`provenance="reminder_catalog"` + `internal=True` +
> source id as `origin`），嵌套标签转义。compaction 重置 throttle 状态。
> 详见 AGENTS.md DynamicReminderMiddleware 条目。

**Zcode 做法** [源码确认] `core/src/system-reminder/source.ts`：一套结构化的 `<system-reminder>` 注入目录，**27 个 source** 分 3 组：

| 组 | 数量 | 成员 |
| --- | --- | --- |
| PREFIX | 2 | `context_prefix`、`skills_listing` |
| PERSISTED | 15 | `todo_reminder`、`task_status`、`tool_result_warning`、`resume_referenced_session_context`、`plan_file_reference`、`resume_goal_state`、`goal_state_change`、`plugin_reference`、`target_continuation`、`goal_completion_verification`、`rewind_notice`、`conversation_fork`、`selection_side_chat`、`queued_system_notification`、`shell_environment_change` |
| PER_REQUEST | 10 | `incoming_message`、`hook_context`、`runtime_mode`、`plan_mode_exit`、`output_style`、`date_change`、`referenced_session_context`、`model_anomaly`、`prompt_attachment`、`diagnostics` |

每个 descriptor 带 3 个正交属性：`channel`（6 个：`request_prefix` / `current_turn` / `tool_result` / `history_continuity` / `mid_turn_event` / `real_user`）、`lifecycle`、`isMeta`。
包装函数 `wrapSystemReminder`（`:203-212`）对**嵌套标签做拒绝/转义**（`:208`, `:233`）——防注入。
触发阈值可配：如 `todo_reminder` 距上次 `TodoWrite` 满 10 个 assistant turn **且**距上次提醒满 10 turn；`runtime_mode`（plan mode 提示）每 5 个 human turn 一次、每第 5 次给全量版。

**Sherry 现状** [实测]：~~`task_intent/`（E7）、`nudge/`、`todo_continuation_enforcer`（E3）等中间件承担类似职责，但**没有统一的注入目录/元数据描述**~~ → **已完成**：`agent/middlewares/reminder_catalog/`（core.py / dynamic.py / sources.py）统一注册 reminder source，`DynamicReminderMiddleware` 在每轮首次 model call 注入 `<system-reminder source="…">` 消息，catalogue 控制注入项 / 优先级 / cooldown / 30 000 字符总预算（超预算按优先级丢弃），包装走 `pub/func/message/system_reminder.py`（嵌套转义 + `provenance="reminder_catalog"` + `internal=True`），compaction 重置 throttle。旧的三个注入器（TaskIntent steering / todo-continuation / workspace notice）保持各自路径，catalogue 是 overlay，迁移分批进行。~~改造建议~~ 已落地。

---

### 2.3 🔨 Prompt 缓存分段（部分完成：stable/dynamic 已分离，但未拆成独立 message）

> **2026-10-14 核验：部分完成。** system prompt 现在只持有 stable 内容（persona +
> memory + skills index），DynamicReminderMiddleware 每轮从 live state 重新派生注入项。
> 旧的「把 reminder 重建进 cached system prompt」的做法已废弃。这实现了 stable/dynamic
> 的概念分离——stable 内容在 compaction 间不变（缓存友好），dynamic 内容每轮重算。
> **但与 Zcode 的差异**：Sherry 没有把 stable/dynamic 拆成独立的 system message + `cacheHint`
> 标注，而是用「一个 cached system prompt（stable）+ 若干 per-turn `<system-reminder>` 消息（dynamic）」
> 的形式。效果近似但 wire 层面不完全是 provider prompt cache 的分段优化。
> 进一步的改造（给 prompt_builder 的 block 加 `cache_hint` 标注 + 拆成两条 message）
> 可作为后续优化。

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

**Sherry 现状** [实测]：~~`workspace/prompt_builder.py`（324 行）组装 7 个 block，**全部是动态注入**，且**没有 `stable` / `dynamic` 概念**~~ → **部分完成**：system prompt 现在只持有 stable 内容（persona + memory + skills index），`DynamicReminderMiddleware` 每轮从 live state 重新派生注入项（plan mode / working dir / todo / boulder / taskflow board / continuity / knowledge index），catalogue 控制优先级 + cooldown + 30 000 字符预算。旧的「把 reminder 重建进 cached system prompt」已废弃（那会冻结到下次重建 + 使 prompt 成为坏缓存公民）。**概念分离已落地**，但未拆成独立 system message + `cacheHint` 标注——provider 侧缓存命中靠 system prompt 的三段缓存（`system_prompt_injection`），而非显式分段。

**差距**：~~Zcode 明确更优——可缓存部分与不可缓存部分被显式分开~~ **已大幅收窄**。Sherry 已实现 stable/dynamic 分离（cached system prompt vs per-turn reminders），但未到 Zcode 的 wire 层分段（独立 message + `cacheHint`）。进一步改造可给 prompt_builder block 加 `cache_hint` 标注。

**改造建议**：
1. 给 `prompt_builder.py` 的 7 个 block 各加一个 `cache_hint: Literal["stable", "dynamic"]` 标注——**`workspace/{SOUL,AGENTS,USER,IDENTITY}.md` 是 stable，todo / boulder / taskflow / continuity / knowledge 是 dynamic**
2. 返回值改为分组结构（stable 组 + dynamic 组），由 `agent/core.py` 装配成两条 message
3. 顺带处理 `skills_listing` 26,597 字符的问题：Zcode 也是全量注入，**这不是 Zcode 的优势而是双方共有的膨胀点**——但既然要动这块，Sherry 可以顺手做渐进披露（只注入当前 session 可见 scope 的 skill）

**风险**：中。`PromptDataProvider` 协议是跨层 seam（`runtime/data_provider.py`），改返回结构要同步 `agent/prompt_data_provider.py` 与 `context_engine` 侧实现，且 `tests/workspace/test_prompt_builder*.py` 需要更新。建议**独立成一个可回滚的改动**。

---

### 2.4 ✅ 计划模式（已完成）

> **2026-10-14 核验：已完成。** `agent/tools/plan_mode/`（enter_plan_mode / exit_plan_mode /
> state）实现了 `needs_planning` / `planning` / `ready` / `needs_replan` 状态机，
> `ToolSelectionMiddleware` 在 planning 状态下收窄工具到只读集，`exit_plan_mode` 持久化
> 计划文件并 HITL interrupt 等待批准，子 agent 强制 disallow plan_mode 工具。
> 详见 AGENTS.md「Plan-Then-Execute Plan Mode」章节。

**Zcode 做法** [落盘实测，34 工具列表]：`EnterPlanMode` / `ExitPlanMode` 一对工具 + `startPlanRecommendationDismissed` 设置项（启动时推荐使用）。配套两个 reminder source：`runtime_mode`（plan mode 激活时每 5 个 human turn 提醒一次）与 `plan_mode_exit`。
另：plan mode 工具对**所有**子 agent 强制 disallow（`subagent/tool-policy.ts`）。

**Sherry 现状** [实测]：~~`HITL_DEFAULTS` 有审批机制，`TaskIntentMiddleware` 有意图识别，但**无「先出方案、后执行」的显式模式切换**~~ → **已完成**：`agent/tools/plan_mode/` 实现了完整状态机（`needs_planning`/`planning`/`ready`/`needs_replan`），`ToolSelectionMiddleware` 在 planning 状态下收窄到只读工具集，`exit_plan_mode` 持久化计划 + HITL interrupt，计划文件写入 `SESSIONS_DIR/<session_id>/plans/plan-<revision>.md`。前端渲染批准卡（`[sid].vue` 的 `isPlanHitl`）。~~改造建议~~ 已落地，包括子 agent 强制 disallow。

---

### 2.5 ✅ taskflow step 挂载 skill（已完成）

> **2026-10-14 核验：已完成。** taskflow step 的 `skills` 字段在 `build_task_with_dep_results`
> 中通过 `_skill_prepend.prepend_skills` 预加载 SKILL.md（`[IMPORTANT: …]` 头 + fail-open
> 绑定 + `bump_use` + 注入扫描），与 cron 的 skill 绑定同构。`taskflow_plan` 可声明该字段，
> `taskflow_update_steps` 可修改它。详见 AGENTS.md Known Pitfalls 条目。

**Sherry 现状** [实测]：~~taskflow 的 step 由 `taskflow_create` 声明式定义（`response_schema` / `judge_criteria` / `depends_on`），**编排逻辑是固定的 Python 代码**，不能运行时生成新逻辑~~ → **已完成**：step 可声明 `skills: ["browser"]`，dispatch 时 `_skill_prepend.prepend_skills` 将每个 SKILL.md 预加载到子任务文本前（`[IMPORTANT: …]` 头 + fail-open + `bump_use` + 注入扫描），模型直接收到 skill 内容，编排者不需要 relay。~~改造建议~~ 已落地。

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

### 3.2 ✅ 工具并行安全静态标注（已完成）

**Zcode 做法** [源码确认] `core/src/tool/scheduler.ts:85::canRunInParallel`：
```
destructive == true                    → 阻止
concurrentSafe == true                 → 允许
readOnly 或 sideEffectScope == "none"  → 允许
否则                                   → 阻止
```
`READ_ONLY_TOOLS`（`:233`）显式列出 9 个：`Read`、`Glob`、`Grep`、`WebSearch`、`WebFetch`、`TodoRead`、`TodoWrite`、`AskUserQuestion`、`Skill`。
调度：拓扑排序 → `groupByParallel` → 按 `maxConcurrency`（默认 10）切分并行组 → 顺序遍历各组并行执行；任一组返回 `stopTurnAfterResult` → 剩余组全部标 `ToolCancelled`。

**Sherry 实现（2026-10-18）**：`SafeToolNode`（`agent/safe_tool_node.py`）替换 ToolNode，`plan_batch` 按 `READ_ONLY_TOOLS` / `concurrent_safe` / `side_effect_scope` 分类；文件工具按 canonical path 分组，同 path 有写按 `tool_calls` 顺序串行、不同 path 并发，非文件不安全工具为整批 barrier。`file_read_lock` / `file_write_lock` 提供跨 agent/进程的共享读、独占写；`ToolResourceLockMiddleware` 补 `system`（默认关）和 `user_interaction`（默认开）资源锁。主链与子链均接入。

**差距**：Zcode 的拓扑分组 + `maxConcurrency` 分波更细；Sherry 不做依赖 DAG，只做文件 path 冲突分组，`terminal` / `python_repl` 的目标无法推断仍是 barrier。该限制记录在 `docs/tool-parallel-safety/`。

---

## 四、工具实现细节（Sherry 最容易直接借鉴的部分）

这是 Zcode 工程成熟度最高的部分，也是**不涉及任何架构变更**、只改单个文件实现就能吃到收益的部分。

### 4.1 ✅ Edit 模糊匹配级联（已完成，11 级 > Zcode 8 级）

> **2026-10-14 核验：已完成。** `agent/tools/pub_base/text_matcher.py`（615 行）实现了
> 11 级级联（比 Zcode 多 3 级），含行号前缀剥离、`\uXXXX` 反转义、弯引号→ASCII、
> 缩进弹性匹配、block anchor（Levenshtein ≥0.8）、`replace_all` 禁用 broad matcher、
> 区域引号风格回写。详见 AGENTS.md `patch_file` 条目。

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

**Sherry 现状** [实测]：~~`patch_file` **无匹配降级策略**~~ → **已完成**：`agent/tools/pub_base/text_matcher.py`（615 行）实现 11 级级联（exact / quote_normalized / line_number_prefix_stripped / escape_normalized / unicode_escape_normalized / line_trimmed / indentation_flexible / block_anchor + 额外 3 级），含歧义拒绝、`replace_all` 禁用 broad matcher、区域引号风格回写、缩进重对齐。**比 Zcode 多 3 级。**

~~**差距**：这是 LLM 编辑失败的最大单一来源。~~ **已消除。**

~~**改造建议**：把 `findEditMatch()` 的 8 级级联移植到 `agent/tools/file_tools/patch_file.py`~~ **已完成，且实现了比建议更多的策略。**

**收益**：**最高性价比**。纯函数、无状态、无架构影响，可单测（8 级各有 fixture）。预计能显著降低「patch 失败 → 重试 → 再失败」的循环。

**风险**：低。唯一要注意 `block_anchor` 的 0.8 阈值需要用中文/CJK fixture 验证（CJK 的 Levenshtein 行相似度分布与英文不同）。

---

### 4.3 ✅ Read：token 感知截断 + 未变更重读 stub（已完成）

> **2026-10-14 核验：已完成。** `read_file` 的截断窗口受 `TOOLS_TIMEOUTS["read_file_token_budget"]`
> （4000，CJK-aware）与 caller 的 `limit` 双重约束（更严者胜，截断总在行边界）。
> 单行超预算时 clip 在行内 + 报 `truncated: true`。未变更重读：`agent/tools/pub_base/read_state.py`
> 记录 session 级完整读取的文件 revision，重读同一未变文件返回指针而非内容。
> 读取的 revision 进入 `patch_file` 的 `expected_revision` CAS（write-before-read license）。
> 详见 AGENTS.md Known Pitfalls `write_file` / `read_file` 条目。

**Zcode 做法** [源码确认] `contracts/src/tools/read.ts:15-17`：
| 常量 | 值 |
| --- | --- |
| `READ_MAX_FILE_SIZE_BYTES` | 256 KiB |
| `READ_MAX_OUTPUT_TOKENS` | 25,000 |
| `READ_DEFAULT_MAX_LINES` | 2,000 |

- **token 感知二分**（`read-text.ts:161-236`）：超限时用**二分搜索**取 ≤ 85% token 上限的最大前缀，返回 `partialViewNotice` + 续读 offset。**UTF-8 边界安全**（按 code point 二分，不会切碎多字节字符）
- **未变更重读省 token**（`read.ts:55,190-198`）：文件未变则返回 stub `"Wasted call — file unchanged since your last Read…"`，不重复吐内容
- 行号用 `cat -n` 风格 `N\t`

**Sherry 现状** [实测]：~~`read_file` 有 2000 行默认 + `truncated` + `hint`，`search_files` 有双截断标记。但 `read_file` 的截断是**行数 / 字符数**口径，**不是 token 口径**~~ → **已完成**：`read_file` 截断窗口受 `read_file_token_budget = 4000`（CJK-aware token 口径）与 caller `limit` 双重约束（更严者胜，截断在行边界）。未变更重读通过 `agent/tools/pub_base/read_state.py` 实现（session 级 revision 记录，重读返回指针而非内容）。读取 revision 进入 `patch_file` 的 `expected_revision` CAS 写前许可。partial read 不授许可、summarized-away read 失去许可。~~改造建议~~ 已落地。

~~**收益**：① 直接降低 CJK 大文件的 token 浪费~~ **已实现。**

**注意**：`search_files` 的双截断标记（`page_truncated` + `scan_truncated`）仍是 Sherry 领先 Zcode 的设计，应继续推广。

---

### 4.4 ✅ 子进程输出 fd 直写文件（已完成）

> **2026-10-14 核验：已完成。** `terminal.py` 的两个 spawn 点（`_execute_sync` 和 `_arun`）
> 均将子进程 stdout fd 直写到文件（`.sherry-tmp-stdout-*.log`，stderr 通过 STDOUT 合并），
> 然后等进程而非 drain pipe。输出 < `fd_write_threshold_bytes`（100 000）时整文件读后删除；
> ≥ 阈值时重命名到 `SESSIONS_DIR/<id>/evicted/terminal-<sha256>.log`，只回 head/tail 预览
> （8 KiB/side，2 KiB/line，`build_file_preview`）。超时/取消/失败丢弃临时文件；
> 硬 kill 残留由 `atomic_write.py` 的 `.sherry-tmp-` sweep 清理。
> 详见 AGENTS.md Known Pitfalls `terminal` 条目。

**Zcode 做法** [源码确认] `bash-file-output.ts`：当输出超过内联预算（`maxInlineBytes` / `maxModelBytes`）时，**子进程 stdout 直接 fd 写到 artifact 文件**，Node 进程不驻留内容，只回传路径 + 预览。

**Sherry 现状** [实测]：~~**工具侧没有输出上限，上下文侧有**。`terminal.py` 的两个落点都用 `proc.communicate()` 把 stdout 一次性收进内存再解码~~ → **已完成**：两个 spawn 均用 fd-direct-write-to-file（`.sherry-tmp-stdout-*.log`），不再 `communicate()`。输出 < 100 KB 整读后删除；≥ 100 KB 重命名到 `evicted/` 目录只回 head/tail 预览（8 KiB/side，2 KiB/line）。~~改造建议~~ 已落地。

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

**Sherry 现状** [实测]：`web_search.py` 用 Tavily（`max_results=5`），有显式重试（3 次 + 指数退避 + jitter）+ `asyncio.wait_for(30s)` + **无 key 时返回解释性 stub**（比 Zcode 的「硬前置 `supportsNativeWebSearch`」更优雅）。

> **落地记录（2026-10-15）**：`agent/tools/web_fetch.py` 已落地 —— 公网分支复用本仓库既有栈（`is_public_url` 预检 + `safe_fetch` 的 DNS pinning 与逐跳复核），本地/私网地址走**显式开关**分支（`SHERRY_WEB_FETCH_ALLOW_LOCAL=1`，tracked `.env`，默认 0，per-call 读取），HTML→Markdown 用 `html2text`（脚本/样式先剥，导入失败降级为剥标签），非 HTML（JSON/纯文本）**原样返回**（html2text 会转义并重排 JSON），输出 50K 字符封顶 + 既有 ContextEviction 二次兜底；分类只认字面量 IP/hostname（解析到 127.0.0.1 的公网域名仍走公网分支被拒）。测试 `tests/agent/tools/test_web_fetch.py`（23 例）。
>
> **有意偏离**：(1) 改造建议 #3 的「重定向只跟同 host」**未采纳** —— 公网分支用 `safe_fetch` 的逐跳模型（每跳重新解析 + 重新校验 + 钉住已校验地址），和媒体管线同一套策略；同 host 白名单会挡掉 `example.com → www.example.com` 这类常见跳转，且是多一份需要维护的分支。(2) 改造建议 #5 的「无凭据降级 + 重试退避」**不适用** —— 本工具不持有凭据，失败按 `safe_fetch` 的 reason 文本返回，由模型决定是否重试。(3) Zcode 的 `prompt`（辅助小模型作答）**未做** —— 工具把 markdown 直接交给调用它的那个模型，多一次无上下文的辅助调用只会丢信息、增延迟。

**⚠️ 不可照抄的部分**：Zcode 的 SSRF 防护**只覆盖字面量**——拦 `localhost` / `*.local` / 非公网 IP 字面量 / URL 内嵌凭据 / 单标签主机名；每次 GET 前 `assertWebFetchLiteralEgress` 要求 `ipaddr.js` 的 `range() === "unicast"` 并排除基准测试段 / IPv4-mapped IPv6 / NAT64 前缀，**但域名无 DNS 预检**（源码注释明说依赖 HTTP egress proxy 兜底）→ **不防 DNS rebinding**。

**改造建议**：WebFetch 是真实的能力缺口（Sherry 只能搜、不能读），但**必须独立做 SSRF 设计**：
1. 复用本仓库已有的防御方案（`pub/func/validator/public_url.py` 判定 + `safe_fetch.py` 传输：解析后逐地址校验、连接钉在已校验地址上、重定向逐跳复查），**不要照抄 Zcode 的字面量检查**
2. 强制**解析后 IP 校验**（resolve → 校验 → 连接该 IP，而非校验域名），关闭 DNS rebinding
3. 重定向策略照抄 Zcode 的同 host 限制（这条是好的）
4. HTML→Markdown 建议**用现成库**而非手写正则——Zcode 手写转换器是可维护性负担
5. 复用 `agent/tools/web_search.py` 已有的「无凭据降级 + 重试退避」模式

**风险**：**高（安全）**。这是本文中唯一「不补齐 SSRF 就不能做」的条目。

### 6.2 定时任务工具族

**Zcode** [落盘实测]：`CronCreate` / `CronList` / `CronUpdate` / `CronDelete` + `OffPeakCreate` / `OffPeakList`（6 个工具在 34 工具列表内）。off-peak 是「低谷期跑」的独立概念。
**Sherry** [实测]：`CRON_SERVICE` 7 键配置齐全（`degrade_backoff_base_ms`、`disabled_threshold = 10`、`max_run_history = 20`、双层退避 base/max）+ cron 与 skill 强绑定已落地（job 可绑定技能并逐次预加载，见 `skills/builtin/core/cron/scripts/`）。

**结论**：**Sherry 服务端能力更强**（降级背压 + 禁用阈值 + skill 绑定），Zcode 是工具侧暴露。
**可借鉴的只有一点**：把 cron 的触发暴露为 agent 工具（Zcode 侧是 agent 主动管理 cron），让模型能「检查自己的定时任务状态」。`OffPeak` 的低谷期概念 Sherry 暂无对应，可作为低优先候选。

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
| 插件 hook 无条件执行 | `canRunPluginHooks` 无条件 `return true`，第三方插件以用户权限执行任意进程。**Sherry 已有 SkillSpector，hook 必须复用同一信任模型**（见 2.1） |
| `keptMessageCount = 1` 的激进压缩 | 2027 条消息压成 1 条。Sherry 的分层保留更成熟，应保留并强化（见 1.6） |

---

## 八、优先级与投入产出

按「收益 / 成本」排序。**成本**列的估计基于 Sherry 现有代码结构，未做实现前评估。
**2026-10-14 更新**：6 项 ✅ 已完成、2 项 🔨 部分完成、7 项 ❌ 不做、1 项已合并。

| 优先级 | 条目 | 收益 | 成本 | 依赖 / 前置 | 状态 |
| --- | --- | --- | --- | --- | --- |
| ~~P0~~ | ~~4.1 Edit 8 级匹配级联~~ | 高 | 低 | 无 | **✅ 已完成**（11 级 > Zcode 8 级） |
| ~~P0~~ | ~~6.4 大窗口利用~~ | 高 | 零代码 | 实测压缩行为 | **⚠️ 非 ZCode 特性**（换模型即生效，是模型选择不是架构借鉴；压缩参数在 1M 下是否过小值得一次性实测验证，但不是 backlog 条目） |
| **P0** | 1.3 Session token 预算封顶 | 高 | 低（加状态键 + 检查点） | 无 | 未开工 |
| **P0** | 5.1 per-agent 用量记账 | 高 | 低（1 表 + 1 写入点） | 与 2.3 一起做收益最大 | 未开工（trajectory ledger 已有，但非用量记账） |
| ~~P1~~ | ~~4.4 terminal fd 直写~~ | 高 | 中 | 无 | **✅ 已完成** |
| ~~P1~~ | ~~2.2 system-reminder 目录化~~ | 中-高 | 低 | 2.3 可并行 | **✅ 已完成**（reminder_catalog） |
| ~~P1~~ | ~~2.3 Prompt cache 分段~~ | 中-高 | 中 | 需更新 tests | **🔨 部分完成**（stable/dynamic 已分离） |
| ~~P1~~ | ~~4.3 Read token 感知二分~~ | 中 | 低-中 | 无 | **✅ 已完成**（token budget + read_state license） |
| ~~P1~~ | ~~2.5 step 挂载 skill~~ | 高 | 中 | 无 | **✅ 已完成**（_skill_prepend） |
| ~~P1~~ | ~~2.3 续：拆独立 message + cacheHint~~ | 中 | 中 | stable/dynamic 已分离 | **❌ 不做**（概念分离已落地；131K 窗口下 wire 层缓存收益小；改跨层 seam + tests 成本不匹配收益） |
| **P2** | 3.1 `guide` 输入语义 | 中 | 中（队列 + 注入点） | 依赖 2.2 目录化（✅ 已完成） | 未开工 |
| ~~P2~~ | ~~3.2 工具并行安全标注~~ | 中 | 低（纯声明式） | 无 | **✅ 已完成**（`SafeToolNode` + 文件 path 分组 + `file_read_lock` / `file_write_lock` + `system` / `user_interaction` 资源锁；见 `docs/tool-parallel-safety/`） |
| ~~P2~~ | ~~2.4 计划模式~~ | 中 | 低 | 与 1.5 协调 | **✅ 已完成**（plan_mode 工具 + 状态机） |
| ~~P2~~ | ~~1.5 流恢复完整锚点协议~~ | 中 | 中 | 无 | **✅ 核心目标已完成**（`resume_stream` 快照 + per-turn `seq` 水位；不重放、不重复。完整 7 事件协议不做） |
| ~~P2~~ | ~~6.1 WebFetch~~ | 中 | **高（SSRF 必须重做）** | 独立安全设计 | **✅ 已完成**（`web_fetch`：复用 `public_url` + `safe_fetch`；本地地址走 opt-in 开关；html2text；测试 23 例） |
| ~~P3~~ | ~~1.1①④ input_hash 短路 + 步骤缓存~~ | 低-中 | 中 | 依赖原型验证 | **❌ 不做**（DAG 已防重跑） |
| ~~P3~~ | ~~1.1② PAUSED 显式挂起~~ | 低 | 中 | — | **❌ 不做**（不杀子 agent 无效果） |
| ~~P3~~ | ~~1.1③ 血缘指针~~ | 低 | 低 | 无 | **✅ 已完成**（`resumed_from`/`superseded_by` + active 读过滤） |
| ~~P3~~ | ~~1.2 taskflow 并行执行~~ | 中 | 高 | 依赖 1.1 | **✅ 已完成**（并发缺陷修复 + `max_parallel_dispatch` 有界并发 + 血缘；6 个并发/血缘测试文件） |
| ~~P3~~ | ~~1.6 microcompact 独立通道~~ | 低 | 低 | 无 | **❌ 不做**（ContextEvictionMiddleware 已做工具结果落盘驱逐 + preemptive_truncate 已做预截断，重叠） |
| ~~P3~~ | ~~6.2 OffPeak 低谷期~~ | 低 | 低 | 无 | **❌ 不做**（cron 系统已有降级背压 + skill 绑定；"低谷期跑"就是 cron 表达式，不需独立概念） |
| ~~P3~~ | 2.1 Hooks（分三步） | 高 | **高（安全设计）** | 2.1① 只读型 | **Step 1 已完成**（2026-10-16：PostToolUse / PostToolUseFailure / Stop 只读派发 + 默认关总开关；Step 2 信任门 / Step 3 updatedInput 待做，见 `计划_2.1_用户可配生命周期Hook.md`） |
| ❌ | 桌面端 / CUA | — | 很高 | 见第七节 | — |

**建议起手顺序**（2026-10-14 更新；**2026-10-17**：1.2 与 2.1 的 Step 1 已完成，见上表）：
1. ~~第一批（零风险、可并行）：4.1 + 1.3 + 3.2~~ → **4.1 已完成**；**3.2 已完成**；1.3 仍待做
2. ~~先核查再决策：4.4~~ → **已完成**（fd 直写已落地）
3. ~~第二批（需设计）：5.1 + 2.3 一起做~~ → **2.3 已部分完成**（stable/dynamic 分离已做，wire 层分段 ❌ 不做）；5.1 仍待做
4. ~~2.2 目录化作为 3.1 的前置~~ → **已完成**，3.1 guide 可开工
5. ~~1.1 四条差距~~ → **①②④ 不做**；**③ 血缘指针已完成**（随并行执行计划一起落地并退休）
6. ~~1.5 流恢复~~ → **核心目标已完成**（`resume_stream` 快照 + `seq` 水位；完整 7 事件协议不做）；~~1.6 microcompact~~ → **不做**（ContextEviction 已覆盖）；~~2.3 续 wire 分段~~ → **不做**（概念分离已落地）；~~6.2 OffPeak~~ → **不做**（cron 已覆盖）；~~6.4 大窗口~~ → **非 ZCode 特性**（换模型即生效）
7. ~~**独立立项**：2.1 Hooks 与 6.1 WebFetch 都涉及新的安全设计，不应混在常规迭代里~~ → **两者都已按独立立项交付**：6.1 WebFetch ✅（SSRF 复用 + 本地 opt-in）；2.1 Hooks Step 1 ✅（只读三事件 + 默认关总开关），Step 2 的信任门 / Step 3 的 updatedInput 仍待做（阻塞点见 2.1 节）

---

## 九、已确认无需改造的项

对标后确认 Sherry 已具备或更优，记录以免重复讨论。
**2026-10-14 更新**：新增 6 项已完成项 + 7 项经评估不需要做的项 + 1 项已合并。
**2026-10-17 更新**：1.2 taskflow 并行执行（含并发缺陷 + 血缘）与 2.1 Hooks Step 1 已落地，对应行已在上表标记。

| 能力 | Zcode | Sherry 结论 |
| --- | --- | --- |
| ~~1.1① input_hash 短路~~ | journal 短路 + inputHash 校验 | **❌ 不需要**（DAG 结构已防重跑：dispatchable 硬拒绝 + DONE 不可变 + replan 不重置 DONE + result 从 state["results"] 复用） |
| ~~1.1② PAUSED 显式挂起~~ | stop("user") abort in-flight | **❌ 不需要**（PAUSED 不杀子 agent 无实际效果；已有 taskflow_cancel + set_waiting 覆盖） |
| ~~1.1④ 步骤级结果缓存~~ | cached + input_hash 命中 | **❌ 不需要**（result 已在 state["results"] 被下游 build_task_with_dep_results 复用） |
| ~~1.1③ 血缘指针~~ | resumed_from + superseded_by | **✅ 已完成**（2026-10-15：两列 additive migration + `taskflow_create(resumed_from=…)` 写入 + active 读过滤；见 `docs/long-running-tasks/taskflow/` Flow Lineage） |
| ~~1.5 流恢复~~ | 7 事件流恢复协议 | **✅ 核心目标已完成**（2026-10-18：`resume_stream` 文本快照 + per-turn `seq` 水位，客户端替换文本并丢弃水位以下帧；不重放/不重复。ZCode 的 reasoning/tool-input delta 级协议不做；已 committed tool call 走 trajectory ledger） |
| ~~1.6 microcompact 独立通道~~ | 只清工具结果保留对话 | **❌ 不需要**（ContextEvictionMiddleware 已做工具结果落盘驱逐 20K 阈值 + head/tail 预览 + preemptive_truncate_ratio 0.7→0.5 预截断，本质重叠） |
| ~~2.3 续：拆独立 message + cacheHint~~ | stable/dynamic wire 分段 | **❌ 不需要**（stable/dynamic 概念分离已落地——cached system prompt 持 stable + per-turn reminders 持 dynamic；131K 窗口下 wire 层缓存收益小，改跨层 seam + tests 成本不匹配） |
| ~~6.2 OffPeak 低谷期~~ | 独立低谷期概念 | **❌ 不需要**（cron 系统已有降级背压 + 禁用阈值 + skill 绑定；"低谷期跑"就是 `0 2 * * 1-5` cron 表达式） |
| ~~6.4 大窗口利用~~ | 1M 窗口 | **⚠️ 非 ZCode 特性**（换模型即生效，是模型选择不是架构借鉴；压缩参数在 1M 下值得一次性实测验证，但不是 backlog 条目） |
| ~~4.1 Edit 模糊匹配~~ | 8 级级联 | **✅ Sherry 已完成且更优**（11 级级联，`text_matcher.py` 615 行） |
| ~~2.4 计划模式~~ | EnterPlanMode / ExitPlanMode | **✅ Sherry 已完成**（`agent/tools/plan_mode/` 完整状态机 + HITL + 子 agent disallow） |
| ~~2.5 taskflow step 挂载 skill~~ | — | **✅ Sherry 已完成**（`_skill_prepend.prepend_skills`，step 可声明 `skills: [...]`） |
| ~~2.2 system-reminder 目录化~~ | 27 个 source | **✅ Sherry 已完成**（`reminder_catalog/` 统一目录 + 30K 预算 + 嵌套转义） |
| ~~4.3 Read token 感知截断~~ | token 二分 + 未变更 stub | **✅ Sherry 已完成**（`read_file_token_budget=4000` CJK-aware + `read_state.py` revision license） |
| ~~4.4 terminal fd 直写~~ | fd 直写到 artifact 文件 | **✅ Sherry 已完成**（`.sherry-tmp-stdout-*.log` + 100KB 阈值 + evicted head/tail 预览） |
| Cron 服务端 | 6 个工具侧 | **Sherry 更强**（降级背压 + 禁用阈值 + skill 绑定计划） |
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
4. **workflow / bot / off-peak 8 张表本机 0 行** [落盘实测]：第 1.1 节的 journal 短路结论基于源码逻辑推断，**实际运行行为未验证**。
5. **`guide` 模式本机从未使用** [落盘实测]：`session_input` 214 行只用 `startNow`（107）与 `queue`（92 + 15 cancelled），`guide` 代码存在但真实行为未验证。3.1 的设计判断基于源码。
6. **落盘计数是快照且库为活库**：`~/.zcode/cli/db/db.sqlite` 仍在写入，本文 `model_usage` 5,880 行为 2026-09-30 快照值，会随使用增长。**趋势可信，绝对值会变。**
7. **Sherry 侧唯一未验证项已核查**：`terminal.py` 的大输出处理（4.4）——工具侧无上限、上下文侧由 20k 字符驱逐兜底，结论已就地改写。
8. **本文未评估 Zcode 的遥测/隐私行为**：`config.isTelemetryEnabled` 等设置项存在，但该 checkout 中遥测实现不完整，未做结论。
9. **本文未记录 Zcode 的安全缺陷**（反向议题）。部分缺陷已就地标注为「不可照抄」（附数据来源表里标了源码位置），但完整安全评估需另文。

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
| `.../core/src/tool/edit-matchers.ts:30-397` | 8 级匹配、Levenshtein ≥0.8、歧义处理 | 源码确认 |
| `.../adapters/src/fs/index.ts:452-647,700-777` | WASM ripgrep、原子写、revision | 源码确认 |
| `.../contracts/src/tools/read.ts:15-17` + `read-text.ts:161-236` | token 二分截断、85% 上限、未变更 stub | 源码确认 |
| `.../core/src/tool/scheduler.ts:48,85,187,233` | 并发 10、并行判定、只读工具集 | 源码确认 |
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
