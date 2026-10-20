# 计划：taskflow 孤儿流清理与防御性兜底

- 建档日期：2026-10-20
- 状态：方案设计（v2，融入 4 个同级项目调研），未开工
- 来源：审计发现（孤儿 taskflow 7 种产生路径 + 3 个写了没接的清理代码）+ 同级项目调研（oh-my-openagent / hermes-agent / openclaw / ZCode）
- 严重性：高（生产环境 `taskflow_registry.db` 无界增长 + 子代理死后残留 + 跨 store 不一致）

---

## 0. 问题

### 0.1 孤儿产生的 7 条路径

| # | 路径 | 状态停留 | 现有兜底 |
|---|---|---|---|
| 1 | LLM 建 flow 时没填 `deadline_hours`，然后走开 | `running` 永久 | `_expire_overdue_taskflows` SQL 过滤 `WHERE deadline_ts IS NOT NULL`（`store_sqlite.py:467-480`），无 deadline 就够不到 |
| 2 | 子代理被 kill / 超时，它建的 flow 留下 | `running`/`waiting` 永久 | `_finalize_cleanup`（`lifecycle.py:300-329`）只发 dead-end EventBus 消息，不调 `delete_flows_by_session`。`delete_transcript`/`session_delete` flag 写了没人读 |
| 3 | `taskflow_create(resumed_from=…)` 抛弃前驱 | `running`（设计性） | `mark_flow_superseded`（`store_sqlite.py:357-374`）只打 `superseded_by` 反向指针，不转终态 |
| 4 | `taskflow_finish` Gate A/B/E 永远拒绝 | `running` 永久 | 拒绝只返回 `Error:` 文本不转状态。Gate E 拒后建议 `taskflow_replan`，但 `REPLAN_MAX=3` 之后 LLM 不调 `taskflow_fail` 就卡死 |
| 5 | `waiting` flow 的子会话死了 | `waiting` 永久 | `_scan_stale_waiting_taskflows`（`sweeper.py:164-243`）只打咨询标记 `stale_detected_at`，docstring line 169-172 明说 "never auto-failed" |
| 6 | 进程崩溃 mid-flow | `running` 永久 | 启动时只扫会话文件夹（`session_dir_sweep.py:72-90` 反而把 flow 存在当 KEEP 信号），不扫 flow 行 |
| 7 | `purge_session` 部分失败 | flow 留下、session 文件夹已拆 | `session_cleanup_service.py:75-87` 的 try/except 把 todos+flows 删除包一起，失败只 log warning 然后继续清别的 → 跨 store 不一致 |

### 0.2 死的 / 不够的清理代码

| 代码 | 现状 | 为什么够不到 |
|---|---|---|
| `delete_flows_by_session`（`store_sqlite.py:627-641`） | 唯一 `DELETE FROM task_flows` | 只通过 HTTP `clear_session` 触发，子代理死 / 崩溃 / 超时都不调它；包在 try/except 里会静默失败 |
| `_expire_overdue_taskflows`（`sweeper.py:132-161`） | 每轮跑 | SQL `WHERE deadline_ts IS NOT NULL`，LLM 没 opt-in deadline 就够不到 |
| `_scan_stale_waiting_taskflows` | 每轮跑 | 只打标记不转状态 |
| 启动扫描 | 跑 | 不扫 flow 行，反而用 flow 存在当保留 session 文件夹的信号 |
| `trajectory_store.prune_session` | **死的** | 生产零调用，只测试调（同模式） |
| `get_todos_by_flow`（`todolist/store_sqlite.py:241`） | **死的** | flow→todo 反向查询，生产零调用，无法做 "找到指向孤儿 flow 的 todo 一起清" |
| `delete_subagent_session_for_cleanup` 的 metadata flag | **死的** | `cleanup.py:27-28` 写的 `injected_event=session_delete`/`delete_transcript=True` 全仓无第二读者 |

### 0.3 schema 约束

`task_flows` 表当前无 `created_at` / `updated_at` / `terminal_reason` 列（`store_sqlite.py:61-82`）。做 stale 检测要先补列。已有 `ALTER TABLE ADD COLUMN` 迁移先例（`_DEADLINE_COLUMN_DDL:107`、`_SESSION_ID_COLUMN_DDL:113`）。

### 0.4 跨项目调研：借鉴来源

调研了 sherry 同目录下 4 个同级 AI agent 项目对相同问题的解法。下表对照 sherry 8 个痛点，每格是该项目的实际机制 + file:line。

| 痛点 | oh-my-openagent | hermes-agent | openclaw | ZCode |
|---|---|---|---|---|
| #1 默认 deadline | background-agent 有默认 TTL 常量；tasklist DAG 无 | **claim 自动 stamp 15min TTL** `kanban_db.py:3387` | opt-in + 2h backstop `subagent-run-liveness.ts:17` | 没解决，靠 process-death 兜底 |
| #2 时间戳列 | `createdAt`/`updatedAt`/`claimedAt` `team-core/types.ts:105-119` | `created_at` + `last_heartbeat_at` + `started_at`/`completed_at` `kanban_db.py:1105-1129` | `createdAt` + 多 role-specific 时间戳 `subagent-registry.types.ts:252` | `created_at`/`updated_at` `NOT NULL` `schema-v1.ts:15-16` |
| #3 子代理死无清理 | PID-stamped locks + `process.kill(pid,0)` `team-state-store/locks.ts:95-105` | **`_pid_alive` 读 `/proc/<pid>/status` 查 `State: Z` + `detect_crashed_workers`** `kanban_db.py:6075-6136, 6641-6909` | sweeper phase 3 `!getAgentRunContext → completeSubagentRunWithRecovery` `subagent-registry-sweeper.ts:358-429` | harness `child.on("close") → interruptRun` + `lost` default + `processTreeTerminator` `harness.ts:456-464` |
| #4 superseded 前驱非终态 | pi-goal 用 file overwrite 无该列；tasklist 无 supersede | **无 `superseded_by` 列，用 DAG + `archive_task` 转终态** `kanban_db.py:5431` | fresh generation 出现时 stale predecessor 自动 `failed` + `retireSupersededSubagentRun` 删行 | amend = supersede，`engine.stop("superseded", …, supersededBy)` 转终态 `harness.ts:385-407` |
| #5 finish 拒绝永远 | completion system-driven；circuit breaker 20 + maxToolCalls 4000 兜底 | **三层独立预算：nudge max=2 + `consecutive_failures` 熔断 limit=2 + 协议违规 streak=3** `agent/kanban_stop.py:22`、`kanban_db.py:6911-7040, 6582` | `MAX_RECOVERY_RETRIES=3 → tombstone` + `wedgedAt` + announce 过期 5/30min `main-session-restart-recovery-shared.ts:21` | refusal zero-side-effect，无 max-attempts，靠 process-death 兜底 |
| #6 行数封顶；prune 死 | mailbox `recipient_unread_max_bytes=32KB` + archive FIFO cap；`resumeAllTeams` 死代码 | 无 cap；`gc_events`/`gc_worker_logs` 30d 裁剪，可达但需手动 CLI | **`SUBAGENT_SUSPENDED_DELIVERY_HARD_CAP=50` 阻断 admission + `archiveAtMs` 默认 60min 自动 purge** | 无 cap；tombstone 防复活，delete 路径都活 |
| #7 无 boot 对账 | **写了但死**：`resumeAllTeams` 全仓零 caller（同 sherry `prune_session` 模式） | **结构上不需要：every tick reconciles** `dispatch_once:7394-7463` | startup phase `"sidecars.main-session-recovery-scan"` + `initSubagentRegistry()` `server-startup-post-attach.ts:658-665` | **service 构造时 `reconcileOrphanRuns` 扫本会话非终态行全标 `stopped(interrupted)`** `dynamic-workflow-run-reconcile.ts:60-108` |
| #8 反向查询死代码 | `blockedBy` 容忍 dangling（缺失视为满足） `dependencies.ts:3-8` | `task_links` join 每 tick 用：`recompute_ready`/`claim_task`/`delete_task` 显式 cascade | `registerSessionMaintenancePreserveKeysProvider` + `transcriptStillOwned` + cron 读失败 fail-closed `subagent-registry-maintenance.ts:60` | tombstone `cli_deleted` + `listDeletedTaskIds` 防复活 `taskIndexRepo.ts:931-952, 1256-1285` |

