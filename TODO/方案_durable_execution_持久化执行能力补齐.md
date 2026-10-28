# 方案：Durable Execution 持久化执行能力补齐（深挖版）

- 建档日期：2026-10-28
- 状态：**待评审 / 未开工**
- 目标：穷举 sherry 的五个 durable execution 子系统在**所有场景、所有条件分支**下无法实现 durable execution 的情景，给出修复方案、实施阶段、验收标准。
- 设计理念：sherry 的 durable execution 不是"自动 replay 工作流"（如 Temporal/Restate），而是 **"状态持久 + sweeper 兜底 + 新 turn 重驱动"**。本方案只补"状态活了但执行没活"的卡死/僵尸/重复，以及"并发写互相打断"的启动失败，**不追求 activity 自动重放**。
- 范围：HITL 中断/恢复、checkpointer 崩溃时点、TaskFlow step/flow 状态机、subagent 生命周期、evidence ledger。

## 0. 结论先行

1. **三个已确认的实际 bug 必须先修**（P0）：subagent registry 的 `_is_terminal` 集合过时导致内存无界增长（开发库已 7.8k 行）、`session_dir_sweep` 的 thread_id 比较永远返回 False、trajectory 的 `prune_session` 是死代码。
2. **重启恢复的核心漏洞是 HITL 标志不持久化**（P1）：`_HITL_PENDING` 纯内存，启动不重建 → 重启后图仍悬挂但服务端报 IDLE → 用户发新消息被 dispatch 成普通 turn，破坏 `Command(resume=...)` 契约。同时 auto-turn 的 `apply_hitl_pending` 是 no-op → headless turn 中断被孤立 → drain 践踏悬挂图。
3. **并发韧性的根因是 per-turn 全局 clean 无 busy_timeout**（P2）：`aclean_old_checkpoints` 每 turn 跑、无 try/except、bare 连接无 busy_timeout → 并发 turn 高频 SQLITE_BUSY 失败；更严重的是 SELECT 后 DELETE 前另一 session commit 新 checkpoint → **最新 checkpoint 被误删**（G2-race）。
4. **质量门的 fail-open 不能全改**（P3）：judge 故障时 fail-open 到 PASS 是"不困住 flow"的有意取舍，但 evidence gate 的"不可重放命令 stale 永久存在"是真正的功能性 bug——可 pin 住 finish。同时 `flow_id` 仅内存 → 重启后新失效事件对 Gate C 不可见 → 回归逃过门控。
5. **不杀子会话是设计性的，但 detach 时应可选 kill**（P3）：fail/cancel/auto-fail 现在只记 `detached_child_session_keys` 不杀子 → 僵尸子进程。delivery 状态只 30s flush → 重启后 TERMINAL+PENDING 无重试路径 → **完成结果永久丢失**。
6. **观测性差但不阻断**（P4）：trajectory 无 WAL、无 busy_timeout、`prune_session` 死代码、`error` 事件无生产者。

## 1. 贯穿全篇的根因缺陷

三个根因是大量分支的共同放大器：

| 根因 | 位置 | 影响 |
|---|---|---|
| **R1：`_is_terminal` 集合过时** | `agent/tools/subagent/registry/memory.py:28-37` | 集合是 `{"COMPLETED","FAILED","TIMEOUT","CANCELLED","STOPPED"}`，但 `ExecutionStatus`（`types/registry.py:12-18`）实际值是 `pending/running/interrupted/terminal`。`"TERMINAL"` 不在集合 → `_is_terminal` 永远 False → `_evict_overflow` 永不驱逐 → 2000 上限失效 → 内存无界增长（dev DB 已 7.8k 行） |
| **R2：`time.monotonic()` 跨重启不连续** | 所有 `started_at/ended_at/archive_at/delivered_at/suspended_at/last_attempt_at/killed_at` 都用 monotonic 写入并持久化 | 重启后 monotonic 归零/跳变，所有 `now - 旧时间戳` 对恢复后的 run 失效（负 elapsed）→ stale/wedged/expiry 检测全部失效 |
| **R3：交付状态只走 30s 周期 flush** | `delivery_state.py` 的 `mark_delivery_delivered` 等只写内存，落盘靠 `periodic_persist`（`state.py:105`，30s） | `complete_run` 同步持久化 TERMINAL，但 delivery 状态崩溃窗口内丢失 → 重启后重复 announce 或永久丢失 |

另外两个结构性事实：

- **`_HITL_PENDING` 纯内存**（`session_state.py:97`），启动不重建 → 重启后图仍悬挂但服务端报 IDLE。
- **`flow_id` 仅内存**（`StateRegisterMeM._states` 是普通 dict，`state_register.py:49`，从不持久化）→ 重启后所有 `FLOW_ID` stamp 消失。

## 2. 缺陷分级总览

| # | 严重度 | 缺陷 | 子系统 | 阶段 |
|---|---|---|---|---|
| 1 | 🔴 高 | Subagent registry `_is_terminal` 集合过时，内存无界增长 | subagent | P0 |
| 2 | 🔴 高 | `session_dir_sweep._checkpoints_has_session` thread_id 比较错误 | checkpointer | P0 |
| 3 | 🔴 高 | Trajectory `prune_session` 死代码，DB 无界增长 | trajectory | P0 |
| 4 | 🔴 高 | 重启后 `_HITL_PENDING` 丢失，busy 守卫被绕过 | HITL | P1 |
| 5 | 🔴 高 | auto-turn `apply_hitl_pending` no-op，headless 中断被孤立 | HITL | P1 |
| 6 | 🔴 高 | 重启后 in-flight step 卡死，需数小时 orphan recovery | TaskFlow/subagent | P1 |
| 7 | 🔴 高 | delivery DELIVERED 未持久化，重启后结果永久丢失 | subagent | P1 |
| 8 | 🟡 中 | 交付状态持久化差距，重启后重复 announce 窗口 ≤30s | subagent | P1 |
| 9 | 🟡 中 | 全程未设 `busy_timeout`（除 evidence ledger） | checkpointer | P2 |
| 10 | 🟡 中 | `aclean_old_checkpoints` 无异常保护，并发 turn 启动失败 | checkpointer | P2 |
| 11 | 🟡 中 | `aclean_old_checkpoints` G2-race：SELECT 后 DELETE 前新 checkpoint 被误删 | checkpointer | P2 |
| 12 | 🟡 中 | `aclean_old_checkpoints` 的 ns 处理与单线程版不一致 | checkpointer | P2 |
| 13 | 🟡 中 | `delete_thread_history` 无 try/except + 无 busy_timeout → purge 半删除 | checkpointer | P2 |
| 14 | 🟡 中 | 不可重放命令的 stale 标记永久存在，pin 住 finish | evidence | P3 |
| 15 | 🟡 中 | `flow_id` 仅内存，重启后旧 evidence 对 Gate C 不可见 | evidence | P3 |
| 16 | 🟡 中 | 空 result/timeout/kill 的子会话被标 DONE | TaskFlow/subagent | P3 |
| 17 | 🟡 中 | fail/cancel/auto-fail 不杀子会话，僵尸子进程 | TaskFlow/subagent | P3 |
| 18 | 🟡 中 | kill INTERRUPTED run 不关 checkpointer → 连接泄漏 | subagent | P3 |
| 19 | 🟡 中 | kill-vs-completion 仲裁死代码 | subagent | P3 |
| 20 | 🟡 中 | WAITING flow 永久滞留（stale 标记刷新 updated_at） | TaskFlow | P3 |
| 21 | 🟡 中 | supersede 后 TOCTOU 复活 | TaskFlow | P3 |
| 22 | 🟡 中 | 循环依赖创建时未拒绝 | TaskFlow | P3 |
| 23 | 🟡 中 | shell 写不触发 stale（evidence 盲点） | evidence | P3 |
| 24 | 🟡 中 | completion drain 对 broken/stale 盲目 | evidence | P3 |
| 25 | 🟡 中 | 每轮超时重置，goal loop 超 `run_timeout_seconds` | subagent | P3 |
| 26 | 🟡 中 | token budget 仅咨询，超支不阻断 | TaskFlow | P3（可选） |
| 27 | 🟡 中 | 所有质量门 fail-open，judge 故障静默退化 | TaskFlow | P3（部分） |
| 28 | 🟡 中 | Trajectory 无 WAL、无 busy_timeout、`error` 事件无生产者 | trajectory | P4 |
| 29 | 🟢 低 | `read_interrupt` 只返回首个 interrupt，并行分支成 zombie | HITL | 不做 |
| 30 | 🟢 低 | `as_node="model"` 硬依赖 langchain 内部命名 | HITL | 不做 |
| 31 | 🟢 低 | `mark_flow_superseded` 不 bump revision（并入 #21） | TaskFlow | 不做 |
| 32 | 🟢 低 | dispatch lock 进程内，多进程部署无保护 | TaskFlow | 不做 |

---

## 3. HITL 中断/恢复

### 3.0 根本原因：两个持久化平面未同步

- **中断 PAYLOAD 是持久化的**：LangGraph checkpointer 存每个 task 的 `interrupts`，重启后 `read_interrupt`（`agent/state_port.py:86-126`）从 checkpoint 重新推导。
- **中断 BUSY FLAG 不持久化**：`_HITL_PENDING` 是进程内 `set[str]`（`session_state.py:97`），只在 `apply_hitl_pending`（`stream_driver.py:154`）和 `_handle_hitl_response`（`ws/messages.py:238`）写，**启动不重建**。
- **CLAIMED 行已终结**：中断路径的 `StreamDriver.drive` 正常返回，`on_turn_finished` 把 CLAIMED 行标 DELIVERED → 重启后无 CLAIMED 占位符阻塞。
- 三者叠加：重启后新普通消息被 dispatch 为全新 turn，直接送给中断挂起的图，绕过整个 HITL 协议。

### 3.1 所有 interrupt 触发点

