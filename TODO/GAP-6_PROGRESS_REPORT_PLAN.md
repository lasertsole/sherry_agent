# GAP-6 进度报告工具 — 实施计划

> 日期: 2026-09-11
> 来源: LONG_RUNNING_TASK_GAP_ANALYSIS.md §6 (行 133-148)
> 决策: 新增 taskflow_progress 工具，复用现有 steps_summary()
> 预估工时: 0.5 天

---

## 1. 问题

`taskflow_summary` 返回原始的 steps/results 列表（含 step 状态计数），但没有面向用户的进度报告（"已完成 60%，预计还需 2 小时"）。长程任务中用户无法快速了解进度，必须阅读原始步骤列表。

**当前状态：**

- `taskflow_summary`（`agent/tools/taskflow/tools/taskflow_summary.py:16`）输出步骤列表 + `step_statuses` 计数
- `steps_summary()`（`agent/tools/taskflow/tools/_shared.py:227`）已实现 `{blocked, ready, dispatched, done}` 计数
- 步骤有 `dispatched_at` 时间戳（`taskflow_run_task.py:110` 写入），可用于估算耗时
- 无专用进度报告工具

---

## 2. 设计决策

### 2.1 纯只读工具，无 DB 变更

`taskflow_progress` 是 `taskflow_summary` 的人类可读变体：读 flow state → 格式化进度报告。不修改任何状态，不需要 DB 迁移。

### 2.2 关键决策

| 决策点       | 选择                                      | 理由                                        |
| ------------ | ----------------------------------------- | ------------------------------------------- |
| 工具入口     | 新增 `taskflow_progress` 工具             | 与 `taskflow_summary` 并列，职责分离        |
| 进度计算     | 复用 `steps_summary()` + `step_status()`  | 已有函数，零新增逻辑                        |
| 预估剩余时间 | 基于已完成步骤的平均 `dispatched_at` 间隔 | 仅在 ≥2 个 done 步骤有 dispatched_at 时估算 |
| 输出格式     | 结构化 + 人类可读                         | 面向用户（区别于 summary 面向模型）         |

---

## 3. 涉及文件

| 文件                                                   | 操作           | 预估行数 |
| ------------------------------------------------------ | -------------- | -------- |
| `agent/tools/taskflow/tools/taskflow_progress.py`      | **新建**       | ~80      |
| `agent/tools/taskflow/tools/__init__.py`               | 修改：注册工具 | ~3       |
| `agent/tools/taskflow/__init__.py`                     | 修改：导出     | ~2       |
| `tests/agent/tools/taskflow/test_taskflow_progress.py` | **新建**       | ~100     |

---

## 4. 详细设计

### 4.1 agent/tools/taskflow/tools/taskflow_progress.py — 新建

```python
"""taskflow_progress: structured progress report for a task flow.

A human-readable companion to taskflow_summary: instead of raw steps/results,
returns a concise progress report with completion percentage, step counts,
next actionable steps, and estimated remaining time based on completed step
durations.
"""

import time

from langchain_core.tools import tool

from ..registry import store_sqlite
from ._shared import not_found_error, step_status, steps_summary

_STEP_ICONS = {
    "done": "✓",
    "dispatched": "→",
    "ready": "○",
    "blocked": "⊘",
}


@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str:
    """Get a structured progress report for a task flow.

    Returns completion percentage, step status breakdown, next actionable
    steps, and estimated remaining time (based on average completed step
    duration). Read-only.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)

    state = flow.get("state") or {}
    steps = state.get("steps") or []
    counts = steps_summary(steps)
    total = len(steps)
    done = counts.get("done", 0)

    if total == 0:
        return (
            f"Progress: flow_id={flow_id}, status={flow['status']}\n"
            f"No steps registered yet."
        )

    pct = int(done / total * 100)

    lines = [
        f"Progress Report: {flow_id}",
        f"  Status: {flow['status']}",
        f"  Description: {state.get('description', '')[:80]}",
        f"  Completion: {done}/{total} steps ({pct}%)",
        f"  Breakdown: "
        + " · ".join(f"{s}={counts.get(s, 0)}" for s in ("done", "dispatched", "ready", "blocked")),
    ]

    # Next actionable steps (first 3 non-done)
    actionable = [s for s in steps if step_status(s) not in ("done",)]
    if actionable:
        lines.append("  Next steps:")
        for s in actionable[:3]:
            icon = _STEP_ICONS.get(step_status(s), "?")
            task_text = (s.get("task", "") or "")[:60]
            lines.append(f"    {icon} [{s.get('step_id', '?')}] {task_text}")

    # Estimated remaining time
    done_steps = [s for s in steps if step_status(s) == "done" and s.get("dispatched_at")]
    remaining = total - done
    if len(done_steps) >= 2 and remaining > 0:
        # Average duration between dispatched_at timestamps of done steps
        timestamps = sorted(float(s["dispatched_at"]) for s in done_steps)
        if len(timestamps) >= 2:
            durations = [
                timestamps[i + 1] - timestamps[i]
                for i in range(len(timestamps) - 1)
            ]
            avg_duration = sum(durations) / len(durations)
            est_remaining_secs = avg_duration * remaining
            if est_remaining_secs < 3600:
                est_text = f"{est_remaining_secs / 60:.0f} minutes"
            else:
                est_text = f"{est_remaining_secs / 3600:.1f} hours"
            lines.append(f"  Est. remaining: ~{est_text} (based on {len(done_steps)} completed steps)")

    if flow.get("wait"):
        lines.append(f"  Waiting on: {flow['wait'].get('reason', 'unknown')}")

    results = state.get("results") or []
    if results:
        lines.append(f"  Results injected: {len(results)}")

    return "\n".join(lines)
```

