# Future Subagent — Python 子 Agent 系统

中文 | **[English](./README.md)**

> Python 实现的多层子 Agent 系统，与现有 `agent/tools/subagent/`（Commander/Worker 模式）共存。全部 7 个实施阶段 + 健壮性补全 v3 增强 + Bug 修复 + OpenClaw 对齐 + 深度对齐 + 接线修复已完成。203 测试通过。

## 快速导航

| 文档 | 用途 |
|------|------|
| [AGENTS.md](./AGENTS.md) | **入口** — 项目规范、当前进度 |
| [architecture.md](./docs/architecture.md) | 整体架构、目录结构、模块依赖图 |
| [decisions.md](./docs/decisions.md) | 关键技术决策记录（22 项决策） |
| [integration.md](./docs/integration.md) | 与现有系统集成方案 |

---

## 执行原理

### 一、系统总览

Subagent 系统的核心目标是让主 Agent 能够将复杂任务分解为并行子任务，分发给独立的子 Agent 执行，并在子 Agent 完成后可靠地将结果投递回父 Agent。整个系统由三条核心管道驱动：

```
┌──────────────────────────────────────────────────────────────────┐
│  Parent Agent (LangGraph CompiledStateGraph)                     │
│    │                                                             │
│    ├─ 1. sessions_spawn ──► Spawn 管道 ──► 子 Agent 异步执行      │
│    │                                                             │
│    ├─ 2. sessions_yield ──► 暂停当前 turn，等待子任务完成          │
│    │                                                             │
│    ├─ 3. sessions_send  ──► A2A 双向通信 (via MessageBus)        │
│    │                                                             │
│    └─ 4. 子 Agent 完成 ──► Announce 管道 ──► MessageBus 投递结果  │
│                         ──► Registry 生命周期更新                 │
└──────────────────────────────────────────────────────────────────┘
```

### 二、Spawn 管道 — 子 Agent 的创建与分发

`spawn_subagent_direct()` 是整个系统的入口函数。当 LLM 调用 `sessions_spawn` 工具时，执行以下流程：

```
spawn_subagent_direct(task, requester_session_key, agent_id, mode, ...)
  │
  ├── 1. 验证阶段
  │     ├── 验证 task 非空
  │     ├── 规范化 task_name（非字母数字替换为 _，截断 64 字符）
  │     ├── 校验 target_policy（agent_id 是否在 allow_agents 白名单中）
  │     ├── 计算深度：parent_depth + 1，校验 ≤ max_spawn_depth (默认 3)
  │     ├── 校验并发限制：活跃子任务数 < max_children_per_agent (默认 5)
  │     └── 校验运行时隔离（阻止跨 runtime 边界 spawn）
  │
  ├── 2. 角色与能力解析
  │     └── resolve_subagent_capabilities(depth, max_depth)
  │           ├── depth == 0       → MAIN,       control_scope=CHILDREN
  │           ├── 0 < depth < max  → ORCHESTRATOR, control_scope=CHILDREN
  │           └── depth >= max     → LEAF,        control_scope=NONE
  │
  ├── 3. 上下文准备
  │     ├── thinking 级别覆盖解析 (plan.py)
  │     ├── 附件物化到磁盘 (attachments.py)
  │     │     安全校验：路径遍历防护、大小限制、数量限制
  │     ├── 工具策略：DEFAULT_SUBAGENT_BLOCKED_TOOLS = [sessions_spawn,
  │     │   sessions_yield, skill_manage, memory]
  │     │   ORCHESTRATOR 角色自动解锁 sessions_spawn 和 sessions_yield
  │     ├── 上下文模式：ISOLATED（空上下文）或 FORK（通过
  │     │       agent.aget_state() 复制父对话历史 — 决策 9）
  │     ├── Thread Binding：SESSION 模式 → bind_thread_for_subagent_spawn()
  │     │       创建 channel thread + delivery_origin（决策 11）
  │     ├── 运行时隔离：resolve_runtime_isolation() + cwd 校验（决策 15）
  │     ├── 来源路由：resolve_requester_origin_for_child()
  │     └── 权限解析：resolve_least_privilege_scopes() 按角色分配权限
  │
  ├── 4. 注册 Run
  │     ├── 生成 child_session_key = "agent:{agent_id}:subagent:{uuid}"
  │     ├── 创建 SubagentRunRecord（UUID, 执行状态=RUNNING, 投递状态=PENDING）
  │     ├── 存入内存 dict + SQLite
  │     └── 注册终端代次（TerminalGenerationTracker）
  │
  ├── 5. 构建 Prompt
  │     ├── build_subagent_system_prompt(role, task, ...)
  │     │   ├── 6 段结构：Your Role / Rules / Output Format /
  │     │   │     What You DON'T Do / Sub-Agent Spawning / Session Context
  │     │   ├── Anti-polling 规则（禁止主动轮询状态）
  │     │   ├── 输出截断提示
  │     │   ├── LEAF：从 output_schema 生成结构化输出模板
  │     │   └── ORCHESTRATOR: "You MAY spawn further subagents via sessions_spawn."
  │     ├── 追加附件位置提示到系统提示词
  │     ├── 追加结构化输出提示词（swarm 模式的 output_schema）
  │     └── build_subagent_initial_user_message(task, context)
  │           └── 结构化信封：[Subagent Context] / [Subagent Task] / [Subagent Additional Context]
  │
  ├── 6. 异步分发（Fire-and-Forget）
  │     └── asyncio.create_task(_execute_subagent(...))
  │
  └── 7. 立即返回 SpawnResult
        └── { status: "accepted", child_session_key, run_id }
```