| # | 触发点 | 位置 | operator 检查 | headless deny | resume 契约 |
|---|---|---|---|---|---|
| A1 | `question` 工具 | `agent/tools/question.py:88-109` | **无** | **无** | `approve/yolo`→返回答案；否则 `_DECLINE_MESSAGE` |
| A2 | `clarify` | `agent/middlewares/humanInTheLoop/core.py:156-194` | **无** | **无** | `approve/yolo`→返回消息；否则 None |
| A3 | `exit_plan_mode` | `agent/tools/plan_mode/exit_plan_mode.py:169-185` | **无** | **无** | `approve`→READY；`reject+msg`→PLANNING+mutation；`reject`→IDLE。**唯一有 revision 绑定**（`:188-211`） |
| A4 | `sandbox_bypass` (terminal) | `strategies.py:133-148`→`core.py:280-364` | 有（`:134-136`） | 有 | `approve`→`(True,None)`；否则 error ToolMessage |
| A5 | `sandbox_bypass` (python_repl) | `strategies.py:265-282` | 有（`:267-269`） | 有 | 同 A4 |
| A6 | `terminal_dangerous` | `strategies.py:173-241` | 有（`:174-176`） | 有 | `yolo/session+can_cache/approve/reject` |
| A7 | `first_call_confirmation` | `strategies.py:355-406` | **部分**（`:347-348` operator None 时 `matches=False`→**跳过门**，非 deny） | **无（绕过）** | `yolo/approve/reject` |
| A8 | `interrupt_on_approval` | `strategies.py:429-510` | 仅 store 验证（`:462-468` 结论 UNKNOWN 时不查 operator 直接 interrupt） | **无（挂起）** | `yolo/approve/edit/reject` |
| A9 | `external_path` | `agent/tools/pub_base/path_utils.py:573-584` | **无**（仅拒绝子代理） | **无** | `approve/approve_dir/yolo/reject`。**唯一把 YOLO 持久化到 `state_register_db`**（`:561-566`） |
| A10 | `MCP_elicitation` | `gates.py:156-175` | — | — | **无 interrupt payload**，恒返回 DENY（fail-closed） |

**关键发现**：A1/A2/A3/A8(UNKNOWN)/A9 五个触发点**不查 operator**，在 headless turn 下会挂起图，而 headless 没人审批。

### 3.2 重启后的恢复分支

| # | 场景 | 当前行为 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|---|
| B1 | 重启后图悬挂 interrupt，用户发 `hitl_response` | resume 成功（payload 从 checkpoint 重推导） | 部分 | resume 前 session 脆弱（见 B2/B3） | 启动重建 `_HITL_PENDING`（见 3.7） |
| B2 | 重启后图悬挂 interrupt，用户发**普通消息** | `submit_user_input`（`input_queue_service.py:427-484`）`detect_state` 报 IDLE → dispatch 新 turn，向悬挂图输入 `HumanMessage` | **否** | GraphInterrupt 重发或 LangGraph 报错；checkpoint 被不当插入的 HumanMessage 污染 | `submit_user_input` 空闲分支前加 `get_pending_interrupt` 查询，有 pending interrupt 则走 queue |
| B3 | 重启后图悬挂 interrupt，queued 消息被 drain | `_drain_loop`（`turn_runner.py:367-405`）`is_hitl_pending` False → claim → 输入悬挂图 | **否** | 同 B2，通过 drain 触发 | 同 B2；或启动重建 `_HITL_PENDING` 后 drain 自然 defer |
| B4 | 重启后图悬挂 interrupt，session 被删除 | `purge_session_data` 删 checkpoint，但**不调 `set_hitl_pending(False)`** | 部分 | `_HITL_PENDING` 泄漏已删除 session ID | `purge_session_data` 加 `set_hitl_pending(session_id, False)` |
| B5 | 重启后图悬挂 interrupt，多个并行 interrupt | `read_interrupt` 返回**首个**带 payload 的 interrupt | **否（数据丢失）** | 客户端只看到一个对话框；`Command(resume=)` 给所有 interrupt 同一 decision | `read_interrupt` 返回列表；用 `Command(resume=[{...},{...}])` 数组形式 |
| B6 | 重启后图悬挂 interrupt，payload 是 `plan_mode` | revision 绑定从 `state_register_db`（持久化）+ 磁盘文件重推导 | **是** | — | 无需修复 |
| B7/B8 | payload 是 `question`/`external_path` | 同 B1/B2 | 同 B1/B2 | BUSY FLAG 丢失 | 同 B2 |
| B9 | 重启后图悬挂 interrupt，turn 有 trailing tool calls | 纯 HITL 中断在 `after_model` 触发；若 cancel 恰好与 interrupt 同时发生，heal 写占位 ToolMessage + 中断共存 → 语义混乱 | 设计如此 | resume 时 tool_calls 既被占位"拒绝"又受 decision 门控 | `heal_interrupted_turn` 检测 `state.tasks[].interrupts` 存在时跳过 trailing tool calls 愈合 |
| B10 | 重启后图悬挂 interrupt，payload 损坏/丢失 | `read_interrupt` 跳过空 payload；`get_pending_interrupt` 返回 None | **否（静默挂起）** | 图挂起无法前进，客户端看不到对话框，新 turn 被 dispatch 到卡住的图 | `read_interrupt` 检查 `state.next`/`state.tasks` 非空但无 interrupt payload 时报告损坏状态或触发 heal |
| B11 | 重启后图悬挂 interrupt，`approvals.json` 也丢失 | `ToolApprovalStore._read_snapshot` 处理缺失→空状态 | 优雅降级 | 先前批准的工具重新提示 | 文档化 |
| B12 | 重启后图悬挂 interrupt，session 不在 `_known_main_sessions` | MesMemory 行被删但 checkpoint 存活 → session 从列表消失 | **否（孤立中断）** | interrupt 无限存在消耗存储 | 启动时枚举 checkpoint thread ID 与 `_known_main_sessions` 交叉引用 |

### 3.3 cancel/heartbeat_timeout 愈合分支

| # | 场景 | 当前行为 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|---|
| C1 | cancel 时图在 model 节点 | `heal_interrupted_turn`（`state_port.py:129-204`）写 marker（确定性 id `interrupted-{thread_id}-{turn_seq}`） | 是（幂等 upsert） | checkpointer 写失败时 marker 丢失（见 C6） | 见 C6 |
| C2 | cancel 时图在 tools 节点（有未应答 tool_calls） | `heal_trailing_tool_calls`（`state_port.py:207-253`）合成 error ToolMessage 占位 | 是（提交成功时） | 见 C6 | 见 C6 |
| C3 | cancel 时图已悬挂在 HITL interrupt | stream 已结束，`_cancel_session` 只设 `ANSWERING=False`，**不写 marker** | 是（interrupt 存活） | **无法放弃 interrupt**——stop 是 no-op | 增加"放弃 interrupt"路径：注入 reject decision + marker heal |
| C4 | heartbeat_timeout | 同 C1/C2，marker reason=`"heartbeat_timeout"` | 是 | 同 C1/C2 | 同 C1/C2 |
| C5 | cancel 时 marker 已存在（幂等） | 检测 marker id 已在 state→`wrote_checkpointer=False`→跳过 aupdate_state | 是（幂等） | `_persist_to_mesmemory` 被跳过→MesMemory 永缺行 | MesMemory 补写与 checkpoint 写入解耦 |
| **C6** | cancel 时 checkpointer 写失败 | `aupdate_state` 抛错→`except Exception`（`interrupt_marker.py:102-109`）吞掉；**`_persist_to_mesmemory` 和 `_void_claimed_rows` 被跳过** | **否** | (a) checkpoint 保留 provider-invalid 形状→下一 turn 硬失败；(b) **CLAIMED 行未清理→24h 阻塞 drain** | 把 `_void_claimed_rows` 移到 `finally` 块，无论 heal 成功与否都运行 |
| C7 | cancel 时 MesMemory 写失败 | 同 C6 结构问题 | 部分 | CLAIMED 行未清理→24h 阻塞 | 同 C6 |

### 3.4 resume 的 decision 分支

| # | decision | 状态变更 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|---|
| D1 | `approve` | TerminalApprovalHandler 无状态变更；InterruptOnApproval `approve_tool_for_session`→mem+持久 store | 是（`_HITL_PENDING` 正确清除时） | — | — |
| **D2** | `session` | terminal `can_cache` 时 `_apply_decision(SESSION)`→`state_register_mem`（`approval.py:376-382`） | **否** | session 白名单纯内存，重启丢失→相同命令重新提示 | session 白名单镜像到 `state_register_db` |
| **D3** | `always` | `_apply_decision(ALWAYS)`→`HITL_PERMANENT_KEY` in `state_register_mem`（`approval.py:368-374`） | **否** | "permanent" 白名单纯内存，重启丢失；命名误导 | `HITL_PERMANENT_KEY` 持久化到 `state_register_db`，或路由通过 `ToolApprovalStore.record` |
| D4 | `edit` | InterruptOnApproval 修订 tool_call | 是 | — | — |
| D5 | `reject` | terminal error ToolMessage；InterruptOnApproval `deny_tool_for_session`→mem+持久 store | 是 | — | — |
| **D6** | `yolo` | `set_session_yolo`→`SESSION_YOLO_KEY=True` in `state_register_mem` | **否** | 重启后 session YOLO 丢失→下一个危险命令重新提示 | session YOLO 通过 `state_register_db` 持久化 |

### 3.5 headless turn + auto-turn no-op

`_is_headless_turn`（`approval_scope.py:67-89`）：最后一条 human 消息 `metadata.origin=="cron"` 或 `metadata.internal` 为真。

**E1：cron/continuation 注入（headless）**：terminal/sandbox/dangerous 自动 deny；但 question/clarify/exit_plan_mode/external_path/interrupt_on(UNKNOWN)→**挂起**（不查 operator）。

**F2：headless auto-turn 中断时的灾难链**：
1. 图挂起→`StreamDriver.drive` 中断分支（`stream_driver.py:142-154`）
2. `apply_hitl_pending()`→**no-op**（`_AutoTurnStreamDriver` 未重写，`auto_turn.py:252-285`）
3. `on_turn_finished`→`is_hitl_pending` False→**运行 prune**（可能删 interrupt 的 checkpoint）+ kick drain
4. `_drain_loop` `is_hitl_pending` False→claim QUEUED→输入 HumanMessage 到悬挂图

**durable？否（灾难性）**。修复：
- `auto_turn.py:252-285` 重写 `apply_hitl_pending` 调 `set_hitl_pending(self.session_id, True)`
- question/clarify/exit_plan_mode/external_path/interrupt_on 在 `interrupt()` 前查 operator，None 则 deny/abort