### 0.5 借鉴的 5 个核心机制（按本计划引用顺序）

1. **hermes 的 "every tick reconciles" 模式**（`kanban_db.py:7394-7463`）：把 reclaim 分多层（TTL/heartbeat/PID/runtime）跑在每 60s 一次的 dispatch tick 里，**boot 后第一 tick 就是 reconcile**。消除了"boot-time reconciliation"作为单独代码路径要写要接的需要——sherry §1.3+§1.4 合并为同一套 idempotent sweep。
2. **ZCode 的 `reconcileOrphanRuns` at construction**（`dynamic-workflow-run-reconcile.ts:60-108`）：service 启动时 `journal.listNonTerminalRuns(parentSessionId)` 扫本会话非终态行全标 `stopped(interrupted)` + `failure: interruptedRunFailure`。三个边界：只本会话、不合成 event、失败不崩构造。sherry §1.4 抄这个模板。
3. **hermes 的 PID liveness with zombie state check**（`kanban_db.py:6075-6136`）：`_pid_alive` Linux 读 `/proc/<pid>/status` 查 `State: Z`，macOS `ps -o stat=` 查 `Z`。**关键**：区分"真死"和"未 reap 的僵尸"——worker exit 但未 waitpid 会永远卡 `running`。sherry §1.5 加这个探针。
4. **ZCode 的 `handoff_pending` bitfield as durable cross-store intent**（`legacy-tasks.ts:9-12`：`HANDOFF_DELETE=1, HANDOFF_TITLE=2, HANDOFF_ORPHAN=4, OWNERSHIP_DECIDED=8`）：不追求跨 store 原子，记一个 durable bit 在 task 行上，reconnect 时 `task-facts/index.ts:129-165` 幂等重放。sherry §1.10 抄这个模式。
5. **hermes 的三层独立预算**（`agent/kanban_stop.py:22` + `kanban_db.py:6911-7040, 6582`）：turn-end nudge max=2（不计数）+ `consecutive_failures` 熔断 `DEFAULT_FAILURE_LIMIT=2` + 协议违规 streak `_PROTOCOL_VIOLATION_FAILURE_LIMIT=3`。每层独立不互相消耗预算。sherry §1.7 改成这个结构。

**重要警示——同级项目也有同款"写了没接"模式**：oh-my-openagent 的 `resumeAllTeams`（`team-core/src/team-state-store/resume.ts:16-86`）在 `team-core/AGENTS.md:16` 有文档但全仓零 caller，跟 sherry 的 `prune_session` / `get_todos_by_flow` / subagent cleanup flag 是同一类问题。本计划落地时**先 grep caller 再宣布胜利**。

---

## 1. 设计

总策略：**三层防御 + 接上死代码 + 跨项目借鉴**。

- L1 **入口防御**：让孤儿难产生（默认 deadline、superseded 转终态、finish 拒绝三层预算）
- L2 **运行时清理**：sweeper 扩展（every tick reconciles）+ 子代理死亡 hook（PID liveness）+ service 构造时 orphan reconcile
- L3 **存量封顶**：per-session 总行数封顶（hard cap 阻断 + 软裁剪），真正接上 prune
- L0 **接死代码**：`get_todos_by_flow` 接到 `purge_session`，`prune_session_flows` 真正被调

### 1.1 schema 改造：补 `created_at` + `updated_at` + `terminal_reason` + `purge_pending`

**文件**：`agent/tools/taskflow/registry/store_sqlite.py`

迁移模式参照 `_DEADLINE_COLUMN_DDL:107` 和 `_SESSION_ID_COLUMN_DDL:113`：

```python
_CREATED_AT_COLUMN_DDL = (
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN created_at REAL"
)  # 建行时 time.time()；旧行回填 created_at = deadline_ts 或 0
_UPDATED_AT_COLUMN_DDL = (
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN updated_at REAL"
)  # 每次 update_flow 写 time.time()；旧行回填 = created_at
_TERMINAL_REASON_COLUMN_DDL = (
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN terminal_reason TEXT"
)  # 'deadline_exceeded' / 'stale_swept' / 'interrupted_at_boot'
    # / 'subagent_died' / 'superseded' / 'finish_gates_exhausted' / 'purge_partial'
_PURGE_PENDING_COLUMN_DDL = (
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN purge_pending INTEGER NOT NULL DEFAULT 0"
)  # §1.10 借鉴 ZCode handoff_pending bitfield：
    #   bit 1 = todos 未清, bit 2 = flows 未删, bit 4 = dangling todo 指针未清
```

`_ensure()` 里在 `_DEADLINE_COLUMN_DDL` / `_SESSION_ID_COLUMN_DDL` 旁边依次 try-execute，`ALTER TABLE ADD COLUMN` 在已有列上抛 OperationalError 时静默跳过（同现有模式）。`create_flow`（`:281` 的 `INSERT`）写 `created_at = time.time()`、`updated_at = time.time()`；`update_flow`（`:444`）每次 bump `updated_at = time.time()`。

索引加两个：

```python
f"CREATE INDEX IF NOT EXISTS idx_taskflow_stale_sweep ON {TABLE_NAME} "
"(session_id, status, updated_at) WHERE status IN ('running', 'waiting')"

# 反向查询索引（接上 §1.8 的 get_todos_by_flow 时用，避免全表扫）
f"CREATE INDEX IF NOT EXISTS idx_taskflow_purge_pending ON {TABLE_NAME} "
"(session_id, purge_pending) WHERE purge_pending != 0"
```

### 1.2 默认 deadline（24h，借鉴 hermes 15min + openclaw 2h backstop 节奏）

**文件**：`agent/tools/taskflow/tools/taskflow_create.py:78-80` + `config/features/agent_side/taskflow_infra.py`

```python
# taskflow_create.py
if deadline_hours is None:
    deadline_hours = TASKFLOW_INFRA["default_deadline_hours"]
if deadline_hours and deadline_hours > 0:
    deadline_ts = time.time() + deadline_hours * 3600
```

`TASKFLOW_INFRA` 加：

```python
#: 默认 deadline（小时）。LLM 不显式传 deadline_hours 时用这个。
#: 借鉴 hermes 15min claim TTL + 1h heartbeat backstop 的节奏，sherry 任务更重所以放大到 24h。
#: 0 = 禁用默认 deadline（回到旧行为）。长任务 LLM 显式传 deadline_hours=168 续命。
default_deadline_hours: int  # 默认 24

# 配置值
"default_deadline_hours": 24,
```

**修订理由（v1 是 72h，v2 改 24h）**：调研发现 hermes 用 15min+1h 极短 TTL + heartbeat backstop 兜底，节奏远比 sherry v1 的 72h 紧。72h 太宽松——LLM 走开后还要等 3 天 sweeper 才动手，期间 db 已积累。改成 24h，配合 §1.3 的 stale-running sweep（12h action）形成两 tier：12h 还在跑且无活动的 flow → fail；24h 还在跑的 flow → deadline 到 fail。两 tier 互补，24h 是最后兜底。