#### 子 Agent 的实际执行

`_execute_subagent()` 是后台 asyncio Task，负责子 Agent 的完整生命周期：

```
_execute_subagent(run, system_prompt, user_message, forked_messages, ...)
  │
  ├── 1. 构建子 Agent
  │     ├── 调用 build_main_tools() 获取全部工具
  │     ├── 按 tool_allow/tool_deny 过滤（黑名单优先）
  │     ├── 构建 LLM：ORCHESTRATOR → build_main_llm()，LEAF → build_auxiliary_llm()
  │     ├── 创建独立 SQLite checkpointer
  │     └── create_agent() 配置五组 middleware:
  │           ├── Summarization(trigger=[fraction:0.5, messages:40, tokens:30000])
  │           ├── IterationBudget(60)     — 最大迭代次数
  │           ├── ToolGuardrails()        — 工具安全护栏
  │           ├── ToolCallNormalize()     — 工具调用归一化
  │           └── HeartbeatStaleness()    — 心跳监控
  │
  ├── 2. 执行
  │     ├── 组装消息列表：forked_messages + HumanMessage(user_message)
  │     └── await asyncio.wait_for(child_agent.ainvoke(...), timeout)
  │
  ├── 3. 结果提取
  │     └── 从 ainvoke 返回的最后一条消息提取 result_text
  │
  └── 4. Finally（无论成功失败均执行）
        ├── TimeoutError → outcome = TIMEOUT
        ├── Exception   → outcome = ERROR
        ├── complete_run(run_id, outcome, result_text)  — 更新 Registry
        │     └── result_text 上限 24000 字符
        └── run_subagent_announce_flow(updated_run)      — 触发 Announce
```

### 三、Registry — 运行状态注册表

Registry 是整个系统的状态中枢，维护所有子 Agent 运行记录的生命周期。

#### 存储架构

```
┌─────────────────────────────────────────────────┐
│  内存存储 (registry/memory.py)                   │
│  threading.Lock 保护的 dict[str, SubagentRunRecord]  │
│  ↓ 定期快照                                      │
│  SQLite (registry/store_sqlite.py)              │
│  future_subagent/data/subagent_registry.db      │
│  表: subagent_runs(run_id PK, data JSON)        │
└─────────────────────────────────────────────────┘
```

- 内存是第一级存储，所有读写操作直接操作内存 dict
- SQLite 是持久化备份，通过 Sweeper 调用 `periodic_persist(interval=30s)` 定期快照
- 启动时通过 `init_registry()` 从 SQLite 恢复已有记录
- 单条 upsert/delete 实时同步到 SQLite

#### SubagentRunRecord 核心字段

