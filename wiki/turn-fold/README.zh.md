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

- 已完成的组默认**折叠**；摘要显示工具卡数量与已测工具执行耗时之和。
  这是**工具耗时**，不是整个 turn 的 wall-clock（不含模型思考与网络往返）。
  没有工具时显示“思考过程”或“执行过程”。
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

分类**刻意放在客户端**：message 行现在同时带 `turn_num` 和 `turn_id`（服务端为每轮
盖章），但 trajectory 账本只存工具事件、不存 assistant 文本；服务端 render-unit API
或 WebSocket frame 只会重复客户端已持有的行。直接从已加载的 message 协议推导，使
历史重载与重连结果一致，不改 schema、不加 frame。

最终回答就是普通的 `AIMessage.content`，没有 `final_answer` / `execution_summary` /
`structured_response` 特殊字段；“已结束”由该行 `tool_calls` 为空来认定。源计划中可选的
structured-response 摘要没有启用：折叠不需要 LLM 配合。

## ⚠️ Spike 实测与已知限制（2026-10-23）

真实会话中执行 2 次 `read_file` + 1 次 `search_files`，thinking 开启：

- turn 运行中：0 个折叠触发器，工具卡可见（不折叠）；
- turn 结束、刷新后：摘要「已执行工具 3 个 · 54ms」，工具卡/思考块隐藏，最终回答可见；
- 展开：3 个工具卡 + 2 个思考块恢复，最终回答仍在；
- 54ms = 9 + 8 + 37ms，确认是工具耗时之和。

**已知 live-session race（尚未修复）**：turn 刚结束、刷新前，draft 行可能被 hydrate 回来并
排在新增 user 行之前，表现为重复最终回答、折叠摘要工具数/时长偏大（实测刷新前
「4 个 · 62ms」，刷新后「3 个 · 54ms」）。根因是 `untrackDraftTurn` 的 IndexedDB 清理未
await 就触发 `loadSessionHistory`，而服务器在 `done` 时可能尚未写入最终 AI 行，stale-draft
守卫无法识别。刷新页面即恢复。修复方向：清库完成后再加载 history，或对“已有 human
行但尚无最终 AI 行”的 turn 不 hydrate draft。

结论：折叠分类本身可用，不需要服务端 render-unit 协议或 structured-response 特殊字段；
上述 race 属于既有 chat/draft 层，需单独修复。

## 🧪 测试

`use-chat-turn-fold.test.ts` 钉住分类规则（已完成最终回答、流式中、尾部工具、带工具
调用的 AI 行、仅 reasoning、未知耗时、仅媒体、注入载体）。ChatBox 集成测试钉住折叠
摘要、展开、运行中线性回退、普通回答不折叠、尾部工具 fail-open。语言与 i18n block
测试覆盖四语言标签。
