# 架构设计

## 整体架构

```
Parent Agent (LangGraph CompiledStateGraph)
  │
  ├─ 调用 sessions_spawn(task, agentId, mode, ...)
  │    │
  │    ├─ spawn/core.py: 验证 → 构建子 agent → 注册 run → 分发执行
  │    ├─ registry/: 注册 run 记录，管理生命周期
  │    └─ 返回 { status: "accepted", child_session_key, run_id, note }
  │
  ├─ (可选) 调用 sessions_yield() 暂停等待子任务完成
  │
  ├─ (可选) 调用 sessions_send() 与子 agent 双向通信
  │
  ├─ (可选) 调用 sessions_kill() 取消运行中的子 agent
  │
  ├─ (可选) 调用 sessions_steer() 向运行中的子 agent 注入新指令
  │
  └─ 子 Agent 执行...
       │
       ├─ announce/core.py: 捕获输出 → 构建通知 → 投递到父 session
       │    ├─ announce/output.py: 等待 outcome / 提取 summary / 统计
       │    ├─ announce/delivery.py: 通过 MessageBus 投递 + 重试 + 挂起管理
       │    ├─ announce/capture.py: 带重试的输出读取
       │    └─ announce/dispatch.py: steer vs direct 策略
       │
       └─ registry/lifecycle.py: 更新 run 状态 → 清理 → 触发 hook
```

## 目录结构与模块职责

```
future_subagent/
├── types/                     数据模型与枚举定义
│   ├── spawn.py               SpawnMode, ContextMode 枚举
│   ├── registry.py            SubagentRunRecord 及子状态模型（含 completion_owner_session_key/output_schema/scopes/spawned_by/spawned_cwd/inherited_tool_policy_version）
│   ├── swarm.py               SwarmMode, SwarmRunState, SwarmGroupConfig
│   ├── lifecycle.py           生命周期事件枚举 (LifecycleEndedReason, LifecycleEndedOutcome)
│   ├── delivery.py            投递上下文
│   └── capability.py          角色枚举 (main/orchestrator/leaf)
│
├── registry/                  Run 注册表（核心状态机）
│   ├── memory.py              内存存储 dict[str, SubagentRunRecord]
│   ├── store_sqlite.py        SQLite 持久化
│   ├── queries.py             纯查询函数（list/count/find/index/find_by_task_name）
│   ├── helpers.py             工具函数（截断、重试延迟、孤儿判定、陈旧检测、附件清理、分层过期）
│   ├── completion.py          结果判定、hook 触发
│   ├── cleanup.py             清理决策
│   ├── delivery_state.py      Delivery 状态机访问器
│   ├── run_manager.py         registerRun, markPaused, depth 管理, save/clear_kill_reconciliation
│   ├── generation.py          代次管理（latest run by child_session_key）
│   ├── terminal_gen.py        TerminalGenerationTracker 回调守门
│   ├── settle_wake.py         RequesterSettleWakeBatch 批量状态机
│   ├── work_admission.py      Gateway-independent root work admission + pending count
│   ├── lifecycle.py           生命周期控制器 (completeRun/resume/announce/pressurePrune/gracePeriod)
│   ├── state.py               persist/restore 桥接（含 settle_wake 持久化恢复）
│   ├── read.py                外部只读 API（find_run_by_task_name + run record primary 查询）
│   ├── task_refs.py           asyncio.Task 引用管理（register/get/remove/cancel）
│   ├── yield_events.py        asyncio.Event 管理（yield wake/descendant settle）
│   ├── sweeper.py             后台 60s 扫描器（分层过期: cron=2h, subagent=6h, interactive=24h）
│   └── reconciliation.py      Session 对账
│
├── swarm/                     Swarm/Collect 调度
│   ├── collector.py           reserve/activate/complete + list/count + outputSchema + validate_structured_output（嵌套/数组/patternProps/additionalProps）+ 幂等启动（launch_fingerprint）+ pumpLane slot 激活
│   └── fifo.py                SwarmFifoQueue FIFO 队列（含 peek）
│
├── spawn/                     Spawn 管道
│   ├── core.py                spawnSubagentDirect() 主入口 + SpawnResult
│   ├── plan.py                thinking 解析, timeout 计算, model+thinking plan
│   ├── ownership.py           spawn 所有权解析 (controller vs completion requester)
│   ├── target_policy.py       allowAgents 校验
│   ├── depth.py               深度计算与限制
│   ├── attachments.py         附件物化到子 workspace（含 Unicode C0+DEL 控制字符检测、重复名称检测、严格 base64 校验）
│   ├── task_name.py           taskName 规范化
│   ├── system_prompt.py       子 agent system prompt 生成（6 段结构：Your Role/Rules/Output Format/What You DON'T Do/Sub-Agent Spawning/Session Context）
│   ├── initial_message.py     子 agent 首条 user message（结构化信封: [Subagent Context]/[Subagent Task]/[Subagent Additional Context]）
│   ├── inherited_tool_policy.py 工具白/黑名单继承
│   ├── context.py             isolated/fork 上下文构建
│   ├── thread_binding.py      Thread Binding 生命周期管理
│   ├── runtime_isolation.py   运行时隔离与安全边界 + workspace 继承
│   ├── origin_routing.py      请求方来源路由解析 + fingerprint 生成（build_origin_fingerprint 暴露为外部 API）
│   ├── gateway_dispatch.py    最小权限 scope 解析 + SubagentLaunchAuthorization + scope→deny 映射
│   ├── accepted_note.py       SpawnResult.note 内容生成
│   └── thinking.py            thinking 级别覆盖解析
│
├── announce/                  完成通知管道
│   ├── core.py                runAnnounceFlow() 主协调
│   ├── output.py              输出捕获、等待 outcome、统计、去重（dedupe_latest_child_completion_rows）、过滤（filter_current_direct_child_completion_rows）、descendant 检查
│   ├── capture.py             带重试的输出读取
│   ├── delivery.py            投递执行（双路径 + 重试/挂起/幂等/镜像 + delivery_target hook 调用 + 瞬态/永久错误分类 + 分级重试调度）
│   ├── dispatch.py            投递策略（steer vs direct）+ AnnounceDeliveryResult
│   ├── origin.py              来源解析（子→子 vs 子→用户）
│   └── idempotency.py         幂等 key 生成（含 suffix）
│
├── control/                   控制与列表
│   ├── controller.py          listControlledRuns, resolveController, can_control_run
│   ├── kill.py                Kill（含 target-state resolution + cascade + admin + kill_all + scope 校验 + per-child controller 所有权验证）
│   ├── steer.py               Steer/Restart（含 abort-settle + suppress_announce + frozen result fallback + new_task 持久化）
│   ├── send.py                sessions_send 完整实现
│   └── list.py                buildSubagentList()（含 visibility 过滤 + model/runtime/pending_descendants）+ build_active_subagents_section()（外部 API）
│
├── capabilities/              角色/能力
│   └── core.py                resolveSubagentCapabilities(), role 分配
│
├── orphan/                    孤儿恢复
│   └── recovery.py            scheduleOrphanRecovery()（含 retry + reclassify + wedged 检测 + wedged_recovery ended_reason + finalize）
│
├── session/                   Session 辅助
│   ├── reconciliation.py      run ↔ session 状态对账（含 canonical alias 解析 for main session keys）
│   ├── metrics.py             运行时长、状态判定
│   └── cleanup.py             session 删除
│
├── tools/                     LLM 工具接口
│   ├── sessions_spawn.py      sessions_spawn 工具
│   ├── sessions_yield.py      sessions_yield 工具
│   ├── sessions_send.py       sessions_send 工具（含 A2A flow）
│   ├── sessions_kill.py       sessions_kill 工具
│   ├── sessions_steer.py      sessions_steer 工具
│   ├── agents_list.py         agents_list 工具
│   └── subagents_list.py      subagents 工具
│
├── hooks/                     Channel hooks
│   ├── base.py                Hook 协议定义（SubagentStartEvent / SubagentStopEvent）
│   └── progress.py            生命周期进度钩子（spawned / progress / ended / delivery_target + register/clear + fire_delivery_target_hook）
│
├── followup/                  Cron followup
│   └── core.py                定时检查超时/挂起
│
└── config.py                  SubagentConfig (pydantic model)
```

