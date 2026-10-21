# 计划：taskflow 孤儿流清理——三色标记法统一 GC + liveness 补充

- 建档日期：2026-10-21
- 状态：**已实现（2026-10-21）**。落地时的安全调整：入口 todo 是**流级根**（无 `step_id`，flow 终态前不可完成；`todo_content` 缺省回退 description/flow id，避免旧调用方直接失败）；三色增加 `tri_color_grace_seconds=300` 覆盖跨 SQLite 文件的创建/绑定竞态；session 级 liveness 只回收**已过 inactivity 阈值**的 flow（子代理 session 用短 grace），不会在启动时因没有 socket 批量误杀；live child 同时否决三色与 liveness；prune 采用 200 行上限，未加 hard cap。
- 来源：审计发现（孤儿 taskflow 7 种产生路径）+ JVM 三色标记法类比 + 跨项目调研（hermes/openclaw/ZCode）
- 严重性：高（生产环境 `taskflow_registry.db` 无界增长 + 子代理死后残留 + 跨 store 不一致）
- **本计划替代** `TODO/计划_taskflow孤儿流清理与防御性兜底.md`（v2）——v2 的 6 个 per-scenario 修复被三色标记统一机制替代，保留 liveness 补充层

---

## 0. 问题

### 0.1 孤儿产生的 7 条路径

| # | 路径 | 根因 |
|---|---|---|
| 1 | LLM 建 flow 没填 deadline 然后走开 | 无引用 → 不可达 |
| 2 | 子代理 kill/timeout，flow 留下 | 子代理 session 的 flow 不被父 session 的 todo 引用 → 不可达 |
| 3 | `taskflow_create(resumed_from=…)` 抛弃前驱 | `mark_flow_superseded` 不转终态 → 前驱留 `running` |
| 4 | `taskflow_finish` Gate A/B/E 永远拒绝 | 有引用但功能死 → stuck-but-reachable |
| 5 | waiting flow 子会话死了 | 同 #2 → 不可达 |
| 6 | 进程崩溃 mid-flow | 有引用但执行者死了 → stuck-but-reachable |
| 7 | `purge_session` 部分失败 | todo 删了 flow 没删 → 不可达（下一轮 sweep 清） |

### 0.2 两个维度

| 维度 | 含义 | 场景 | 检测方式 |
|---|---|---|---|
| **不可达孤儿** | 没有活跃 todo 引用的非终态 flow | #1, #2, #3, #5, #7 | **三色标记**（引用图 GC） |
| **stuck-but-reachable 孤儿** | 有活跃 todo 引用但功能已死 | #4, #6 | **liveness 检测**（心跳 GC） |

三色标记是**引用计数 GC**——判断"有没有人引用"。liveness 是**心跳 GC**——判断"引用者/被引用者是否还活着"。两个维度互补不替代。

### 0.3 v2 方案为什么复杂

v2（`计划_taskflow孤儿流清理与防御性兜底.md`）用 6 个独立机制各防一个场景：

| v2 机制 | 防的场景 | 复杂度 |
|---|---|---|
| §1.2 默认 deadline 24h | #1 | 改 `taskflow_create` + 配置 |
| §1.3 stale-running 两 tier sweep | #1, #4 | 新 sweeper 函数 + SQL |
| §1.4 service 构造时 reconcile | #6 | 新文件 + 调用接入 |
| §1.5 subagent death → PID liveness + flow fail hook | #2 | 新 `pid_liveness.py` + 改 lifecycle |
| §1.6 superseded 转终态 | #3 | 改 SQL |
| §1.7 finish 三层独立预算 | #4 | 改 prompt + 计数器 |
| §1.9 prune + hard cap | 存量封顶 | 新 prune 函数 + 接入 |
| §1.10 bitfield durable intent | #7 | 新 `purge_pending` 列 + reconcile |

**8 个机制 × 各自测试 × 各自风险** = 系统复杂度高。每个机制只防 1-2 个场景，互不复用。

### 0.4 三色标记为什么更简单

三色标记用**一个 SQL 事务**统一防 5/7 场景（#1/2/3/5/7）：

- 不需要默认 deadline（§1.2）——三色每轮 sweep 不可达 flow
- 不需要 subagent death hook（§1.5）——三色自动清子代理的不可达 flow
- 不需要 bitfield durable intent（§1.10）——三色下一轮 sweep 清 purge 残留
- 不需要 PID liveness（§1.5 的 `pid_liveness.py`）——三色看引用图不看 PID

**保留 3 个 liveness 机制**（三色覆盖不了的 #4/#6）：
- stale-running sweep（§1.3 简化版）——#4 finish 拒绝 + #6 崩溃
- boot reconcile——#6 崩溃恢复
- finish 三层预算（§1.7）——#4 finish 死循环
- prune——存量封顶（终态行清理，三色只清非终态）

---

## 1. 设计

### 1.1a 入口防御：`taskflow_create` 必须携带 todo 引用——flow 出生即有 GC root

**文件**：`agent/tools/taskflow/tools/taskflow_create.py`

**问题**：当前 `taskflow_create` 不创建 todo——todo→flow 链接靠 LLM 事后通过 `todowrite` 手填。如果 LLM 创建了 flow 但忘了写 todo，flow 从出生就是孤儿（无引用 → 三色白 → sweep）。

**改法**：`taskflow_create` 加必填参数 `todo_content: str`，创建 flow 后**自动**创建一条 `pending` todo，`flow_id` + `step_id`（第一个 step）自动 stamp 到 todo 行上——**flow 和 todo 原子绑定，出生即有 GC root**。

```python
class TaskflowCreateSchema(BaseModel):
    # ... 现有参数 ...
    todo_content: str = Field(
        description="A one-line description of this task for the todo list. "
                    "A todo will be auto-created referencing this flow's first step. "
                    "Required — a flow cannot exist without an active todo reference."
    )
    todo_category: str = Field(default="tasks", description="Todo category (default: tasks).")

@tool("taskflow_create", args_schema=TaskflowCreateSchema)
def _taskflow_create_tool(
    # ... 现有参数 ...
    todo_content: str,
    todo_category: str = "tasks",
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    # 1. 创建 flow（现有逻辑）
    flow = await store.create_flow(session_id=session_id, ...)

    # 2. 自动创建 todo，绑定 flow_id + 第一个 step
    first_step_id = flow.steps[0].step_id if flow.steps else ""
    await todo_store.add_or_update_todo(
        session_id=session_id,
        content=todo_content,
        status="pending",
        category=todo_category,
        flow_id=flow.flow_id,
        step_id=first_step_id,
    )

    # 3. 推送 todo_updated + taskflow_updated（现有推送逻辑）
    ...
```

