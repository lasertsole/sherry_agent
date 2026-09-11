# GAP-11 空闲任务检测 — 实施计划

> 日期: 2026-09-11
> 来源: LONG_RUNNING_TASK_GAP_ANALYSIS.md §11 (行 233-249)
> 决策: state_json 存 waiting_since_ts + sweeper 扫描 + 自动标记/告警
> 预估工时: 1 天

---

## 1. 问题

TaskFlow 有 WAITING 状态（`taskflow_set_waiting`），但无超时检测。一个 WAITING 了 3 天的任务不会被标记异常。任务卡在等待子代理结果，子代理崩溃后任务永远 WAITING。

**当前状态：**

- `taskflow_set_waiting`（`agent/tools/taskflow/tools/taskflow_set_waiting.py:14`）设置 `status=waiting` + `wait` payload
- `taskflow_resume` 清除 `wait` + 返回 `status=running`
- `wait` payload 有 `set_at` 时间戳（`taskflow_set_waiting.py:38`），但无超时检测逻辑
- Sweeper（`agent/tools/subagent/registry/sweeper.py`）扫描子代理 orphan/stale，不扫描 TaskFlow WAITING 状态
- 无 `waiting_since_ts` 字段（`wait.set_at` 可复用但语义不明确）

---

## 2. 设计决策

### 2.1 复用 wait.set_at 作为 waiting_since_ts

`wait` payload 已有 `set_at: time.time()`（`taskflow_set_waiting.py:38`）。无需新增列或字段，直接用 `wait.set_at` 作为等待起始时间。

### 2.2 关键决策

| 决策点     | 选择                                          | 理由                                      |
| ---------- | --------------------------------------------- | ----------------------------------------- |
| 等待时间戳 | 复用 `wait.set_at`                            | 已有字段，零迁移                          |
| 超时阈值   | `WAITING_TIMEOUT_HOURS = 24`（可配置）        | 与 sweeper 的 interactive TTL 一致（24h） |
| 超时处理   | 检查子代理是否活跃 → 不活跃则标记 needs_retry | 先检查再行动，避免误杀正在运行的子代理    |
| 告警方式   | 日志 + taskflow_summary 显示 "WAITING_STALE"  | 非阻塞，不自动 failed                     |
| 扫描位置   | Sweeper `_do_sweep()` 新增 TaskFlow 扫描      | 复用已有周期性扫描基础设施                |

---

## 3. 涉及文件

| 文件                                                | 操作                             | 预估行数 |
| --------------------------------------------------- | -------------------------------- | -------- |
| `config/num.py`                                     | 修改：新增 WAITING_TIMEOUT_HOURS | ~2       |
| `agent/tools/taskflow/tools/taskflow_summary.py`    | 修改：显示 WAITING 超时状态      | ~10      |
| `agent/tools/subagent/registry/sweeper.py`          | 修改：新增 TaskFlow WAITING 扫描 | ~40      |
| `tests/agent/tools/taskflow/test_idle_detection.py` | **新建**                         | ~80      |

---

## 4. 详细设计

### 4.1 config/num.py — 新增阈值

```python
# === Idle TaskFlow Detection (GAP-11) ===
WAITING_TIMEOUT_HOURS = 24  # TaskFlow WAITING 超过此时间则标记 stale
```

### 4.2 taskflow_summary.py — 显示 WAITING 超时状态

在 `taskflow_summary` 中，`wait` 非空时追加超时检测：

```python
    if flow["wait"] is not None:
        wait_payload = flow["wait"]
        lines.append(f"wait: {json.dumps(wait_payload, ensure_ascii=False)}")

        # === 新增：WAITING 超时检测 (GAP-11) ===
        set_at = wait_payload.get("set_at")
        if set_at:
            from config.num import WAITING_TIMEOUT_HOURS

            now = time.time()
            waiting_secs = now - float(set_at)
            timeout_secs = WAITING_TIMEOUT_HOURS * 3600
            if waiting_secs > timeout_secs:
                waiting_h = waiting_secs / 3600
                lines.append(
                    f"wait_status: STALE (waiting {waiting_h:.1f}h, "
                    f"timeout={WAITING_TIMEOUT_HOURS}h) — child session may have crashed; "
                    f"consider taskflow_resume with a failure result or re-dispatch"
                )
            else:
                remaining_h = (timeout_secs - waiting_secs) / 3600
                lines.append(f"wait_status: active (waiting {waiting_secs/3600:.1f}h, {remaining_h:.1f}h to timeout)")
```