**长任务逃生路径**：LLM 在 `taskflow_summary` 看到 `terminal_reason='deadline_exceeded'` 后可 `taskflow_create(resumed_from=…)` 续上（§1.6 的 superseded 转终态保证不会双计 active）。如果 LLM 知道会跑长，建 flow 时显式传 `deadline_hours=168` 续命一周。

**风险与缓解**：少数 LLM 真的在跑 24h+ 的合法长 flow 被误 fail。缓解：到期 fail 时附带 `terminal_reason="deadline_exceeded"` 和原始 `deadline_ts`，LLM 在下一轮看到这个 reason 可以 `taskflow_create(resumed_from=…)` 续上。

### 1.3 stale-running sweep（借鉴 hermes "every tick reconciles" 单层 action 模式）

**文件**：`agent/tools/subagent/registry/sweeper.py`（已有 `_do_sweep` 调度，加一个 `_sweep_stale_running_taskflows`）

**v2 修订（v1 是 advisory 12h → action 72h 两 tier）**：调研发现 hermes/openclaw/ZCode **都没有 advisory tier**——hermes 是 four-layer idempotent sweep every tick（TTL/heartbeat/PID/runtime 全是 action，无 advisory）；openclaw 是 60s sweep 直接 `completeSubagentRunWithRecovery`，无 advisory；ZCode 靠 process-death + boot reconcile。advisory 是过度设计——LLM 看到 `stale_warned_at` 标记也不会主动处理（它走开了才会触发 stale），所以打标记只是浪费一次写。**改成单层 action**：

```python
async def _sweep_stale_running_taskflows(self) -> None:
    """单层 action（借鉴 hermes every tick reconciles）：
    running/waiting flow 的 updated_at 距今 > stale_action_hours → mark_flow_failed
    waiting 的 advisory 标记（_scan_stale_waiting_taskflows）保留——那是它原本的职责。
    本函数承接 waiting 的 action：waiting + updated_at > stale_action_hours → failed。
    """
    threshold = time.time() - TASKFLOW_INFRA["stale_action_hours"] * 3600
    candidates = await store_sqlite.get_stale_candidates(
        statuses=("running", "waiting"),
        action_before=threshold,
    )
    for flow_id, session_id, status in candidates:
        await _mark_stale_failed(flow_id, session_id,
            reason="stale_swept", prev_status=status)
```

`TASKFLOW_INFRA` 加：

```python
stale_action_hours: int   # action 阈值，默认 12（跟 §1.2 default_deadline_hours=24 形成两 tier：12h 无活动 → fail；24h 到期 → fail）
```

**`get_stale_candidates` SQL**（`store_sqlite.py` 新增）：

```sql
SELECT flow_id, session_id, updated_at, status
FROM task_flows
WHERE status IN ('running', 'waiting')
  AND updated_at < ?
  AND superseded_by IS NULL
ORDER BY updated_at
```

参数用 `threshold`。filter `superseded_by IS NULL` 跳过已 supersede 的前驱（§1.6 已转 cancelled，理论上不会出现，防御性跳过）。

**为什么单层 action 而不是 advisory**：advisory 给 LLM 主动处理的窗口，但触发 stale 的前提就是 LLM 走开了——它会看到 advisory 标记的概率极低。直接 fail 更干净，配合 §1.6 的 `taskflow_create(resumed_from=…)` 续接路径，被误 fail 的合法长 flow 可以恢复。

### 1.4 service 构造时 orphan reconcile（借鉴 ZCode `reconcileOrphanRuns` at construction）

**新文件**：`agent/tools/taskflow/runtime/reconcile.py`

抄 ZCode 的 `reconcileOrphanRuns`（`dynamic-workflow-run-reconcile.ts:60-108`）模板：

```python
async def reconcile_orphan_runs(session_id: str) -> dict:
    """service 构造时（session 启动 / agent.core.init）扫本会话的非终态 flow 全标 failed。

    借鉴 ZCode dynamic-workflow-run-reconcile.ts:60-108，三个边界照抄：
    1. 只本会话（parentSessionId 过滤，不跨会话扫——避免误杀别的会话的活 flow）
    2. 不合成 event（state 权威在 flow 行，不在 trajectory 事件）
    3. 失败不崩构造（warn-and-continue）

    幂等：第二次调用找不到非终态行（都已被转 failed）。
    """
    non_terminal = await store_sqlite.get_non_terminal_flows_for_session(session_id)
    reconciled = 0
    for flow_id, status in non_terminal:
        try:
            await _mark_failed(flow_id, session_id,
                reason="interrupted_at_boot",
                prev_status=status)
            reconciled += 1
        except Exception as e:
            logger.warning(
                "[taskflow] reconcile_orphan_runs failed for {} in session {}: {}",
                flow_id, session_id, e,
            )
    if reconciled:
        logger.info(
            "[taskflow] session {}: marked {} non-terminal flow(s) failed at reconcile",
            session_id, reconciled,
        )
    return {"reconciled": reconciled}
```

**接入点**：`agent/core.py::built_agent()` 或 `runtime/session/state_register.py` 的 session 初始化路径。在 agent 构造时（每会话一次）调用。这比 v1 计划的"启动时扫所有会话找孤儿"更精确——只扫本会话，不误杀。

**为什么不是 boot 时全局扫**：调研发现 ZCode 明确反对跨会话扫——`dynamic-workflow-run-reconcile.ts:52-58` 注释："only reconciles this session — global sweeping would kill sibling sessions' live runs; same-session dual-process is excluded by single-ownership"。全局扫会误杀别的会话还在跑的合法 flow。session-scoped at construction 才对。

**和 §1.3 的关系**：§1.3 是运行时每 60s tick 的 stale sweep；§1.4 是 service 启动时的一次性 reconcile。两者共用 `_mark_failed` 写终态路径，但触发时机和扫描范围不同。借鉴 hermes 的 "every tick reconciles" 哲学，§1.4 的 reconcile 其实就是 §1.3 的第一次 tick——但因为 service 启动到第一次 sweeper tick 之间有最多 60s 窗口，§1.4 把这个窗口里的 reconcile 提前到构造时同步做掉。

### 1.5 子代理死亡 → flow fail hook（加 hermes PID liveness + zombie state check）

**文件**：`agent/tools/subagent/registry/lifecycle.py:300-329` `_finalize_cleanup`

在现有 dead-end `delete_subagent_session_for_cleanup` 调用之前，加 PID liveness 探针 + flow fail hook：

```python
# §1.5 子代理死亡 → flow fail hook
# 借鉴 hermes _pid_alive（kanban_db.py:6075-6136）+ detect_crashed_workers（kanban_db.py:6641-6909）
try:
    from agent.tools.subagent.pid_liveness import is_subagent_pid_alive, is_zombie
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    child_pid = run.child_pid  # 子代理进程 PID
    if child_pid is not None:
        if is_zombie(child_pid):
            # 子进程 exit 了但没被 waitpid——看起来 alive 但实际已死
            # 这是 hermes _pid_alive 重点处理的场景（kanban_db.py:6087-6097）
            reason = "subagent_zombie_unreaped"
        elif not is_subagent_pid_alive(child_pid):
            # 子进程真死
            reason = "subagent_died"
        else:
            # 子进程还活着——可能是 timeout 主动 kill 但子进程还在退出中
            # 给 5s grace 再检查，仍活就强制 fail（避免 flow 卡 running）
            reason = "subagent_timeout_killed"

        await taskflow_store.fail_non_terminal_flows_for_session(
            child_session_key=run.child_session_key,
            reason=reason,
        )
    else:
        # 没 PID 信息（旧 run 或 spawn 失败），按 session 兜底
        await taskflow_store.fail_non_terminal_flows_for_session(
            child_session_key=run.child_session_key,
            reason="subagent_died_no_pid",
        )
except Exception as e:
    logger.warning(
        "[subagent] failed to fail taskflows for child {}: {}",
        run.child_session_key, e,
    )
```

