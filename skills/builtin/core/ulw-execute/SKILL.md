---
name: ulw-execute
description: Orchestrator execution doctrine. Run a plan file to completion by delegating every implementation unit to subagents, verifying with the Sisyphus contract, and using TaskFlow for dependency-ordered DAG execution.
scope: main_only
---

# ulw-execute: 编排执行

规划与纪律层的执行侧。它把一个计划文件(.omo/plans/*.md)从头推到完成:选计划、写 boulder 状态、执行下一个 checkbox、验证并存证、标记进度。它不实现 DAG 调度,那部分归 TaskFlow。

## Doctrine (MANDATORY)

> YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.
> YOU DO NOT WRITE CODE. YOU DO NOT EDIT PRODUCT FILES.
> EVERY unit of implementation MUST be delegated to a spawned subagent.

主会话负责:建计划、拆任务、委派、验证完成。任何一行产品代码、测试或修复,都派给子代理执行。违反此 doctrine 即视为工作未完成。

## 5 Phase 流程

```
Phase 1: Select the plan
  -> 读 .omo/boulder.json
  -> 列 .omo/plans/*.md
  -> 按 plan-name 匹配,或恢复 status="active" 的活跃工作

Phase 2: Create or update Boulder state
  -> 写 .omo/boulder.json(session_id 前缀 sherry:)
  -> 把计划里的 Phase 与 Task 注册为 todos

Phase 3: Execute the next checkbox（调度全部交给 TaskFlow）
  -> 读计划,找第一个未勾选的 column-0 checkbox
  -> 拆成 atomic sub-tasks(一个 worker 一次运行可完成)
  -> 带依赖的 checkbox 登记为 TaskFlow step:
       taskflow_run_task(flow_id, task, depends_on=[...])
  -> 依赖未满足的步骤由 TaskFlow 记为 blocked,不派发
  -> 对 ready 步骤调用 taskflow_dispatch(flow_id, step_ids) 并行派发
  -> taskflow_wait_all(flow_id) 等本流派发的子会话 settle
  -> taskflow_resume(flow_id, child_session_key, result) 注入结果并解锁后继
  -> DELEGATE EVERYTHING,按下方路由表分发每个 sub-task

Phase 4: Verify and record evidence
  -> 5 gates(见 Sisyphus 契约)
  -> 证据写入 .omo/ledger.jsonl

Phase 5: Mark progress
  -> 编辑计划 checkbox: - [ ] -> - [x]
  -> 重读计划,确认剩余数减少
  -> 追加 task-completed ledger entry
  -> 继续下一个 checkbox,不询问是否继续
```

## 委派路由表

每个 sub-task 按类别路由。类别与 todo 的 `category` 字段一致:

| category | 适用场景 |
| -------- | -------- |
| `quick` | 单文件、10 行级以内的小改动,或纯查询 |
| `deep` | 多文件、复杂逻辑、需要跨模块理解与推理 |
| `ultrabrain` | 高难架构决策、疑难调试、需要长时间思考的难点 |
| `visual` | UI/前端/样式/截图验证类工作 |
| `git` | 提交、rebase、历史检索、分支操作 |
| `writing` | 文档、README、注释、面向人的文字产出 |

路由裁决写入 todo 的 `category` 字段;`delegation: subagent` 时,派出子代理后把返回的 `child_session_key` 填进 `subagent_id`。

## 并行拓扑

- **独立通道 -> 并行 workers**:分离文件、无共享契约的任务,一次性并行派发(`taskflow_dispatch` 批量派发)。
- **有序依赖通道 -> 波次串行**:C 依赖 A 和 B 先完成时,等前置 step `done` 解锁(`depends_on` + `taskflow_resume` 的 unlock),再派发。
- **重叠通道 -> 串行或手动协调**:同模块、同契约的并发改动更快出错时,串行执行。

原则:能并行不串行,有依赖不抢跑,有重叠不硬并发。判断依据始终是 TaskFlow 的 `depends_on` 与 step `status`,不在 todo 层重算。

## Sisyphus 完成契约

一个 checkbox 走完三段状态才算真正完成:

```
DoneClaim          -> 你相信做完了,先声明,但不要标记 completed
AdversarialVerify  -> 跑验收标准,主动找反例
FullyDone          -> 验证通过后,才把 checkbox 标记 completed
```

5 gates(逐条通过才算 FullyDone):

1. **plan reread**:重读计划原文,确认目标与验收标准没有理解偏差。
2. **automated verification**:跑自动化验证(测试、构建、静态检查)。
3. **manual QA**:真实执行一次,观察端到端行为,而不是只看代码。
4. **adversarial QA**:主动探针,至少覆盖 dirty worktree 与 misleading success output;stale state 类按实际缓存情况判断适用性。
5. **cleanup**:清理临时资源(会话、进程、临时文件),确认无残留。

验证失败则任务未完成,重新派发或修复后从 DoneClaim 重来。未验证的完成声明一律拒绝。

## DAG 执行委托给 TaskFlow

编排层只调用、不重建 TaskFlow:

- `taskflow_run_task(flow_id, task, depends_on=[...])`:登记步骤并按依赖决定是否派发。
- `taskflow_dispatch(flow_id, step_ids)`:批量派发 ready 步骤。
- `taskflow_wait_all(flow_id, ...)`:等待本流派发的子会话 settle。
- `taskflow_resume(flow_id, child_session_key, result)`:注入结果、标 done、解锁后继(幂等)。
- `taskflow_summary(flow_id)`:只读回读每步 `status` / `depends_on` 与状态计数。

完整步骤状态机(`blocked -> ready -> dispatched -> done`)与乐观锁重试规则见 `taskflow` skill。不要在 todolist 层实现 `depends_on`、波次或 frontier 调度。

## 移植说明

本 skill 从 oh-my-openagent (omo) 的 `ulw-execute/SKILL.md` 移植并适配 sherry_agent 基础设施(subagent spawn、auto_turn、SQLite)。上游仓库在本环境不可达,移植所依据的 omo 摘录已内嵌在 `TODO/TODOLIST_PLAN.md` 第 7 节。