### 3.6 HITL 持久化评估总结

| 维度 | durable? | 重启失败 |
|---|---|---|
| interrupt payload (checkpoint) | 是 | 仅 B10 (payload 损坏) |
| `_HITL_PENDING` busy flag | **否** | B2/B3/F：session 报 IDLE，新 turn 进入悬挂图 |
| session YOLO / confirm-all / 白名单 / permanent 白名单 | **否** | D2/D3/D6：重启重新提示 |
| external_path YOLO | 是 | 存活 |
| plan revision 绑定 | 是 | B6：存活 |
| `ToolApprovalStore` | 是 | 存活（B11：优雅降级） |
| interrupt marker (cancel/timeout) | 是 | C5：幂等；C6：写失败跳过 CLAIMED 清理 |
| `_AutoTurnStreamDriver.apply_hitl_pending` | **no-op** | F：孤立中断，drain 践踏 |

### 3.7 HITL 全局修复

`server/__main__.py:main()` 在 `init_trigger()` 后增加启动对账：
1. 枚举所有 session ID（`get_session_ids` + checkpoint thread ID）
2. 对每个调 `read_interrupt(session_id)`→非 None 则 `set_hitl_pending(session_id, True)`
3. fail-open（单个失败不影响其他）

`submit_user_input`（`input_queue_service.py:427-435`）加 `get_pending_interrupt` 作为第 5 个 busy 信号。

---

## 4. Checkpointer 崩溃时点

### 4.0 架构事实基线

- **单一共享 DB 文件**：所有 session 共享 `SRC_DIR/checkpoints/sqlite.db`（`async_sqlite_checkpointer.py:12-14`）。
- **WAL 是 persistent DB-header 属性**，设一次后继承；但 **busy_timeout 是 per-connection，不继承**。
- **busy_timeout 完全缺失**：主连接（`:16-19`）设了 WAL/foreign_keys/cache_size，**无 busy_timeout**；所有独立连接都是 bare `aiosqlite.connect`。**唯独 checkpointer 漏了**。
- **每 turn force_rebuild → 每 turn 全局 clean**：`messages.py:194,435`→`core.py:431` `aclean_old_checkpoints()`（**无 try/except**）。
- **aput/aput_writes 是两个独立事务**：checkpoint 行先 commit，writes 后 commit，中间有窗口。
- **无 boot-time 恢复**：`server/__main__.py` 不含 heal/interrupt 重检测/CLAIMED recover。

### 4.1 Turn 执行中的崩溃时点

| # | 崩溃时点 | 落盘 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|---|
| A1 | model 调用前 | checkpoint=上一 super-step | 部分 | CLAIMED row 未标 DELIVERED→24h 阻塞 drain | boot 全局 `recover()` 扫描过期 CLAIMED row |
| A2 | model 调用中 | checkpoint=before_agent | 部分 | 已流式文本丢失；CLAIMED 阻塞同 A1 | 同 A1 |
| A3a | after_model 中、aput 前 | MesMemory 可能有 AIMessage；checkpoint 仍是 before_agent | **双写不一致** | UI 有回复，checkpoint 没有 | `read_messages` 以 checkpoint 为准做 reconcile |
| **A3b** | aput 后、aput_writes 前 | checkpoint 含 AIMessage(tool_calls)，**无 writes** | **否** | **dangling tool_calls**→provider-invalid transcript→下一 turn 硬失败；generic crash 不触发 heal | boot 时检测 dangling 并调 `heal_interrupted_turn`；或 `_GenerateTurn._prepare` 开始前检测 heal |
| A4 | tool 执行中 | 同 A3b | **否** | 同 A3b | 同 A3b |
| A5a | tool 执行后 aput 后 | checkpoint 含 AIMessage+ToolMessages（valid） | **能** | 最安全的崩溃点 | 无需修复 |
| A6 | 最终 AIMessage 写 checkpoint 前 | MesMemory 可能有答案；checkpoint 没有 | **双写不一致** | 后续 turn model 上下文丢失上一轮答案 | `read_messages` reconcile |
| A7 | 最终 checkpoint 写后、prune 前 | checkpoint=完整 turn | 能（数据层面） | CLAIMED row 阻塞；客户端无 done 帧 | 同 A1 |
| A8 | prune 中 | 两 DELETE 同一事务→全回滚或 durable | 能 | busy_timeout 缺失→SQLITE_BUSY→fail-open（prune 跳过，历史增长） | bare 连接加 busy_timeout |
| A9 | prune 后、drain kick 前 | checkpoint=完整 turn，pruned | 能 | queued rows 未 drain→等下一个 user message | boot 时对每个 session kick drain |

### 4.2 HITL interrupt 的崩溃时点

| # | 崩溃时点 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| B1 | interrupt() 调用前 | 能（interrupt 重新触发） | duplicate model call | 无需修复 |
| B2 | interrupt() 写 checkpoint 中 | 能（rollback→重新触发） | 同 B1 | 无需修复 |
| **B3-B5** | interrupt 写后、apply_hitl_pending 前/后、on_turn_finished 前 | **否** | interrupt durable 但 `_HITL_PENDING` 未设或崩溃后丢失→drain 不 defer→新 turn 打断 interrupted thread | 启动重建 `_HITL_PENDING`（见 3.7） |

### 4.3 Resume + heal 的崩溃时点

| # | 崩溃时点 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| C1 | Command(resume=) 调用前 | 能 | 用户可重新提交 | 无需修复 |
| C2 | resume 执行中 | 部分 | resume value 丢失，需重新 approve | 无需修复 |
| D1-D3 | heal 的各时点（读/写 marker/MesMemory） | **否（generic crash 不触发 heal）** | dangling tool_calls→下一 turn 硬失败 | boot-time heal |
| **D4** | 写 marker 后、MesMemory 写前 | **双写不一致** | checkpoint 有 marker（valid），MesMemory 缺 interrupted 行 | `_write_interrupted_marker_inner` 在 `wrote_checkpointer=False` 时仍检查 MesMemory 补写 |

### 4.4 Session purge 的崩溃时点

| # | 崩溃时点 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| E1 | delete_thread_history 前（mes_memory 可能已删） | **否（跨 store 非原子）** | checkpoint 存在但 mes_memory 空 | purge 顺序反转：先删 checkpoint 再删 mes_memory |
| **E2-BUSY** | delete_thread_history 被 SQLITE_BUSY 打断 | **否（半删除）** | `session_cleanup_service.py:127` **无 try/except**→OperationalError 传播→mes_memory 已删但 checkpoint/folder/registers 还在 | (1) 加 try/except；(2) bare 连接加 busy_timeout；(3) purge 顺序反转 |
| E3 | delete_thread_history 后 | 部分 | 重新跑 purge 可修复（idempotent） | 所有 step 包 try/except |

### 4.5 Subagent child + 全局扫描的崩溃时点

| # | 崩溃时点 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| F1-F3 | child checkpoint 写前/中/后 | F1/F2 否，F3 部分 | child 结果丢失；root 仍存在的 mid-crash child 不被 boot sweep 清理 | boot 扫描 active/PENDING 但进程不存在的 child run，调 `purge_session_data` |
| F4-F7 | _release_child_resources 各时点 | 能（rollback 或 durable） | busy_timeout 缺失→SQLITE_BUSY→fail-open（checkpoint 未 prune） | bare 连接加 busy_timeout |
| **G2-BUSY** | aclean_old_checkpoints 被 SQLITE_BUSY 打断 | **否（turn 失败）** | bare 连接无 busy_timeout→每 turn 都跑→并发 turn **高频失败** | (1) 移除 per-turn clean，改 boot 时一次；(2) bare 连接加 busy_timeout |
| **G2-race** | SELECT 后、DELETE 前另一 session commit 新 checkpoint N+1 | **否（数据丢失）** | SELECT 看到旧 latest(N)，DELETE `WHERE checkpoint_id != N` 在事务升级时看到 N+1→**N+1 也被删**→最新 checkpoint 被误删 | DELETE 条件改为 `checkpoint_id IN (子查询)`；或 `BEGIN IMMEDIATE` + busy_timeout |

### 4.6 Checkpointer 全局修复

| # | 修复 | 文件 | 影响 |
|---|---|---|---|
| H1 | 主连接加 `PRAGMA busy_timeout=30000` | `async_sqlite_checkpointer.py:16-19` | 覆盖主 graph 的 aput/aput_writes/aget_tuple |
| H2 | 所有独立连接加 busy_timeout | `thread_safe_checkpointer.py:234,336`，`async_sqlite_checkpointer.py:37,55` | 覆盖 aclean/delete/prune |
| H3 | 移除 per-turn 全局 clean | `core.py:431` | 改 boot 时一次+定期 cron；消除 G2 高频失败和 race |
| H4 | prune_session_checkpoints 双连接浪费 | `async_sqlite_checkpointer.py:43-60` | 让 aclean_thread_checkpoints 接受可选 conn 复用 |
| H5 | delete_thread_history 加 try/except | `session_cleanup_service.py:127` | 与其他 step 一致 |
| H6 | boot-time 恢复 | `server/__main__.py` | (1) 全局 queue recover；(2) HITL interrupt 扫描；(3) trailing incomplete heal；(4) 全局 aclean 一次 |
| H7 | purge 顺序反转 | `session_cleanup_service.py` | 先删 checkpoint 再删 mes_memory |

---

## 5. TaskFlow step/flow 状态机

关键阈值：`persist_max_attempts=3`、`finish_nudge_max=2`、`finish_failure_limit=2`、`finish_protocol_violation_limit=3`、`stale_action_hours=12`、`unreferenced_flow_grace_seconds=300`、`waiting_timeout_hours=24`、`max_parallel_dispatch=2`、`STEP_JUDGE.max_retries=2`、`GOAL_GATE.min_steps=3`、`REPLAN_MAX=3`、`max_steps_per_flow=20`。

### 5.1 step 的所有失败组合