**新文件**：`agent/tools/subagent/pid_liveness.py`

抄 hermes `_pid_alive`（`kanban_db.py:6075-6136`）+ `is_zombie`：

```python
"""PID liveness + zombie state check.

借鉴 hermes kanban_db.py:6075-6136 的 _pid_alive 和 detect_crashed_workers。
关键：区分"真死"和"未 reap 的僵尸"——worker exit 但未 waitpid 会永远卡 running。
"""

import sys

def is_subagent_pid_alive(pid: int) -> bool:
    """跨平台 PID 活性检查。返回 False 表示进程已退出（不含僵尸状态，见 is_zombie）。"""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # Windows: 用 tasklist 检查 PID
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=2,
            )
            return str(pid) in result.stdout
        else:
            # POSIX: process.kill(pid, 0) 不抛 = 活
            import os
            os.kill(pid, 0)
            return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但属于别的用户——认为 alive
        return True
    except Exception:
        return False


def is_zombie(pid: int) -> bool:
    """检查是否是僵尸状态（exit 了但没被 waitpid）。

    借鉴 hermes kanban_db.py:6087-6097：
    - Linux: 读 /proc/<pid>/status 查 State: Z
    - macOS: ps -o stat= 查 Z
    - Windows: 无僵尸概念，返回 False
    """
    if sys.platform == "win32":
        return False
    try:
        if sys.platform == "darwin":
            import subprocess
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True, text=True, timeout=2,
            )
            return "Z" in result.stdout
        else:
            # Linux
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("State:"):
                        return "Z" in line
            return False
    except Exception:
        return False
```

**`store_sqlite.fail_non_terminal_flows_for_session` 新增**：

```python
async def fail_non_terminal_flows_for_session(
    *, child_session_key: str, reason: str
) -> int:
    """把 session_id == child_session_key 且 status 非终态的所有 flow 转 failed。
    幂等：已是终态的行不动。保留行，写 terminal_reason。返回受影响行数。
    """
    sql = (
        f"UPDATE {TABLE_NAME} SET status = ?, updated_at = ?, "
        f"terminal_reason = ? "
        f"WHERE session_id = ? AND status NOT IN ({_TERMINAL_PLACEHOLDERS})"
    )
    params = [TaskFlowStatus.FAILED.value, time.time(), reason,
              child_session_key, *TERMINAL_STATUSES]
    ...
```

**spawn 失败回滚**（`spawn/core.py:1638-1651`）也加同一个调用。

**为什么不直接 fail_non_terminal_flows_for_session**：调研发现 hermes 专门处理了僵尸状态（`_pid_alive` 读 `/proc/<pid>/status` 查 `State: Z`，`kanban_db.py:6087-6097`）——一个 worker exit 了但没被 waitpid，简单 PID 检查会认为还活着，让 task 永远卡 `running`。sherry 直接调 `fail_non_terminal_flows_for_session` 会漏掉这种情况：子代理进程已 exit 但 PID 还在内核进程表里（未 reap），simple `os.kill(pid, 0)` 不抛——但其实已死。加 `is_zombie` 区分这个场景，是 hermes 的关键贡献。

### 1.6 superseded_by 清理：predecessor 转终态

**文件**：`agent/tools/taskflow/registry/store_sqlite.py:357-374` `mark_flow_superseded`

**v2 验证（v1 设计）**：调研确认四个项目里**没有一个**让 predecessor 留非终态：
- hermes：没 `superseded_by` 列，用 `archive_task` 把前驱转 `archived` 终态（`kanban_db.py:5431`）
- ZCode：`engine.stop("superseded", …, supersededBy)` 把前驱转 `stopped(superseded)`（`harness.ts:385-407`）
- openclaw：fresh generation 出现时 stale predecessor 自动 `failed` + `retireSupersededSubagentRun` 删行（integration test 印证）
- oh-my：pi-goal 用 file overwrite 完全没该列

sherry §1.6 的设计跟 ZCode 模式完全一致，**设计是对的，照 v1 实施**：

```python
async def mark_flow_superseded(
    flow_id: str, superseded_by: str, *, session_id: str
) -> bool:
    """打反向指针 + 把 predecessor 转 cancelled（如果还非终态）。

    幂等：已是终态的行不动 status，但 superseded_by 还是更新（处理重入）。
    保留行：跟 active-flow 查询（filter `superseded_by IS NULL`）一致，
    superseded 的行从 active 视图消失，但留在表里供 taskflow_summary 审计。
    """
    sql = (
        f"UPDATE {TABLE_NAME} SET superseded_by = ?, updated_at = ?, "
        f"status = CASE WHEN status IN ({_TERMINAL_PLACEHOLDERS}) "
        f"  THEN status ELSE ? END, "
        f"terminal_reason = CASE WHEN status IN ({_TERMINAL_PLACEHOLDERS}) "
        f"  THEN terminal_reason ELSE ? END "
        f"WHERE flow_id = ? AND session_id = ?"
    )
    params = [superseded_by, time.time(),
              *TERMINAL_STATUSES, TaskFlowStatus.CANCELLED.value,
              *TERMINAL_STATUSES, "superseded",
              flow_id, session_id]
    ...
```

predecessor 转 `cancelled` 而不是 `failed`：语义上不是失败，是被新版本替代。`terminal_reason="superseded"` 让 LLM 在 summary 里能区分。

**附加借鉴（openclaw `transcriptStillOwned` 检查，`subagent-registry-sweeper-retire.ts:18-38`）**：sherry §1.6 保留行不删，所以这个检查不需要，但模式可以记下——如果未来引入"删 superseded 行"功能，要加类似检查防止删了别的 flow 还在引用的行。

### 1.7 finish 拒绝循环 auto-fail（借鉴 hermes 三层独立预算）

**文件**：`agent/tools/taskflow/tools/taskflow_finish.py`

**v2 修订（v1 是单一 `finish_attempt_count` 阈值 5）**：调研发现 hermes 用**三层独立预算**：
- Turn-end nudge `max=2`（不计数，只是给 LLM 再来一次的提醒）
- `consecutive_failures` 熔断 `DEFAULT_FAILURE_LIMIT=2`（计数真失败）
- 协议违规 streak `_PROTOCOL_VIOLATION_FAILURE_LIMIT=3`（独立计数，不消耗主预算）

每层独立不互相消耗预算——nudge 用完不影响 failure limit，failure limit 用完不影响 protocol streak。sherry v1 的单一 `finish_attempt_count` 阈值 5 粗糙：nudge 和真失败一起计数，LLM 用 3 次 nudge 就吃掉 3 次预算。改成三层：

