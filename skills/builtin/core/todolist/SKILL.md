---
name: todolist
description: Session-scoped task tracking with delegation routing and TaskFlow-backed DAG execution. Use for 3+ step work or any multi-item request.
scope: main_only
---

# TodoList: 会话级任务跟踪

会话级的轻量计划/清单层。它负责"想清楚、盯住、逼完成";真正的跨会话 DAG 执行、依赖解锁与并行派发由 TaskFlow 承担。

## 何时使用

- 收到 3+ 步骤的复杂工作时,先建 todo 列表再动手。
- 一个请求包含多个独立条目时,拆成原子 todo。
- 范围不确定时也用它:写下来才能看清边界。
- 每完成一步更新状态,全部完成后用空列表 `[]` 清除。

## 工具

- `todowrite(todos)`: **全量替换**当前会话的 todo 列表。每次都要传完整列表,不是增量补丁。
- `todoread()`: 从数据库读回当前列表。不确定当前状态时先读再写。

## 字段

- `content`: 单条 todo 的文本。推荐格式 `[WHERE] [HOW] to [WHY] - expect [RESULT]`,一条只做一个能在 1-3 次工具调用内完成的原子动作。
- `status`: `pending | in_progress | completed | cancelled`,默认 `pending`。
- `priority`: `high | medium | low`,默认 `medium`。
- `category`(可选): `quick | deep | ultrabrain | visual | git | writing`,委派路由裁决。
- `delegation`(可选): `self | subagent`,是否自己干还是派给子代理。
- `subagent_id`(可选): 派出子代理后填入其 `child_session_key`。
- `plan_ref`(可选): 关联的 `.omo/plans/*.md` 路径。
- `flow_id`(可选): 关联的 TaskFlow flow id。
- `step_id`(可选): 关联的 TaskFlow step id(如 `step-2`)。

## 规则

- 每次 `todowrite` 传完整列表,不要试图只改一行。
- **同一时刻只能有一个 `in_progress`。** 当前项没完成前,不要把下一项置为进行中。
- **只有验证通过后才能标记 `completed`。** 先跑验收标准,再改状态;验证不过就保持未完成或改 `cancelled` 后重规划。
- `delegation: subagent` 的 todo,在子代理返回前不得标记 `completed`。
- 批量把多条一起标完成会掩盖真实进度,逐条标记。
- 完成状态一经写入,后续 turn 的续作系统会据此判断是否放行。

## DAG / 依赖 / 波次不在这里

todolist 层**不**跟踪 `depends_on`、**不**保存波次、**不**缓存 step 状态,也**不**做 frontier 重算。这些全部归 TaskFlow:

- 声明依赖: `taskflow_run_task(flow_id, task, depends_on=["step-1"])`。依赖未满足时步骤记为 `blocked` 且不派发。
- 并行派发就绪步骤: `taskflow_dispatch(flow_id, step_ids)`。
- 等待本流派发的子会话 settle: `taskflow_wait_all(flow_id, ...)`。
- 注入结果并解锁后继: `taskflow_resume(flow_id, child_session_key, result)`。resume 只解锁,不自动派发。
- 回读 DAG 状态: `taskflow_summary(flow_id)` 渲染每步的 `status`(`blocked | ready | dispatched | done`)与 `depends_on`。

todo 只通过可选的 `flow_id` / `step_id` 指向对应 flow step,需要 DAG 状态时用 `taskflow_summary` 回读,不要在 todo 层重建调度器。依赖图、解锁与并行派发只有一个权威源:TaskFlow。