| 分类 | 字段 | 说明 |
|------|------|------|
| **身份** | `run_id` | UUID，唯一标识 |
| | `task_run_id` | 跨 steer/restart 的稳定 ID |
| | `child_session_key` | `"agent:{agentId}:subagent:{uuid}"` |
| | `requester_session_key` | 父 session key |
| **Spawn 参数** | `spawn_mode` | RUN（一次性）/ SESSION（持久） |
| | `context_mode` | ISOLATED / FORK |
| | `depth` | 嵌套深度 |
| | `role` | MAIN / ORCHESTRATOR / LEAF |
| **所有权** | `completion_owner_session_key` | 负责完成投递的 session key |
| | `spawned_by` | 发起 spawn 的身份 |
| | `spawned_cwd` | spawn 时的工作目录 |
| **权限** | `scopes` | 授予的权限 scope |
| | `inherited_tool_policy_version` | 继承工具策略版本 |
| **Schema** | `output_schema` | 结构化输出的 JSON Schema 验证 |
| **执行状态** | `execution.status` | RUNNING → INTERRUPTED → TERMINAL |
| | `execution.outcome` | OK / ERROR / TIMEOUT / UNKNOWN |
| **投递状态** | `delivery.status` | PENDING → IN_PROGRESS → DELIVERED |
| | `delivery.attempt_count` | 投递重试次数 |
| **附件** | `attachments_dir` | 附件目录绝对路径 |
| | `attachments_root_dir` | 附件根目录（用于安全清理校验） |

### 四、三个核心状态机

#### 1. ExecutionState — 执行状态机

```
    RUNNING ──────────────────► INTERRUPTED
      │                            │
      │ (正常完成/出错/超时)         │ (resume)
      ▼                            │
    TERMINAL ◄─────────────────────┘
      ▲
      │ (restart)
      └────────────────────────────
```

- `RUNNING`：子 Agent 正在执行
- `INTERRUPTED`：被 yield/steer 暂停
- `TERMINAL`：终态（完成/出错/超时），不可逆转

#### 2. CompletionDeliveryState — 投递状态机

```
    not_required ──(RUN 模式跳过)──► delivered

    pending ──► in_progress ──► delivered
                    │
                    ├──(失败)──► failed ──(重试)──► pending
                    │                               │
                    │            (重试耗尽 + soft cap) │
                    │                               ▼
                    └──(soft cap 超限)──► suspended ──► discarded
```

- `not_required`：SESSION 模式无需投递
- `pending → in_progress → delivered`：正常投递路径
- `failed → pending`：指数退避重试（1s, 2s, 4s，最多 3 次）
- `suspended → discarded`：挂起超限后丢弃

#### 3. CleanupState — 清理状态机

```
    registered ──► cleanup_handled ──► cleanup_completed_at
```

- 由 `resolve_deferred_cleanup_decision()` 决定是否删除 session
- cleanup="delete" 且投递已完成/丢弃/无需投递 → 删除
- 投递挂起/失败 → 保留
- 附件清理使用 `safe_remove_attachments_dir()`，含 symlink 遍历攻击防护

### 五、Announce 管道 — 结果通知与投递

子 Agent 完成后，Announce 管道负责将结果可靠地投递回父 Agent。

```
子 Agent 执行完成
  │
  └──► run_subagent_announce_flow(run)
         │
         ├── 前置守卫
         │     ├── execution.status != TERMINAL → 跳过
         │     ├── completion.required == False → 跳过
         │     └── delivery.status == DELIVERED → 跳过（幂等）
         │
          └──► deliver_subagent_announcement(run)
                │
                ├── 1. 进程内幂等检查
                │     └── _is_already_delivered(run) → 检查内存 set
                │         key = "subagent_announce:{run_id}:gen:{generation}"
                │         set 上限 10K，超限驱逐最早的 5K 条
                │
                ├── 2. 硬上限检查
                │     └── 待投递后代数 ≥ hard_cap(50) → 直接 SUSPENDED
                │
                ├── 3. 后代检查
                │     └── 仅当请求方有未完成后代时才投递 wake
                │
                ├── 4. 标记 IN_PROGRESS
                │
                ├── 5. 重试循环（最多 3 次）
                │     ├── _do_deliver(ctx)
                │     │     ├── 构建 InboundMessage:
                │     │     │     channel = "system"
                │     │     │     sender_id = "subagent"
                │     │     │     chat_id = "direct"
                │     │     │     session_id = requester_session_key
                │     │     │     metadata.injected_event = "subagent_result"
                │     │     │     content = 格式化结果（截断 4K）
                │     │     └── MessageBus.publish_inbound(msg)
                │     │     fire_delivery_target_hook() → 允许重定向
                │     │
                │     ├── 成功 → mark DELIVERED + 记录幂等 key → 返回
                │     ├── 瞬态错误 → sleep [5s/10s/20s] → 重试
                │     ├── Compaction 错误 → sleep [1s/2s/4s/8s] → 重试
                │     └── 永久错误 → 不重试
               │
                ├── 6. 重试耗尽
                │     ├── mark FAILED
                │     └── 待投递数 ≥ soft_cap(25) → mark SUSPENDED
                │
                └── 7. 清理
                     └── cleanup="delete" → safe_remove_attachments_dir()
```