**效果**：
- flow 出生即有一条 `pending` todo 引用它 → 三色标黑 → 不被 sweep
- LLM 把 todo 标 `completed` → todo 变终态 → 不在 GC root 集合 → flow 变白 → 下一轮 sweep 清掉
- 不再需要 LLM 事后用 `todowrite` 手填 `flow_id`/`step_id`——自动绑定，减少 LLM 出错概率
- 场景 #1（LLM 建 flow 然后走开）**在入口就防住**——没有 `todo_content` 不让创建

**`todowrite` 的 `flow_id`/`step_id` 字段保留**——LLM 仍可以手填（比如一个 todo 跟踪 flow 的第二个 step），但不再是"唯一建立链接的方式"。入口绑定是主路径，`todowrite` 手填是补充。

**`taskflow_plan` 工具也改**——`taskflow_plan` 内部调 `taskflow_create`，也要传 `todo_content`。如果 `taskflow_plan` 的输入有 plan description，用它做 `todo_content`。

### 1.1 三色标记 sweep——统一 GC 基础层

**文件**：`agent/tools/subagent/registry/sweeper.py`（在现有 `_do_sweep` 调度里加一个 `_tri_color_sweep`）

**JVM 概念映射**：

| JVM | taskflow |
|---|---|
| GC roots | 活跃 todo（非终态 status：`pending` / `in_progress`） |
| 对象引用 | todo 行的 `(flow_id, step_id)` 软指针 |
| 白（未标记） | 没有活跃 todo 引用的非终态 flow |
| 黑（已标记） | 有活跃 todo 引用的 flow |
| sweep | 白且非终态且 `superseded_by IS NULL` 的 flow → 标 `failed` |
| stop the world | 一个 SQLite 事务（`BEGIN IMMEDIATE` → mark → sweep → `COMMIT`，毫秒级） |

```python
async def _tri_color_sweep(session_ids: list[str]) -> dict:
    """三色标记 GC：每轮 sweeper tick 跑。一个 SQLite 事务 = STW。

    mark: 活跃 todo 引用的 flow_id = 可达集（黑）
    sweep: 非终态 + 不在可达集 + 未被 supersede 的 flow → failed（白 → 清）

    一个 SQL 事务统一防 5/7 孤儿场景（#1/2/3/5/7）。
    """
    import time
    from agent.tools.taskflow.registry.store_sqlite import get_shared_db
    from agent.tools.taskflow.config import TERMINAL_STATUSES

    db = get_shared_db()
    now = time.time()
    total_swept = 0
    total_marked = 0

    for session_id in session_ids:
        with db:  # BEGIN IMMEDIATE → COMMIT（STW，毫秒级）
            # ── mark 阶段：收集活跃 todo 引用的 flow_id ──
            marked_rows = db.execute(
                "SELECT DISTINCT flow_id FROM todos "
                "WHERE session_id = ? "
                "AND status NOT IN ('completed', 'cancelled') "
                "AND flow_id IS NOT NULL",
                (session_id,),
            ).fetchall()
            marked_ids = [r["flow_id"] for r in marked_rows]
            total_marked += len(marked_ids)

            # ── sweep 阶段：非终态 + 不在可达集 + 未被 supersede → failed ──
            placeholders = ",".join("?" * len(marked_ids)) if marked_ids else ""
            if marked_ids:
                cursor = db.execute(
                    f"UPDATE task_flows "
                    f"SET status = 'failed', terminal_reason = 'orphan_unreferenced', "
                    f"updated_at = ? "
                    f"WHERE session_id = ? "
                    f"AND status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))}) "
                    f"AND superseded_by IS NULL "
                    f"AND flow_id NOT IN ({placeholders})",
                    (now, session_id, *TERMINAL_STATUSES, *marked_ids),
                )
            else:
                # 没有活跃 todo 引用任何 flow → 所有非终态 flow 都是白
                cursor = db.execute(
                    f"UPDATE task_flows "
                    f"SET status = 'failed', terminal_reason = 'orphan_unreferenced', "
                    f"updated_at = ? "
                    f"WHERE session_id = ? "
                    f"AND status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))}) "
                    f"AND superseded_by IS NULL",
                    (now, session_id, *TERMINAL_STATUSES),
                )
            total_swept += cursor.rowcount

    if total_swept:
        logger.info(
            "[taskflow] tri-color sweep: marked {} flow_ids, swept {} orphan flows",
            total_marked, total_swept,
        )
    return {"marked": total_marked, "swept": total_swept}
```

**接入点**：`_do_sweep`（`sweeper.py:58-129`）里加一行 `await _tri_color_sweep(active_session_ids)`。`active_session_ids` 从 subagent registry 的活跃 session 集合拿（跟现有 sweeper 扫的 session 范围一致）。

**频率**：每 sweeper tick（60s）。mark 是一条 SELECT，sweep 是一条 UPDATE——单 session 毫秒级。

**schema 改动**：加 `terminal_reason TEXT` 列（跟 v2 §1.1 相同，`ALTER TABLE ADD COLUMN` 自愈迁移）。加 `updated_at REAL` 列（liveness 检测用，跟 v2 §1.1 相同）。

### 1.2 sweep 条件逐条解析

```sql
UPDATE task_flows
SET status = 'failed', terminal_reason = 'orphan_unreferenced', updated_at = ?
WHERE session_id = ?
  AND status NOT IN ('done', 'failed', 'cancelled')   -- 只清非终态（白）
  AND superseded_by IS NULL                            -- superseded 的不清（§1.4 已转终态）
  AND flow_id NOT IN (活跃 todo 引用的 flow_id 集合)    -- 不清黑（有活跃引用）
```

| 条件 | 作用 | 防哪个场景 |
|---|---|---|
| `status NOT IN (terminal)` | 只清非终态 flow（`running` / `waiting`）。终态行留给 prune（§1.6） | — |
| `superseded_by IS NULL` | superseded 的 flow 不被三色清——它们由 §1.4 `mark_flow_superseded` 转 `cancelled` 后变成终态，自然被 `status NOT IN (terminal)` 排除 | #3 |
| `flow_id NOT IN (marked)` | 有活跃 todo 引用的 flow 不清（黑）。活跃 todo = `pending` / `in_progress`（非终态） | #1, #2, #5, #7 |

**为什么 `superseded_by IS NULL` 而不是 `superseded_by IS NULL OR status IN (terminal)`**：因为 `status NOT IN (terminal)` 已经排除了终态行。superseded 的 flow 如果已被 §1.4 转成 `cancelled`（终态），就不会被 sweep。如果 §1.4 没转（当前 bug），`superseded_by IS NULL` 条件会跳过它——但下一轮 §1.4 会修复（转终态后自然排除）。所以 `superseded_by IS NULL` 是防御性的——确保不会误清一个被 supersede 但还没来得及转终态的 flow。

