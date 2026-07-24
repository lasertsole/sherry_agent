# 关键技术决策记录

> 每做出一个影响架构的决策，记录在此。新 agent 接手时必读。

## 决策 1: 独立新建而非替换

**日期**: 2026-07-15

**背景**: 项目已有 `agent/tools/subagent/`（Commander+Worker 模式，302行 `SubagentManager`），需决定是替换还是新建。

**决策**: 在 `future_subagent/` 根目录独立新建，与现有 subagent 共存。

**理由**:
- 现有 subagent 有经验知识图谱闭环（draft→distill→ingest→recall），新系统暂不包含
- 两套工具命名不同（现有 `subagent` vs 新 `sessions_spawn` 等），无冲突
- 独立新建可渐进迁移，不破坏现有功能

---

## 决策 2: 复用 MessageBus 而非新建 Announce 管道

**日期**: 2026-07-15

**背景**: OpenClaw 的 announce 投递深度依赖 gateway/channel（Discord/Slack/Telegram），Python 版需决定投递方式。

**决策**: 复用现有 `MessageBus.publish_inbound()` 进行结果投递。

**理由**:
- 项目已有 MessageBus + InboundMessage 机制
- 无 Discord/Slack/Telegram 渠道需求
- 保留重试/挂起/幂等等可靠性机制，但投递通道用 MessageBus

**影响**:
- `announce/delivery.py` 大幅精简（1983行 → ~500行）
- 无需实现 channel thread binding / route projection

---

## 决策 3: 不移植 ACP/沙箱

**日期**: 2026-07-15

**背景**: OpenClaw 有 ACP (Agent Control Plane) 容器沙箱运行时（1730行）。

**决策**: 不移植。`runtime` 参数仅保留 `"subagent"`，`sandbox` 参数已移除。

**理由**:
- 按用户要求排除容器沙箱
- Python 版子 agent 在同进程内通过 LangGraph ainvoke 执行
- 如需沙箱隔离，后续可通过 process-level 或 Docker 隔离单独实现

**影响**:
- `spawn/core.py` 不含 ACP 分支
- `SandboxMode` 枚举已移除，不再接受 sandbox 参数
- 子 agent 不受沙箱约束，需通过工具黑名单控制权限

---

## 决策 4: 子 Agent 执行方式

**日期**: 2026-07-15

**背景**: OpenClaw 通过 `callGateway({ method: "agent" })` 分发子 agent 执行。Python 版需确定执行方式。

**决策**: 子 agent 通过 `CompiledStateGraph.ainvoke()` 执行。

**理由**:
- 项目已有 LangGraph agent 构建基础设施
- `ainvoke()` 在当前 event loop 中运行，天然支持异步
- 不需要额外的进程管理

**影响**:
- `spawn/core.py` 中的 `callGateway` 替换为 `agent.ainvoke()`
- 子 agent 的 config 传入 `session_id`、`system_prompt` 等
- 子 agent 构建复用项目现有的 agent builder

---

## 决策 5: 持久化用 aiosqlite

**日期**: 2026-07-15

**背景**: OpenClaw 有 JSON fallback + SQLite 两层持久化。

**决策**: 仅用 aiosqlite，不做 JSON fallback。

**理由**:
- 项目已有 aiosqlite 依赖
- SQLite 在所有平台可靠，无需 fallback
- 减少代码量

---

## 决策 6: sessions_yield 实现方式

**日期**: 2026-07-15

**背景**: OpenClaw 的 yield 通过 gateway steering 实现（暂停当前 turn，子 agent 完成后恢复）。

**决策**: 用 `asyncio.Event` + 注册表回调实现推送等待。

**理由**:
- Python 无 gateway steering 机制
- `asyncio.Event` 可实现同等效果：yield 时设置 event 等待，子 agent 完成时 set event
- 父 agent 的 ainvoke 可通过 middleware 监听 event

**影响**:
- `sessions_yield` 工具内部创建 Event 并 await
- 子 agent 完成时通过 registry lifecycle 触发 Event.set()
- 需要在 registry 中维护 `yield_events: dict[str, asyncio.Event]`

---

## 决策 7: sessions_send A2A 通信

**日期**: 2026-07-15

**背景**: OpenClaw 的 sessions_send 支持 agent-to-agent 双向消息、ping-pong 轮次控制。

