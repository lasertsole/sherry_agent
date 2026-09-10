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
- `taskflow_run_task(flow_id, task, label, depends_on, expected_revision)`:登记一个
  步骤并派发 detached 子会话(走既有 spawn 入口);`child_session_key` 落库。
  `depends_on` 是前置步骤 id 列表(如 `["step-1"]`):依赖未满足时该步骤记为 `blocked`
  且不派发,依赖全部 `done` 时立即派发并记为 `dispatched`。子会话完成后结果经由既有
  announce/settle-wake 管线自动回流,不要轮询,等结果送达后用 `taskflow_resume` 注入。
- `taskflow_dispatch(flow_id, step_ids, expected_revision)`:批量派发一个或多个当前
  `ready` 的步骤(依赖已满足但仍未派发的步骤)。用于并行派发互不依赖的步骤,或派发被
  `taskflow_resume` 解锁的步骤。任一 id 未知、已是 `dispatched`/`done`、或依赖仍未满足
  时整批拒绝、不派发、不改状态;批次中途派发失败会先持久化已成功的步骤并报告失败项。
- `taskflow_wait_all(flow_id, timeout_seconds, poll_interval_seconds)`:有界轮询,等待
  本流所有 `dispatched` 步骤的子会话 settle。只检查本流派发的子会话,不等待无关会话,
  不会卡在别的后台任务后面。返回每个步骤的 settle 情况,并提示对每个已完成的子会话调用
  `taskflow_resume`。
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

## 步骤依赖与状态

步骤(step)除 `task` / `label` / `child_session_key` 外还带两个字段:

- `depends_on`:前置步骤 id 列表(如 `["step-1"]`)。步骤 id 形如 `step-1`、`step-2`,
  由 `taskflow_run_task` 顺序分配;依赖按 id 引用,不按列表位置。
- `status`:步骤状态,取值 `blocked | ready | dispatched | done`。

状态推进:`blocked -> ready -> dispatched -> done`。

- `blocked`:依赖尚未全部 `done`,`taskflow_run_task` 只登记、不派发。
- `ready`:依赖已满足、等待派发。**由 `taskflow_resume` 解锁**:某个前置步骤被标记
  `done` 后,依赖它的 `blocked` 步骤转为 `ready`;`taskflow_resume` 只报告新解锁的步骤
  id,不会自动派发。
- `dispatched`:已派发 detached 子会话,`child_session_key` 落库。
- `done`:已通过 `taskflow_resume` 把子会话结果注入流状态。

`taskflow_summary` 会渲染每个步骤的 status、depends_on 以及各状态计数。

## 推荐流程(依赖 + 并行)

1. `taskflow_run_task` 登记步骤:独立步骤立即 `dispatched`;带未满足 `depends_on` 的
   步骤记为 `blocked`(不派发)。
2. 前置步骤结果送达后,`taskflow_resume` 注入结果:该步骤转为 `done`,并解锁依赖它的
   `blocked` 步骤为 `ready`(返回新增的 ready 步骤 id)。
3. `taskflow_dispatch` 一次性派发这些 `ready` 步骤(可与其他独立步骤一起并行派发)。
4. `taskflow_wait_all` 等待本流已派发步骤的子会话 settle。
5. 对每个已完成的子会话调用 `taskflow_resume` 逐一注入结果;每注入一个可能再解锁下一层
   `ready` 步骤,回到第 3 步,直到所有步骤 `done`。

## 已知限制

- 步骤 `done` 只表示"结果已被注入",**不代表子会话执行成功**。本阶段没有步骤级
  `failed` / `skipped` 状态,也不做失败感知的解锁或重试:即使子会话失败,其
  `taskflow_resume` 仍会把步骤标记 `done` 并解锁后继步骤。失败感知的重试策略是后续
  差距项(见 gap #8),不在本阶段范围内。
- `taskflow_wait_all` 只做有界轮询;超时会返回 partial 报告。永不 settle 的子会话不会
  让流程自动失败。