| # | 失败组合 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| A1 | schema 失败 + retry 耗尽→FAILED | 是 | FAILED step 下游永不解锁→flow 挂起直到 finish Gate B auto-fail | Gate B failed 分支提示 `taskflow_replan` |
| **A2** | judge RETRY + STEP_JUDGE max_retries 耗尽→BLOCKED | 是 | **两套独立上限共用同一 retry_count**：`retry_policy.max_retries=1` 且 `STEP_JUDGE.max_retries=2`→超额重试；反之预算被截断 | 统一为 `min(STEP_JUDGE.max_retries, policy.max_retries)` |
| A3 | judge BLOCK→BLOCKED | 是 | BLOCKED 无 depends_on→永久 BLOCKED，无法 dispatch 重试 | 新增"unblock 重试"动作 |
| A4 | judge 异常→fail-open PASS | 是 | judge 崩溃时劣质结果被误判 DONE | 记录 `judge_degraded=True` 标记 |
| **A5** | timeout（子会话） | 是 | **`taskflow_resume` 没收到 `step_outcome=failure`**→timeout 结果走正常 Tier1/Tier2→**DONE**（timeout 被当成功） | `delivery.py:419` 传入 `step_outcome="failure"` |
| **A6** | kill（用户 kill 子会话） | 是 | 同 A5，kill 的 child 不产出 result→auto-resume 注入空串→DONE | 同 A5 |
| A7 | crash（重启） | 部分 | 见 5.5 节 | 见 5.5 节 |
| **A8** | 空 result / result_text 缺失 | 是 | **系统无法区分"成功但产出为空"与"死亡未产出"**→两者都标 DONE | `taskflow_resume.py:329` 前：空 result 且无 judge_criteria→标 FAILED |

### 5.2 flow 完成失败 + DAG 调度 + 并发冲突

| # | 组合 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| B1-B12 | Gate A-E 各失败组合 + auto-fail | 是 | `_persist_finish_state` 遇 conflict 不重试→streak/failure 计数可能丢失 | 关键计数走 conflict-retry |
| **C1** | dep failed→下游永不解锁 | 设计如此 | FAILED dep 下游永久 BLOCKED（不像 skipped/cancelled 级联） | 考虑把 FAILED 加入级联 |
| **C6** | 循环依赖（A→B→A） | 是（写盘） | `validate_steps_list`（`_shared.py:637-694`）**无环检测**→环被接受→所有 step 永久 BLOCKED→flow 必然失败且无诊断 | 加拓扑排序/DFS 环检测 |
| **D1-retry** | 并发 resume retry→重复 spawn | 孤儿 child | 各自 `spawn_replacement`→第一个 replacement child 成孤儿；retry_count 少算 | resume retry 分支加 `flow_dispatch_lock` |
| **D3** | dispatch 中 cancel→孤儿 children | 是 | children 已 spawn 但 flow 已 cancel→孤儿 | dispatch 失败且 flow terminal 时 kill 已 spawn children |
| **D5** | supersede 后 TOCTOU 复活 | 是但语义错误 | `mark_flow_superseded`（`store_sqlite.py:318`）**不 bump revision**→持旧 revision 的并发写可把 CANCELLED 复活为 RUNNING | bump `expected_revision`；或 WHERE 加 `AND superseded_by IS NULL` |

### 5.3 重启后 + sweeper + budget

| # | 组合 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| **E1** | 重启时 step DISPATCHED（子 run RUNNING） | 部分 | 恢复的 RUNNING run 让 `_has_live_child=True`→flow 不被 GC 也不被 wait_all 推进→step 永久 DISPATCHED | (1) 恢复的 RUNNING 标 INTERRUPTED；(2) sweeper 扫描 DISPATCHED step + dead child 主动 resume/fail |
| **E2** | 重启时 step DISPATCHED（子 run PENDING） | **否** | PENDING child 被 finalize 后**不 announce**→step 永远 DISPATCHED | `_finalize_restored_pending`（`state.py:23`）调 announce |
| **F2** | WAITING 24h 超时 | **否（永久滞留）** | `_scan_stale_waiting_taskflows` 每 sweep 写 `stale_detected_at`→`update_flow` bump `updated_at`→liveness `age = now - updated_at` 永远 < 12h→**WAITING flow 永不被 liveness 收集** | 写标记时不走 `update_flow`；或 liveness 用 `wait.set_at` 而非 `updated_at` |
| F3 | liveness fail-open 过度保守 | 部分 | `_has_live_child` 任何异常都 veto→registry 不可用时 liveness 完全不工作 | 区分"registry 未初始化"与"lookup 失败" |
| **G3** | replan/update_steps 绕过 max_steps_per_flow | 是 | `taskflow_create.py:91` 创建时拒绝 >20，但 replan/update_steps **不检查** | persist 前加 `len(merged) > max_steps_per_flow` 拒绝 |
| **G5** | max_parallel_dispatch 跨调用失效 | 部分 | 每次 dispatch 新建 `Semaphore(2)`→两个并发 dispatch 实际并发可达 4 | process-global 信号量（模块级单例） |

---

## 6. Subagent 生命周期

### 6.1 spawn + 执行中

| # | 组合 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| A1 | lane 满了→PENDING 排队 | 是 | PENDING 期间 attachments/worktree 不被 `_finalize_restored_pending` 清理→**磁盘泄漏** | `_finalize_restored_pending` 调 `safe_remove_attachments_dir` + discard workspace |
| **A2** | kill PENDING run | 是 | `_start_announce_cleanup_flow` 对**从未启动**的 run 触发 announce→误导父代理 | PENDING run 设 `suppress_announce_reason="never_started"` |
| **B1** | timeout 是 per-turn 非 wall-clock | 是 | goal loop 多轮每轮 wait_for(timeout)→实际可跑 `goal_max_turns×timeout`（默认 5×600=3000s） | 外层包总 wall-clock deadline，或每轮传剩余 budget |
| **B2** | kill 双重 complete | 是 | kill 的 `complete_subagent_run`（result_text=None）与 task finally 竞争→**partial result_text 丢失** | kill 传 `expected_generation`；或 `complete_run` 已 TERMINAL 时不覆盖 result_text |
| **B4** | child agent 内部 GraphInterrupt | 是 | `GraphInterrupt` 被 `except Exception` 捕获→outcome=ERROR（应转 INTERRUPTED） | 增加 `except GraphInterrupt:`→`mark_run_paused_after_yield` |
| **B6** | completion judge 异常未 fail-open | 是 | 注释说 fail-open DONE，但**实现没对 `judge_completion` 包 try**→judge 抛错变 ERROR | `_run_goal_loop`（`core.py:1032`）包 try/except |
| **B9** | tool 子进程未响应取消 | **否（资源泄漏）** | 僵尸 Bash 进程持续占用 CPU/文件锁；sweeper 不扫 OS 进程 | tool 层 asyncio 取消时 `proc.kill()`；或 `_release_child_resources` 杀残留子进程 |

### 6.2 kill + 完成/交付

| # | 组合 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| **C3** | kill INTERRUPTED run | 是 | **aiosqlite checkpointer 连接泄漏**！INTERRUPTED run 是 yield 出来的，不走 `_execute_subagent_scoped.finally`→kill 不调 `_release_child_resources` | kill INTERRUPTED run 时调 `_release_child_resources` |
| **C5** | kill vs completion 仲裁 | — | **仲裁检查 `kr.snapshot_execution.outcome`，但 kill RUNNING 时快照 outcome 是 None→`if snapshot_outcome and ... == KILLED` 永远 False→"completion overrides kill" 是死代码** | 检查传入的 outcome 而非快照 outcome |
| C6 | cascade kill O(n) | 是 | `get_latest_run_by_child_session_key` O(n)→大子树 kill 慢 | 建 `child_session_key → run_id` 索引 |
| **D3** | descendants 不 settle→delivery 永久 PENDING | — | 父 run delivery 永远 PENDING→cleanup 永远 defer→run 永不清理 | settle_wake retry 失败后转 SUSPENDED |
| **D7** | 交付后崩溃（DELIVERED 未持久化） | **否** | 重启→SQLite 是 TERMINAL+PENDING；sweeper 无路径重试→**完成结果永久丢失** | `mark_delivery_delivered` 后同步 `upsert_run_sync`；sweeper 增加 `finalize_pending_deliveries` |
| **D8** | 重复 announce（`_delivered_keys` 丢失） | 部分 | 并发场景 TOCTOU 双发风险 | per-run async lock 串行化 |

### 6.3 重启后 + orphan recovery + 内存

| # | 组合 | durable? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| **E2** | 重启时 run RUNNING（保留 RUNNING，task 死） | 部分 | R2 导致 wedged 检测失效→卡死的 restored run 不被判 wedged；steer 重建失败→僵尸 | (1) 恢复的 RUNNING 标 INTERRUPTED；(2) `started_at` 改 wall-clock；(3) steer 加超时 |
| **E4** | 重启时 run TERMINAL（未交付） | **否** | 见 D7——TERMINAL+PENDING 无重试→结果永久丢失 | 见 D7 |
| **E6** | monotonic clock 不连续（R2） | **否** | restored run 的 stale/wedged/expiry 检测全部失效 | 所有持久化时间戳改 `time.time()` |
| **F4** | orphan recovery finalize 不落盘 | **否** | `set_run(updated)`（`recovery.py:322`）只 `memory.set_run`，不 `upsert_run_sync`→重启后状态回退 | 跟 `store_sqlite.upsert_run_sync` |
| **F5** | cancel_recovery 不清落盘字段 | **否** | `cancel_recovery` 清进程内 attempt 计数，但不清 `run.recovery_attempts_persisted`（落盘）→重启后误判 wedged | 同时 `update_run(run_id, recovery_attempts_persisted=0)` |
| **G1** | `_is_terminal` 集合过时（R1） | — | 内存无界增长，2000 上限失效 | `memory.py:28-37` 改为 `run.execution.status == ExecutionStatus.TERMINAL` |
| **G5** | `find_by_child_session_key` O(n) | — | R1 导致 registry 撑大→spawn/kill 随 run 数线性慢 | 维护 `child_session_key → run` 索引 dict |

---

## 7. Evidence ledger

两个锚点事实：
1. **`flow_id` 仅内存**：`_active_flow_id`（`evidence_ledger.py:81-91`）读 `state_register_mem`，`StateRegisterMeM._states` 从不持久化。
2. **状态是派生的**：stale 在读取时从 append-only 事件流用 `derive_evidence_state`（`:676-708`）重构。

