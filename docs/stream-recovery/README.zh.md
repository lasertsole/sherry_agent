# 🔁 流恢复

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 本文记录机制而非 API 契约：哪份状态是权威的、重连如何找到它，以及为什么用快照水位而不是重放 delta。

## 设计问题

一个回合是一条长时间运行的 async generator。输出产生时就被推给所有在线 socket；没有 per-client 队列，也没有 replay log。观看者可能在任意 chunk 处消失，而回合必须继续运行，因为它可能已经执行过工具调用并消耗了 token。

需求互相拉扯：

- 重连不能重跑回合：那会重复副作用和成本。
- 重连不能重放帧：其他观看者可能已经收到，重放会让可见文本重复。
- 重连不能要求服务端为每个观看者保存所有 delta：那是无界状态。
- 同一会话的多个观看者必须收敛到同一文本，而不共享重放游标。

答案不是事件日志，而是**时间点快照加序列号水位**。

## 权威状态

`StreamTurn` 持有运行中回合的权威文本。可见模型文本经过 think scrubber 后追加到 `self.ai_text`；reasoning 由 `_has_reasoning` 单独记录。工具调用不属于 `ai_text`，它们记录在 trajectory ledger 中。

引擎在 `_run_frames` 开始时注册到 `_ACTIVE_TURNS[session_id]`，在 generator 的 `finally` 中移除，并用身份检查防止旧回合移除新回合。每个会话最多只有一个活动回合，因为 turn runner/queue 会串行化回合。

`active_turn_snapshot()` 从该引擎读取三个字段：

- `ai_text`：目前产生的可见文本；
- `_has_reasoning`：是否存在 reasoning 通道；
- `_frame_seq`：目前打上的最高帧序列号。

这次读取与 stream generator 运行在同一个事件循环上，因此文本和序列号是在同一个一致切面上读出的，不需要锁。若没有注册的引擎，会话就没有运行中的回合，快照函数返回 `None`。

## 帧序列与水位

`StreamTurn.run()` 发出的每一帧在 yield 前都会打上逐回合单调的 `_frame_seq`：

```text
_run_frames() 产生一帧
  → run() 递增 _frame_seq
  → frame["seq"] = _frame_seq
  → 该帧被广播
```

快照携带读取时的 `_frame_seq`。这个数字就是水位：

- 任何 `seq <= snapshot.seq` 的帧，在快照切面上都已经反映到 `ai_text` 中。
- 这样的帧仍可能在飞往重连 socket，或在快照回复之后才送达。
- 因此恢复后的客户端会丢弃水位及以下的每一帧，并用快照文本替换该回合的 AI 行。

这是一个线性化论证：快照是流过流的一个切面，`seq` 标识这个切面，替换文本就是该切面上的状态。客户端不需要知道快照具体包含了哪些 delta。

## 断线期间的生命周期

`_release_socket` 注销 socket，并可能清理 pending tool arguments，但**不会**取消回合。回合任务留在 `_active_tasks` 中直到完成；`_ACTIVE_TURNS` 由 generator 自己的 `finally` 移除，而不是由断线路径移除。因此最后一个观看者离开后回合仍然存活。

重连时客户端调用 `_handle_resume_stream`。处理器读取活动引擎：

- 存在引擎 → `ok` 加快照；
- 没有引擎但会话有持久化消息 → `ended`（客户端重新拉取历史）；
- 没有引擎也没有历史 → `gone`；
- 读取失败 → `unknown`，客户端重新拉取，而不是静默保留陈旧文本。

回复只发给请求的 socket。其他观看者继续接收实时帧；快照不是广播，也永远不会成为重放来源。

## 为什么没有重放日志

重放日志必须为每个回合保存所有 delta、为每个观看者保留、并维护 per-viewer 游标。它还需要保留策略、背压，以及区分“已送达”和“在途”的方法——还是同一个水位问题，只是多了一份日志。

快照方案的状态有界：每个运行中回合只有一个文本累加器和一个计数器。它天然幂等：无论客户端错过一帧还是一千帧，用快照替换 AI 行都得到相同结果。重连成本是 O(当前文本)，不是 O(历史)。

代价是明确的：快照恢复的是**可见文本尾部**，不是每一个中间事件。

## 快照刻意省略的内容

- reasoning delta：快照只报告 `has_reasoning`，不包含 reasoning 文本。
- 增量 tool-input JSON 与逐 delta 的 usage 遥测。
- 作为流事件的工具调用卡片；已 committed 的工具调用仍可在 trajectory ledger（`GET /sessions/:session_id/trajectory`）中查询。

这个划分是刻意的。文本是唯一无法从持久记录重建部分渲染的状态；工具调用已经有持久账本，重放它们的流帧只会重复一条已经存在的记录。

## 失败语义

- 快照之后到达的旧帧会被水位丢弃，永远不会让可见文本重复。
- `unknown` 与 `ended` 都会触发历史重新拉取；`gone` 不采取动作。
- 快照读取是一致的，因为它运行在流的事件循环上；一帧要么已经包含在 `ai_text` 中且不高于水位，要么更新并通过过滤。
- 按 socket 定向回复保持了多观看者的独立性：一个观看者的重连不会改变另一个观看者的游标或流。

## 被否定的方案

- **重连时重跑回合。** 会重复工具副作用和 token 消耗，还可能改变答案。
- **重放所有 delta。** 需要 per-viewer 历史、游标和保留策略；`seq` 水位解决的那个竞态会重新出现。
- **ZCode 的七事件锚点协议。** 它提供更细粒度的 reasoning/tool-input 恢复，但需要前端协议和持久化的 anchor/tail-discard 流水线。Sherry 的快照协议用更少的机制满足用户可见的恢复目标；更细的事件类型按设计不实现。

## 代码位置

- `server/service/stream_dispatch.py`：`StreamTurn`（`ai_text`、`_frame_seq`、`_ACTIVE_TURNS`）与 `active_turn_snapshot`。
- `server/trigger/ws/messages.py`：`_handle_resume_stream` 与 `_release_socket`。
- `runtime/session/ws_push.py`：恰好一次的广播，没有重放日志。
- `client/app/composables/bridge/agent-socket.ts`：断线检测与 `resume_stream`。
- `client/app/composables/bridge/agent-socket-frames.ts`：快照 `seq` 水位与帧过滤。
- `client/app/composables/use-chat-stream.ts`：快照替换与历史重新拉取。
- `tests/server/test_stream_resume.py`、`client/app/composables/__tests__/agent-socket-resume.test.ts`：上述不变量。