### 1.3 `mark_flow_superseded` 转终态（保留 v2 §1.6，验证正确）

**文件**：`agent/tools/taskflow/registry/store_sqlite.py:357-374`

v2 §1.6 的设计正确——`mark_flow_superseded` 应该把前驱转 `cancelled`。三色标记依赖它：

- 前驱被转 `cancelled`（终态）→ 三色 sweep 的 `status NOT IN (terminal)` 排除它 → 不清（已经终态了，留给 prune）
- 前驱没被转（当前 bug）→ 三色 sweep 的 `superseded_by IS NULL` 条件跳过它 → 不会误清

照 v2 §1.6 实施：

```python
async def mark_flow_superseded(flow_id, superseded_by, *, session_id):
    sql = (
        f"UPDATE {TABLE_NAME} SET superseded_by = ?, updated_at = ?, "
        f"status = CASE WHEN status IN ({_TERMINAL_PLACEHOLDERS}) "
        f"  THEN status ELSE ? END, "
        f"terminal_reason = CASE WHEN status IN ({_TERMINAL_PLACEHOLDERS}) "
        f"  THEN terminal_reason ELSE ? END "
        f"WHERE flow_id = ? AND session_id = ?"
    )
    # params = [superseded_by, time.time(),
    #           *TERMINAL_STATUSES, TaskFlowStatus.CANCELLED.value,
    #           *TERMINAL_STATUSES, "superseded",
    #           flow_id, session_id]
```

### 1.4 liveness 补充层——三重 liveness 检查

三色标记覆盖不了 #4（finish 拒绝）和 #6（崩溃）——这些 flow 有活跃 todo 引用（黑），但功能已死。需要 liveness 检测——**不是看"有没有引用"，而是看"被引用的对象/引用者本身是否还活着"**。

**文件**：`agent/tools/subagent/registry/sweeper.py`（在三色 sweep 之后跑，同一个 tick）

三重 liveness，从粗到细：

#### 1.4.1 session-level liveness——session 死了，所有 flow 都是孤儿

**检测**：flow 的 session_id 不在活跃 session 集合里（session 进程已死 / session 已过期 / session 被关闭但没 `clear_session`）。

**这覆盖 boot reconcile（v2 §1.4）的运行时版本**——不只是 boot 时跑，每 tick 都跑。

```python
async def _liveness_session_check(active_session_ids: set[str]) -> dict:
    """session-level liveness：session 不在活跃集合的非终态 flow → failed。

    每 tick 跑（不只是 boot）。active_session_ids 从 subagent registry + 
    session register 拿——活跃 = 有活 agent 进程或最近有 turn 活动。
    """
    if not active_session_ids:
        # 没有 active session → 所有非终态 flow 都是孤儿
        cursor = db.execute(
            f"UPDATE task_flows "
            f"SET status = 'failed', terminal_reason = 'session_dead', updated_at = ? "
            f"WHERE status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))}) "
            f"AND superseded_by IS NULL",
            (now, *TERMINAL_STATUSES),
        )
    else:
        placeholders = ",".join("?" * len(active_session_ids))
        cursor = db.execute(
            f"UPDATE task_flows "
            f"SET status = 'failed', terminal_reason = 'session_dead', updated_at = ? "
            f"WHERE status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))}) "
            f"AND superseded_by IS NULL "
            f"AND session_id NOT IN ({placeholders})",
            (now, *TERMINAL_STATUSES, *active_session_ids),
        )
    return {"session_dead_swept": cursor.rowcount}
```

`active_session_ids` 来源：
- subagent registry 的活跃 session（`subagent_registry.get_active_session_ids()`）
- server 的 session register（`relation_register.get_active_sessions()`）
- 两者并集 = 所有有活 agent 进程的 session

**效果**：子代理被 kill（#2）→ 子代理 session 不在活跃集合 → 子代理的 flow 被 liveness 清掉。**这跟三色是双重保险**——三色看"有没有活跃 todo 引用"（子代理的 flow 没有父 session 的 todo 引用 → 白 → sweep），liveness 看"session 是否活着"（子代理 session 死了 → 所有 flow → failed）。

#### 1.4.2 flow-level liveness——flow 太久没活动

**检测**：flow 还在 `running`/`waiting`，但 `updated_at` 距今超过阈值——没有人在推进它。

**覆盖 #4（finish 拒绝）和 #6（崩溃 mid-flow）**——finish 拒绝不 bump `updated_at`（只有真正的 step 状态变化才 bump），崩溃后也没人 bump。

```python
async def _liveness_flow_check(active_session_ids: set[str]) -> dict:
    """flow-level liveness：updated_at 太久没活动 → failed。

    只扫活跃 session 的 flow（session_dead 的已由 1.4.1 清掉）。
    阈值 stale_action_hours（默认 12h）。
    """
    if not active_session_ids:
        return {"stale_swept": 0}
    placeholders = ",".join("?" * len(active_session_ids))
    threshold = time.time() - TASKFLOW_INFRA["stale_action_hours"] * 3600
    cursor = db.execute(
        f"UPDATE task_flows "
        f"SET status = 'failed', terminal_reason = 'stale_swept', updated_at = ? "
        f"WHERE session_id IN ({placeholders}) "
        f"AND status IN ('running', 'waiting') "
        f"AND updated_at < ? "
        f"AND superseded_by IS NULL",
        (time.time(), *active_session_ids, threshold),
    )
    return {"stale_swept": cursor.rowcount}
```

**`updated_at` 的语义**：每次 `update_flow`（step 状态变、finish 尝试、replan、set_waiting 等）bump `updated_at = time.time()`。一个一直在活动的 flow（step 在 dispatch → `updated_at` 新）不会被清。只有真正不活动的 flow 才会被清。

**`stale_action_hours=12`** 的理由：长任务编排（多步代码改造、跨子代理研究）实际跑 6-8h 不罕见，但 12h+ 无任何 step 状态变化大概率 stuck 了。如果 LLM 知道会跑长，可以频繁调 `taskflow_progress` 查看（这会 bump `updated_at`）。

#### 1.4.3 todo-level liveness——引用者本身是僵尸

**检测**：todo 是 `in_progress`（活跃），但它引用的 flow 已被 1.4.1 或 1.4.2 清成 `failed`——todo 的引用变成了"僵尸引用"。

**这不是 sweep flow，而是 sweep todo 的引用**——把僵尸 todo 也标终态，让 todolist 干净：