### 7.1 stale 触发 + 清除 + broken

| # | 组合 | 正确? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| A1/A2 | write_file/patch_file 编辑→自身 stale | 正确 | — | — |
| **A3** | shell 写（terminal/python_repl）→**不触发 stale** | **否（盲点）** | `echo >`/`cp`/`sed -i`/Python 写文件→`changed_paths` 不获得路径→**shell 修改的源文件 pytest 永远 [pass]** | `ensure_checkpoint` 后与 shadow store diff 获取触及路径，调 `mark_evidence_stale_for_paths` |
| **A6** | 跨会话 200 会话扫描上限 | **否（漏报）** | 201+ 会话→只前 200 个得到 stale 行 | 改"每会话一行"为"一条带 affected_sessions JSON 的 stale 行" |
| **A7** | 跨会话路径匹配不一致（legacy 子串 vs token 级） | **否（误报+漏报）** | 只读会话验证用子串→`a.py` 误匹配 `tests/test_data.py`→false-positive stale | 废弃 legacy 子串分支；始终携带 `modified_files` |
| **A8** | 200+ 文件编辑→changed_paths 驱逐 | **否（漏报）** | 超 200 不同编辑→验证快照缺早期编辑→false-negative stale | `derive_evidence_state` 按 `last_edit_at` 时间戳派生 stale |
| **B3** | 回归门控 skip（unsafe/非白名单/timeout）→**不追加**→stale 永存 | **否（pin 住 finish）** | 不可重放命令的 stale 永远存在→Gate C 拒绝直到 auto-fail→**flow 永久 pin 在 unfinished** | 对 unsafe skip 追加 `unmark`（默认信任验证仍有效） |
| **B4** | 新 passed 行不清除旧 stale | **否（pin 住 finish）** | 旧行 stale 导致 Gate C 拒绝，即使存在新干净行 | `record_verification_evidence` 在重跑相同命令 passed 时追加隐式 `unmark` |
| **C4** | completion drain 对 broken/stale 盲目 | **否（门控失效）** | `_completion_gate_violated`（`completion_drain/core.py:70`）只查 `"FAIL"`→带 broken 回归的子代理可宣布完成 | 改为 `return "FAIL" in evidence or "[broken]" in evidence or "[stale]" in evidence` |

### 7.2 flow_id 绑定 + retention + 读取点

| # | 组合 | 正确? | 失败情景 | 修复建议 |
|---|---|---|---|---|
| **D2** | 重启→flow_id 丢失 | **否（灾难性）** | 重启后新 stale/broken/unmark 事件获 `flow_id=NULL`→`read_for_flow` 不匹配→**Gate C 看不到新失效事件→回归逃过门控** | (a) FLOW_ID 持久化到 flow store；(b) `read_for_flow` fallback 从 flow store state 重建会话集 |
| **F1** | 30 天 TTL | **否（漏报）** | 运行超 30 天的 flow 丢失早期 stale/unmark 事件→验证行重新派生为 clean→Gate C 通过 | flow 锚定的行豁免年龄删除 |
| **F2** | 每会话 100 行上限 | **否（漏报+孤立）** | 驱逐 broken 事件→flow 在确认回归下通过 | 豁免 broken 事件免于驱逐直到验证行被驱逐 |
| **F4** | 账本不可读时 Gate C fail-open | **否（漏报偏向）** | `collect_evidence_summary` 返回 None→Gate C 通过→回归逃过门控 | Gate C 区分"无证据"（通过）与"收集器错误"（拒绝重试） |
| **G4 失败 2** | 回归门控会话作用域重放 | **否** | `reverify_flow_evidence` 通过 `read_for_keys`（`regression_gate.py:341`）而非 `read_for_flow`→重放其他 flow 的命令 | 通过 `read_for_flow(flow_id)` 读取 |
| **G4 失败 3** | max_reverify_commands=10 | **否** | 11+ 唯一 passed 命令→11+ 不被重放→若 stale→永存→auto-fail | 超 cap 时为 cap 外命令追加 unmark |

### 7.3 读取点不对称总结

| 读取点 | 作用域 | 运行回归门控? | 检查 | 严格度 |
|---|---|---|---|---|
| spawn `_first_turn_self_evident` | session_key | 否 | `[pass]` & 无 `FAIL`/`[stale]`/`[broken]` | 严格 |
| completion drain | session_key | 否 | **仅 `FAIL`** | **过宽** |
| resume judge prompt | session_key | 否 | 渲染到 prompt | 中 |
| finish Gate C | **flow_id** | 是 | `FAIL`/`[broken]`/`[stale]` | 严格 |

重启后：flow 作用域读取（Gate C）丢失新失效事件，会话作用域读取仍看到→**重启后 drain/spawn/resume 比 finish 更严格，重启前相反**。

---

## 8. 跨子系统最严重缺陷 Top 20

| 排名 | 缺陷 | 子系统 | 位置 | 后果 |
|---|---|---|---|---|
| 1 | G2-race：per-turn aclean_old_checkpoints 无 busy_timeout + race | checkpointer | `core.py:431`、`thread_safe_checkpointer.py:234` | turn 高频失败；并发 session 最新 checkpoint 被误删 |
| 2 | `_HITL_PENDING` 不持久化，启动不重建 | HITL | `session_state.py:97`、`__main__.py` | 重启后图悬挂但服务端报 IDLE→新 turn 打断 interrupted thread |
| 3 | auto-turn `apply_hitl_pending` no-op | HITL | `stream_driver.py:105-106`、`auto_turn.py:252-285` | headless auto-turn 中断被孤立→drain 践踏→checkpoint 修剪 |
| 4 | WAITING flow 永久滞留 | TaskFlow | `sweeper.py:476-484` + `store_sqlite.py:423` | stale 标记刷新 updated_at→liveness 永不触发 |
| 5 | 空 result/死 child 被标 DONE | TaskFlow/subagent | `taskflow_resume.py:329` + `delivery.py:412-424` | 丢失结果当成功→下游级联错误 |
| 6 | delivery 状态只 30s flush，TERMINAL+PENDING 重启无重试 | subagent | `run_manager.py:231` vs `delivery.py:330/365` | 完成结果永久丢失 |
| 7 | `time.monotonic()` 跨重启用于持久化时间戳 | subagent | `state.py:37`、`recovery.py:134`、`helpers.py:67/104` | restored run 的 stale/wedged/expiry 检测全部失效 |
| 8 | 不可重放命令的 stale 永存 | evidence | `regression_gate.py:395-402` | flow 永久 pin 在 unfinished |
| 9 | `flow_id` 仅内存，重启后丢失 | evidence | `evidence_ledger.py:81-91`、`state_register.py:49` | 重启后新失效事件对 Gate C 不可见→回归逃过门控 |
| 10 | generic crash 不触发 heal | checkpointer | `state_port.py`、`__main__.py` | dangling tool_calls→provider-invalid transcript→下一 turn 硬失败 |
| 11 | CLAIMED row 无 boot 恢复 | checkpointer | `user_input_queue.py`、`__main__.py` | session 24h 内无法 drain 新消息 |
| 12 | `_is_terminal` 集合过时 | subagent | `memory.py:28-37` | 内存无界增长，2000 上限失效 |
| 13 | delete_thread_history 无 try/except + 无 busy_timeout | checkpointer | `session_cleanup_service.py:127` | SQLITE_BUSY 中断整个 purge→session 半删除 |
| 14 | kill-vs-completion 仲裁死代码 | subagent | `lifecycle.py:203-235`、`run_manager.py:315` | provider OK+result 被 kill 吞掉 |
| 15 | kill INTERRUPTED run 不关 checkpointer | subagent | `kill.py:28` | aiosqlite 连接泄漏 |
| 16 | supersede 后 TOCTOU 复活 | TaskFlow | `store_sqlite.py:318` | 旧 flow 被并发写复活→与 successor 并行运行 |
| 17 | shell 写不触发 stale | evidence | `terminal.py`、`python_repl.py` | shell 修改的源文件验证永远 [pass] |
| 18 | completion drain 对 broken/stale 盲目 | evidence | `completion_drain/core.py:70` | 确认回归不阻塞非 flow announce |
| 19 | timeout 是 per-turn 非 wall-clock | subagent | `core.py:1296-1303` | run 实际可跑 goal_max_turns×timeout |
| 20 | orphan recovery finalize 不落盘 | subagent | `recovery.py:322` | 重启后状态回退（内存说终结，重启又活过来） |

---

## 9. 实施方案

### 9.1 P0：紧急修复（已确认的实际 bug，1-2 天）

**P0-1 Subagent registry `_is_terminal` 枚举对齐**（缺陷 #1）
- `agent/tools/subagent/registry/memory.py:28-37`：改为 `run.execution.status == ExecutionStatus.TERMINAL`，不再用字符串字面量集合。
- 测试：注册 2001 个 terminal run→驱逐最旧一个；active run 不被驱逐。

**P0-2 `session_dir_sweep` thread_id 映射统一**（缺陷 #2）
- `server/service/session_dir_sweep.py:104-105`：查询参数从原始 session_id 改为 `str(rand_str_to_int(session_id))`。
- 新增 `session_thread_id(session_id) -> str` 单一映射函数，所有 checkpoint 查询点统一使用。
- 测试：仅有 checkpoint 的 session 文件夹不被删。

**P0-3 Trajectory `prune_session` 接线或删除**（缺陷 #3）
- 选项 A（推荐）：`server/service/trajectory_store.py:188` append 后调 `prune_session`，debounce 60s。
- 选项 B：删除 `prune_session` 方法和 `max_turns_per_session` 配置。
- 测试：选项 A 下 append 51 turn 后只返回最新 50 turn 事件。

### 9.2 P1：重启恢复契约（3-5 天）

**P1-1 HITL 启动重建 + busy 信号双保险**（缺陷 #4）
- `server/__main__.py`：boot 时遍历所有 session 调 `read_interrupt`→非 None 则 `set_hitl_pending(True)`。fail-open，串行或小批量（每批 10）。
- `server/service/input_queue_service.py:427-435`：空闲分支前加 `get_pending_interrupt` 查询，有 pending interrupt 则走 queue。
- `server/service/session_cleanup_service.py`：`purge_session_data` 加 `set_hitl_pending(session_id, False)`。
- 测试：重启后 checkpointer 持有 interrupt 的 session，`is_hitl_pending` 为 True；`submit_user_input` 走 queue。