```python
# taskflow_finish.py 顶部
_FINISH_NUDGE_MAX = TASKFLOW_INFRA["finish_nudge_max"]            # 默认 2
_FINISH_FAILURE_LIMIT = TASKFLOW_INFRA["finish_failure_limit"]    # 默认 2
_PROTOCOL_VIOLATION_LIMIT = TASKFLOW_INFRA["finish_protocol_violation_limit"]  # 默认 3

# _finish 内部
state = flow["state"]

# Layer 1: turn-end nudge（借鉴 hermes agent/kanban_stop.py:22）
# 仅当 LLM 调 taskflow_finish 但 step 还在 dispatched/blocked 时触发
# 不增加 consecutive_failures，给 LLM 一次修正机会
state["finish_nudge_count"] = int(state.get("finish_nudge_count", 0)) + 1
if state["finish_nudge_count"] <= _FINISH_NUDGE_MAX:
    await store_sqlite.update_flow(flow_id, session_id, state=state)
    return _nudge_message(flow_id, "Some steps are not done. Use taskflow_wait_all then taskflow_resume.")

# Layer 2: consecutive_failures 熔断（借鉴 hermes kanban_db.py:6911-7040）
# nudge 用完后，每次 finish 失败都增加 consecutive_failures
state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
await store_sqlite.update_flow(flow_id, session_id, state=state)
if state["consecutive_failures"] >= _FINISH_FAILURE_LIMIT:
    await _mark_failed(flow_id, session_id,
        reason="finish_gates_exhausted",
        prev_status=TaskFlowStatus.RUNNING.value)
    return (
        f"Flow {flow_id} finish gates rejected {_FINISH_FAILURE_LIMIT} "
        f"times; auto-failed. Use taskflow_create(resumed_from='{flow_id}') "
        f"to fork a fresh plan, or call taskflow_summary to inspect."
    )

# Layer 3: protocol violation streak（借鉴 hermes _PROTOCOL_VIOLATION_FAILURE_LIMIT=3）
# LLM 调 finish 但根本没跑 step（hallucinated progress）——独立计数
if state.get("protocol_violation_streak", 0) >= _PROTOCOL_VIOLATION_LIMIT:
    await _mark_failed(flow_id, session_id,
        reason="protocol_violation_exhausted",
        prev_status=TaskFlowStatus.RUNNING.value)
    return f"Flow {flow_id} auto-failed: protocol violation streak exhausted."
```

**`TASKFLOW_INFRA` 加**：

```python
finish_nudge_max: int                  # 默认 2；turn-end 提醒上限（不计数）
finish_failure_limit: int              # 默认 2；连续失败熔断
finish_protocol_violation_limit: int   # 默认 3；协议违规 streak 上限
```

**为什么三层而不是一层**：nudge 是"温和提醒"（LLM 可能只是忘了 `taskflow_wait_all`），不应该消耗"真失败"的预算；protocol violation 是"LLM 谎报进度"（声称做了但 step status 是 ready），跟"step 还没做完"是不同性质的失败。三层独立预算让每种失败有自己的兜底，互不挤占。

**reset 时机**：`finish_nudge_count` 在 `taskflow_resume` 推进 step 后重置（LLM 修正了就给新的提醒预算）；`consecutive_failures` 在 `taskflow_resume` 成功推进 step 后重置；`protocol_violation_streak` 只在 step 真的从 `dispatched` 转 `done` 时重置（LLM 真做了才清零）。

### 1.8 反向孤儿：dangling todo 指针的逃生

**问题**：todo 指向的 flow 被删（HTTP `clear_session` 或未来某个 DELETE 路径）后，`_assert_transition_allowed`（`service.py:132-165`）返回 `"TaskFlow flow '{flow_id}' not found"`，永久阻塞 todo 标完成。建议逃生路径 `taskflow_wait_all` / `taskflow_resume` 也都以 `get_flow` 开头，找不到就直接 `not_found_error`——死路。

**改 `_assert_transition_allowed` 的错误消息**：让永久找不到（连续 N 次返回 not_found）的 todo 提示 LLM 抹掉 `flow_id`/`step_id`：

```python
# service.py 的 _assert_transition_allowed 内
if problem is not None and "not found" in problem:
    # 提示 LLM 抹指针。barrier 本身照旧拦（防止误标完成），
    # 但消息告诉 LLM 唯一出路是改写 todo 去掉 flow_id/step_id。
    raise TodoStoreError(
        f"Cannot mark todo completed: {problem}. The linked flow no longer "
        "exists (deleted or typo'd). Rewrite this todo WITHOUT flow_id/step_id "
        "to clear the barrier, or delete the todo."
    )
```

不动 barrier 逻辑（永久阻塞完成是对的，防止模型把没做完的事标完成），只改逃生提示。LLM 看到 "rewrite without flow_id/step_id" 知道该改 todo。

**接上 `get_todos_by_flow`**：把当前死的反向查询接到 `purge_session`，让删 flow 时同步清 todo 指针（避免 todo 留下 dangling）——细节挪到 §1.10 一起讲（bitfield 模式）。

### 1.9 per-session 总行数封顶（接上 prune + 借鉴 openclaw hard cap）

**`TASKFLOW_INFRA` 加**：

```python
#: 一个 session 保留的总 flow 行数（active + terminal + superseded）。
#: 超过时按 updated_at 升序裁剪最旧的 terminal/superseded 行（active 不裁）。
#: 0 = 不封顶。analog to trajectory_store 的 max_turns_per_session。
max_total_flows_per_session: int  # 默认 200

#: hard cap：达到时拒绝新 flow 创建（admission block），不裁旧。
#: 借鉴 openclaw SUBAGENT_SUSPENDED_DELIVERY_HARD_CAP=50（subagent-registry-suspended-delivery.ts:13）
#: 比裁旧更激进——阻止无界积累源头而不是事后裁剪。0 = 禁用 hard cap。
max_total_flows_hard_cap: int  # 默认 500（soft cap 200 + hard cap 500，缓冲层）

#: prune 触发节流：每个 session 每 N 秒最多 prune 一次，防止 finish/create 风暴里反复扫表。
prune_debounce_seconds: float  # 默认 60.0
```

**新增 `store_sqlite.prune_session_flows(session_id)`**（真正接上的 prune，对标 `trajectory_store.prune_session` 但实际被调）：

```python
async def prune_session_flows(self, session_id: str) -> int:
    """保留 newest max_total_flows_per_session 行（按 updated_at 降序）。
    只 DELETE terminal/superseded 行（status IN (done, failed, cancelled)
    OR superseded_by IS NOT NULL）——active 行不裁。
    返回删除行数。
    """
    keep = TASKFLOW_INFRA["max_total_flows_per_session"]
    if keep <= 0:
        return 0
    sql = f"""
    DELETE FROM {TABLE_NAME}
    WHERE session_id = ?
      AND rowid IN (
        SELECT rowid FROM {TABLE_NAME}
        WHERE session_id = ?
          AND (status IN (?, ?, ?) OR superseded_by IS NOT NULL)
        ORDER BY updated_at ASC
        LIMIT -1 OFFSET ?
      )
    """
    ...
```

**Hard cap admission block（借鉴 openclaw）**：`taskflow_create.py` 加：

```python
# taskflow_create.py 顶部
total_flows = await store_sqlite.count_total_flows_for_session(session_id)
hard_cap = TASKFLOW_INFRA["max_total_flows_hard_cap"]
if hard_cap > 0 and total_flows >= hard_cap:
    return _error_response(
        f"Session has {total_flows} flows (hard cap {hard_cap}). "
        "Run taskflow_summary to see stale flows, call taskflow_cancel "
        "on obsolete ones, or use HTTP clear_session to reset."
    )
```

**接入点**（节流调用，不在每次 mutation 时跑）：

- `agent/tools/taskflow/progress_push.py::push_taskflow_progress` 之后，按 session 节流地 schedule 一个 prune task（`asyncio.create_task`）。`push_taskflow_progress` 本来就在每个 mutation 后被调，加一个 prune 调用是最自然的接入点。
- 节流：用 `runtime.session.state_register` 暂存 `last_prune_ts`（或进程内 dict），同 session 60s 内只跑一次。

```python
# progress_push.py
_PRUNE_DEBOUNCE: dict[str, float] = {}

async def _maybe_prune_session(session_id: str) -> None:
    now = time.time()
    last = _PRUNE_DEBOUNCE.get(session_id, 0)
    if now - last < TASKFLOW_INFRA["prune_debounce_seconds"]:
        return
    _PRUNE_DEBOUNCE[session_id] = now
    try:
        from agent.tools.taskflow.registry.store_sqlite import prune_session_flows
        await prune_session_flows(session_id)
    except Exception as e:
        logger.debug("[taskflow] prune_session_flows failed: {}", e)

# push_taskflow_progress 末尾
asyncio.create_task(_maybe_prune_session(session_id))
```