#### 投递消息格式

```
**Subagent Result** [{label}]
Status: completed successfully / failed: {error} / timed out
Task: {task description}
Result:
{result_text, truncated at 4000 chars}
```

### 5.1 Swarm/Collect 模式（v3）

Swarm 系统支持并发批量执行子任务，通过 FIFO 调度和并发控制管理：

```
configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=3))
  │
  ├── reserve_swarm_run(group_id, task, requester)
  │     └── 入队 FIFO + 设置状态=RESERVED
  │
  ├── activate_swarm_run(run_id)
  │     └── 出队 + 设置状态=ACTIVE（受 max_concurrent 控制）
  │
  ├── complete_swarm_run(run_id, outcome)
  │     └── 设置状态=COMPLETED/FAILED + 自动激活下一个预留
  │
  └── build_structured_output_prompt(output_schema)
        └── 从 schema 生成 JSON 结构化输出提示词

validate_structured_output(result_text, output_schema)
  │
  ├── 将 result_text 解析为 JSON
  ├── 检查 required 字段是否存在
  ├── 按 schema 验证字段类型
  └── 返回 (is_valid, error_message) 元组

SwarmGroupConfig 字段: group_id, max_children_per_group (5), max_total_per_group (0=不限), max_concurrent (3)

reserve_swarm_run(group_id, task, requester, launch_fingerprint=None)
  │
  ├── launch_fingerprint 非空 → 检查 _launch_fingerprints 幂等命中
  └── 新 run → 入队 FIFO + 设置状态=RESERVED

_pump_lane(group_id)
  │
  ├── 检查可用 slot（受 max_concurrent 控制）
  ├── 自动激活已预留的 swarm run
  └── 激活时触发 _on_swarm_run_started 回调

onStartFailure 处理:
  │
  ├── 自动标记 FAILED
  └── 自动激活下一个排队中的预留 run
```

### 5.2 投递双路径路由（v3）

Announce 投递根据请求方类型分流：

```
deliver_subagent_announcement(run)
  │
  ├── 请求方是子 Agent → _deliver_internal_injection()
  │     ├── metadata.internal = True
  │     ├── 内容: "[Subagent Internal] {label}: {status}"
  │     └── 不产生用户可见输出
  │
  └── 请求方是用户 session → _deliver_completion_message()
        ├── 完整 markdown 格式 + 审阅引导
        ├── 内容: "**[Subagent Task]** [{label}]..."
        └── "请审阅以上子 Agent 执行结果，如需进一步操作请指示。"
```

### 5.3 代次守卫生命周期与 Kill 仲裁（v3）

```
complete_subagent_run(run_id, outcome, expected_generation)
  │
  ├── TerminalGenerationTracker.is_callback_current()
  │     └── 拒绝超代回调
  │
  ├── _arbitrate_kill_vs_completion(run, outcome)
  │     ├── 无 kill_reconciliation → 直接通过
  │     ├── Kill + Provider OK 且有结果 → Provider 胜出
  │     └── Kill + 其他结果 → Kill 胜出
  │
  ├── _should_suspend_pending_final_delivery()
  │     └── cleanup="keep" + complete + ok + expects + PENDING → 挂起
  │
  └── _start_announce_cleanup_flow()
        ├── SettleWakeBatch: IDLE → COMPLETING → SETTLED → DONE
        └── 延迟清理带代次守卫
```

### 5.4 Kill 目标状态解析与可见性（v3）