**P1-2 auto-turn `apply_hitl_pending` 重写 + headless fail-closed**（缺陷 #5）
- `server/service/auto_turn.py:252-285`：`_AutoTurnStreamDriver` 重写 `apply_hitl_pending` 调 `set_hitl_pending(self.session_id, True)`。
- `agent/tools/question.py:88-109`、`core.py:156-194`、`exit_plan_mode.py:169-185`、`path_utils.py:573-584`、`strategies.py:462-468`：在 `interrupt()` 前查 `current_operator() is None`，None 则 deny/abort。
- 测试：headless auto-turn 调 question→不挂起，直接 deny。

**P1-3 重启后 in-flight step 快速恢复**（缺陷 #6）
- `agent/tools/subagent/registry/state.py:54-79`：恢复的 RUNNING run 标 INTERRUPTED + `pause_reason="process_restart"`，重置 `started_at = time.monotonic()`。
- `agent/tools/subagent/registry/state.py:23-42`：`_finalize_restored_pending` 调 `run_subagent_announce_flow`，让 finalized PENDING child 触发 resume。
- `config/features/agent_side/subagent_infra.py`：`orphan_wedged_age_seconds` 分级（重启后首次 60s，后续正常）。
- 测试：恢复的 RUNNING run 标 INTERRUPTED，`is_live_unended_run` 返回 False。

**P1-4 delivery 即时持久化 + sweeper 重试**（缺陷 #7, #8）
- `agent/tools/subagent/registry/delivery_state.py`：`mark_delivery_delivered` / `mark_delivery_failed` / `mark_delivery_suspended` 在写内存后立即 `upsert_run_sync`。
- `agent/tools/subagent/registry/lifecycle.py`：sweeper 增加 `finalize_pending_deliveries` 扫 TERMINAL+PENDING 的 run 重试 announce。
- 测试：`mark_delivery_delivered` 后立即读 SQLite 已 DELIVERED；重启后不重复 announce 已 DELIVERED 的 run。

**P1-5 boot-time heal + CLAIMED recover**（缺陷 #10, #11）
- `server/__main__.py`：boot 时对每个 session 检测 dangling AIMessage(tool_calls) 无 ToolMessage→调 `heal_interrupted_turn`；全局 `recover()` 扫描过期 CLAIMED row。
- 测试：崩溃后重启，dangling tool_calls 被 heal；过期 CLAIMED row 被 recover。

**P1-6 monotonic 时钟改 wall-clock**（缺陷 #7/R2）
- 所有持久化的时间戳（`started_at/ended_at/archive_at/delivered_at/suspended_at/last_attempt_at/killed_at`）改用 `time.time()`（wall-clock）。
- 或 restore 时把旧 monotonic 时间戳重设为当前 monotonic（牺牲跨重启时长精度）。
- 测试：恢复后 `is_stale_unended_run` 计时从重启点算。

### 9.3 P2：并发韧性（1-2 天）

**P2-1 全局 `busy_timeout`**（缺陷 #9）
- `agent/checkpointer/async_sqlite_checkpointer.py:16-19`：加 `PRAGMA busy_timeout=30000`。
- `thread_safe_checkpointer.py:234,336`、`async_sqlite_checkpointer.py:37,55`：所有 bare 连接加 `busy_timeout=30000`。
- 新建 `_connect_with_pragmas(path)` helper 统一。
- 测试：并发写第二个写者等待而非立即 SQLITE_BUSY。

**P2-2 移除 per-turn 全局 clean + 异常保护**（缺陷 #10, #11, #12）
- `agent/core.py:431`：移除 `aclean_old_checkpoints` 从 `_build_graph`，改 boot 时一次 + 定期 cron。加 try/except（fail-open）。
- `agent/checkpointer/thread_safe_checkpointer.py:237-242`：选保留行 SQL 改为 `ORDER BY (checkpoint_ns = '') DESC, rowid DESC LIMIT 1`（与单线程版一致）。
- G2-race 修复：DELETE 条件改为 `checkpoint_id IN (子查询)` 或 `BEGIN IMMEDIATE` + busy_timeout。
- 测试：`aclean_old_checkpoints` 抛 SQLITE_BUSY 时 `_build_graph` 不失败；并发 turn 不互相打断。

**P2-3 `delete_thread_history` 加 try/except + purge 顺序反转**（缺陷 #13）
- `server/service/session_cleanup_service.py:127`：加 try/except（与其他 step 一致）。
- purge 顺序：先删 checkpoint（step 3）再删 mes_memory（step 1）。
- 测试：SQLITE_BUSY 时 purge 不中断后续步骤。

### 9.4 P3：质量门与 evidence 改进（5-7 天）

**P3-1 不可重放命令 stale 自愈**（缺陷 #14）
- `agent/tools/pub_base/regression_gate.py:395-402`：unsafe skip 时追加 `unmark`（默认信任验证仍有效）；timeout skip 保持 stale。
- `agent/tools/pub_base/evidence_recorder.py`：会话重跑相同命令 passed 时追加隐式 `unmark`。
- 测试：含 shell 元字符的命令 stale 后→regression gate skip→追加 unmark→Gate C 不拒绝。

**P3-2 `flow_id` 持久化或 fallback**（缺陷 #15）
- `agent/tools/pub_base/evidence_ledger.py:299-313` `read_for_flow`：空 flow_id 时从 flow store state（`state.steps[].child_session_key` + `creator_session_key`）重建会话集，`read_for_keys` 这些会话。
- 或 FLOW_ID 双写到 `state_register_db`。
- 测试：重启后 flow_id 为空时 `read_for_flow` fallback 仍返回 evidence。

**P3-3 空 result/timeout/kill 传 `step_outcome`**（缺陷 #16）
- `agent/tools/subagent/announce/delivery.py:419`：`taskflow_resume.coroutine(...)` 传入 `step_outcome`：timeout→`"failure"`，kill→`"failure"`。
- `agent/tools/taskflow/tools/taskflow_resume.py:329`：空 result 且无 judge_criteria→标 FAILED。
- 测试：timeout/kill 的 step 标 FAILED 而非 DONE。

**P3-4 `_auto_fail_finish` kill detached children**（缺陷 #17）
- `config/features/agent_side/taskflow_infra.py`：新增 `auto_kill_on_detach: bool = True`。
- `agent/tools/taskflow/tools/taskflow_finish.py:224-278`：detach 时对每个 detached child 调 `kill_subagent_run_with_cascade`。
- 测试：fail 一个有 live child 的 flow→child 被 kill。

**P3-5 kill INTERRUPTED run 调 `_release_child_resources`**（缺陷 #18）
- `agent/tools/subagent/control/kill.py:28`：对 INTERRUPTED run 调 `_release_child_resources`（或专门的 checkpoint 关闭）。
- 测试：kill INTERRUPTED run 后 checkpointer 连接已关闭。

**P3-6 kill-vs-completion 仲裁修复**（缺陷 #19）
- `agent/tools/subagent/registry/lifecycle.py:203-235`：检查传入的 outcome（而非快照 outcome）。
- 或 `save_kill_reconciliation`（`run_manager.py:310-323`）在快照中标记"kill 发起"。
- 测试：即将成功完成的 run 被 kill→completion 覆盖 kill 交付 result。

**P3-7 WAITING flow 不刷新 updated_at**（缺陷 #20）
- `agent/tools/subagent/registry/sweeper.py:476-484`：写 `stale_detected_at` 时不走 `update_flow`（会 bump updated_at），改用不更新 updated_at 的专用 update。
- 或 liveness sweep（`:363-366`）用 `wait.set_at` 而非 `updated_at` 作为 WAITING flow 的 age 基准。
- 测试：WAITING flow 超时后被 liveness 收集而非永久滞留。

**P3-8 supersede bump revision**（缺陷 #21）
- `agent/tools/taskflow/registry/store_sqlite.py:318`：`mark_flow_superseded` bump `expected_revision`。
- 或 `update_flow` 的 WHERE 加 `AND superseded_by IS NULL`。
- 测试：supersede 后持旧 revision 的并发写触发 FlowConflictError。

**P3-9 循环依赖检测**（缺陷 #22）
- `agent/tools/taskflow/tools/_shared.py:637-694`：`validate_steps_list` 末尾加拓扑排序/DFS 环检测。
- `taskflow_replan.py:282`：merged 列表也跑该校验。
- 测试：A→B→A 环被拒绝。

**P3-10 shell 写触发 stale**（缺陷 #23）
- `agent/tools/terminal.py` / `python_repl.py`：`ensure_checkpoint` 后与 shadow store diff 获取触及路径，对每个调 `mark_evidence_stale_for_paths`。
- 测试：shell 编辑文件后该 session 的 evidence 被标 stale。

**P3-11 completion drain 检查 broken/stale**（缺陷 #24）
- `agent/middlewares/subagent_completion_drain/core.py:70`：改为 `return "FAIL" in evidence or "[broken]" in evidence or "[stale]" in evidence`。
- 测试：带 broken 回归的子代理 announce 时 drain 注入门控消息。

**P3-12 goal loop wall-clock timeout**（缺陷 #25）
- `agent/tools/subagent/spawn/core.py:1296-1303`：每轮传剩余 budget（`run_timeout_seconds - accumulated_runtime_ms/1000`）。
- 测试：5 轮 goal loop 总 wall-clock ≤ `run_timeout_seconds`。

**P3-13（可选）token budget 硬执行 + WAITING auto-fail**（缺陷 #26, #27）
- `taskflow_dispatch.py` 校验阶段加 `total_tokens >= token_budget` 拒绝（cap 达到时）。
- `waiting_auto_fail_after_hours` 配置（默认 None=不 auto-fail）。

### 9.5 P4：观测性（2-3 天）

**P4-1 trajectory `error` 事件**
- `server/service/stream_dispatch.py:585-590`：异常路径 reraise 前 best-effort 调 `record_event(kind="error", is_error=True)`。fail-open。

