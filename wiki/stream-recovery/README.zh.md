# 🔁 流恢复

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 本文记录机制，不是 API 契约：哪份状态是权威的、重连如何找到它，以及为什么用快照水位而不是重放增量。

## 设计问题

一个回合是一条长生命周期的 async generator。它的输出在产生时推送给所有在线 socket；没有按客户端的队列，也没有重放日志。观看者可能在任何 chunk 处消失，而回合必须继续运行——它可能已经执行了工具调用并消耗了 token。

需求彼此拉扯：

- 重连不能重跑回合：那会重复副作用和花费。
- 重连不能重放帧：别的观看者可能已经收到，重放会重复可见文本。
- 重连不能让服务端为每个观看者记住每个增量：那是无界状态。
- 同一会话的多个观看者必须收敛到同一份文本，而不共享重放游标。

答案是**某一时刻的快照 + 按回合的序号水位**，并与持久化历史在同一次请求中返回。

## 权威状态

`StreamTurn` 拥有运行中回合的权威文本。通过 think scrubber 的可见模型文本追加到 `self.ai_text`；推理由 `_has_reasoning` 单独记录。工具调用不属于 `ai_text`；它们写入 trajectory ledger。

引擎在 `_run_frames` 开始时注册到 `_ACTIVE_TURNS[session_id]`，在 generator 的 `finally` 中移除，并使用身份检查避免旧回合移除新回合。由于 turn runner/queue 串行化回合，每个会话最多只有一个活跃回合。

`StreamTurn` 从外围 turn scope 捕获 `turn_id`。`active_turn_snapshot()` 从引擎读取四个字段：

- `ai_text`：到目前为止产生的可见文本；
- `_has_reasoning`：是否存在推理通道；
- `_frame_seq`：到目前为止盖上的最高帧序号；
- `turn_id`：文本所属回合的身份。

读取与 流 generator 在同一事件循环上运行，因此文本和序号在同一个一致切面读取，无需锁。没有注册引擎时，会话没有运行中回合，快照函数返回 `None`。HTTP 历史路径额外附加 `turn_num`（下一个持久化回合号）作为展示元数据。

## 帧序号与水位

`StreamTurn.run()` 产生的每一帧在 yield 之前都盖上按回合单调递增的 `_frame_seq`：

```text
_run_frames() produces a frame
  → run() increments _frame_seq
  → frame["seq"] = _frame_seq
  → frame["turn_id"] = self.turn_id
  → the frame is broadcast
```

`StreamDriver` 也会在 `turn_started`、`done`、`hitl_request`、`error` 和 `stopped` 上盖 `turn_id`（这些是它在 chunk 循环之外构造的帧）。因此重连客户端可以把每一帧路由到它所属的回合，即使期间发生了回合切换。

水位是**按回合的**。快照携带读取时的 `seq` 和 `turn_id`；只有帧的 `turn_id` 匹配且 `seq <= snapshot.seq` 时才丢弃。新回合从 `seq = 1` 重新开始，绝不会因为数值低于另一回合的水位而被误判为旧数据。

## 重连时的一次往返

客户端重连时不再发送 `resume_stream`，而是发一次历史请求：

```text
GET /get_history_by_turn_page?session_id=…&include_active_turn=1
  → {messages: [...persisted rows...],
     active_turn: {text, has_reasoning, seq, turn_id, turn_num} | null}
```

不带 `include_active_turn=1` 时端点仍返回纯行数组，所有现有调用方的契约不变。带该参数时，持久化历史和运行中回合的快照一起到达：断线期间完成的回合在 `messages` 里，仍在运行的回合在 `active_turn` 里。

WS handler `_handle_resume_stream` 及其 `stream_resume` 回复作为旧客户端的回退保留；当前客户端不再发送该帧。

## 客户端快照队列

历史请求在途时，socket 不 apply 收到任何 WS 帧，而是全部暂存（`holdFrames`，上限 1000 帧，超出丢最旧的）。响应到达后：