## 模块依赖图

```
types/ ← (无依赖，纯数据定义)
  ↑
config.py
  ↑
registry/memory.py ← registry/delivery_state.py ← registry/queries.py
  ↑                                    ↑
registry/store_sqlite.py         registry/helpers.py
  ↑                                    ↑
registry/state.py ← registry/run_manager.py ← registry/completion.py
  ↑                                    ↑
registry/generation.py ← registry/terminal_gen.py ← registry/lifecycle.py
  ↑                    ↑                              ↑
registry/settle_wake.py  registry/work_admission.py    registry/sweeper.py
                                                        ↑
                                                   registry/read.py

swarm/fifo.py ← swarm/collector.py ← types/swarm.py

capabilities/core.py ← types/
  ↑
spawn/depth.py ← spawn/target_policy.py ← spawn/core.py
  ↑                    ↑                       ↑
spawn/plan.py    spawn/ownership.py      spawn/system_prompt.py
  ↑                    ↑                       ↑
spawn/inherited_tool_policy.py          spawn/attachments.py
  ↑                                            ↑
spawn/context.py ← spawn/initial_message.py ← spawn/task_name.py
  ↑
spawn/thread_binding.py ← spawn/runtime_isolation.py
  ↑
spawn/origin_routing.py ← spawn/gateway_dispatch.py

announce/idempotency.py ← announce/capture.py ← announce/output.py
  ↑                                                    ↑
announce/dispatch.py ← announce/origin.py ← announce/delivery.py
  ↑                                                    ↑
announce/core.py                              announce/core.py

control/controller.py ← control/kill.py ← control/steer.py
  ↑                      ↑
control/send.py    control/list.py

orphan/recovery.py ← announce/core.py + registry/lifecycle.py

hooks/progress.py ← types/registry.py

tools/* ← spawn/core.py + registry/* + announce/* + control/*
```

## 三个核心状态机

### 1. ExecutionState

```
running → interrupted → terminal
   ↑                        ↑
   └────────────────────────┘ (restart)
```

### 2. CompletionDeliveryState

```
not_required → pending → in_progress → delivered
                                    → failed → (retry) → pending
                                    → suspended → discarded
```

### 3. CleanupState (在 SubagentRunRecord 内)

```
registered → cleanup_handled → cleanup_completed_at
```

## 与现有系统的关系

```
现有系统 (agent/tools/subagent/):
  SubagentManager (singleton) → Commander → Worker
  工具名: "subagent"
  投递: MessageBus

新系统 (future_subagent/):
  SubagentRegistry → Spawn → Announce
  工具名: "sessions_spawn", "sessions_yield", "sessions_send",
          "sessions_kill", "sessions_steer", "agents_list", "subagents_list"
  投递: MessageBus（复用）

共存方式:
  - 两套工具同时注册到 _MAIN_TOOLS_BUILDERS
  - 现有 "subagent" 工具保持不变
  - 新工具使用不同的命名空间
```