**决策**: 通过 MessageBus + session key 路由实现。

**理由**:
- MessageBus 已有 publish/consume 机制
- session key 可作为路由标识
- ping-pong 通过消息计数器控制

**影响**:
- `sessions_send` 工具内部通过 MessageBus 发送定向消息
- 目标 session 通过 middleware 接收注入的消息
- 需要维护 `pending_messages: dict[str, list[InboundMessage]]` 暂存

---

## 决策 8: 全量复刻而非 MVP

**日期**: 2026-07-15

**背景**: 用户选择全量复刻，包含 yield 推送等待、send 双向通信、descendant wake、超时管理、orphan recovery 等。

**决策**: 按 7 阶段全量实施，不裁剪功能。

**影响**:
- 总工期约 17 天
- 阶段 5 需实现完整的 lifecycle/sweeper/orphan recovery
- 阶段 6 需实现完整的 yield + send
- 阶段 7 需实现 hooks + followup

---

## 决策 9: fork context 从父 session 复制 transcript

**日期**: 2026-07-21

**背景**: fork 模式下子 agent 需要继承父 session 的对话历史，但原实现依赖调用方传入 `parent_messages`，实际无人传入。

**决策**: 通过 `agent.aget_state()` 从 checkpointer 读取父 session 历史消息，不再依赖外部传参。

**理由**:
- 与项目现有 `_get_agent_history_list()` 方式一致
- 调用方只需传 `requester_session_id`，无需感知 checkpointer
- 内部处理异常降级为 isolated 模式，不影响主流程

**影响**:
- `spawn/context.py` 的 `prepare_spawned_context` 改为 async，参数从 `parent_messages` 改为 `requester_session_id`
- `spawn/core.py` 的 `spawn_subagent_direct` 参数从 `parent_messages` 改为 `requester_session_id`

---

## 决策 10: Swarm/Collect FIFO 调度而非并发池

**日期**: 2026-07-22

**背景**: OpenClaw 的 swarm 模式用 collector 启动一组子 agent 并收集结构化输出。需决定调度策略。

**决策**: 基于 FIFO 队列的并发控制调度。`reserve_swarm_run` 入队，`activate_swarm_run` 按 `max_concurrent` 出队激活。

**理由**:
- FIFO 保证公平性和可预测性
- `max_concurrent` 控制并行度，避免资源爆炸
- 完成后自动激活下一个预留，无需外部调度器

**影响**:
- 新增 `swarm/collector.py` + `swarm/fifo.py`
- `types/swarm.py` 新增 SwarmMode / SwarmRunState / SwarmGroupConfig
- SubagentRunRecord 新增 `swarm_group_id` + `swarm_run_state` 字段

---

## 决策 11: Thread Binding 仅在 SESSION 模式激活

**日期**: 2026-07-22

**背景**: OpenClaw 的 thread binding 将 channel thread 绑定到子 agent session，用于投递来源路由和 idle timeout。

**决策**: 仅 `SpawnMode.SESSION` 下创建 thread binding，RUN 模式跳过。delivery_origin 从 binding 推导。

**理由**:
- RUN 模式是 fire-and-forget，无需持久线程绑定
- SESSION 模式需要长期存活，thread binding 提供投递来源追踪和超期清理
- 简化逻辑，避免 RUN 模式不必要的资源消耗

**影响**:
- `spawn/thread_binding.py` 新增，`resolve_thread_binding_policy()` 按 spawn_mode 分流
- SubagentRunRecord 新增 `thread_binding_info: ThreadBindingInfo | None`

---

## 决策 12: Generation-guarded callback dedup + Kill 仲裁

**日期**: 2026-07-22

**背景**: 子 agent 可被 steer/restart 产生多代，完成回调需防止超代写入。Kill 和 completion 可并发到达，需仲裁。

**决策**: `TerminalGenerationTracker` 守门完成回调；`_arbitrate_kill_vs_completion()` 仲裁：provider OK + 有 result → provider 赢，否则 kill 赢。

**理由**:
- 超代回调会覆盖最新状态，必须守门
- Kill↔completion 竞态下，provider 完成有价值结果时应保留
- arbitrator 结果通过 `suppress_completion_delivery` 控制投递行为