**P4-2 trajectory WAL + busy_timeout**
- `server/service/trajectory_store.py:152-162`：加 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`。

**P4-3 trajectory seq**
- `server/service/trajectory_store.py:344-360`：用进程内单调递增计数器生成 seq，不再恒 0。ts 改毫秒精度。

---

## 10. 验收标准

### P0
- [ ] `_is_terminal` 用 `ExecutionStatus.TERMINAL` 枚举判定，注册 2001 个 terminal run 后 `_runs` ≤2000。
- [ ] `session_dir_sweep._checkpoints_has_session` 用 `str(rand_str_to_int(session_id))` 查询，仅有 checkpoint 的 session 文件夹不被删。
- [ ] Trajectory `prune_session` 接线或删除；选项 A 下 append 51 turn 后只返回最新 50 turn 事件。

### P1
- [ ] 重启后 checkpointer 持有 interrupt 的 session，`is_hitl_pending` 为 True。
- [ ] 重启后有 pending interrupt 的 session，`submit_user_input` 走 queue 而非直接 dispatch。
- [ ] headless auto-turn 调 question→不挂起，直接 deny。
- [ ] 恢复的 RUNNING run 标 INTERRUPTED + `pause_reason="process_restart"`，`is_live_unended_run` 返回 False。
- [ ] PENDING child finalize 后触发 announce，step 不永远 DISPATCHED。
- [ ] `mark_delivery_delivered` 后立即读 SQLite 已 DELIVERED；重启后不重复 announce。
- [ ] 重启后 dangling tool_calls 被 heal；过期 CLAIMED row 被 recover。
- [ ] 恢复后 `is_stale_unended_run` 计时从重启点算。

### P2
- [ ] 所有 checkpointer 连接设 `busy_timeout=30000`。
- [ ] `aclean_old_checkpoints` 抛 SQLITE_BUSY 时 `_build_graph` 不失败，turn 正常启动。
- [ ] turn 级不再调 `aclean_old_checkpoints`，只 session purge 和 boot 调。
- [ ] G2-race：并发 session commit 新 checkpoint 不被误删。
- [ ] `delete_thread_history` SQLITE_BUSY 时 purge 不中断后续步骤。

### P3
- [ ] 不可重放命令的 stale 被 `unmark` 清除，Gate C 不拒绝 finish（timeout skip 仍保留 stale）。
- [ ] 重启后 `flow_id` 为空时，`read_for_flow` fallback 到 session_id 查询，仍返回 evidence。
- [ ] timeout/kill 的 step 标 FAILED 而非 DONE。
- [ ] `auto_kill_on_detach=True` 时 fail/cancel/auto-fail 的 detached child 被 kill。
- [ ] kill INTERRUPTED run 后 checkpointer 连接已关闭。
- [ ] kill vs completion 仲裁：即将成功的 run 被 kill→completion 覆盖交付 result。
- [ ] WAITING flow 超时后被 liveness 收集而非永久滞留。
- [ ] supersede 后持旧 revision 的并发写触发 FlowConflictError。
- [ ] A→B→A 循环依赖被创建时拒绝。
- [ ] shell 编辑文件后该 session 的 evidence 被标 stale。
- [ ] 带 broken 回归的子代理 announce 时 drain 注入门控消息。
- [ ] 5 轮 goal loop 总 wall-clock ≤ `run_timeout_seconds`。

### P4
- [ ] 流异常时 trajectory 有 `turn_start` + `error` 事件。
- [ ] Trajectory DB 在 WAL 模式，外部 reader 不 SQLITE_BUSY。
- [ ] Trajectory hook 事件有真实 seq，`read()` 按 chronological 顺序返回。

## 11. 风险与取舍

- **HITL 启动重建的扫描成本**：boot 时遍历所有 session 调 `read_interrupt` 会 lease graph、读 checkpointer。session 多时（100+）可能拖慢启动。缓解：串行 + 小批量（每批 10），fail-open。
- **恢复的 RUNNING 标 INTERRUPTED 改变了现有语义**：`is_live_unended_run` 的所有调用点需重新验证。但当前"按 live 处理"正是导致卡死的根因，改为"非 live"是修 bug 而非引入回归。
- **`auto_kill_on_detach` 可能误杀**：fail 一个 flow 时 child 可能正在做有用工作。缓解：默认 True（资源泄漏比误杀更常见），日志记录 kill 决策，可配 False。
- **`stale_ack`/`unmark` 削弱 evidence gate 保证**：不可重放命令的 stale 不再阻断 finish。取舍：当前是"永久 pin 住 finish"，比"放行可能 stale"更糟。
- **`flow_id` fallback 的性能**：`read_for_flow` fallback 从 flow store 重建会话集是 O(steps) 的 DB 读。缓解：只在 flow_id 为空时 fallback，正常路径不变。
- **monotonic → wall-clock 的 NTP 跳变风险**：wall-clock 有 NTP 跳变，需夹紧（如 `max(0, now - started_at)`）。
- **不做自动 activity replay**：in-flight step 仍靠 orphan recovery + 新 turn 重驱动，只是缩短了卡死窗口。
- **不改 fail-open 为 fail-closed**：只在"不可重放 stale"这一具体 bug 上改，不全局改。
- **不修低危项**（#29-32）：`read_interrupt` 只返回首个 interrupt、`as_node="model"` 硬依赖、`mark_flow_superseded`（已并入 #21）、dispatch lock 进程内。

## 12. 不做什么

- **不引入 Temporal 式 activity replay**：asyncio task 进程死即死，本方案只补"状态活了但执行没活"的卡死/僵尸/重复。
- **不改 fail-open 为 fail-closed**：judge/evidence gate 的 fail-open 是"不困住 flow"的有意取舍，只在"不可重放 stale"这一具体 bug 上改。
- **不修 `read_interrupt` 只返回首个 interrupt**：并行分支后续 interrupt 成 zombie 是已知限制，修需重设计 interrupt 聚合协议。
- **不修 dispatch lock 进程内**：多进程部署不是当前形态（单进程 Robyn）。
- **不默认开启 token budget 硬执行**：只做"cap 达到时阻断新 dispatch"的可选配置。
- **不修工具级子进程取消**（kill 时僵尸 Bash）：需工具自身响应 `CancelledError`，是工具层改动；只在 kill 日志记录"可能残留工具子进程"。
- **不做 trajectory 完整重构**：只补 `error` 事件、WAL、seq，不改 schema。
- **不做 evidence legacy jsonl 迁移**：声明但未实现的 jsonl 迁移是另一个独立问题。

## 13. 实施顺序建议

```
P0（1-2 天）：三个已确认 bug，独立可并行
  ├─ P0-1 _is_terminal 枚举对齐
  ├─ P0-2 thread_id 映射统一
  └─ P0-3 trajectory prune 接线 or 删除

P1（3-5 天）：重启恢复契约，有依赖关系
  ├─ P1-1 HITL 启动重建（独立）
  ├─ P1-2 auto-turn apply_hitl_pending + headless fail-closed（独立）
  ├─ P1-3 in-flight step 快速恢复（与 P1-6 联动）
  ├─ P1-4 delivery 即时持久化（独立）
  ├─ P1-5 boot-time heal + CLAIMED recover（独立）
  └─ P1-6 monotonic → wall-clock（与 P1-3 联动）

P2（1-2 天）：并发韧性，独立可并行
  ├─ P2-1 busy_timeout 统一
  ├─ P2-2 移除 per-turn clean + G2-race 修复
  └─ P2-3 delete_thread_history try/except + purge 顺序

P3（5-7 天）：质量门与 evidence，部分可选
  ├─ P3-1 stale 自愈（独立，高价值）
  ├─ P3-2 flow_id fallback（独立）
  ├─ P3-3 空 result 传 step_outcome（独立）
  ├─ P3-4 auto_kill_on_detach（独立）
  ├─ P3-5 kill INTERRUPTED 关 checkpointer（独立）
  ├─ P3-6 kill-vs-completion 仲裁（独立）
  ├─ P3-7 WAITING 不刷新 updated_at（独立）
  ├─ P3-8 supersede bump revision（独立）
  ├─ P3-9 循环依赖检测（独立）
  ├─ P3-10 shell 写触发 stale（独立）
  ├─ P3-11 drain 检查 broken/stale（独立）
  ├─ P3-12 goal loop wall-clock（独立）
  └─ P3-13（可选）token budget + WAITING auto-fail

P4（2-3 天）：观测性，独立可并行
  ├─ P4-1 trajectory error 事件
  ├─ P4-2 trajectory WAL
  └─ P4-3 trajectory seq