1. 持久化消息合并进聊天列表（断线期间完成的回合立即显示）；
2. `active_turn` 应用到它所属回合的行：行上已有 `turn_id` 时按 `turn_id`，否则按 `turn_num`，都没有则新建 AI 行；
3. socket 记录快照水位（`seq` 和 `turn_id`）；
4. 按到达顺序释放暂存帧。同一回合中 `seq <= 水位` 的帧丢弃；其余帧按自己的 `turn_id` 正常路由。

迟到或丢失的响应不会卡住 UI：等待上限为 10 秒。超时后该响应被排除在聊天合并之外，队列在没有水位的情况下释放，由 socket 的终止帧或 busy watchdog 收口。收到过 chunk 的发送才可能在断线期间结束并结算；未收到 chunk 的发送可重发，不会被空闲快照结算。

## 断线期间的生命周期

`_release_socket` 会注销 socket、可能清理待处理工具参数，但**不会**取消回合。回合任务留在 `_active_tasks` 直到结束；`_ACTIVE_TURNS` 由 generator 自己的 `finally` 移除，而不是断线路径。因此最后一个观看者离开后回合仍然存活。

帧只广播一次、从不重放；重连客户端从快照取得权威文本，并只收到注册之后产生的帧——两者组合无缺口，也不需要重放日志。

## 快照省略的内容

- 推理增量：快照只报告 `has_reasoning`，不包含推理文本。
- 增量工具输入 JSON 和逐 delta 的使用量遥测。
- 工具调用卡片作为流事件；已提交的工具调用仍可在 trajectory ledger（`GET /sessions/:session_id/trajectory`）查询。

这种划分是刻意的。文本是唯一无法从持久记录重建部分渲染的状态；工具调用已有持久 ledger，重放它们的流帧只会重复已有记录。

## 失败语义

- 快照之后到达的旧帧会被按回合水位丢弃，永远不会重复可见文本。
- 历史响应缺失或超时会无水位地释放暂存帧；更早的帧正常 apply，watchdog 仍是最后兜底。
- 断线期间结束的回合会出现在历史行里；快照报告没有活跃回合时，收到过 chunk 的发送会被结算。未收到 chunk 的发送会重发（后端按 `msg_id` 去重），并保持 pending 直到它的回合开始。
- 按 socket 定向保持了多观看者独立性：一个观看者的重连不会改变另一个的游标或流。

## 被拒绝的方案

- **重连时重跑回合。** 重复工具副作用和 token 花费，且可能改变答案。
- **重放全部增量。** 需要按观看者的历史、游标和保留策略；`seq` 水位解决的那个竞态会重新出现。
- **ZCode 的七事件锚点协议。** 它能恢复更细的推理/工具输入，但需要前端协议和持久的 anchor/tail-discard 管线。Sherry 的快照协议用更少机制达到用户可见的恢复目标；更细的事件类型按设计仍未实现。

## 代码地图

- `server/service/stream_dispatch.py`：`StreamTurn`（`ai_text`、`_frame_seq`、`turn_id`、`_ACTIVE_TURNS`）和 `active_turn_snapshot`。
- `server/service/stream_driver.py`：所有 driver 构造的帧都带 `turn_id`。
- `server/trigger/http/messages.py`：`GET /get_history_by_turn_page` 的 `include_active_turn`。
- `server/trigger/ws/messages.py`：旧的 `_handle_resume_stream` 路径和 `_release_socket`。
- `runtime/session/ws_push.py`：恰好一次广播，无重放日志。
- `client/app/composables/messages.ts`：`get_history_page_with_active_turn`。
- `client/app/composables/use-session-lifecycle.ts`：返回快照的历史合并。
- `client/app/composables/use-chat-stream.ts`：`syncHistory`、快照应用与回合路由。
- `client/app/composables/bridge/agent-socket.ts`：帧暂存/释放与快照水位。
- `client/app/composables/bridge/agent-socket-frames.ts`：按回合的水位过滤。
- `tests/server/test_stream_resume.py`、`tests/server/service/test_stream_turn_id.py`、`tests/server/trigger/test_history_active_turn.py`、`client/app/composables/__tests__/agent-socket-resume.test.ts`：上述不变量。