**和 trajectory `prune_session` 的关系**：本计划**不顺带修 trajectory 的死 prune**（那是另一个独立 TODO，但同模式）。taskflow 这边把 prune 真正接上是核心，不留死代码。

### 1.10 `purge_session` 鲁棒性（借鉴 ZCode `handoff_pending` bitfield）

**文件**：`server/service/session_cleanup_service.py:75-87`

**v2 修订（v1 是 try/except 拆分 + 对账 log）**：调研发现 ZCode 用 **durable intent bitfield + idempotent reconcile** 模式（`legacy-tasks.ts:9-12`：`HANDOFF_DELETE=1, HANDOFF_TITLE=2, HANDOFF_ORPHAN=4, OWNERSHIP_DECIDED=8`）——不追求跨 store 原子（`todos.db` / `taskflow_registry.db` 物理隔离不可能共享 SQLite 事务），而是记一个 durable bit 在 flow 行上，reconnect 时 `task-facts/index.ts:129-165` 幂等重放。比 try/except + log 更稳：失败留 bit，下次必补。

借鉴这个模式到 §1.1 schema 的 `purge_pending` 列（bitfield）：

```python
# session_cleanup_service.py:75-87 重写
# §1.10 借鉴 ZCode handoff_pending bitfield as durable cross-store intent

PURGE_BIT_TODOS_NOT_CLEARED = 1      # todos 未删
PURGE_BIT_FLOWS_NOT_DELETED = 2      # flows 未删
PURGE_BIT_DANGLING_TODO_PTR = 4      # 指向被删 flow 的 todo 指针未清

async def purge_session(session_id: str, ...) -> None:
    # (1) 先在 flow 行上写 purge_pending 全置位（durable intent）
    #     这一步如果失败，整个 purge_session 失败，session 还在——安全
    await taskflow_store.mark_purge_pending(session_id, mask=0b111)
    
    # (2) 删 todos（独立 try）
    try:
        cleared_todos = await todo_store.delete_todos_by_session(session_id)
        await taskflow_store.clear_purge_bit(session_id, PURGE_BIT_TODOS_NOT_CLEARED)
    except Exception as e:
        logger.warning("todo clear failed for {}: {}", session_id, e)
        # bit 留着，下次 reconcile 重放
    
    # (3) 删 flows + 清 dangling todo 指针（独立 try）
    try:
        flows_to_delete = await taskflow_store.get_flow_ids_by_session(session_id)
        # 借鉴 §1.8：用 get_todos_by_flow 反向查清 dangling 指针
        for flow_id in flows_to_delete:
            linked_todos = await todo_store.get_todos_by_flow(session_id, flow_id)
            for todo in linked_todos:
                await todo_store.clear_flow_link(session_id, todo["position"])
        cleared_flows = await taskflow_store.delete_flows_by_session(session_id)
        await taskflow_store.clear_purge_bit(session_id, PURGE_BIT_FLOWS_NOT_DELETED | PURGE_BIT_DANGLING_TODO_PTR)
    except Exception as e:
        logger.warning("flow clear failed for {}: {}", session_id, e)
        # bit 留着，下次 reconcile 重放
    
    # (4) 其他清理（checkpointer, session folder, register, ...）
    ...
```

**新增 `store_sqlite.mark_purge_pending` / `clear_purge_bit`**：

```python
async def mark_purge_pending(self, session_id: str, mask: int) -> None:
    """在 session 的所有 flow 行上置 purge_pending bit。durable intent。"""
    sql = f"UPDATE {TABLE_NAME} SET purge_pending = purge_pending | ? WHERE session_id = ?"
    ...

async def clear_purge_bit(self, session_id: str, bit: int) -> None:
    """清 session 所有 flow 行的指定 purge_pending bit。幂等。"""
    sql = f"UPDATE {TABLE_NAME} SET purge_pending = purge_pending & ~? WHERE session_id = ?"
    ...
```

**Reconcile sweeper（借鉴 ZCode `task-facts/index.ts:129-165` 幂等重放）**：在 §1.3 的 `_sweep_stale_running_taskflows` 同一个 sweeper tick 里加一段：

```python
async def _reconcile_purge_pending(self) -> None:
    """每 tick 扫 purge_pending != 0 的 flow 行，重放未完成的清理。
    
    借鉴 ZCode task-facts/index.ts:129-165 的 confirm-then-mirror 两 pass：
    1. 重试 pending 的 delete/clear 操作
    2. 成功后清 bit
    幂等：操作已成功的不会重复执行（delete_flows_by_session 第二次返回 0）。
    """
    pending = await store_sqlite.get_flows_with_purge_pending()
    for flow_id, session_id, bits in pending:
        if bits & PURGE_BIT_TODOS_NOT_CLEARED:
            try:
                await todo_store.delete_todos_by_session(session_id)
                await store_sqlite.clear_purge_bit_for_flow(flow_id, session_id, PURGE_BIT_TODOS_NOT_CLEARED)
            except Exception: pass
        if bits & PURGE_BIT_FLOWS_NOT_DELETED:
            try:
                await store_sqlite.delete_flows_by_session(session_id)
                await store_sqlite.clear_purge_bit_for_flow(flow_id, session_id, PURGE_BIT_FLOWS_NOT_DELETED)
            except Exception: pass
        if bits & PURGE_BIT_DANGLING_TODO_PTR:
            try:
                # 反向查 todos 指向这个 flow，清指针
                linked = await todo_store.get_todos_by_flow(session_id, flow_id)
                for todo in linked:
                    await todo_store.clear_flow_link(session_id, todo["position"])
                await store_sqlite.clear_purge_bit_for_flow(flow_id, session_id, PURGE_BIT_DANGLING_TODO_PTR)
            except Exception: pass
```

**为什么比 try/except + log 强**：try/except 失败后只 log，bit 留在 flow 行上，下次 sweeper tick 必重放——失败不会丢，只会延迟。ZCode 的 `reconcileSnapshotOmissions`（`zcodeTaskIndexSyncer.ts:565-580`）注释印证这个哲学："CLI 会话对账未完成，保留已有行和待交接意图"——失败不删行，留 intent。

**接上 `get_todos_by_flow`**：本节是 `get_todos_by_flow` 真正接到生产的接入点——`PURGE_BIT_DANGLING_TODO_PTR` 的重放路径调它。之前死的反向查询现在每 tick 都用，不再是孤儿 API。

---

## 2. 关键文件清单