```
resolve_kill_target_state(run) → "killable" | "finalizing" | "terminal"

kill_subagent_run_with_cascade(run_id, cascade=True)
  │
  ├── 解析目标状态
  │     ├── "terminal" → 返回（已完成）
  │     ├── "finalizing" → 等待 1s，重新检查
  │     └── "killable" → 继续 kill
  │
  ├── 保存 kill reconciliation 快照
  ├── 取消 task + 清除 session 队列
  ├── cascade 模式：递归 kill 所有子 Agent
  └── 所有子 Agent settled 后 wake 父 Agent

is_subagent_run_visible_to_session(run, session_key)
  ├── controller_session_key 匹配 → 可见
  ├── requester_session_key 匹配 → 可见
  └── 其他 → 不可见
```

### 六、深度与角色系统 — 层级控制

Subagent 系统支持多层嵌套，通过深度和角色控制递归生成能力：

```
depth 0:  MAIN Agent
           ├── 可以 spawn 子 agent
           └── control_scope = CHILDREN

depth 1:  ORCHESTRATOR (if max_depth > 1)
           ├── 可以继续 spawn 子 agent
           └── control_scope = CHILDREN

depth 2:  ORCHESTRATOR (if max_depth > 2)
           ├── 可以继续 spawn 子 agent
           └── control_scope = CHILDREN

depth N:  LEAF (depth == max_spawn_depth)
           ├── 不能 spawn 子 agent
           └── control_scope = NONE
```

默认 `max_spawn_depth = 3`，形成三层树：MAIN → ORCHESTRATOR → LEAF

**深度计算**：从 `requester_session_key` 中提取父深度，子深度 = 父深度 + 1。Session key 格式 `"agent:{id}:subagent:{uuid}"` 中 `:subagent:` 的出现次数即为深度。

**工具策略联动**：
- LEAF 角色被 `DEFAULT_SUBAGENT_BLOCKED_TOOLS`（`sessions_spawn`、`sessions_yield`、`skill_manage`、`memory`）完全限制，无法调用 `sessions_spawn`
- ORCHESTRATOR 角色自动解锁 `sessions_spawn` 和 `sessions_yield`，可递归 spawn
- 这确保了嵌套深度的硬约束不会被绕过

### 七、附件系统

Spawn 管道支持向子 Agent 传递文件附件：

```
materialize_subagent_attachments(attachments, child_workspace, ...)
  │
  ├── 1. 校验
  │     ├── 文件名：禁止路径遍历、控制字符（C0+DEL）、保留名、重复名称
  │     ├── 数量限制：单次 spawn 最多 50 个文件
  │     ├── 大小限制：单文件 1MB，总大小 5MB
  │     └── mount_path 清洗：仅允许字母数字 + ._-/
  │
  ├── 2. 写入隔离目录
  │     └── <childWorkspace>/.openclaw/attachments/<uuid>/
  │
  ├── 3. 生成清单
  │     └── .manifest.json 记录文件名、大小、SHA-256 哈希
  │
  └── 4. 返回系统提示词后缀
        └── "Attachments: N file(s), M bytes. Available at: .openclaw/attachments/<uuid>"
```

### 八、后台守护机制

#### Sweeper（注册表扫描器）

```
registry/sweeper.py — 60 秒间隔循环

每次扫描执行：
  1. recover_orphaned_runs()     — 恢复孤儿 run
  2. finalize_suspended_deliveries() — 重试/丢弃挂起的投递
  3. persist_runs_to_disk()      — 快照到 SQLite
```

孤儿判定条件：`RUNNING` 且 `started_at` 不为空 且 运行超过分层阈值（cron=2h, subagent=6h, interactive=24h）。Sweeper 跳过 wedged run。

#### Followup（超时检查器）

```
followup/core.py — sweeper_interval * 2 间隔循环

每次检查执行：
  1. 遍历所有 run
  2. 找到 RUNNING 且超过 run_timeout_seconds 的 run
  3. 调用 recover_orphaned_runs() 强制恢复
```

#### Orphan Recovery（孤儿恢复）

```
orphan/recovery.py — 按 run_id 延迟调度

对每个孤儿 run：
  1. 等待 delay_seconds（默认 120s）
  2. 检查是否仍然活跃且未结束
  3. reconcile_orphaned_run() → 标记 TERMINAL + TIMEOUT
  4. 触发 run_subagent_announce_flow() → 投递超时结果给父 Agent
```

去重机制：同一 `run_id` 只调度一次恢复任务。

### 九、LLM 工具接口