### 4.2 agent/tools/taskflow/tools/**init**.py — 注册

```python
# 在 _TASKFLOW_TOOLS 列表中新增：
from .taskflow_progress import taskflow_progress

_TASKFLOW_TOOLS: list[BaseTool] = [
    taskflow_create,
    taskflow_run_task,
    taskflow_set_waiting,
    taskflow_resume,
    taskflow_finish,
    taskflow_fail,
    taskflow_cancel,
    taskflow_summary,
    taskflow_progress,      # ← 新增
    taskflow_dispatch,
    taskflow_wait_all,
]
```

注释更新：`"""TaskFlow tool family: 11 tools..."""`

### 4.3 agent/tools/taskflow/**init**.py — 导出

```python
from .tools import (
    ...,
    taskflow_progress,
)

__all__ = [
    ...,
    "taskflow_progress",
]
```

---

## 5. 实施顺序

```
Step 1: agent/tools/taskflow/tools/taskflow_progress.py — 新建工具
Step 2: agent/tools/taskflow/tools/__init__.py — 注册到 _TASKFLOW_TOOLS
Step 3: agent/tools/taskflow/__init__.py — 导出
Step 4: tests/agent/tools/taskflow/test_taskflow_progress.py — 编写测试
Step 5: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

### 6.1 单元测试 (tests/agent/tools/taskflow/test_taskflow_progress.py)

| 测试                                 | 说明                                            |
| ------------------------------------ | ----------------------------------------------- |
| `test_progress_empty_flow`           | 无步骤 → 返回 "No steps registered yet"         |
| `test_progress_all_done`             | 全部 done → 100%, 无 next steps                 |
| `test_progress_partial`              | 2/5 done → "40%", 列出 next steps               |
| `test_progress_with_blocked`         | 有 blocked 步骤 → breakdown 含 blocked=N        |
| `test_progress_next_steps_max_three` | 5 个 actionable → 只列前 3 个                   |
| `test_progress_est_remaining`        | ≥2 done + dispatched_at → 包含 "Est. remaining" |
| `test_progress_no_est_when_one_done` | 仅 1 done → 不显示预估                          |
| `test_progress_waiting_flow`         | wait 非空 → 包含 "Waiting on"                   |
| `test_progress_not_found`            | 不存在的 flow_id → 返回 not_found_error         |
| `test_progress_results_count`        | 有注入结果 → 包含 "Results injected: N"         |

### 6.2 运行验证

```bash
python -m pytest tests/agent/tools/taskflow/test_taskflow_progress.py -v
ruff check agent/tools/taskflow/tools/taskflow_progress.py
pyright agent/tools/taskflow/tools/taskflow_progress.py
```

---

## 7. 数据流

```
Agent 调用 taskflow_progress(flow_id="deploy-v2")
  → store_sqlite.get_flow("deploy-v2")
  → steps_summary(steps) → {"done": 2, "dispatched": 1, "ready": 1, "blocked": 1}
  → 计算 2/5 = 40%
  → 找 actionable steps → [step-3 (ready), step-4 (blocked)]
  → 估算剩余时间 (基于 done 步骤的 dispatched_at 间隔)
  → 返回:
    "Progress Report: deploy-v2
       Status: running
       Description: deploy to staging
       Completion: 2/5 steps (40%)
       Breakdown: done=2 · dispatched=1 · ready=1 · blocked=1
       Next steps:
         ○ [step-3] run smoke tests
         ⊘ [step-4] verify results
       Est. remaining: ~12.0 minutes (based on 2 completed steps)"
```

---

## 8. 与其他方案的关系

| 方案            | 关系                                                                     |
| --------------- | ------------------------------------------------------------------------ |
| **LT-2**        | 互补。LT-2 在会话启动时被动注入 flow 摘要；#6 是会话中主动查询详细进度。 |
| **#3 预算跟踪** | 协作。进度报告可显示 token 使用进度。                                    |
| **#9 看板**     | 互补。#9 列出所有活跃 flow 的简要信息；#6 查询单个 flow 的详细进度。     |

---

## 9. 风险与缓解

| 风险                  | 缓解                                                              |
| --------------------- | ----------------------------------------------------------------- |
| 预估时间不准确        | 仅在 ≥2 个 done 步骤时估算；明确标注 "based on N completed steps" |
| 步骤无 dispatched_at  | 跳过预估（旧 flow 或 blocked 步骤从未 dispatch）                  |
| 工具数量增加（11→12） | progress 是轻量只读工具，不影响性能                               |