```python
async def _liveness_todo_check(active_session_ids: set[str]) -> dict:
    """todo-level liveness：引用已 failed flow 的 todo → 标 cancelled。

    三色标记的补充：当 flow 被 liveness sweep 清成 failed 后，
    引用它的 active todo 变成僵尸引用——todo 说 in_progress 但 flow 已死。
    把这些 todo 也标 cancelled，让 todolist 不留僵尸。
    """
    if not active_session_ids:
        return {"todo_swept": 0}
    placeholders = ",".join("?" * len(active_session_ids))
    # 找引用了 failed flow 的 active todo
    cursor = todo_db.execute(
        f"UPDATE todos "
        f"SET status = 'cancelled' "
        f"WHERE session_id IN ({placeholders}) "
        f"AND status NOT IN ('completed', 'cancelled') "
        f"AND flow_id IS NOT NULL "
        f"AND flow_id IN ("
        f"  SELECT flow_id FROM task_flows WHERE status = 'failed'"
        f")",
        (*active_session_ids,),
    )
    return {"todo_swept": cursor.rowcount}
```

**跨 db 查询**：`todos` 在 `todos.db`，`task_flows` 在 `taskflow_registry.db`——SQLite 不能跨 db JOIN。需要先查 `taskflow_registry.db` 拿 failed flow_ids，再用这些 ids 查 `todos.db`。或者用 `ATTACH DATABASE` 临时附加（但并发安全性差）。简单做法：

```python
# 先从 taskflow db 拿 failed flow_ids
failed_flow_ids = [r["flow_id"] for r in flow_db.execute(
    f"SELECT flow_id FROM task_flows WHERE status = 'failed' "
    f"AND session_id IN ({placeholders})",
    (*active_session_ids,),
).fetchall()]
if not failed_flow_ids:
    return {"todo_swept": 0}
# 再从 todo db 标 cancelled
ph = ",".join("?" * len(failed_flow_ids))
cursor = todo_db.execute(
    f"UPDATE todos SET status = 'cancelled' "
    f"WHERE session_id IN ({placeholders}) "
    f"AND status NOT IN ('completed', 'cancelled') "
    f"AND flow_id IN ({ph})",
    (*active_session_ids, *failed_flow_ids),
)
```

#### 1.4.4 liveness sweep 整体调度

```python
async def _liveness_sweep(active_session_ids: set[str]) -> dict:
    """三重 liveness 检查，每 tick 在三色 sweep 之后跑。"""
    result = {}
    # 1. session 死了 → 所有非终态 flow failed
    result.update(await _liveness_session_check(active_session_ids))
    # 2. 活跃 session 内 flow 太久没活动 → failed
    result.update(await _liveness_flow_check(active_session_ids))
    # 3. 引用已死 flow 的 todo → cancelled（清僵尸引用）
    result.update(await _liveness_todo_check(active_session_ids))
    return result
```

**执行顺序**：三色 sweep → liveness sweep（1.4.1 → 1.4.2 → 1.4.3）。三色先清不可达（白），liveness 再清 stuck-but-reachable（黑但死）。1.4.3 最后跑——等 flow 被标 failed 后再清僵尸 todo 引用。

#### 1.4.5 跟三色的关系

| | 三色 sweep | liveness sweep |
|---|---|---|
| 判断维度 | "有没有活跃 todo 引用"（引用图） | "session/flow/todo 本身是否还活着"（心跳） |
| 清什么 | 非终态 + 不可达的 flow | 非终态 + session 死/太久没活动的 flow + 僵尸 todo |
| 防的场景 | #1, #2, #3, #5, #7 | #4, #6（+ 僵尸 todo 清理） |
| 顺序 | 先 | 后（看到三色的 failed 状态后清僵尸 todo） |
| 冲突 | 无——三色标 `failed` 是终态，liveness 的 `status IN ('running','waiting')` 跳过 | 无——liveness 标 `failed` 也是终态，下一轮三色跳过 |

### 1.5 boot reconcile（§1.4.1 的启动时特例）

§1.4.1 的 session-level liveness 每 tick 跑——**包括启动后第一 tick**。但启动到第一 tick 之间有最多 60s 窗口，boot reconcile 在这个窗口里提前清：

**文件**：`server/trigger/channels/core.py:353-396` 的 `_schedule_sweeper`

在 `sweep_orphaned_session_dirs` 之前调 `_liveness_session_check(live_session_ids)`——直接复用 §1.4.1 的逻辑，不写新函数。`live_session_ids` 在启动时从 session register 拿（刚启动，只有 `prime_mem_from_store` 恢复的 session）。

**效果**：boot 后立刻清掉上次进程崩溃留下的孤儿 flow，不等 60s。之后每 tick 的 §1.4.1 持续防。

### 1.6 prune 终态行（保留 v2 §1.9，简化）

三色和 liveness 只清非终态 flow（标 `failed`）。终态行（`done` / `failed` / `cancelled`）留作审计但会积累——需要 prune。

**文件**：`agent/tools/taskflow/registry/store_sqlite.py`

```python
async def prune_session_flows(session_id: str) -> int:
    """保留 newest max_total_flows_per_session 行。只 DELETE 终态/superseded 行。"""
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

**接入点**：`progress_push.py::push_taskflow_progress` 末尾节流 schedule（跟 v2 §1.9 相同）。`max_total_flows_per_session` 默认 200。

### 1.7 接上 `get_todos_by_flow` 反向查询（接死代码）

`get_todos_by_flow`（`todolist/store_sqlite.py:241`）之前是死代码。三色 sweep 的 mark 阶段不需要它（直接 `SELECT DISTINCT flow_id FROM todos` 查全量），但 **debugging 时有用**——看一个 flow 被哪些 todo 引用。

**不改三色 sweep 的 SQL**（它用 `SELECT DISTINCT flow_id` 更高效），但把 `get_todos_by_flow` 作为 debugging API 保留——从"死代码"变成"debugging 工具"。

### 1.8 `todowrite` 全量替换原子性要求——防三色 sweep 竞态

**问题**：`todowrite` 是全量替换——执行顺序是 DELETE ALL 旧 todo → INSERT ALL 新 todo。如果这两步不在同一个 SQLite 事务里，三色 sweep 的 mark 阶段（`SELECT DISTINCT flow_id FROM todos`）可能跑到 DELETE 之后 INSERT 之前的**空窗期**：

```
todowrite: DELETE ALL（autocommit 已提交）→ todos 表空
三色 sweep: SELECT DISTINCT flow_id → 空集合（没有任何 GC root）
三色 sweep: UPDATE task_flows SET status='failed' WHERE flow_id NOT IN (空) → 所有非终态 flow 全标 failed
todowrite: INSERT ALL 新 todo → 但 flow 已经 failed → 僵尸 todo
```

**根因**：sherry 的 SQLite 连接默认 **autocommit 模式**（`isolation_level=None`，`core.py:420` 注释确认）。如果 `todowrite` 的 DELETE 和 INSERT 是两条独立 SQL，各自 autocommit——中间窗口对 sweep 可见。

**解法**：`TodoService.update_todos`（`service.py:217`）的 DELETE ALL + INSERT ALL 必须包在一个显式事务里：

```python
# todolist/service.py::update_todos 改造
def update_todos(self, session_id: str, todos: list[dict]) -> None:
    db = _shared_db()
    with db:  # ★ BEGIN IMMEDIATE → COMMIT（原子事务）
        db.execute(f"DELETE FROM {TABLE_NAME} WHERE session_id = ?", (session_id,))
        for row in normalized_rows:
            db.execute(f"INSERT OR IGNORE INTO {TABLE_NAME} (...) VALUES (...)", row)
    # 事务提交后，sweep 的 SELECT 才能看到新 todo