**影响**:
- 新增 `registry/terminal_gen.py`
- `registry/lifecycle.py` 增强 complete_subagent_run + _arbitrate_kill_vs_completion
- SubagentRunRecord 新增 `suppress_completion_delivery: bool`
- Kill reconciliation 保存后需 `set_run` 确保内存一致

---

## 决策 13: 双路径投递——子→子内部注入 vs 子→用户 completion message

**日期**: 2026-07-22

**背景**: 子 agent 完成后向请求方投递结果，但请求方可能是另一个子 agent（内部调度），也可能是用户 session。

**决策**: 根据 `resolve_announce_origin()` 判断请求方类型分流：
- 子→子：内部注入格式（`metadata.internal=True`），简洁，不产生用户可见输出
- 子→用户：完整 completion message 格式，含审阅引导

**理由**:
- 子→子投递是内部调度信号，不需要用户态消息
- 子→用户投递需要完整的任务报告和审阅引导
- 同一投递管道不同格式，避免创建新的通信机制

**影响**:
- `announce/delivery.py` 新增 `_deliver_internal_injection()` + `_deliver_completion_message()`
- `announce/idempotency.py` 支持 suffix 参数（如 `:wake`）用于 descendant-wake 去重

---

## 决策 14: Kill target-state resolution

**日期**: 2026-07-22

**背景**: Kill 操作可能遇到三种状态：可 kill、正在 finalize、已终态。盲目 kill 会导致竞态。

**决策**: `resolve_kill_target_state()` 返回 "killable"/"finalizing"/"terminal"，kill 前先检查，finalizing 时等待 1 秒后重试。

**理由**:
- 避免与正在进行的 provider completion 竞态
- finalizing 状态下等待可让 provider completion 自然完成
- terminal 状态直接返回，幂等安全

**影响**:
- `control/kill.py` 新增 `resolve_kill_target_state()` + `kill_subagent_run_with_cascade()` + `kill_subagent_run_admin()`
- `control/list.py` 新增 `is_subagent_run_visible_to_session()` visibility 过滤

---

## 决策 15: Runtime isolation 替代沙箱

**日期**: 2026-07-22

**背景**: 不移植 ACP 沙箱（决策 3），但需阻止跨 runtime 边界的非法 spawn 和 cwd 越界。

**决策**: `resolve_runtime_isolation()` + `validate_runtime_isolation()` + `validate_cwd_restriction()` 三层防护，逻辑隔离而非容器隔离。

**理由**:
- 同进程内无法实现容器级隔离
- 逻辑隔离足以防止误操作（跨 runtime spawn、cwd 越界）
- 与 `resolve_least_privilege_scopes()` 配合，形成最小权限体系

**影响**:
- 新增 `spawn/runtime_isolation.py` + `spawn/origin_routing.py` + `spawn/gateway_dispatch.py`
- `resolve_least_privilege_scopes()` 按角色分配最小权限 scope，无 gateway 依赖
- `SubagentLaunchAuthorization` 模型封装 scope 授权

---

## 决策 16: Bug 修复 — RunOutcomeStatus 导入路径

**日期**: 2026-07-23

**背景**: `registry/helpers.py` 和 `orphan/recovery.py` 中 `RunOutcomeStatus` 从 `types.lifecycle` 导入，但该枚举定义在 `types.registry` 中。

**决策**: 修正导入路径为 `from ..types.registry import RunOutcomeStatus`。

**理由**:
- `RunOutcomeStatus` 在 `types/registry.py` 中定义
- `types/lifecycle.py` 不包含此枚举
- 运行时触发 `ImportError` 导致孤儿恢复和 finalize 流程失败

---

## 决策 17: Bug 修复 — kill reconciliation 内存一致

**日期**: 2026-07-23

**背景**: `complete_subagent_run()` 调用 `_arbitrate_kill_vs_completion()` 后返回含 `kill_reconciliation.reconciled=True` 的记录，但未 `set_run()` 就调用 `_complete_run()`，后者从内存读取旧记录，导致 reconciled 状态丢失。

**决策**: 在仲裁后、调用 `_complete_run` 前，若 `kill_reconciliation.reconciled` 为 True，先 `set_run()` 刷入内存。

**理由**:
- `_complete_run()` 从 `memory.get(run_id)` 读取，不接收外部传入的 run 对象
- 仲裁结果必须持久化到内存才能被后续操作看到
- 避免重复仲裁和状态不一致

