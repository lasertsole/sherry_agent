# 计划：跨 turn diff-hash 无进展检测

- 日期：2026-11-23
- 状态：**待评审**
- 来源：eval 的 `no_progress_stop`（`evals/deepswe/suite.py:825-836`）对比连续 continuation turn 的 git diff，如果代码没变就停止。产品侧只有 plan-snapshot 检测（看 todo 文本变没变），看不出来 agent 反复跑 closure flow 但代码没改。
- 风险等级：**中** — 在 continuation 路径加 diff 哈希比对
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

产品的 PlanContinuationEnforcer 有 stagnation 检测（`stagnation_tracker.py`），但它的快照是 **plan 文本**（todo 的 status 字符串），不是**代码产物**：

```
agent 反复跑 closure flow：
  turn 1: 编辑 todo 文本 → 代码没改
  turn 2: 再编辑 todo 文本 → 代码没改
  turn 3: 再编辑 todo 文本 → 代码没改

plan-snapshot 检测：todo 文本变了 → 不算 stagnation → 继续
diff-hash 检测：git diff 没变 → 算 stagnation → 停止
```

eval 实测过这个失败模式（"repeated closure-flow passes with an unchanged patch"）。

## 1. 设计

在 `PlanContinuationEnforcer.aafter_agent` 中，每次 continuation turn 结束时记录当前 git diff 的哈希。如果连续 N 次 continuation turn 的 diff 哈希相同，且没有在飞的 step child，注入一条"无进展停止"指令而不是继续。

### 1.1 diff 哈希

```python
import subprocess

def _working_tree_diff_hash(project_dir: str) -> str:
    """当前工作树的 git diff 哈希（changed paths + content hash）。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--binary", "--cached", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        # 加上 unstaged
        result2 = subprocess.run(
            ["git", "diff", "--binary", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        import hashlib
        return hashlib.md5(
            (result.stdout + result2.stdout).encode()
        ).hexdigest()
    except Exception:
        return ""  # fail-open：不是 git 仓库或 git 不可用
```

### 1.2 检测逻辑

在 `stagnation_tracker.py` 中新增 diff-hash 检测，和现有 plan-snapshot 检测并行：

```python
class StagnationTracker:
    # 现有：plan_snapshot（todo 文本）
    # 新增：diff_snapshot（git diff 哈希）

    def record(self, session_id, plan_snapshot, diff_hash):
        # plan_snapshot 变了但 diff_hash 没变 → 可能是 churn
        # 两者都没变 → 确认 stagnation
        # plan_snapshot 没变但 diff_hash 变了 → plan 没更新但代码在改 → 继续（agent 在干活）
```

### 1.3 触发条件

```
连续 N 次 continuation turn 的 diff_hash 相同
  且 没有 step child 在飞（_active_flows 无 dispatched 状态的 step）
  → 注入 [SYSTEM DIRECTIVE: NO PROGRESS DETECTED]
  → 停止 continuation
```

N 默认 2（eval 用 1，产品给多一次机会）。

### 1.4 和现有 stagnation 的关系

| 检测 | 快照 | 触发条件 | 已有？ |
|------|------|---------|:------:|
| plan-snapshot stagnation | todo 文本 | 连续 3 次不变 | ✅ |
| **diff-hash stagnation**（新增） | git diff 哈希 | 连续 2 次不变 | ❌ |

两者并行，任一触发都停止。plan-snapshot 抓"计划没更新"，diff-hash 抓"代码没改"。

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/tools/todolist/stagnation_tracker.py` | 新增 diff_hash 记录 + 比对 | 核心检测 |
| 2 | `agent/middlewares/todo_continuation/core.py` | aafter_agent 中调 `_working_tree_diff_hash` + 传给 tracker | 注入点 |
| 3 | `config/features/agent_side/todolist_infra.py` | 新增 `diff_stagnation_threshold: 2` | 配置 |

## 3. 验收

- 连续 2 次 continuation turn 的 git diff 没变 → 停止
- diff 变了 → 不触发（agent 在干活）
- 有 step child 在飞 → 不触发（等子 agent 完成）
- 非 git 项目 → fail-open（哈希为空，不检测）
- plan-snapshot 检测不受影响（并行运行）