```

`with db:` 上下文管理器在 sqlite3 Python 驱动里是 `BEGIN` → `COMMIT`（异常时 `ROLLBACK`）。sweep 的 `SELECT` 在另一个连接上——SQLite 的 READ COMMITTED 隔离让 sweep 只看到**已提交**的数据。`todowrite` 的事务未提交时，sweep 的 SELECT 看到旧 todo（DELETE+INSERT 还没提交）→ 正确。

**另一个竞态**：sweep 的 mark（SELECT on `todos.db`）和 sweep（UPDATE on `taskflow_registry.db`）在两个不同 db 文件——不能共享事务。`todowrite` 在 sweep 的 mark 之后 INSERT 新 todo → sweep 已有旧 marked 集合 → 可能误清新引用的 flow。

**实际影响极小**：sweep 60s 一次，`todowrite` 在 turn 内同步执行（毫秒级）。竞态窗口 = sweep tick 正好命中 `todowrite` 执行的那几毫秒。概率极低。且误清的 flow 可以 `taskflow_create(resumed_from=...)` 续。不额外加锁——复杂度不值得。

**todo 变更场景适应性总结**：

| 场景 | 三色 sweep 行为 | 正确？ |
|---|---|---|
| **添加 todo**（新 todo 引用已有 flow） | 新 todo = 新 GC root → flow 标黑 → 不 sweep | ✓ |
| **删除 todo**（全量替换不含某 todo） | 旧 todo 删 → flow 失去 GC root → 下轮 sweep 变白 → 清 | ✓ |
| **替换节点**（全量替换，新 list 含同 flow_id 不同 content） | 旧 todo 删 + 新 todo 插（同 flow_id）→ flow 仍有 GC root → 标黑 | ✓（事务内原子） |
| **状态变更**（todo 从 `pending` 变 `completed`） | 终态 todo 不是 GC root → flow 失去 root → 下轮 sweep 变白 → 清 | ✓ |
| **全量替换空窗**（DELETE 后 INSERT 前 sweep 跑了） | 事务原子性保证 sweep 看不到空窗 | ✓（§1.8 保证） |
| **新 todo 引用已 failed flow** | flow 已终态 → 三色跳过 → liveness §1.4.3 清僵尸 todo | ✓ |

### 1.9 超时自动清理——liveness = 不活动超时；现有 deadline sweeper 保留 opt-in

**问题**：v3 删了 v2 的默认 deadline（§1.2）——那还有没有超时清理？

**有，两层**：

#### 1.9.1 liveness flow-level = 不活动超时（12h）

§1.4.2 的 `_liveness_flow_check` 就是超时清理：flow 的 `updated_at` 12h+ 无活动 → `failed`。这不是 wall-clock deadline（"创建后 24h 到期"），而是 **inactivity timeout**（"12h 没人动 → 死了"）。

- 一直在活动的 flow（step 在 dispatch、finish 在尝试）→ `updated_at` 新 → 不清
- 真正 stuck 的 flow（finish 拒绝后 LLM 走开、崩溃后无人推进）→ `updated_at` 旧 → 清

**覆盖**：#4（finish 拒绝后 LLM 走开）+ #6（崩溃后无人推进）

#### 1.9.2 现有 deadline sweeper 保留 opt-in

现有 `_expire_overdue_taskflows`（`sweeper.py:132-161`）仍然保留——如果 LLM 在 `taskflow_create` 时显式传 `deadline_hours=2`，flow 会得到一个 `deadline_ts`，deadline 到期后现有 sweeper 标 `failed`。

v3 **不删** 这个现有机制——只是**不加默认 deadline**（v2 §1.2 删掉了）。原因：
- 三色标记每轮 sweep 不可达 flow → 不需要默认 deadline 兜底
- liveness 12h 不活动超时 → 覆盖 stuck-but-reachable
- 如果 LLM 知道某个 flow 应该在 2h 内完成，显式传 `deadline_hours=2` → deadline sweeper 2h 到期清——比 liveness 12h 更快

**三种超时清理的分工**：

| 机制 | 触发条件 | 清什么 | 场景 |
|---|---|---|---|
| **deadline sweeper**（现有，opt-in） | `deadline_ts < now`（LLM 显式传 deadline） | 到期的非终态 flow | LLM 知道有硬性时限（"这个任务 2h 必须完成"） |
| **liveness flow-level**（§1.4.2） | `updated_at` 12h+ 无活动 | 不活动的 running/waiting flow | LLM 走开 / 崩溃后无人推进 |
| **finish 三层预算**（§1.7） | finish 拒绝次数超限 | finish 死循环的 flow | finish Gate 一直拒绝 + 每次 bump `updated_at`（liveness 够不到） |

deadline 是**主动超时**（LLM 声明时限），liveness 是**被动超时**（不活动检测），finish 预算是**尝试超限**（拒绝次数计数）。三层互补。

**如果 LLM 不传 deadline 也不调 finish**：liveness 12h 兜底 → flow 标 `failed`。如果 LLM 一直调 finish（bump `updated_at`）但 finish 一直拒绝：finish 三层预算兜底 → 第 N 次拒绝后 auto-fail。两条路径都兜住。

---

## 2. v2 被替代/保留/新增清单

### 2.1 被三色标记 + 入口防御 + liveness 替代（删掉 v2 的这些）

| v2 机制 | v2 章节 | 为什么被替代 |
|---|---|---|
| 默认 deadline 24h | v2 §1.2 | 三色每轮 sweep 不可达 flow + 入口防御让 flow 出生即有 GC root——不需要 deadline |
| subagent death → PID liveness + flow fail hook | v2 §1.5 | §1.4.1 session-level liveness 每 tick 检查 session 是否活着——子代理 session 死了 → flow 被 liveness 清掉。不需要 PID 探针、不需要 `pid_liveness.py`、不需要改 `_finalize_cleanup` |
| bitfield durable intent（`purge_pending` 列 + reconcile） | v2 §1.10 | 三色下一轮 sweep 清 purge 残留（todo 删了 flow 没删 → 不可达 → 白 → sweep）。不需要 bitfield + reconcile sweeper |
| 独立 boot reconcile 函数 | v2 §1.4 | §1.4.1 的 session-level liveness 在启动后第一 tick 就跑——boot 只是它的特例（§1.5 复用 §1.4.1 的函数，不写新代码） |
| stale-running 独立 sweep | v2 §1.3 | §1.4.2 flow-level liveness 覆盖同一逻辑——`updated_at` 太久没活动的 flow → failed |

**净简化**：删 5 个机制 + 1 个新文件（`pid_liveness.py`）+ 1 个 schema 列（`purge_pending`）+ 3 个接入点（`_finalize_cleanup` + `session_cleanup_service.py` + 独立 `reconcile_orphan_sessions` 函数）。

### 2.2 保留（liveness + 入口防御覆盖不了的）

| 保留的机制 | 章节 | 为什么保留 |
|---|---|---|
| `taskflow_create` 强制 todo 绑定（新） | §1.1a | 入口防御——flow 出生即有 GC root，从源头防 #1 |
| 三色标记 sweep（新） | §1.1 | 统一 GC——清不可达孤儿（#1/2/3/5/7） |
| session-level liveness（新） | §1.4.1 | session 死了 → 所有 flow failed——防 #2（双重保险）+ #6（运行时崩溃） |
| flow-level liveness（新） | §1.4.2 | flow 太久没活动 → failed——防 #4（finish 拒绝）+ #6（崩溃后无人推进） |
| todo-level liveness（新） | §1.4.3 | 引用已死 flow 的僵尸 todo → cancelled——清引用图残留 |
| `mark_flow_superseded` 转终态 | §1.3 | 三色依赖它——前驱转 `cancelled` 后被 `status NOT IN (terminal)` 排除 |
| finish 三层独立预算 | §1.7 | 防 #4 finish 死循环——如果 finish 一直在尝试（bump `updated_at`），flow-level liveness 够不到。需要 finish 自身的拒绝计数器 |
| prune 终态行 | §1.6 | 三色/liveness 只标 `failed`，终态行积累——prune 按 `updated_at` 裁最旧的 |

### 2.3 新增（三色标记本身）

| 新机制 | 章节 | 代码量 |
|---|---|---|
| `_tri_color_sweep` 函数 | §1.1 | ~40 行（mark + sweep 两 SQL + 事务包装） |
| `_liveness_sweep` 函数 | §1.4 | ~15 行（一条 UPDATE SQL） |
| `reconcile_orphan_sessions` 函数 | §1.5 | ~15 行（一条 UPDATE SQL） |
| schema 加 `terminal_reason` + `updated_at` 列 | §1.1 | ~10 行（ALTER DDL + 索引） |

**总新增 ~80 行**，vs v2 的 ~200 行（8 个机制 × 各自实现 + 测试 + 接入）。

---

## 3. 关键文件清单

| 文件 | 改动 |
|---|---|
| `agent/tools/taskflow/tools/taskflow_create.py` | 加 `todo_content` 必填参数 + 创建 flow 后自动建 todo 绑定 `flow_id`/`step_id`（§1.1a） |
| `agent/tools/taskflow/tools/taskflow_plan.py` | 传 `todo_content` 给 `taskflow_create`（§1.1a） |
| `agent/tools/subagent/registry/sweeper.py` | `_do_sweep` 加 `_tri_color_sweep` + `_liveness_sweep`（§1.4.1-1.4.4 三重 liveness）调用 |
| `agent/tools/taskflow/registry/store_sqlite.py` | 加 `terminal_reason` + `updated_at` 列（ALTER DDL）；`mark_flow_superseded` 转终态（§1.3）；加 `prune_session_flows`（§1.6） |
| `agent/tools/taskflow/config.py` | `TASKFLOW_INFRA` 加 `stale_action_hours` / `max_total_flows_per_session` |
| `agent/tools/todolist/service.py` | `update_todos` 的 DELETE ALL + INSERT ALL 包在 `with db:` 显式事务里（§1.8 防三色 sweep 竞态） |
| `agent/tools/todolist/registry/store_sqlite.py` | `add_or_update_todo` 支持 `flow_id`/`step_id` auto-stamp（§1.1a 供 `taskflow_create` 调） |
| `server/trigger/channels/core.py` | `_schedule_sweeper` 启动调 `_liveness_session_check`（§1.5 复用 §1.4.1） |
| `agent/tools/taskflow/progress_push.py` | `push_taskflow_progress` 末尾节流 schedule prune（§1.6） |
| `agent/tools/taskflow/tools/taskflow_finish.py` | finish 三层独立预算（v2 §1.7 照搬） |

### 不改的文件（v2 要改但三色不需要）

| 文件 | v2 要改 | 三色为什么不需要 |
|---|---|---|
| `agent/tools/taskflow/tools/taskflow_create.py` | 默认 deadline（v2 §1.2） | 三色 sweep 不需要 deadline |
| `agent/tools/subagent/registry/lifecycle.py` | PID liveness + flow fail hook（v2 §1.5） | 三色自动清子代理不可达 flow |
| `agent/tools/subagent/pid_liveness.py`（v2 新文件） | PID 探针（v2 §1.5） | 不需要 |
| `server/service/session_cleanup_service.py` | bitfield durable intent（v2 §1.10） | 三色下一轮 sweep 清 purge 残留 |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| sweeper `_do_sweep` 调度框架 | `sweeper.py:58-129` | 加 `_tri_color_sweep` + `_liveness_sweep` 两个并行 sweep |
| `TERMINAL_STATUSES` 常量 | `taskflow/config.py:69-75` | sweep SQL 的 WHERE 复用 |
| `ALTER TABLE ADD COLUMN` 自愈 | `store_sqlite.py:107, 113` | `terminal_reason` / `updated_at` 列同模式 |
| `mark_flow_superseded` | `store_sqlite.py:357-374` | 改 SQL 加 CASE 转终态 |
| `push_taskflow_progress` 接入点 | `progress_push.py:107` | prune 节流 schedule |
| `_schedule_sweeper` 启动路径 | `server/trigger/channels/core.py:353` | boot reconcile 在这里调 |

---

## 4. 测试

| 测试 | 场景 |
|---|---|
| `test_taskflow_create_requires_todo_content` | `taskflow_create` 不传 `todo_content` → 拒绝创建，返回错误 |
| `test_taskflow_create_auto_binds_todo` | `taskflow_create(steps=..., todo_content="排查内存泄漏")` → flow 创建后，自动有一条 `pending` todo 引用该 flow 的 `flow_id` + 第一个 `step_id` |
| `test_taskflow_create_todo_flow_binding_atomic` | 如果 todo 创建失败 → flow 也回滚（不让 flow 存在而无 todo） |
| `test_tri_color_mark_collects_active_todo_flow_ids` | 2 个活跃 todo 引用 flow A/B → marked 集合 = {A, B} |
| `test_tri_color_mark_skips_terminal_todos` | 1 个 `completed` todo 引用 flow C → C 不在 marked 集合 |
| `test_tri_color_sweep_clears_unreferenced_running_flow` | flow D `running`，无活跃 todo 引用 → sweep 后 D status=`failed`，terminal_reason=`orphan_unreferenced` |
| `test_tri_color_sweep_keeps_referenced_running_flow` | flow A `running`，有活跃 todo 引用 → sweep 后 A 不变（黑） |
| `test_tri_color_sweep_skips_terminal_flows` | flow E `done`，无活跃 todo → sweep 后 E 不变（终态行留给 prune） |
| `test_tri_color_sweep_skips_superseded_flows` | flow F `running` + `superseded_by` 非空 → sweep 后 F 不变（防御性跳过） |
| `test_tri_color_sweep_multi_session` | session A 有活跃 todo 引用 flow X；session B 无 → B 的 flow 被 sweep，A 的不被 |
| `test_liveness_session_dead_clears_all_flows` | session 不在 active_session_ids → 该 session 所有非终态 flow → `failed`，terminal_reason=`session_dead` |
| `test_liveness_session_dead_skips_live_session` | session 在 active_session_ids → 该 session 的 flow 不动 |
| `test_liveness_flow_stale_clears_old_running` | flow `running`，`updated_at` 12h+ 前 → `failed`，terminal_reason=`stale_swept` |
| `test_liveness_flow_stale_keeps_recent` | flow `running`，`updated_at` 1h 前 → 不清 |
| `test_liveness_flow_stale_skips_tri_colored` | flow 被三色标 `failed` → liveness 的 `status IN ('running','waiting')` 跳过 |
| `test_liveness_flow_stale_skips_waiting_in_active_session` | flow `waiting` 在活跃 session，`updated_at` 12h+ → `failed`（waiting 也清） |
| `test_liveness_todo_zombie_cleared` | todo `in_progress` 引用已 `failed` 的 flow → todo 标 `cancelled`（清僵尸引用） |
| `test_liveness_todo_zombie_skips_completed_todos` | todo `completed` 引用 `failed` flow → 不动（已终态） |
| `test_liveness_order_tri_color_then_liveness` | 三色先清不可达 → liveness 后清 stuck-but-reachable → liveness todo 最后清僵尸引用——三步不冲突 |
| `test_mark_flow_superseded_transitions_to_cancelled` | `taskflow_create(resumed_from=F1)` → F1 status=`cancelled`，terminal_reason=`superseded` |
| `test_mark_flow_superseded_already_terminal_unchanged` | 前驱已 `done` → 只更新 `superseded_by`，status 不变 |
| `test_finish_three_layer_budget` | nudge max=2 + consecutive_failures=2 + protocol_violation=3 → 各层独立 auto-fail |
| `test_prune_keeps_active` | session 有 250 flow（200 terminal + 50 active），cap 200 → 50 个最旧 terminal 被删，50 个 active 全留 |
| `test_prune_zero_disables` | `max_total_flows_per_session=0` → 不裁 |
| `test_terminal_reason_column_migration` | 旧 db 无 `terminal_reason` 列 → ALTER 自愈加列，不崩 |
| `test_updated_at_column_migration` | 旧 db 无 `updated_at` 列 → ALTER 自愈加列，不崩 |
| `test_updated_at_bumps_on_update` | `update_flow` 写入 → updated_at > created_at |
| `test_boot_liveness_runs_before_session_dir_sweep` | `_schedule_sweeper` 启动时调 `_liveness_session_check` → 在 `sweep_orphaned_session_dirs` 之前跑 |
| `test_todowrite_atomic_full_replace` | `update_todos` 的 DELETE ALL + INSERT ALL 在一个 SQLite 事务里——sweep 的 SELECT 看不到中间空窗 |
| `test_tri_color_sweep_after_todowrite_add` | `todowrite` 添加引用 flowA 的 active todo → 下轮 sweep flowA 标黑不 sweep |
| `test_tri_color_sweep_after_todowrite_delete` | `todowrite` 全量替换不含引用 flowA 的 todo → flowA 失去 root → 下轮 sweep 清 |
| `test_tri_color_sweep_after_todowrite_status_change` | todo 从 `pending` 变 `completed` → 不再是 GC root → flow 失去 root → sweep |
| `test_liveness_zombie_todo_after_sweep` | todo 引用被三色 sweep 标 `failed` 的 flow → liveness §1.4.3 把 todo 标 `cancelled` |
| `test_liveness_inactivity_timeout_clears_stale` | flow `running`，`updated_at` 12h+ → `failed`（不活动超时） |
| `test_liveness_inactivity_keeps_active_flow` | flow `running`，`updated_at` 1h 前（一直在活动）→ 不清 |
| `test_deadline_sweeper_opt_in` | LLM 传 `deadline_hours=2` → `deadline_ts` 设定 → 2h 后现有 deadline sweeper 标 `failed` |
| `test_no_default_deadline` | LLM 不传 `deadline_hours` → `deadline_ts` 为 NULL → deadline sweeper 跳过（靠 liveness 兜底） |
| `test_finish_budget_catches_active_loop` | finish 一直拒绝 + 每次 bump `updated_at` → liveness 够不到（`updated_at` 新）→ finish 三层预算第 N 次拒绝后 auto-fail |

---

## 5. 数据流总览

```
入口防御（创建时）：
  taskflow_create(steps=..., todo_content="排查内存泄漏")
    → 创建 flow + 自动建 pending todo 绑定 flow_id/step_id
    → flow 出生即有 GC root（三色标黑，不被 sweep）