---

## 决策 18: 缺失 API 补全

**日期**: 2026-07-23

**背景**: 测试覆盖了多个 v3 新增功能的 API，但部分 API 在实现中缺失。

**决策**: 补全以下缺失 API：
- `registry/queries.py`: `find_run_by_task_name()` — 按 task_name 查询
- `swarm/fifo.py`: `peek()` — 查看队首元素
- `swarm/collector.py`: `list_swarm_runs_by_group()`, `count_pending_swarm_runs()`, `count_active_swarm_runs()` — 群组级查询
- `hooks/progress.py`: `register_spawned_hook()`, `register_progress_hook()`, `register_ended_hook()`, `clear_all_hooks()` — 钩子注册
- `spawn/origin_routing.py`: `build_origin_fingerprint()` — 来源指纹生成
- `spawn/gateway_dispatch.py`: `SubagentLaunchAuthorization` — 授权模型
- `registry/work_admission.py`: `pending_root_work_count()` — 待处理工作计数
- `control/kill.py`: `kill_subagent_run_admin()` — 管理员级 kill
- `announce/output.py`: `dedupe_latest_child_completion_rows()`, `filter_current_direct_child_completion_rows()` — 结果去重与过滤

**理由**:
- 测试用例反映真实使用场景
- 这些 API 与已有实现一致，属于遗漏而非设计变更

---

## 决策 19: OpenClaw 对齐补全 — 结构化输出验证 + Swarm 配置 + 幂等启动 + kill_all

**日期**: 2026-07-23

**背景**: 对照 OpenClaw `subagent-spawn` 原版功能检查，发现以下功能缺失。

**决策**: 补全以下 OpenClaw 对齐功能：

1. **结构化输出 JSON Schema 验证** (`swarm/collector.py:validate_structured_output()`)
   - OpenClaw 验证子 agent 输出是否符合 output_schema，Python 版此前仅生成 prompt 无实际校验
   - 新增 `validate_structured_output(result_text, output_schema)` — 解析 JSON + 校验 required 字段 + 类型检查
   - 返回 `(is_valid, error_message)` 元组

2. **SwarmGroupConfig.maxTotalPerGroup** (`types/swarm.py`)
   - OpenClaw 有 `maxTotalPerGroup` 独立于 `maxChildrenPerGroup` 的总量上限
   - 默认 0 表示不限制，>0 时在 `reserve_swarm_run` 中强制执行

3. **幂等启动 launch_fingerprint** (`swarm/collector.py`)
   - OpenClaw 通过 `swarmLaunchReplayKey`/`swarmLaunchRequestFingerprint` 防止重复 spawn
   - `reserve_swarm_run(launch_fingerprint=)` 参数，相同 fingerprint 返回已有 run 而非创建新 run
   - 内部维护 `_launch_fingerprints: dict[str, str]` 映射表

4. **kill_all 接口** (`control/kill.py:kill_all_controlled_subagent_runs()`)
   - OpenClaw 有 `killAllControlledSubagentRuns()`，Python 版此前只能逐个 kill
   - 遍历 `list_killable_children()` + 逐个 kill + 完成后 wake 父 Agent

---

## 决策 20: OpenClaw 对齐补全 — 投递压力 target + delivery_target hook + grace period + settle_wake 持久化 + list 增强 + workspace 继承

**日期**: 2026-07-23

**背景**: 继续对齐 OpenClaw 原版功能。

**决策**: 补全以下功能：

1. **投递压力 target-based trimming** (`config.py` + `registry/lifecycle.py`)
   - OpenClaw 在 pressure prune 时不仅裁剪超出 soft_cap 的部分，还主动裁剪到 target 水平（默认 10）
   - 新增 `delivery_suspend_target: int = 10`，`pressure_prune_suspended_deliveries` 使用 `max(target, total - soft_cap)` 作为目标保留数

2. **delivery_target hook** (`hooks/progress.py:fire_delivery_target_hook()`)
   - OpenClaw 在投递目标解析时触发 `subagent_delivery_target` hook，允许插件拦截或重定向
   - `fire_delivery_target_hook(run, target_session_key)` 返回 `str | None`（重定向目标或 None）