| 文件 | 改动 |
|---|---|
| `agent/tools/taskflow/registry/store_sqlite.py` | 加 4 列（`created_at`/`updated_at`/`terminal_reason`/`purge_pending`）+ 4 个 ALTER DDL；`create_flow`/`update_flow` 写时间戳；`mark_flow_superseded` 转 cancelled；新增 `get_stale_candidates` / `fail_non_terminal_flows_for_session` / `get_non_terminal_flows_for_session` / `get_flow_ids_by_session` / `count_flows_for_session` / `count_total_flows_for_session` / `prune_session_flows` / `mark_purge_pending` / `clear_purge_bit` / `get_flows_with_purge_pending` |
| `agent/tools/taskflow/tools/taskflow_create.py` | 默认 deadline（§1.2）+ hard cap admission block（§1.9） |
| `agent/tools/taskflow/tools/taskflow_finish.py` | 三层独立预算：nudge max=2 / consecutive_failures limit=2 / protocol_violation streak=3（§1.7） |
| `agent/tools/subagent/registry/sweeper.py` | `_do_sweep` 调度 `_sweep_stale_running_taskflows`（§1.3）+ `_reconcile_purge_pending`（§1.10） |
| `agent/tools/subagent/registry/lifecycle.py` | `_finalize_cleanup` 加 PID liveness 探针 + `fail_non_terminal_flows_for_session`（§1.5） |
| `agent/tools/subagent/spawn/core.py` | spawn 失败回滚也加 PID liveness + fail_non_terminal_flows_for_session（§1.5） |
| `agent/tools/subagent/pid_liveness.py`（新） | `is_subagent_pid_alive` + `is_zombie`（借鉴 hermes `kanban_db.py:6075-6136`） |
| `agent/tools/taskflow/runtime/reconcile.py`（新） | `reconcile_orphan_runs`（借鉴 ZCode `dynamic-workflow-run-reconcile.ts:60-108`） |
| `agent/core.py` | `built_agent()` 构造时调 `reconcile_orphan_runs(session_id)`（§1.4） |
| `agent/tools/todolist/service.py` | `_assert_transition_allowed` 的 not_found 错误消息提示抹指针（§1.8） |
| `agent/tools/todolist/registry/store_sqlite.py` | 新增 `clear_flow_link`（§1.8）；`get_todos_by_flow` 接到生产（§1.10 reconcile） |
| `agent/tools/taskflow/progress_push.py` | `push_taskflow_progress` 末尾节流 schedule prune（§1.9） |
| `server/service/session_cleanup_service.py` | `purge_session` 改成 bitfield durable intent 模式（§1.10） |
| `config/features/agent_side/taskflow_infra.py` | 加 `default_deadline_hours` / `stale_action_hours` / `finish_nudge_max` / `finish_failure_limit` / `finish_protocol_violation_limit` / `max_total_flows_per_session` / `max_total_flows_hard_cap` / `prune_debounce_seconds` |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| `ALTER TABLE ADD COLUMN` 迁移模式 | `store_sqlite.py:107, 113` (`_DEADLINE_COLUMN_DDL` / `_SESSION_ID_COLUMN_DDL`) | §1.1 补 4 列走同一 try-execute 模式 |
| sweeper 调度框架 | `subagent/registry/sweeper.py::_do_sweep:58-129` | §1.3+§1.10 加两个并行 sweep |
| `mark_flow_failed` 写终态 + terminal_reason | 现有 `taskflow_fail.py` | §1.4/1.5/1.6/1.7 复用同一写终态路径 |
| `TERMINAL_STATUSES` 常量集合 | `taskflow/config.py:69-75` | §1.5/1.6/1.9 的 SQL WHERE 复用 |
| `get_todos_by_flow` 反向查询（之前死的） | `todolist/store_sqlite.py:241` | §1.10 接到 reconcile sweeper |
| `progress_push.push_taskflow_progress` 接入点 | `taskflow/progress_push.py:107` | §1.9 在末尾 schedule prune |

### 不引入的依赖

- 不引入新表（`task_flows` 加列就够）
- 不引入跨 db 事务（todos.db / taskflow_registry.db 物理隔离，靠 §1.10 bitfield + §1.4 reconcile 兜底）
- 不引入新的 sweeper 进程（沿用 subagent sweeper，它是已有的 taskflow 兜底人）

---

## 3. 测试

| 测试 | 场景 |
|---|---|
| `test_default_deadline_applied` | `taskflow_create` 不传 `deadline_hours` → `deadline_ts` ≈ now + 24h |
| `test_default_deadline_zero_disables` | 配置 `default_deadline_hours=0` → 不传 `deadline_hours` 时 `deadline_ts` 为 NULL（回到旧行为） |
| `test_stale_sweep_action_only_no_advisory` | running flow `updated_at` 12h+ 前 → 直接 status 转 `failed`，`terminal_reason='stale_swept'`，无中间 advisory 标记（v2 改动，对齐 hermes 模式） |
| `test_stale_sweep_skips_superseded` | superseded_by 非空的 running flow 不被本函数 fail（已被 §1.6 转 cancelled，理论上不会出现；防御性测试） |
| `test_reconcile_orphan_runs_scoped_to_session` | session A 有非终态 flow，session B 也有 → 调 `reconcile_orphan_runs(A)` 只转 A 的 flow，B 的不动（借鉴 ZCode 只本会话边界） |
| `test_reconcile_orphan_runs_idempotent` | 二次调用 `reconcile_orphan_runs(session_id)` 找不到非终态行，返回 `reconciled=0` |
| `test_reconcile_orphan_runs_fail_soft` | mock `_mark_failed` 抛 → reconcile 不崩，其他 flow 继续，log warning |
| `test_reconcile_at_agent_construction` | `built_agent()` 构造时调 `reconcile_orphan_runs(session_id)`，本会话遗留的非终态 flow 在 agent 启动时全转 failed |
| `test_pid_liveness_alive` | mock 活 PID → `is_subagent_pid_alive` 返回 True，`is_zombie` 返回 False |
| `test_pid_liveness_dead` | mock 死 PID → `is_subagent_pid_alive` 返回 False |
| `test_pid_liveness_zombie_state` | Linux `/proc/<pid>/status` 含 `State: Z` → `is_zombie` 返回 True（借鉴 hermes `kanban_db.py:6087-6097` 关键场景） |
| `test_subagent_death_fails_child_flows_with_zombie_check` | subagent 进程是僵尸状态 → fail reason 是 `subagent_zombie_unreaped`；真死 → `subagent_died`；timeout kill → `subagent_timeout_killed` |
| `test_subagent_death_idempotent` | 二次调用 `fail_non_terminal_flows_for_session` 不动已是 failed 的行 |
| `test_superseded_marks_predecessor_cancelled` | `taskflow_create(resumed_from=F1)` → F1 status='cancelled'，terminal_reason='superseded'，superseded_by 指向新 flow |
| `test_superseded_already_terminal_unchanged` | predecessor 已是 done → 只更新 superseded_by，status/terminal_reason 不变 |
| `test_finish_three_layer_independent_budget` | nudge 用满 2 次 + consecutive_failures 用满 2 次 + protocol_violation streak 用满 3 次 → 各自独立 auto-fail，互不挤占 |
| `test_finish_nudge_resets_on_resume` | nudge 用 1 次后 `taskflow_resume` 推进 step → `finish_nudge_count` 重置 |
| `test_finish_consecutive_failures_resets_on_progress` | consecutive_failures=1 后 step 从 dispatched 转 done → `consecutive_failures` 重置 |
| `test_dangling_todo_suggests_clear_link` | todo 指向不存在的 flow → 错误消息含 "Rewrite this todo WITHOUT flow_id/step_id" |
| `test_purge_session_writes_purge_pending_bits` | purge_session 调用 → 所有 flow 行的 `purge_pending` 被置位 0b111 |
| `test_purge_session_partial_failure_keeps_bits` | mock `delete_flows_by_session` 抛 → `PURGE_BIT_FLOWS_NOT_DELETED` 留在行上，todos bit 清掉 |
| `test_reconcile_purge_pending_replays_failed_deletes` | flow 行有 `PURGE_BIT_FLOWS_NOT_DELETED` → 下次 sweeper tick 重试 `delete_flows_by_session`，成功后清 bit |
| `test_reconcile_purge_pending_clears_dangling_todo` | flow 行有 `PURGE_BIT_DANGLING_TODO_PTR` → sweeper 用 `get_todos_by_flow` 反向查并清指针（接上死的 API） |
| `test_hard_cap_blocks_admission` | session 有 500 个 flow（hard cap 500）→ `taskflow_create` 拒绝，返回 admission block 错误 |
| `test_soft_cap_prunes_oldest_terminal` | session 有 250 个 flow（soft cap 200）→ prune 删 50 个最旧 terminal，50 个 active 全留 |
| `test_prune_skips_when_zero_cap` | `max_total_flows_per_session=0` → prune 不动表 |
| `test_prune_debounce` | 同 session 60s 内连续 5 次 `push_taskflow_progress` → prune 只跑一次 |
| `test_prune_debounces_per_session` | session A 和 B 同时 push → 各自 60s 一次 prune，互不阻塞 |
| `test_created_at_backfill` | 旧 db（无 created_at 列）启动 → ALTER 成功；旧行 created_at 回填为 deadline_ts 或 0；新行有正确 created_at |
| `test_updated_at_bumps_on_update` | `update_flow` 写入 → updated_at > created_at |
| `test_schema_migration_idempotent` | 二次启动 → ALTER 抛 OperationalError 静默跳过，不崩 |

