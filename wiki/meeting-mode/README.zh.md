# 会议模式

会议模式是 Sherry 的第三种产品模式：多位注册专家围绕同一主题讨论，形成共同结论，并把结构化计划交给专家团执行组件。讨论阶段与执行阶段相互独立——会议可以只产出计划并保存，稍后再执行。

## 为什么需要会议

单个 agent 只从一种视角回答。会议让多个领域专家进入同一段对话，使决策记录下彼此的依赖关系：前端方案影响后端，后端又影响数据库。用户是"老板"，可以随时插话；专家之间从不直接互调，因此讨论不会死锁。

## 架构

```
User  ->  Host  ->  Selector  ->  Participants  ->  Conclusion
            |           |              |
         tools       one JSON       one statement
         allowlist   verdict        per turn
```

三个角色位于 `agent/meeting/`。`host` 只负责组织，其工具面被裁剪为协调白名单；`selector` 每轮返回一个 `MeetingTurnDecision`；每位 `participants` 基于共享历史贡献一次发言。`run_meeting` 是原始循环；`run_meeting_with_interrupts` 增加了阶段流转、插话处理和阶段 allow-list 门控。

## 生命周期

会议以主持人确认主题开场，随后进入派发循环：选择器指定下一位发言人，该参会者发言，发言被追加保存。当选择器不再指定发言人——或到达 40 轮上限——主持人起草总结，会议进入结束确认。每条发言都以 speaker kind 和 message role 持久化。

## 插话与回滚

`apply_interruption` 把用户插话归类为六类之一。补充信息只追加而不改变计划；结束请求把会议推进到结束确认。纠正、范围变更和优先级变更会调用 `apply_local_rollback`：受影响的执行计划阶段——以及依赖它们的所有阶段——被标记为 `needs_rediscussion`，让选择器在下一轮重新规划。阶段 allow-list 门控阻止参会者在选择它的阶段之外发言。

## 从结论到执行

`build_conclusion` 产出带 TaskFlow 形态计划的 `MeetingConclusion`：每一步包含任务、注册专家名及其依赖。`execute_conclusion` 用确定性 id `meeting-{meeting_id}` 创建流，并通过专家团机制派发其步骤。再次执行请求会找到已有流，而不是重复创建。`reusable` 提示保留在会议结论上，因为它不是 TaskFlow 步骤的原生字段。

## 实时流

会议流是位于 `/meetings/ws` 的按会议划分的 WebSocket 通道。agent 事件通过 `MEETING_EVENT_PUSH` hook 发出，该 hook 是 fail-open 的：没有 WS 服务的进程仍能正常运行会议。帧覆盖发言人变化、消息增量与完成、thinking 增量、ask-user 提示和总结草稿。Nuxt 页面把每一帧送入 store 的帧归约器；mock 模式让页面在没有后端时也可导航。

## 持久化与恢复

会议、消息和 `meeting_compaction_segments` 存放在共享的 registry 数据库中。长讨论通过会议自有 trigger 复用 summarization 中间件的压缩原语，因此不会重新触发主 agent 的 task-intent。启动时，`auto_adjourn_at_boot` 以 `process_restart` 原因取消仍存活的会议，因为它们的内存循环无法在重启后继续。

## API

HTTP 接口包括 `POST /meetings` 创建、`GET /meetings` 列表、`GET /meetings/one` 与 `GET /meetings/messages` 读取、`DELETE /meetings` 取消，以及 `POST /meetings/run`、`/meetings/interrupt`、`/meetings/reply`、`/meetings/confirm-end`、`/meetings/execute` 和 `GET /meetings/conclusion`。所有路由使用查询参数或 JSON body，不使用路径插值。

## 测试与门禁

`tests/agent/meeting/` 覆盖循环、分类、回滚、压缩、结论和启动恢复；`tests/server/trigger/http/test_meetings_api.py` 覆盖路由，`tests/server/trigger/ws/test_meeting_ws.py` 覆盖帧发送。前端测试固定 store 归约器与页面。静态门禁是 `ruff`、`basedpyright` 和 `lint-imports`；文档门禁是 parity、links 和 language。