#### sessions_spawn — 创建子 Agent

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `task` | str | 必填 | 任务描述 |
| `task_name` | str\|None | None | 稳定别名 |
| `label` | str\|None | None | 显示标签 |
| `agent_id` | str | "main" | 目标 Agent ID |
| `thinking` | str\|None | None | 覆盖思考模式 |
| `mode` | str | "run" | "run"（一次性）/ "session"（持久） |
| `cleanup` | str | "delete" | "delete" / "keep" |
| `context` | str | "isolated" | "isolated" / "fork" |
| `attachments` | list\|None | None | 文件附件（name, content, encoding, mount_path） |

返回：`"Subagent spawned: status={status}, run_id={id}, session_key={key}"`

#### sessions_yield — 暂停等待

让主 Agent 结束当前 turn，等待子任务结果到达。这是一个**信号工具**，不阻塞线程，而是告知框架当前 turn 可以暂停。

返回：`"Turn yielded. You will be resumed when subagent results arrive."`

#### sessions_send — 双向通信

| 参数 | 类型 | 说明 |
|------|------|------|
| `target_session_key` | str | 目标子 Agent 的 session key |
| `message` | str | 消息内容 |
| `max_turns` | int | 最大轮次（默认 1） |

通过 `MessageBus.publish_inbound()` 投递定向消息，`metadata.injected_event = "subagent_message"`。

#### agents_list — 可用 Agent 列表

返回配置中的 `allow_agents` 白名单。

#### subagents_list — 子 Agent 状态列表

返回当前 session 下的活跃和近期子 Agent：

```
Subagents: total=5, active=3, recent=2

Active:
  - [abc12345] research (depth=1, role=leaf, model=gpt-4, runtime=30s, pending=0)
  - [def67890] analysis (depth=1, role=leaf, model=gpt-4, runtime=2.5m, pending=0)
  - [ghi11223] writer (depth=1, role=orchestrator, model=gpt-4, runtime=1.2h, pending=2)

Recent:
  - [jkl44556] lookup status=ok runtime=45s
  - [mno77889] verify status=timeout runtime=5.0m
```

#### sessions_kill — 取消子 Agent

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `run_id` | str | 必填 | 要取消的运行 ID |
| `reason` | str | "killed" | 取消原因 |

取消运行中的子 Agent。仅 controller session 可执行 kill。支持 cascade 模式（递归 kill 所有子 Agent）。Kill reconciliation 仲裁机制处理与并发 completion 的竞态。

`kill_all_controlled_subagent_runs(requester_session_key)` — 一次性 kill 一个 session 下所有可 kill 的子 Agent。

#### sessions_steer — 操控/重启子 Agent

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `run_id` | str | 必填 | 要操控的运行 ID |
| `new_instructions` | str | 必填 | 注入的新指令 |

向运行中的子 Agent 注入新指令。运行转为 INTERRUPTED 状态（`pause_reason="steer"`），`generation` 递增。

### 十、Hook 协议

Hook 机制允许外部代码监听子 Agent 的生命周期事件：

```python
from future_subagent.hooks.base import register_start_hook, register_stop_hook
from future_subagent.hooks.progress import register_spawned_hook, register_ended_hook, register_delivery_target_hook

async def on_start(event: SubagentStartEvent):
    print(f"Subagent started: {event.child_session_key}")

async def on_stop(event: SubagentStopEvent):
    print(f"Subagent stopped: {event.child_status}")

async def on_delivery_target(run, target_session_key):
    return None  # 返回 session_key 可重定向，返回 None 不干预

register_start_hook(on_start)
register_stop_hook(on_stop)
register_delivery_target_hook(on_delivery_target)
```

| 事件 | 字段 |
|------|------|
| `SubagentStartEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_goal` |
| `SubagentStopEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_status`, `child_summary`, `duration_ms` |

Hook 按注册顺序串行执行，异常被吞咽不中断流程。

### 十一、与现有系统的共存

