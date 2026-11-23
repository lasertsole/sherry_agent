# 计划：verify 前的 settle 等待

- 日期：2026-11-23
- 状态：**待评审**
- 来源：eval 在最后 turn 后等 `_FLOW_SETTLE_WAIT_S`（180s）让在飞的 step child 完成，再判定完成（`evals/deepswe/suite.py:875-877`）。产品侧 agent 标记 `[VERIFY]` 完成时不等子 agent 编辑刷完，可能产生假完成。
- 风险等级：**中** — 在 completion 路径加 settle 检查
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

```
agent 标记 [VERIFY] todo 完成
  → 但有一个 executor step child 刚完成、编辑还在写回
  → agent 宣布完成，用户看到"完成"
  → 子 agent 的编辑几秒后才刷到磁盘
  → 用户看到的"完成"其实缺了子 agent 的最后改动
```

## 1. 设计

在 `_detect_todo_all_complete`（或 verify transition barrier）中，如果所有 todo 都完成了但还有 step child 在 `dispatched` 状态，阻塞 verify 完成，等到没有在飞 step 或超时。

### 1.1 settle 检查

```python
# 在 verify 完成路径
if all_todos_complete:
    active_flows = _get_active_flows(session_id)
    if _has_dispatched_steps(active_flows):
        # 有 step 还在飞，等待 settle
        settled = await _wait_for_step_settle(
            session_id, timeout=TODOLIST_INFRA["verify_settle_wait_s"]
        )
        if not settled:
            # 超时：仍有未完成的 step
            # 注入提示："部分子任务仍在执行中，结果可能不完整"
            # 不阻塞完成，但告知用户
```

### 1.2 配置

```python
# config/features/agent_side/todolist_infra.py
"verify_settle_wait_s": 30.0,  # verify 完成前最多等 30s 让在飞 step 完成
```

默认 30s（eval 用 180s，产品不需要等那么久——子 agent 通常几秒内完成编辑写回）。

### 1.3 和 SubagentCompletionDrain 的关系

`SubagentCompletionDrainMiddleware` 已经在模型调用前注入完成的子 agent 结果。settle 等待是额外的保险——确保 drain 之后、verify 之前，没有遗漏的在飞 step。

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/middlewares/todo_continuation/core.py` | verify 完成路径加 settle 检查 | 核心逻辑 |
| 2 | `config/features/agent_side/todolist_infra.py` | 新增 `verify_settle_wait_s: 30.0` | 配置 |

## 3. 验收

- 所有 todo 完成 + 无在飞 step → 立即完成（不变）
- 所有 todo 完成 + 有在飞 step → 等 settle（最多 30s）
- step 在 30s 内完成 → 正常完成
- step 在 30s 内没完成 → 超时，注入"部分子任务仍在执行中"提示，允许完成
