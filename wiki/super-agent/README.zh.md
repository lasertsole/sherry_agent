# 超级智能体

一个独立进程编排器，把 GitHub issue 和飞书话题中的消息转化为可追踪的工作，
委派给 Sherry worker 子代理执行，并把结果回报到原始频道。它拥有自己的
SQLite 状态，只通过窄回环接缝与 Sherry 通信。

## 为什么独立成进程

Sherry 是执行单元，超级智能体是管理者。两者分离意味着管理者的 SQLite 状态、
tmux 会话和每小时检查能在 Sherry 重启后存活；Sherry 宕机只会退化为
"继续入队 Trigger，不丢任何东西"，而不会中断工作流。超级智能体从不导入
Sherry 核心代码——它只调用 `/internal/superagent/*` 回环路由。

## 架构

```
GitHub / Feishu  →  Sources  →  Engine  →  Effects  →  Sherry workers
                                  ↓          ↓
                             SQLite Store   Outbox → channel replies
```

Engine 是 Trigger、Thread、Decision 和 Ticket 的唯一写入者。每条命令都是一次
SQLite 事务；适配器（Sources、Executor、Outbox、Assessor）是异步的，通过窄端口
与 Engine 通信。

- **Sources** 把外部事件翻译成 Trigger，并把回复发回。
- **Engine** 单向运行五个部件：Intake → Evals → Tickets →
  Assignments → Replies。
- **Effects** 是持久化、可合并、可重试的队列——每个副作用都能在重启后存活。
- **Outbox** 是频道 Receipt 的唯一写入者，保证顺序与幂等。
- **Passes** 每两秒运行一次到期 Effects，然后运行到期 Evaluations。

## 授权与评估

Policy 通过 admission 表和 `off`、`observe`、`auto` 三种 accept mode 决定一个
Trigger 是显式还是推断的。只有 auto 模式且规则同时启用时，推断的工作才会被
接受。评估委托给一个专门的 Sherry 会话，它读取有界的 thread 窗口并返回
Decision；HITL 回答若拒绝，则 fail closed 为一个 blocked Ticket。

## 设置与生命周期

`superagent.jsonc` 保存状态目录、Sherry 回环 URL、面板端口和 Source 配置。
`scripts/superagent` 通过专用的 tmux 会话提供 `start`、`stop`、`restart`
和 `status`，退出码为 0（健康）、1（未运行／运行时错误）、2（部件错误）和
3（设置未完成）。每小时检查读取这些码，只在 1 时重启。完整运维手册见
[deployment](deployment.md)。

## 面板

Nuxt `/superagent` 页面轮询回环面板 API，提供六个标签页：Overview、Sources、
Triggers、Evals、Execution 和 Settings。API 受回环保护（host + origin + 请求头），
其他来源的页面无法驱动它。设置有版本号；过期的保存会被拒绝。

## 测试与门禁

`tests/superagent/` 镜像包结构，为域模型、Engine、Policy、Effects、Outbox、
Sources 和面板提供单测，并有端到端链路：GitHub issue → Ticket → PR 以及
Feishu mention → Ticket（两者都 mock CLI 和 worker）。静态门禁是 `ruff`、
`basedpyright` 和 `lint-imports` 分层契约；文档门禁是 parity、links 和 language。