---

## 4. 优先级与分阶段落地

**P0（彻底堵住主要孤儿源，最小代价）**：
- §1.2 默认 deadline 24h（一行 + 配置）
- §1.5 子代理死亡 → PID liveness + flow fail hook（一个新文件 + 一个 try-except 块 + 一个 store 方法）
- §1.6 superseded_by 转终态（改一个 SQL）

这三条覆盖 §0.1 的场景 1/2/3，是最容易落地、影响最大的修复。落地后孤儿新增率应大幅下降。借鉴来源：§1.2 hermes claim TTL 节奏；§1.5 hermes `_pid_alive` + ZCode `lost` default；§1.6 ZCode `engine.stop("superseded")`。

**P1（兜底已产生的孤儿）**：
- §1.1 schema 加列（迁移要先跑）
- §1.3 stale-running 单层 action sweep
- §1.4 service 构造时 `reconcile_orphan_runs`（抄 ZCode 模板）

这三条需要 §1.1 先行（无 `updated_at` 做不了 stale 检测）。落地后已有的孤儿存量会被消化掉。借鉴来源：§1.3 hermes every tick reconciles；§1.4 ZCode `reconcileOrphanRuns` at construction。

**P2（防御性收尾）**：
- §1.7 finish 三层独立预算（抄 hermes）
- §1.8 反向孤儿逃生
- §1.9 per-session 总行数封顶（soft cap 200 + hard cap 500，借鉴 openclaw）+ 真正接上 prune
- §1.10 purge_session bitfield durable intent（抄 ZCode `handoff_pending`）+ 接上 `get_todos_by_flow`

P2 是兜底的兜底，让边缘场景（finish 死循环、dangling pointer、purge 部分失败）也不留孤儿。借鉴来源：§1.7 hermes 三层独立预算；§1.9 openclaw `SUBAGENT_SUSPENDED_DELIVERY_HARD_CAP`；§1.10 ZCode `handoff_pending` bitfield。

---

## 5. 已知不在本计划范围内

- **`trajectory_store.prune_session` 的死代码**：同模式但不同子系统。本计划不动 trajectory。建议单独 TODO 跟进（接 `push_frame` 末尾节流 schedule prune，跟 §1.9 同模式）。
- **`PLAN_FILE_PATH` 孤儿假阳性**：上一轮审计误报，`workspace/prompt_builder.py:160-172` 实际有生产读者。无修复需求。
- **`task_flows` 表的 child_session_key 反向引用**：本计划不动 child_session_key 语义，子代理死亡只靠 §1.5 的 session_id 匹配 + PID liveness 转 fail。
- **跨 db 事务**：物理上不可能（todos.db / taskflow_registry.db 不同文件）。靠 §1.10 bitfield durable intent + §1.4 boot reconcile 兜底。
- **多机部署的 PID liveness**：hermes 的 `_pid_alive` 是 host-local（`kanban_db.py:6684-6689`），跨机要靠 TTL reclaim。sherry 当前是单机部署，PID liveness 够用；未来多机部署时补 TTL 跨机 reclaim（hermes 已有此 fallback，`kanban_db.py:3633, 3643`）。

---

## 6. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| 默认 deadline 把长任务 flow 误 fail | 默认 24h（不是 72h，对齐 hermes/openclaw 节奏）；LLM 可显式传 `deadline_hours=168` 续命；fail 时 `terminal_reason='deadline_exceeded'`，LLM 看到 summary 可 `taskflow_create(resumed_from=…)` 续 |
| stale-running sweep 误 fail 正在跑的长 flow | 12h action 阈值 + §1.4 reconcile_at_construction 已先把 boot 时的孤儿转掉；12h 无 updated_at 的 flow 大概率真死 |
| PID liveness 把活子代理误判为死 | `is_zombie` 严格查 `/proc/<pid>/status` 的 `State: Z`，简单 `process.kill(pid, 0)` 不抛的活进程不会被误判；timeout kill 的子进程给 `subagent_timeout_killed` reason 留出 5s grace |
| superseded 转 cancelled 改变 LLM 读取的 summary | `get_active_flows_sync` 本来就 filter `superseded_by IS NULL`，predecessor 本来就不在 active 视图；`taskflow_summary` 全量视图会从 `running` 变成 `cancelled`，但附带 `terminal_reason='superseded'` 让 LLM 明白 |
| finish 三层预算太复杂 LLM 难理解 | 三层对 LLM 透明——LLM 只看到 `taskflow_finish` 的返回文本，错误消息明确指出"nudge 用满请用 taskflow_wait_all"或"consecutive failures 用满已 auto-fail"；行为可预测 |
| prune 误删未完成的 flow | SQL 限定 `status IN (done, failed, cancelled) OR superseded_by IS NOT NULL`，active 行永远不裁 |
| hard cap 阻断合法新 flow 创建 | soft cap 200 + hard cap 500 有缓冲层；到 hard cap 时错误消息明确指出"call taskflow_cancel on obsolete ones or use HTTP clear_session"——给 LLM 出路 |
| ALTER TABLE 在大表上锁库 | `task_flows` 行级表，单 session 上限 500（hard cap 后），全表也不会大；`ALTER TABLE ADD COLUMN` 在 SQLite 是元数据操作不复制数据 |
| schema 迁移失败 | try-execute 模式静默跳过已有列；新行有 created_at/updated_at，旧行回填；sweeper 查询对 NULL updated_at 容错（回退到 created_at，再回退到 deadline_ts） |
| reconcile_orphan_runs 误杀会话启动时的活 flow | 借鉴 ZCode 只本会话边界——只扫 `session_id == current_session` 的非终态行，不动别的会话；构造时本会话确实零个在飞 flow（同一会话不会同时有两个 agent 实例） |
| bitfield reconcile 死循环 | 每个 bit 的重放都幂等（`delete_flows_by_session` 第二次返回 0；`clear_flow_link` 在 flow_id 已 NULL 时是 no-op）；成功后清 bit 不会再扫；最多每 tick 跑一次 |

---

## 7. 一句话总结

孤儿 taskflow 能诞生，根源是"清理代码写了没接 + 入口防御太弱"。解决方案三层 + 跨项目借鉴：**入口防御**（默认 deadline 24h 借鉴 hermes、superseded 转终态借鉴 ZCode、finish 三层独立预算借鉴 hermes）+ **运行时清理**（stale-running 单层 action sweep 借鉴 hermes every tick reconciles、子代理死亡 PID liveness + zombie check 借鉴 hermes、service 构造时 orphan reconcile 借鉴 ZCode）+ **存量封顶**（per-session soft+hard cap 借鉴 openclaw + prune 真正接上）+ **跨 store 鲁棒性**（bitfield durable intent 借鉴 ZCode `handoff_pending`）。死代码 `get_todos_by_flow` 接到 §1.10 reconcile sweeper；新代码 `prune_session_flows` 节流接到 `push_taskflow_progress`；新 schema 加 `created_at`/`updated_at`/`terminal_reason`/`purge_pending` 四列支持 stale 检测、审计可见性、跨 store durable intent。P0 三条覆盖 7 个场景里的 3 个主源，落地代价最小、收益最大。同级项目调研印证：oh-my-openagent 也有同款"写了没接"模式（`resumeAllTeams` 全仓零 caller），落地时先 grep caller 再宣布胜利。