### 4.3 sweeper.py — 新增 TaskFlow WAITING 扫描

在 `_do_sweep()` 中新增：

```python
async def _do_sweep() -> None:
    # ... 现有逻辑 ...

    # === 新增：TaskFlow WAITING 超时扫描 (GAP-11) ===
    stale = await _scan_stale_waiting_taskflows()
    if stale > 0:
        logger.warning("Sweeper: {} TaskFlow(s) in stale WAITING state", stale)

    # ... 现有 persist 逻辑 ...
```

```python
async def _scan_stale_waiting_taskflows() -> int:
    """Scan TaskFlows in WAITING state whose wait.set_at exceeds the timeout.

    For each stale flow, check if the associated child session is still live.
    If the child is dead, log a warning and add a stale marker to the wait payload
    so taskflow_summary can surface it. Does NOT auto-fail the flow.
    """
    import time
    from config.num import WAITING_TIMEOUT_HOURS
    from agent.tools.taskflow.config import TaskFlowStatus
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    try:
        await taskflow_store.ensure_db()
        waiting_flows = await _get_waiting_flows(taskflow_store)
        if not waiting_flows:
            return 0

        now = time.time()
        timeout_secs = WAITING_TIMEOUT_HOURS * 3600
        stale_count = 0

        for flow in waiting_flows:
            wait = flow.get("wait") or {}
            set_at = wait.get("set_at")
            if not set_at:
                continue

            waiting_secs = now - float(set_at)
            if waiting_secs <= timeout_secs:
                continue  # Not stale yet

            # Check if the child session is still live
            child_key = flow.get("child_session_key")
            child_live = False
            if child_key:
                try:
                    from agent.tools.subagent.registry.memory import get_run_by_child_session_key
                    from agent.tools.subagent.registry import is_live_unended_run

                    run = get_run_by_child_session_key(child_key)
                    child_live = run is not None and is_live_unended_run(run)
                except Exception:
                    pass  # If we can't check, assume not live

            if child_live:
                # Child is still running; just approaching timeout
                logger.debug(
                    "TaskFlow '{}' waiting {:.1f}h but child still live",
                    flow["flow_id"], waiting_secs / 3600,
                )
                continue

            # Child is dead or unknown → mark as stale
            stale_count += 1
            logger.warning(
                "TaskFlow '{}' in stale WAITING ({:.1f}h, child={}): "
                "child session appears inactive; consider resume or re-dispatch",
                flow["flow_id"], waiting_secs / 3600, child_key,
            )

            # Add stale marker to wait payload (non-destructive: just adds a field)
            try:
                wait["stale_detected_at"] = now
                wait["stale_child_session_key"] = child_key
                await taskflow_store.update_flow(
                    flow["flow_id"],
                    flow["expected_revision"],
                    wait=wait,
                )
            except Exception as e:
                logger.debug(
                    "Could not mark TaskFlow '{}' as stale: {}",
                    flow["flow_id"], e,
                )

        return stale_count
    except Exception as e:
        logger.warning("TaskFlow WAITING sweep failed: {}", e)
        return 0
```

需要 `store_sqlite.py` 新增查询方法：

```python
async def get_waiting_flows() -> list[dict]:
    """Return all flows in WAITING status."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + f" WHERE status = ?",
            (TaskFlowStatus.WAITING.value,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_flow(row) for row in rows]
```

`store_sqlite.py` 需要导入 `TaskFlowStatus`（如果 GAP-3 已添加则跳过）。

---

## 5. 实施顺序