```

总工期约 12-19 天（P0-P2 必做，P3 部分可选，P4 视优先级）。建议 P0 + P2 先做（修复实际 bug + 并发韧性，2-4 天），再 P1（重启恢复契约，3-5 天），P3/P4 视团队带宽排期。

---

## 附录 A：State Register 分层策略

### A.0 背景

`state_register_mem`（`StateRegisterMeM`，`state_register.py:44-129`）是进程内 dict，不持久化；`state_register_db`（`StateRegisterDB`，`:135-323`）是 SQLite。86 个 StateKey 中，32 个已 durable、31 个正确地 mem-only、23 个可选 durable。**不是所有 key 都该双写**——per-turn 计数器双写会跨 turn 污染，in-flight 锁双写会重启死锁。

### A.1 判断标准（三问）

一个 key 是否该 durable，取决于：

1. **丢失方向是否安全？** HITL YOLO 丢失→重新审批（变严格，安全）；`HITL_SESSION_CONFIRM_ALL` 丢失→从严格变宽松（**不安全**，必须 durable）。
2. **turn 死了它该死吗？** `ITERATION_BUDGET_USED` 每 turn 重置，turn 死了也该死；`TOKEN_BUDGET_USED` 跨 turn 累加，turn 死了不能归零。
3. **是 in-flight 锁吗？** `NUDGE_PLAN_EXTRACTION_LOCK` 持久化→重启后永久死锁。锁类 key **绝不** durable。

### A.2 三类分类总览

| 分类 | 数量 | 存储 | 代表 key | 不 durable 的后果 |
|---|---|---|---|---|
| **已 durable**（保持） | 32 | mem+DB 或 DB-only | `PROJECT_DIR`、`LLM_MAIN_MODEL`、`TOKEN_BUDGET_CAP`、`PLAN_STATUS`、`AGENT_CONFIG`、`SYSTEM_PROMPT` | 契约/安全/路径解析破坏 |
| **不该 durable**（保持 mem-only） | 31 | mem-only | `ITERATION_BUDGET_USED`、`HEARTBEAT_*`、`TOOL_GUARDRAIL_STATE`、`CURRENT_TOOL_*`、`ANSWERING`、`IS_STREAM_TURN`、`NUDGE_*_LOCK` | per-turn 计数器跨 turn 污染 / in-flight 锁重启死锁 / stream 瞬态标志 turn 死即应死 |
| **可选 durable**（多数 fail-open 安全） | 23 | mem-only | `HITL_SESSION_YOLO`、`OUTPUT_REPETITION_*`、`SUMMARIZATION_LAST_USER_QUESTION` | 丢失不致命，多数变严格/重置计数器 |

### A.3 不该 durable 的 31 个 key（完整清单）

| key | state_keys.py 行 | 不 durable 理由 |
|---|---|---|
| `ITERATION_BUDGET` / `ITERATION_BUDGET_USED` | 18, 19 | 每 turn before_agent 重置（`iteration_budget/core.py:90-91`） |
| `TOOL_GUARDRAIL_STATE` | 31 | turn 内累积，turn 边界 reset（`tool_guardrails/core.py:364`） |
| `IS_STREAM_TURN` | 32 | per-stream-turn，turn 结束删 |
| `LLM_FALLBACK_INDEX` / `LLM_FALLBACK_SWITCHES` | 35, 37 | per-model-call / per-turn（before_agent reset，`llm_retry/core.py:124`） |
| `LLM_CONTENT_FILTER_BLOCKED` / `TERMINATED` | 38, 39 | per-stream，立即消费即清 |
| `LLM_PARTIAL_STREAM_STUB` / `CAUSE` | 40, 41 | per-call，下次 call 消费即清 |
| `MULTIMODAL_TRYING_NATIVE` / `NATIVE_MODEL` | 108, 109 | per-call/stream |
| `HEARTBEAT_ITER/TOOL/STALE/KILLED/SKIP/LAST_ITER/LAST_TOOL` | 111-117 | per-turn，before_agent reset（`heartbeat_staleness/core.py:190-196`） |
| `ANSWERING` | 174 | per-turn |
| `CURRENT_TOOL_NAME` / `CURRENT_TOOL_ID` | 178, 180 | per-tool/per-turn |
| `NUDGE_PLAN_EXTRACTION_LOCK` | 141 | **in-flight 锁，持久化会死锁** |
| `NUDGE_REVIEW_MEMORY_LOCK` | 148 | 同上 |
| `COMPRESSION_TODO_UPDATE_LOCK` | 149 | 同上 |
| `HITL_TURN_INTERRUPTED` | 167 | per-turn |
| `TODO_REFERENCE_WARNING` | 190 | 瞬态，读取即删 |
| `REQUESTER_SESSION_KEY` / `CALLER_SCOPE` | 181, 182 | 子 session 生命周期=单次 subagent run，ephemeral（durable subagent resume 实现时再考虑） |
| `FLOW_ID` / `STEP_ID` | 186, 187 | 同上（见 Gap #5） |
| `SUMMARIZATION_TURN_ATTEMPTS` / `OVERFLOW_RETRIES` | 131, 132 | per-turn，`_reset_turn_state` 重置 |
| `SUMMARIZATION_COMPACT_REQUESTED` | 139 | 瞬态信号 |

### A.4 五种双写模式（仓库已有，按写入频率选择）

| 模式 | 适用 | 范例 | 性能 | 待行动 |
|---|---|---|---|---|
| **即时双写**（写时 mem+DB） | 低频写、丢失=契约破坏 | `session_settings._write_value`（`:108-111`）、`project_dir.write_project_dir`（`:196-197`） | 每次写 1 次 SQLite（~1-2ms），低频可接受 | Gap #1/#2 |
| **惰性 rehydrate**（mem miss→DB→回填） | agent 侧 mem-only 读、偶尔 miss | `session_settings._read_value`（`:95-105`）、`read_project_dir_durable`（`:149`） | 热路径 mem 命中零 I/O；重启后首次读 1 次 DB | Gap #4 |
| **turn 边界批量 flush** | 热路径高频写、DB 仅为 durability、in-flight 用 mem | 无（待新建） | 消除热路径 DB I/O，turn 结束 flush 1 次 | Gap #3 |
| **boot prime**（启动时全量灌 mem） | agent 侧 mem-only 读、重启后立即需要 | `project_dir.prime_mem_from_store`（`:214-264`） | boot 时 O(N) DB 批量读，不影响运行时 | P1-1 HITL 启动重建同思路 |
| **per-session 一次性 rehydrate**（LRU 守卫） | 重启后恢复但不必 boot 全量灌 | `summarization._RESTORED_COOLDOWN_SESSIONS`（OrderedDict 上限 512） | 每 session 首次访问 1 次 DB | Gap #6 |

### A.5 六个 Gap（最值得行动的项）

**Gap #1（契约破坏）：`HITL_PERMANENT` 文档声称"persistent across sessions"但 mem-only**
- `ApprovalDecision.ALWAYS` docstring（`humanInTheLoop/types.py:58`）："persistent across sessions"。
- 但 `HITL_PERMANENT`（`state_keys.py:161`）经 `TypedState`（`:198-241`）只读写 `state_register_mem`，不进 DB。
- 注意区分：精确 tool-call 的"ALWAYS"裁决由独立的 `ToolApprovalStore`（`approvals.json`）持久化——那是另一条路径。此 gap 仅指命令模式（terminal 等）的"永久"allowlist。
- **修复**：走 `session_settings._write_value`/`_delete_value` 即时双写；或确认它本就该 session-scoped，修正文档。需与设计者确认意图。

**Gap #2（安全姿态）：`HITL_SESSION_CONFIRM_ALL` 丢失方向不安全**
- 严格模式丢失→会话从"每次确认"静默降级到 auto_edit（变宽松）。
- 这是少数"丢失方向不安全"的 mem-only 安全 key（其余 HITL 丢失都变严格）。
- **修复**：走 `_write_value`/`_delete_value` 即时双写。

**Gap #3（热路径性能）：`TOKEN_BUDGET_USED`/`COST` 每 model call 即时双写**
- `token_budget/core.py:70-71`：每次 model call `_post_call` 对 USED 和 COST 各做 `mem.set + db.set`→**每 model call 2 次 SQLite 提交**。
- cap 判定（`_pre_call:179-187`）只读 mem，DB 纯粹为重启 durability。
- **修复**：改 turn 边界批量 flush——model call 只写 mem，turn 结束时 flush 一次 DB。in-flight cap 执行不受影响。

```
当前：每 model call → mem(USED) + db(USED) + mem(COST) + db(COST) = 2 次 DB 提交
改后：每 model call → mem(USED) + mem(COST) = 0 次 DB
      turn 结束   → db(USED) + db(COST) = 1 次 DB 提交
```

**Gap #4（热路径性能）：`EXTERNAL_PATH_YOLO` 是热路径上唯一 DB-only 读**
- `path_utils._is_yolo`（`:268-272`）：每次文件工具外部路径检查都 `state_register_db.get_state`。
- **修复**：加 mem 缓存（读时 rehydrate mem，全局 session `__global__`），写时双写失效。

**Gap #5（durable subagent 执行）：`FLOW_ID`/`STEP_ID`/`REQUESTER_SESSION_KEY`/`CALLER_SCOPE`**
- spawn 时 stamp 的 mem-only key，子 session 生命周期=单次 subagent run，通常 ephemeral。
- 若实现 durable subagent resume（P1-3），`CALLER_SCOPE` 丢失→子 session fail-closed 失效（安全）；`FLOW_ID` 丢失→evidence 记录失去 flow 绑定（见正文缺陷 #15）。
- **修复**：暂不动；待 P1-3 durable subagent resume 实现时，优先双写 `CALLER_SCOPE`（涉及安全）和 `FLOW_ID`（涉及 evidence gate）。

**Gap #6（thrash 检测一致性）：4 个配套 key mem-only**
- `SUMMARIZATION_COOLDOWN_ROUNDS`/`SKIP_LLM`/`COMPRESSION_COUNT`/`COMPRESSION_INEFFECTIVE` 已 durable（`_COOLDOWN_PERSIST_KEYS`），但配套的 `LAST_USER_QUESTION`/`DEGRADATION_NO_TEXT`/`RECOVERY_ATTEMPTS`/`PREVIOUS_FILE_OPS`（`state_keys.py:126-129`）是 mem-only。
- 重启后 cooldown 还在但 thrash 上下文丢了→thrash 检测行为不一致（fail-open 不致命）。
- **修复**：把 4 个 key 加入 `_COOLDOWN_PERSIST_KEYS`（`summarization/core.py:98-103`），复用已有镜像/rehydrate 机制，改动极小。

### A.6 核心判断

不是"要不要双写"的问题，而是"用哪种模式双写"的问题。仓库已有 5 种成熟模式，覆盖从低频设置到高频计数器到 in-flight 锁的全谱。需要做的不是发明新机制，而是：

1. **修 2 个契约/安全 gap**（#1 `HITL_PERMANENT`、#2 `HITL_SESSION_CONFIRM_ALL`）——即时双写，复用 `_write_value`
2. **优化 2 个热路径**（#3 `TOKEN_BUDGET_USED`、#4 `EXTERNAL_PATH_YOLO`）——改 flush 时机 / 加 mem 缓存
3. **补 1 个 thrash 一致性**（#6）——加入 `_COOLDOWN_PERSIST_KEYS`
4. **不动 31 个 per-turn key**——它们 mem-only 是正确的
5. **不碰 in-flight 锁**——持久化会死锁
6. **Gap #5 待 P1-3**——durable subagent resume 实现时再处理 `FLOW_ID`/`CALLER_SCOPE`

这些 gap 可融入方案的 P1（#1/#2 随 HITL durable 一起做）、P2（#3/#4 随并发韧性一起做，消除热路径 DB I/O）、P3（#6 随 evidence 改进一起做）。