| 维度 | 现有 subagent (`agent/tools/subagent/`) | 新 subagent (`future_subagent/`) |
|------|---------------------------------------|---------------------------|
| 工具名 | `subagent` | `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list` |
| 管理器 | `SubagentManager` (singleton) | `SubagentRegistry` (dict + SQLite) |
| 子 Agent | Commander + Worker 两层 | 直接 spawn LangGraph agent |
| 深度 | 单层 | 多层嵌套（默认 3 层） |
| 通信 | 单向回传 | 双向（sessions_send） |
| 知识图谱 | 有（draft→distill→ingest） | 暂无 |
| 投递通道 | MessageBus | MessageBus（共用） |
| 中间件 | — | Summarization + IterationBudget + ToolGuardrails + ToolCallNormalize + HeartbeatStaleness |

两套工具同时注册到 `_MAIN_TOOLS_BUILDERS`，互不冲突，可渐进迁移。

### 十二、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 子 Agent 执行方式 | `CompiledStateGraph.ainvoke()` | 复用 LangGraph 基础设施，天然异步 |
| 投递通道 | 复用 `MessageBus.publish_inbound()` | 项目已有机制，无需重建 |
| 持久化 | aiosqlite（仅 SQLite，无 JSON fallback） | 项目已有依赖，SQLite 全平台可靠 |
| 沙箱 | 不移植 ACP | 同进程执行，通过工具黑名单控制权限 |
| yield 实现 | `asyncio.Event` + Registry 回调 | Python 无 gateway steering，Event 等价 |
| A2A 通信 | MessageBus + session key 路由 | 复用现有消息机制 |
| 共存策略 | 独立新建，新工具命名空间不同 | 渐进迁移，不破坏现有功能 |
| Fork 上下文 | `agent.aget_state()` 从 checkpointer 读取 | 决策 9：无需外部传入 parent_messages |
| 屏蔽工具 | `sessions_spawn`、`sessions_yield`、`skill_manage`、`memory` | 防止递归 spawn 和越权操作 |

---

## 配置

所有配置通过 `SubagentConfig`（Pydantic 模型，单例）管理：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_spawn_depth` | 3 | 最大嵌套深度 |
| `max_children_per_agent` | 5 | 每个 agent 最大并发子 agent 数 |
| `run_timeout_seconds` | 300.0 | 子 agent 执行超时（秒） |
| `require_agent_id` | False | 是否强制要求 agent_id |
| `allow_agents` | `["*"]` | 允许的 agent_id 白名单 |
| `default_cleanup` | "delete" | 默认清理策略 |
| `default_context_mode` | ISOLATED | 默认上下文模式 |
| `announce_retry_max` | 3 | 投递最大重试次数 |
| `announce_retry_delay_base_ms` | 1000 | 投递重试指数退避基数（1s, 2s, 4s） |
| `delivery_suspend_soft_cap` | 25 | 投递挂起软阈值 |
| `delivery_suspend_hard_cap` | 50 | 投递挂起硬阈值 |
| `delivery_suspend_target` | 10 | 压力修剪目标保留数 |
| `lifecycle_grace_period_seconds` | 15.0 | 错误/超时后等待最终化时间（秒） |
| `sweeper_interval_seconds` | 60 | 后台 sweeper 扫描间隔 |
| `orphan_recovery_delay_seconds` | 120 | 孤儿恢复延迟 |
| `announce_expiry_ms` | 7,200,000 | 投递软过期（2 小时） |
| `announce_hard_expiry_ms` | 86,400,000 | 投递硬过期（24 小时） |
| `max_announce_retry_count` | 10 | 投递最大重试次数 |
| `stale_unended_threshold_seconds` | 7200 | 陈旧未结束 run 判定阈值 |
| `recent_ended_window_seconds` | 1800 | 近期结束显示窗口 |
| `steer_rate_limit_ms` | 2000 | Steer 频率限制 |
| `archive_after_minutes` | 1440 | 自动归档时间（分钟） |
| `attachments_enabled` | True | 是否允许附件 |
| `attachments_max_files` | 50 | 单次 spawn 最大附件数 |
| `attachments_max_file_bytes` | 1MB | 单个附件最大字节数 |
| `attachments_max_total_bytes` | 5MB | 单次 spawn 附件总大小上限 |

---

## 项目状态

**所有 7 个阶段已完成 (2026-07-15)。健壮性补全 v3 增强已完成 (2026-07-22)。Bug 修复 + OpenClaw 对齐 + 深度对齐 + 接线修复已完成 (2026-07-23)。** 203 测试通过。参见 [AGENTS.md](./AGENTS.md) 了解项目规范，[decisions.md](./docs/decisions.md) 了解技术决策。
