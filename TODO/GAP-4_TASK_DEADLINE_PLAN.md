# GAP-4 任务级截止时间 — 实施计划

> 日期: 2026-09-11
> 来源: LONG_RUNNING_TASK_GAP_ANALYSIS.md §4 (行 94-108)
> 决策: task_flows 表新增 deadline_ts 列 + taskflow_create 支持 deadline_hours + sweeper 扫描
> 预估工时: 1 天

---

## 1. 问题

子代理有 `run_timeout_seconds`（单次执行超时），但 TaskFlow 没有整体截止时间。一个跨多会话的任务可以无限期运行，占用资源。

**当前状态：**

- `task_flows` 表无 `deadline_ts` 列
- `taskflow_create`（`agent/tools/taskflow/tools/taskflow_create.py:12`）无 `deadline_hours` 参数
- Sweeper（`agent/tools/subagent/registry/sweeper.py`）仅扫描子代理 orphan/stale 状态，不扫描 TaskFlow 截止时间
- 无截止时间告警机制

---

## 2. 设计决策

### 2.1 DB 迁移：task_flows 表新增 deadline_ts 列

```sql
ALTER TABLE task_flows ADD COLUMN deadline_ts REAL;
```

`deadline_ts` 为 Unix 时间戳（`time.time()` 格式）。NULL = 无截止时间。

### 2.2 关键决策

| 决策点   | 选择                                             | 理由                                            |
| -------- | ------------------------------------------------ | ----------------------------------------------- |
| 存储     | `deadline_ts REAL` 列                            | 时间戳可 SQL 比较，NULL 表示无截止时间          |
| 设置时机 | `taskflow_create` 时设置                         | 创建时声明截止时间，不可后续修改（语义清晰）    |
| 参数     | `deadline_hours: float`                          | 用户友好（小时），内部转为时间戳                |
| 超限处理 | Sweeper 扫描 → 标记 failed + 日志告警            | 非阻塞标记，不主动取消（让模型/用户决定下一步） |
| 超限通知 | 日志 + taskflow_summary 显示 "DEADLINE_EXCEEDED" | 不发 WS 消息（避免复杂的事件管线）              |

---

## 3. 涉及文件

| 文件                                             | 操作                              | 预估行数 |
| ------------------------------------------------ | --------------------------------- | -------- |
| `agent/tools/taskflow/registry/store_sqlite.py`  | 修改：schema + migration + update | ~20      |
| `agent/tools/taskflow/tools/taskflow_create.py`  | 修改：新增 deadline_hours 参数    | ~10      |
| `agent/tools/taskflow/tools/taskflow_summary.py` | 修改：显示截止时间状态            | ~8       |
| `agent/tools/subagent/registry/sweeper.py`       | 修改：新增 TaskFlow deadline 扫描 | ~30      |
| `tests/agent/tools/taskflow/test_deadline.py`    | **新建**                          | ~80      |

---

## 4. 详细设计

### 4.1 store_sqlite.py — Schema 迁移

```python
# _CREATE_TABLE_SQL 新增列：
_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL
);
"""

# _SELECT_COLUMNS_SQL 新增列（在 GAP-3 基础上追加）：
_SELECT_COLUMNS_SQL = (
    f"SELECT flow_id, state_json, wait_json, expected_revision, status, "
    f"child_session_key, total_tokens, total_cost, token_budget, deadline_ts "
    f"FROM {TABLE_NAME}"
)

# _row_to_flow 新增字段：
def _row_to_flow(row: tuple) -> dict:
    (
        flow_id, state_json, wait_json, expected_revision, status,
        child_session_key, total_tokens, total_cost, token_budget, deadline_ts,
    ) = row
    return {
        ...,
        "deadline_ts": float(deadline_ts) if deadline_ts is not None else None,
    }
```

Migration 函数（与 GAP-3 的 `_ensure_token_columns` 合并或单独）：

```python
def _ensure_deadline_column(db) -> None:
    """Additive migration: add deadline_ts column if absent."""
    try:
        db.execute("ALTER TABLE task_flows ADD COLUMN deadline_ts REAL")
    except Exception:
        pass
```

`update_flow()` 新增 `deadline_ts` 参数支持（与 GAP-3 的 token 字段同模式）。

### 4.2 taskflow_create.py — 新增 deadline_hours 参数

