# 轮次执行历史折叠

> 已完成的轮次把工具/思考历史折叠成一条摘要，最终回答始终可见。

## 🎯 概述

一次生成轮次结束后，聊天区把助手的执行历史——工具卡片、中间 assistant
文本与思考过程——折叠到一条摘要行后面，最终回答始终显示在下方。展开摘要即可
恢复完整的线性历史。

仍在运行、以工具调用结束、没有最终回答或仍带未完成工具调用的轮次**不会**折叠，
继续走原有的线性渲染。这是 fail-open 边界：异常轮次绝不隐藏执行过程。

## 🧭 分类规则

折叠完全由现有 message 行推导。一个 assistant 工作组必须同时满足：

| 条件 | 要求 |
| :--- | :--- |
| 最后一行 | 组的最后一行是 AI 行 |
| 已结束 | `streaming` 为 false |
| 有内容 | 有文本或媒体附件 |
| 已完成 | 该行不声明工具调用（`hasToolCalls` 为 false） |
| 非注入 | 不是注入载体（没有非 user 的 `origin`） |
| 有可折叠内容 | 它前面有历史行，或它自身带 reasoning |

`history` 是最终回答之前的全部行；既无历史也无 reasoning 的单独回答没有可折叠
内容，保持线性。

## ⚙️ 交互

- 已完成的组默认**折叠**；摘要显示可测量的工具工作（N 个工具，已知耗时求和）
  或“思考过程”。
- 摘要行原地切换历史；展开后内部工具卡片仍保留各自的展开/收起状态。
- 折叠时最终行不显示思考块，但保留头像，避免被误认为连续消息。
- 展开状态属于当前挂载的聊天页（不持久化）；重新加载会重新计算并回到折叠态。
- 切换后虚拟列表会重新测量行高，滚动几何保持正确。

## 🧩 架构

`use-chat-turn-groups` 已按说话者边界分组：USER 行独占一组，其后的 AI/TOOL 行组成
一个 assistant 工作组。`use-chat-turn-fold.ts::classifyTurnFold` 把该组转成
`{history, finalMessage, toolCount, durationMs, hasReasoning}`。

`MessageItem.hasToolCalls` 由持久化 AI 行的 `tool_calls` 映射而来，因此带工具调用的
行不会被误当作最终回答。`ChatTurnHistoryStatus.vue` 是摘要触发器；`ChatBox.vue`
决定实际渲染的行，并在折叠时隐藏最终行的思考块；`use-chat-card-expansion.ts`
持有每组展开状态。

分类**刻意放在客户端**：message 行只有 `turn_num`，没有稳定的 turn id；trajectory
账本只存工具事件、不存 assistant 文本。服务端 render-unit API 或 WebSocket frame
只会重复客户端已持有的行。直接从已加载的 message 协议推导，使历史重载与重连结果
一致，不改 schema、不加 frame。

源计划中可选的 structured-response 摘要没有启用：折叠不需要 LLM 配合，最终回答
直接取自 assistant 行原文。

## 🧪 测试

`use-chat-turn-fold.test.ts` 钉住分类规则（已完成最终回答、流式中、尾部工具、带工具
调用的 AI 行、仅 reasoning、未知耗时、仅媒体、注入载体）。ChatBox 集成测试钉住折叠
摘要、展开、运行中线性回退、普通回答不折叠、尾部工具 fail-open。语言与 i18n block
测试覆盖四语言标签。