```
Step 1: config/num.py — 新增 WAITING_TIMEOUT_HOURS
Step 2: store_sqlite.py — 新增 get_waiting_flows() (async)
Step 3: taskflow_summary.py — 显示 WAITING 超时状态
Step 4: sweeper.py — 新增 _scan_stale_waiting_taskflows()
Step 5: tests/agent/tools/taskflow/test_idle_detection.py — 编写测试
Step 6: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

| 测试                                | 说明                                                   |
| ----------------------------------- | ------------------------------------------------------ |
| `test_summary_shows_active_waiting` | WAITING 但未超时 → "wait_status: active"               |
| `test_summary_shows_stale`          | WAITING 且超时 → "wait_status: STALE"                  |
| `test_summary_no_wait_payload`      | wait=None → 不显示 wait_status                         |
| `test_sweeper_detects_stale`        | WAITING + set_at 过期 → stale_count=1                  |
| `test_sweeper_skips_active_child`   | WAITING + 超时 + child 仍活跃 → 不标记 stale           |
| `test_sweeper_marks_dead_child`     | WAITING + 超时 + child 不活跃 → 标记 stale_detected_at |
| `test_sweeper_skips_recent_waiting` | WAITING + set_at 未超时 → stale_count=0                |
| `test_sweeper_no_waiting_flows`     | 无 WAITING flow → 返回 0                               |
| `test_stale_marker_in_wait_payload` | stale 标记后 wait.stale_detected_at 存在               |

### 运行验证

```bash
python -m pytest tests/agent/tools/taskflow/test_idle_detection.py -v
python -m pytest tests/agent/tools/taskflow/ -v
ruff check agent/tools/taskflow/tools/taskflow_summary.py agent/tools/subagent/registry/sweeper.py
```

---

## 7. 数据流

```
TaskFlow 进入 WAITING:
  taskflow_set_waiting(flow_id="deploy-v2", wait_reason="awaiting child deploy result")
  → wait = {"reason": "...", "set_at": 1726000000}
  → status = "waiting"

Sweeper 每周期扫描:
  → get_waiting_flows() → [{"flow_id":"deploy-v2","wait":{"set_at":1726000000},...}]
  → waiting_secs = now - 1726000000 = 90000s (25h)
  → 90000 > 24*3600=86400 → 超时
  → 检查 child_session_key = "agent:main:session:child-1"
  → get_run_by_child_session_key("agent:main:session:child-1") → None (child 已死)
  → 标记 stale: wait["stale_detected_at"] = now
  → 日志: "TaskFlow 'deploy-v2' in stale WAITING (25.0h, child=agent:main:session:child-1)"

用户查询:
  taskflow_summary(flow_id="deploy-v2")
  → "wait_status: STALE (waiting 25.0h, timeout=24h) — child session may have crashed"
  → "consider taskflow_resume with a failure result or re-dispatch"

模型恢复:
  taskflow_resume(flow_id="deploy-v2", child_session_key="agent:main:session:child-1",
                  result="Error: child session crashed (stale waiting)")
  → status 恢复为 running, wait 清除
```

---

## 8. 与其他方案的关系

| 方案              | 关系                                                                       |
| ----------------- | -------------------------------------------------------------------------- |
| **#4 截止时间**   | 协作。#11 检测 WAITING 超时（子代理崩溃），#4 检测整体截止时间。不同维度。 |
| **#5 检查点恢复** | 协作。#11 检测到 stale 后，#5 的 `taskflow_resume_from` 执行实际恢复。     |
| **#8 重试策略**   | 协作。检测到 stale 后，有重试策略的 flow 可自动重新 dispatch。             |
| **Sweeper**       | 扩展。复用已有 sweeper 基础设施，新增 TaskFlow 扫描分支。                  |

---

## 9. 风险与缓解

| 风险                         | 缓解                                                            |
| ---------------------------- | --------------------------------------------------------------- |
| 子代理存活检测误判           | `get_run_by_child_session_key` + `is_live_unended_run` 双重检查 |
| sweeper 不在运行             | `taskflow_summary` 独立计算超时（不依赖 sweeper 标记）          |
| 乐观锁冲突标记 stale 失败    | try/except 日志，下次 sweeper 周期重试                          |
| wait payload 已有 stale 字段 | `stale_detected_at` 覆盖更新，不累积                            |
| 误杀长时间合理等待           | 24h 阈值可配置（`WAITING_TIMEOUT_HOURS`）；仅标记不自动 failed  |