```python
import time

@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: SessionId = "",
    deadline_hours: float | None = None,  # ← 新增
) -> str:
    """Create a durable task flow and return its initial revision.

    A flow tracks multi-step work across turns with optimistic locking: every
    mutation bumps expected_revision, so concurrent writers are detected via
    revision conflicts instead of silent last-write-wins.

    Pass deadline_hours to set an overall deadline (e.g. 24.0 for 24 hours).
    The sweeper will mark the flow as failed when the deadline passes.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    creator_key = requester_session_key(session_id) if session_id else ""
    state = default_state(description, initial_state, creator_session_key=creator_key)

    deadline_ts = None
    if deadline_hours is not None and deadline_hours > 0:
        deadline_ts = time.time() + (deadline_hours * 3600)

    try:
        flow = await store_sqlite.create_flow(
            flow_id, state, deadline_ts=deadline_ts,
        )
    except FlowExistsError:
        existing = await store_sqlite.get_flow(flow_id)
        revision = existing["expected_revision"] if existing else INITIAL_REVISION
        return (
            f"Error: TaskFlow '{flow_id}' already exists (revision={revision}). "
            "Re-read it with taskflow_summary."
        )

    deadline_text = f", deadline={time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline_ts))}" if deadline_ts else ""
    return (
        f"TaskFlow created: flow_id={flow['flow_id']}, status={flow['status']}, "
        f"revision={flow['expected_revision']}{deadline_text}"
    )
```

`create_flow()` 也需要接受 `deadline_ts` 参数并写入 DB。

### 4.3 taskflow_summary.py — 显示截止时间状态

在 `taskflow_summary` 的输出中追加：

```python
    # 截止时间状态（GAP-4）
    deadline_ts = flow.get("deadline_ts")
    if deadline_ts:
        now = time.time()
        if now > deadline_ts and flow["status"] not in TERMINAL_STATUSES:
            lines.append(f"deadline: EXCEEDED (was {time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline_ts))})")
        else:
            remaining_h = (deadline_ts - now) / 3600
            if remaining_h > 0:
                lines.append(f"deadline: {time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline_ts))} ({remaining_h:.1f}h remaining)")
            else:
                lines.append(f"deadline: {time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline_ts))} (passed)")
```

### 4.4 sweeper.py — 新增 TaskFlow deadline 扫描

在 `_do_sweep()` 中新增 TaskFlow deadline 检查：

```python
async def _do_sweep() -> None:
    # ... 现有逻辑 ...

    # === 新增：TaskFlow deadline 扫描 (GAP-4) ===
    expired = await _expire_overdue_taskflows()
    if expired > 0:
        logger.warning("Sweeper: {} TaskFlow(s) exceeded deadline, marked as failed", expired)

    # ... 现有 persist 逻辑 ...
```

```python
async def _expire_overdue_taskflows() -> int:
    """Scan for TaskFlows whose deadline has passed and mark them failed."""
    import time
    from agent.tools.taskflow.config import TaskFlowStatus, TERMINAL_STATUSES
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    try:
        await taskflow_store.ensure_db()
        # 同步查询所有有 deadline 且非终态的 flow
        # 由于 sweeper 已在 async 上下文中，可直接用 async API
        all_flows = await _get_all_non_terminal_flows_with_deadline(taskflow_store)
        expired_count = 0
        now = time.time()
        for flow in all_flows:
            deadline = flow.get("deadline_ts")
            if deadline and now > deadline:
                try:
                    state = dict(flow.get("state") or {})
                    state["failure_reason"] = f"Deadline exceeded: {time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline))}"
                    await taskflow_store.update_flow(
                        flow["flow_id"],
                        flow["expected_revision"],
                        state=state,
                        status=TaskFlowStatus.FAILED.value,
                    )
                    expired_count += 1
                    logger.warning(
                        "TaskFlow '{}' deadline exceeded, marked failed",
                        flow["flow_id"],
                    )
                except Exception as e:
                    logger.warning("Failed to expire TaskFlow '{}': {}", flow["flow_id"], e)
        return expired_count
    except Exception as e:
        logger.warning("TaskFlow deadline sweep failed: {}", e)
        return 0
```

需要 `store_sqlite.py` 新增查询方法：

```python
async def get_overdue_flows(now_ts: float) -> list[dict]:
    """Return non-terminal flows whose deadline_ts < now_ts."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL
            + f" WHERE deadline_ts IS NOT NULL AND deadline_ts < ? "
            f"AND status NOT IN ({','.join('?' for _ in TERMINAL_STATUSES)})",
            (now_ts, *TERMINAL_STATUSES),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_flow(row) for row in rows]
```

