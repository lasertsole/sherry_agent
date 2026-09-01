---
name: taskflow
description: Durable multi-step task flows with optimistic locking, detached subagent step dispatch, waiting and idempotent resume via the taskflow_* tool family
scope: main_only
---

# TaskFlow

用 `taskflow_*` 工具家族管理跨轮次的长任务流(task flow):状态持久化到 SQLite,
支持乐观锁、步骤派发、等待与恢复。对应 openclaw managedFlows 的 API 面
(createManaged / runTask / setWaiting / resume / finish / fail / requestCancel /
getTaskSummary)。

## 何时使用

- 一个多步骤工作需要跨多轮追踪进度(步骤、结果、状态)。
- 某个步骤要派发给子会话(subagent)执行,完成后把结果回注到流状态。
- 多个写入方(主会话、并发子任务)同时改同一个流,需要冲突检测而不是静默覆盖。

## 工具一览

- `taskflow_create(flow_id, description, initial_state)`:创建流,初始 revision=1,
  状态 running。
- `taskflow_run_task(flow_id, task, label, expected_revision)`:登记一个步骤并派发
  detached 子会话(走既有 spawn 入口);`child_session_key` 落库。子会话完成后结果
  经由既有 announce/settle-wake 管线自动回流,不要轮询,等结果送达后用
  `taskflow_resume` 注入。
- `taskflow_set_waiting(flow_id, wait_reason, expected_revision)`:把流置为
  waiting,记录等待原因。
- `taskflow_resume(flow_id, child_session_key, result, expected_revision)`:把子会话
  结果注入流状态并回到 running。幂等:同一 (child_session_key, result) 重复 resume
  不会二次注入,revision 也不变。
- `taskflow_finish(flow_id, summary, expected_revision)`:标记 done(终态)。
- `taskflow_fail(flow_id, reason, expected_revision)`:标记 failed(终态)。
- `taskflow_cancel(flow_id, reason, expected_revision)`:取消流(终态)。
- `taskflow_summary(flow_id)`:只读回读状态、revision、child_session_key、步骤与
  结果;也是冲突后的重读入口。

## 乐观锁与冲突重试

所有变更走 `UPDATE ... WHERE flow_id = ? AND expected_revision = ?`;并发双写时
恰好一个成功,另一个收到冲突错误,错误里带有最新 revision(例如
`latest revision=3`)。重试流程:

1. 用 `taskflow_summary(flow_id)` 重读,拿到最新 `revision`。
2. 基于最新状态重放你的变更,带上 `expected_revision=<最新 revision>` 重试。
3. 冲突错误文本里的 expected_revision 就是可直接使用的重试值。

终态(done / failed / cancelled)的流不可再变更,任何变更都会被拒绝。

## 状态机

running -> waiting(置等待)-> running(resume)-> done / failed / cancelled(终态)。
`taskflow_resume` 对 waiting 的流回到 running;对 running 的流保持 running。