3. **生命周期 error/timeout grace period** (`config.py` + `spawn/core.py`)
   - OpenClaw 在子 agent 错误/超时后等待 15 秒再 finalize，允许迟到的 completion 到达
   - 新增 `lifecycle_grace_period_seconds: float = 15.0`
   - `_execute_subagent` 的 finally 块中，ERROR/TIMEOUT 时先 sleep(grace) 再检查是否已被人完成

4. **settle_wake 持久化** (`registry/settle_wake.py` + `registry/store_sqlite.py`)
   - OpenClaw 的 settle-wake 状态是 durable outbox，Python 版此前仅内存态，重启丢失
   - 新增 `get_pending_state()`/`restore_pending_state()`/`_persist_state()`/`load_persisted_state()`
   - SQLite 新增 `settle_wake_state` 表，同步读写

5. **subagent_list 增强** (`control/list.py`)
   - OpenClaw 的 subagent 列表显示 model/runtime/token/pending_descendants
   - 新增 `_resolve_runtime_display(run)` 计算 "30s"/"2.5m"/"1.2h" 格式
   - active 条目增加 `model`/`runtime`/`pending_descendants` 字段
   - recent 条目增加 `model`/`runtime` 字段

6. **Workspace 继承** (`spawn/runtime_isolation.py:resolve_spawned_workspace_inheritance()`)
   - OpenClaw 解析跨 agent 的 workspace 路径，Python 版此前不继承
   - `resolve_spawned_workspace_inheritance(requester_session_key, target_agent_id, requester_cwd)`
   - `spawn/core.py` 中当 `cwd is None` 时自动调用，推断子 agent workspace

## 决策 21: 深度对齐 — System Prompt / Initial Message / Announce / Swarm / Orphan / Sweeper / Kill / Steer / Attachment / Ownership / Depth

**日期**: 2026-07-23

**背景**: 对照 OpenClaw 原版的交互深度对齐，涉及子 agent 的 prompt 结构、投递行为、swarm 调度、孤儿恢复、sweeper 策略、kill 级联、steer 行为、附件校验、所有权解析和深度回退等。

**决策**: 逐项对齐以下深度行为：

1. **System Prompt 6 段结构** (`spawn/system_prompt.py`)
   - Your Role / Rules / Output Format / What You DON'T Do / Sub-Agent Spawning / Session Context
   - 新增 anti-polling rule（禁止主动轮询状态）和 truncation hint（输出截断提示）
   - LEAF 角色注入 structured output template（从 output_schema 生成）

2. **Initial User Message 结构化信封** (`spawn/initial_message.py`)
   - 格式：`[Subagent Context]\n{ctx}\n\n[Subagent Task]\n{task}\n\n[Subagent Additional Context]\n{extra}`
   - 替代原有的简单 task 文本

3. **Announce 投递增强** (`announce/output.py` + `announce/delivery.py`)
   - 投递前 descendant check：仅当 requester 有未完成后代时才投递 wake
   - 瞬态/永久错误分类：transient error 用短延迟重试 [5s/10s/20s]，permanent error 不重试
   - compaction error（子 agent 请求方投递失败）用 `_COMPACTION_RETRY_DELAYS_MS` [1s/2s/4s/8s]

4. **Swarm pumpLane slot 激活** (`swarm/collector.py`)
   - `_pump_lane()`: reserve 后自动检查 slot 并激活已预留的 swarm run
   - `_on_swarm_run_started` callback: swarm run 启动时触发 lifecycle 通知
   - `onStartFailure` auto-fail + next queued: 启动失败时自动标记 FAILED 并激活下一个

5. **Orphan wedged 恢复** (`orphan/recovery.py`)
   - wedged run 标记 `ended_reason="wedged_recovery"`
   - sweeper 跳过 wedged run 避免重复处理

6. **Sweeper 分层过期** (`registry/sweeper.py` + `registry/helpers.py`)
   - 按 requester type 分层过期：cron=2h, subagent=6h, interactive=24h
   - 替代原有的单一过期阈值

7. **Kill cascade per-child 所有权验证** (`control/kill.py`)
   - cascade 时逐子验证 controller 所有权，非所属子 run 不 kill

8. **Steer frozen result fallback + new_task 持久化** (`control/steer.py`)
   - frozen result（已完成 run 的结果）注入 steer message 作为上下文
   - `new_task` 参数持久化到 run record