---

## 5. 实施顺序

```
Step 1: store_sqlite.py — _CREATE_TABLE_SQL 新增 deadline_ts + migration
Step 2: store_sqlite.py — _SELECT_COLUMNS_SQL + _row_to_flow 新增字段
Step 3: store_sqlite.py — create_flow() 接受 deadline_ts 参数
Step 4: store_sqlite.py — 新增 get_overdue_flows()
Step 5: taskflow_create.py — 新增 deadline_hours 参数
Step 6: taskflow_summary.py — 显示截止时间状态
Step 7: sweeper.py — 新增 _expire_overdue_taskflows()
Step 8: tests/agent/tools/taskflow/test_deadline.py — 编写测试
Step 9: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

| 测试                                   | 说明                                                     |
| -------------------------------------- | -------------------------------------------------------- |
| `test_create_with_deadline`            | deadline_hours=24 → deadline_ts ≈ now+24h                |
| `test_create_without_deadline`         | deadline_hours=None → deadline_ts=None                   |
| `test_summary_shows_deadline`          | 有 deadline → summary 包含 "deadline:" 行                |
| `test_summary_shows_exceeded`          | deadline 过期 + 非终态 → summary 包含 "EXCEEDED"         |
| `test_summary_no_deadline`             | deadline_ts=None → summary 不包含 deadline 行            |
| `test_sweeper_expires_overdue`         | 模拟时间过期 → sweeper 标记 failed                       |
| `test_sweeper_skips_terminal`          | 已终态的过期 flow → 不重复处理                           |
| `test_sweeper_skips_no_deadline`       | 无 deadline 的 flow → 不处理                             |
| `test_expired_flow_has_failure_reason` | 过期标记后 state.failure_reason 包含 "Deadline exceeded" |

### 运行验证

```bash
python -m pytest tests/agent/tools/taskflow/test_deadline.py -v
python -m pytest tests/agent/tools/taskflow/ -v  # 全部 taskflow 测试
ruff check agent/tools/taskflow/registry/store_sqlite.py agent/tools/taskflow/tools/taskflow_create.py agent/tools/subagent/registry/sweeper.py
```

---

## 7. 数据流

```
创建带截止时间的 flow:
  taskflow_create(flow_id="urgent-fix", description="hotfix deploy", deadline_hours=4)
  → deadline_ts = time.time() + 4*3600 = 1726000000
  → create_flow("urgent-fix", state, deadline_ts=1726000000)
  → 返回 "TaskFlow created: ... deadline=2026-09-11 16:00"

Sweeper 定期扫描:
  → get_overdue_flows(now=time.time())
  → 发现 "urgent-fix" 的 deadline_ts < now 且 status=running
  → update_flow("urgent-fix", revision, state={...,failure_reason="Deadline exceeded"}, status="failed")
  → 日志: "TaskFlow 'urgent-fix' deadline exceeded, marked failed"

用户查询:
  taskflow_summary(flow_id="urgent-fix")
  → 显示 "status=failed, deadline: EXCEEDED (was 2026-09-11 16:00)"
  → state.failure_reason: "Deadline exceeded: 2026-09-11 16:00"
```

---

## 8. 与其他方案的关系

| 方案             | 关系                                                         |
| ---------------- | ------------------------------------------------------------ |
| **#3 预算跟踪**  | 互补。#3 管 token，#4 管时间，双重资源控制。                 |
| **#11 空闲检测** | 协作。#11 检测 WAITING 超时；#4 检测整体截止时间。不同维度。 |
| **#8 重试策略**  | 协作。超时 flow 如果有重试策略可重新 dispatch。              |
| **LT-2**         | 依赖。LT-2 注入未完成 flow 时可显示截止时间状态。            |

---

## 9. 风险与缓解

| 风险                        | 缓解                                                                  |
| --------------------------- | --------------------------------------------------------------------- |
| DB migration 失败           | Additive ALTER TABLE + try/except                                     |
| Sweeper 不在运行            | deadline 过期的 flow 不会被自动标记，但 taskflow_summary 仍显示状态   |
| deadline_hours = 0 或负数   | 参数校验：仅正数才设置 deadline_ts                                    |
| 乐观锁冲突导致过期标记失败  | try/except 记录日志，下次 sweeper 周期重试                            |
| 用户不知情任务被标记 failed | 日志告警 + taskflow_summary 显示 "DEADLINE_EXCEEDED" + failure_reason |
