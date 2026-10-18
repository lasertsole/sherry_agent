# 🔁 流恢复

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 回合进行中的 WebSocket 断开后，客户端不应被迫重跑该回合，也不应重放其他观看者已经收到的帧。Sherry 用文本快照加逐回合序列号水位来恢复运行中的回合。

## 适用范围

- **在该回合至少产生一个 chunk 之后**断开的观看者，重连时用 `resume_stream` 请求运行中回合的快照。
- **在第一个 chunk 之前**断开的观看者会重发待发送的 payload；后端按 `client_msg_id` 去重，因此回合不会被启动两次。
- 最后一个观看者断开后，回合本身继续运行；socket 释放不再取消它。
- 该协议基于快照，不是 ZCode 的七事件锚点协议。核心目标——不重跑、不重放、不重复、文本尾部以服务端为准——已经覆盖。

## 线上协议

1. 客户端在 agent socket 上发送 `{"type": "resume_stream", "session_id": ..., "content": {}}`。
2. 服务端调用 `active_turn_snapshot(session_id)`。若有回合正在运行，返回引擎的 `ai_text`、`has_reasoning` 与当前逐回合 `seq`。
3. 服务端**只向该 socket** 回复 `stream_resume` 帧，`status` 为 `ok`、`ended`、`gone` 或 `unknown`。
4. `ok` 携带快照。`ended` 表示没有运行中的回合但存在历史（客户端重新拉取）；`gone` 表示会话已删除或从未持久化；`unknown` 是读取失败时的 fail-open 结果。
5. 服务端从不广播快照，也从不重放过去的帧。实时帧仍只在产生时广播一次。

## 客户端契约

- 客户端先 flush 已缓冲的 delta；快照已经包含它们。
- 该回合的 AI 行会被快照文本**替换**，绝不追加。
- 每个实时帧都携带逐回合 `seq`；快照携带读取时的 `seq`。恢复后的客户端丢弃 `seq` 及以下的帧，从而关闭“快照读完后仍有在途帧到达”的竞态。
- `stream:resumed` 是业务层消费的总线事件；水位保存在 socket 帧上下文中，而不是聊天 store 中。

## 未覆盖范围

- reasoning 增量不恢复；快照只报告 `has_reasoning`。
- 增量 tool-input JSON 与逐 delta 的 usage 遥测不在快照中。
- 断线前已 committed 的工具调用仍可通过逐回合 trajectory ledger（`GET /sessions/:session_id/trajectory`）查询，但不会作为流事件重放。
- 完整的 ZCode 七事件锚点协议没有实现，也不计划实现；快照协议以更少的机制覆盖用户可见的恢复目标。

## 代码位置

- `server/service/stream_dispatch.py`：`active_turn_snapshot` 与活动回合注册表。
- `server/trigger/ws/messages.py`：`_handle_resume_stream`，按 socket 回复。
- `runtime/session/ws_push.py`：`broadcast_to_session` 负责实时帧；没有重放日志。
- `client/app/composables/bridge/agent-socket.ts`：断线检测与 `resume_stream` 请求。
- `client/app/composables/bridge/agent-socket-frames.ts`：`stream_resume` 路由与快照 `seq` 水位。
- `client/app/composables/use-chat-stream.ts`：文本替换与 `ended` / `unknown` 的重新拉取决策。

## 测试

- `tests/server/test_stream_resume.py`：广播恰好一次、死 socket 剔除、快照只回请求方、不跨 session，以及水位契约。
- `client/app/composables/__tests__/agent-socket-resume.test.ts`：重连请求与 `ok` / `ended` / `gone` 处理。
- `client/app/composables/__tests__/agent-socket-frames.test.ts`：`stream_resume` 帧路由与快照应用。

## 失败模式

- 快照读取失败时，服务端回答 `unknown`；客户端重新拉取历史，而不是静默保留陈旧文本。
- 会话没有活动回合且没有持久化历史时，回答 `gone`；客户端不做任何事。
- 如果携带 `seq` 且不高于快照水位的帧在快照之后到达，客户端会丢弃它。因此重复投递不会让可见文本重复。
- 快照只发给请求 socket；同一会话的其他观看者继续接收实时帧，不会把快照当成重放。