每 sweeper tick（60s）：
  │
  ├─ _tri_color_sweep（统一 GC 基础层）
  │   │  for each active session_id:
  │   │    BEGIN IMMEDIATE
  │   │    mark: SELECT DISTINCT flow_id FROM todos WHERE status NOT IN (terminal) AND flow_id IS NOT NULL
  │   │    sweep: UPDATE task_flows SET status='failed' WHERE status NOT IN (terminal)
  │   │           AND superseded_by IS NULL AND flow_id NOT IN (marked)
  │   │    COMMIT
  │   └─ 清不可达孤儿（#1/2/3/5/7）
  │
  ├─ _liveness_sweep（liveness 补充层，三重检查）
  │   ├─ 1.4.1 session-level：session 不在活跃集合 → 所有非终态 flow → failed
  │   │   └─ 防 #2（子代理死）+ #6（运行时崩溃）
  │   ├─ 1.4.2 flow-level：活跃 session 内 updated_at 12h+ 无活动 → failed
  │   │   └─ 防 #4（finish 拒绝）+ #6（崩溃后无人推进）
  │   └─ 1.4.3 todo-level：引用已 failed flow 的 active todo → cancelled
  │       └─ 清僵尸引用，让 todolist 干净
  │
  └─ _maybe_prune（存量封顶）
      DELETE FROM task_flows WHERE terminal/superseded AND rowid NOT IN (newest 200)