9. **Attachment 安全增强** (`spawn/attachments.py`)
   - Unicode C0+DEL 控制字符检测（拒绝含控制字符的文件名）
   - 重复文件名检测（同一 spawn 内不允许重复附件名）
   - 严格 base64 校验（解码失败直接拒绝）

10. **Ownership canonical alias 解析** (`session/reconciliation.py`)
    - main session key 的 canonical alias 解析，确保跨 session 查找一致

11. **Depth run record primary 回退** (`registry/read.py` + `registry/queries.py`)
    - 查询时 run record 为主、session key 为 fallback 的策略

---

## 决策 22: 接线验证修复 — model_plan → _build_child_agent / thinking_resolved / cwd / scopes / completion_owner / fire_delivery_target_hook / launch_fingerprint / output_schema / compaction retry

**日期**: 2026-07-23

**背景**: 全量功能实现后，端到端接线检查发现多个参数/数据未正确从上游流到下游，属于实现遗漏而非设计变更。

**决策**: 修复以下接线问题：

1. **model_plan → _build_child_agent** (`spawn/core.py`)
   - `plan.py` 解析的 `resolved_model` 现在通过 `model_override` 参数传递到 `_build_child_agent()`
   - 子 agent 使用 plan 指定的模型而非固定模型

2. **thinking_resolved 存储** (`spawn/core.py` + `registry/run_manager.py`)
   - `thinking_resolved` 存储到 run record
   - 传递到 `ainvoke` config 的 `tags` 中供后续追踪

3. **cwd 传递** (`spawn/core.py`)
   - `cwd` 参数传递到 `ainvoke` config 供子 agent 使用

4. **scopes → deny 映射** (`spawn/core.py` + `spawn/gateway_dispatch.py`)
   - `scopes` 用于拒绝不在 scope 内的工具（如无 `subagent:spawn` scope → deny `sessions_spawn`）

5. **completion_owner / spawned_by / spawned_cwd 写入** (`registry/run_manager.py` + `spawn/core.py`)
   - `completion_owner_session_key`, `spawned_by`, `spawned_cwd` 在 `register_run` / `spawn` 时写入 run record

6. **fire_delivery_target_hook 调用** (`announce/delivery.py`)
   - 投递目标解析时调用 `fire_delivery_target_hook()`，允许插件拦截或重定向

7. **launch_fingerprint 暴露** (`spawn/core.py`)
   - `launch_fingerprint` 参数暴露在 `spawn_subagent_direct()` 参数中，传递到 swarm reserve

8. **output_schema 持久化** (`registry/run_manager.py`)
   - `output_schema` 存储到 run record，供 announce 和 structured output 验证使用

9. **_COMPACTION_RETRY_DELAYS_MS 使用** (`announce/delivery.py`)
   - 子 agent 请求方投递错误使用 compaction 重试调度 [1s/2s/4s/8s]

10. **finalize_interrupted_run_with_retry 新鲜 run 支持** (`registry/lifecycle.py`)
    - 原逻辑仅处理 stale run，现也支持 fresh run 的 interrupted finalize

11. **死代码移除**
    - `descendants_active` 函数（未使用）
    - `activate_swarm_run` 从 tools 直接 import（应通过 collector）
    - `build_compact_announce_stats_line` import（未使用）

### 合理跳过的 OpenClaw 功能

以下功能因平台/基础设施不适用而跳过：

| 功能 | 跳过原因 |
|------|---------|
| Lightweight context | Python 无此分层概念，isolated/fork 已覆盖 |
| Fast mode inheritance | Python LLM 调用无 fast mode 概念 |
| Thread binding placement | 无 Discord/Slack/Telegram，仅 stub |
| Context engine integration | 项目无 context engine 基础设施 |
| ACP spawn variant | 按决策 3 不移植 |
| Browser/MCP cleanup | 项目无 browser/MCP 基础设施 |
| Session write lock | Python 单进程无需跨 session 写锁 |
| Cron session handling | 项目无 cron 调度基础 |
| Completion handoff | MessageBus 路由已覆盖 |
| Force synthetic client | Python 单进程无需 in-process 优化 |
| Legacy field migration | 无旧格式数据需迁移 |