启动时（boot，§1.4.1 特例）：
  _liveness_session_check(live_session_ids) → 在 sweep_orphaned_session_dirs 之前跑
    └─ 清崩溃残留（#6），不等第一 tick

入口防御（保留 v2）：
  mark_flow_superseded 转终态（§1.3）→ 前驱变 cancelled → 三色/liveness 都跳过终态
  finish 三层预算（§1.7）→ finish 死循环 auto-fail → 变 failed → 三色/liveness 跳过

原子性保证（§1.8）：
  todowrite 全量替换 = 一个 SQLite 事务（BEGIN → DELETE ALL → INSERT ALL → COMMIT）
  → 三色 sweep 的 SELECT 看不到中间空窗（READ COMMITTED）

超时清理分工（§1.9）：
  deadline sweeper（现有，opt-in）→ LLM 显式传 deadline_hours → 到期清
  liveness flow-level（§1.4.2）→ 12h 不活动 → 清（被动超时）
  finish 三层预算（§1.7）→ N 次拒绝 → auto-fail（尝试超限）
```

---

## 6. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| 三色 sweep 误清有活跃 todo 引用的 flow | `flow_id NOT IN (marked)` 条件保证有活跃引用的 flow 不被清。marked 集合来自 `SELECT DISTINCT flow_id FROM todos WHERE status NOT IN (terminal)` —— 只要 todo 是 `pending`/`in_progress`，它引用的 flow 就被标黑 |
| 三色 sweep 在 todo 表和 flow 表不同 db 文件时事务不原子 | todo 在 `todos.db`，flow 在 `taskflow_registry.db`——两个 SQLite 文件不能共享事务。三色 sweep 只在 flow 表的事务里读 todo 表的 marked 集合（跨 db 只读 SELECT，不要求原子）。如果 sweep 过程中 todo 表变了（新 todo 创建引用了一个白 flow），那个 flow 可能被误清。**缓解**：sweeper tick 60s 一次，todo 变更频率远低于此——race 窗口极小。如果误清了，下一轮 `taskflow_create(resumed_from=…)` 可续 |
| liveness sweep 误清正在跑的长 flow | `stale_action_hours=12` 给足回旋；`updated_at` 每次 mutation bump——一直在活动的 flow 不会被清。如果真跑了 12h+ 没活动，大概率确实 stuck 了 |
| boot reconcile 误清别的 session 的活 flow | `reconcile_orphan_sessions` 只扫 `session_id NOT IN (live_session_ids)` ——只清 session 注册表里不存在的 session 的 flow。活跃 session 的 flow 不动 |
| LLM 给 flow 填了 `in_progress` todo 但实际不再用 → flow 被保活（不 sweep） | 这是"引用者自己判断错误"的场景——三色看引用图不判断引用合理性。LLM 后来把 todo 标 `completed` → 下一轮 sweep flow 变白 → 清掉。可接受的延迟 |
| `terminal_reason` 列迁移 | `ALTER TABLE ADD COLUMN` 自愈（同 `_DEADLINE_COLUMN_DDL` 模式），旧行 `terminal_reason = NULL` |
| 三色 sweep + liveness sweep 同一 tick 里顺序 | 三色先（清不可达）→ liveness 后（清 stuck-but-reachable）。三色标的 `failed` 是终态，liveness 的 `status IN ('running', 'waiting')` 跳过——不冲突 |

---

## 7. 优先级与分阶段落地

**P0（三色标记 + schema + superseded 转终态）**：
- §1.1 `_tri_color_sweep` 函数 + `_do_sweep` 接入
- schema 加 `terminal_reason` + `updated_at` 列
- §1.3 `mark_flow_superseded` 转终态

落地后防 5/7 场景（#1/2/3/5/7）。

**P1（liveness 补充）**：
- §1.4 `_liveness_sweep`（stale-running）
- §1.5 `reconcile_orphan_sessions`（boot reconcile）
- §1.7 finish 三层独立预算

落地后防剩下 2/7（#4/6）。

**P2（存量封顶 + 健壮性）**：
- §1.6 prune 终态行
- `get_todos_by_flow` 作为 debugging API 保留
- 测试覆盖所有边界

---

## 8. 一句话

用 JVM 三色标记法做 taskflow 孤儿 GC + 入口强制 todo 绑定 + 三重 liveness 检查 + todowrite 原子性保证 + 三层超时分工。**入口防御**：`taskflow_create` 加必填 `todo_content`，创建 flow 后自动建 `pending` todo 绑定 `flow_id`/`step_id`——flow 出生即有 GC root。**三色标记**：每 sweeper tick 一个 SQLite 事务，mark = 活跃 todo 引用的 flow_id，sweep = 非终态 + 不可达 + 未被 supersede → `failed`——一个 SQL 防 5/7 场景。**三重 liveness**：① session-level（session 死了 → flow failed）② flow-level（12h 不活动 → failed，**被动超时**）③ todo-level（僵尸 todo → cancelled）。**todowrite 原子性**（§1.8）：全量替换 DELETE+INSERT 在一个 SQLite 事务里——防三色 sweep 的 mark 阶段跑到空窗期误清全部 flow。**三层超时分工**（§1.9）：deadline sweeper（现有 opt-in，主动超时）+ liveness flow-level（被动超时）+ finish 三层预算（尝试超限）——互补兜底。替代 v2 的默认 deadline + subagent death hook + PID liveness + bitfield durable intent + 独立 boot reconcile 5 个机制，新增 ~120 行 vs v2 ~200 行。
