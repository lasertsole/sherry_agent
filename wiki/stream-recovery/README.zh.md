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

## 持久化执行：崩溃后什么还活着

Sherry 的持久化模型是**「状态持久 + sweeper 兜底 + 新 turn 重驱动」**，不是 Temporal 式的 activity 重放。崩溃无法重放；能保证的是没有任何**状态**在说谎，恢复才有可信的依据。2026-10 对五个子系统（HITL、checkpointer、TaskFlow、subagent registry、evidence ledger）的审计落地了第一批修复：

| 缺陷 | 修复 |
| :--- | :--- |
| subagent registry 的终态判定用字符串字面量（`COMPLETED`、`FAILED`…）比对值为 `pending / running / interrupted / terminal` 的枚举——永不命中，保留上限形同虚设，内存无界增长 | 改为比对 `ExecutionStatus.TERMINAL`；上限现在保留活跃 run、驱逐旧的终态 run |
| 会话目录清扫用原始 session id 探测 checkpoint，而 checkpointer 存的是 `session_thread_id(session_id)`（INTEGER）——只剩 checkpoint 的会话被当成空目录删掉 | `pub.func` 里唯一的 `session_thread_id()` 映射，清扫点统一调用 |
| trajectory 文档写「写入时裁剪」，但 `prune_session` 没有调用者；其排序还用了钩子事件恒写 0 的 `MAX(seq)`，保留的是任意一批 turn | 写入时接线，按会话每 60 秒节流一次，按插入顺序（rowid）排序 |
| 全局 keep-latest checkpoint 裁剪跑在 `built_agent()` 里——即**每一轮**——且连接没有 `busy_timeout`，keep-set 在删除前就已过期（期间别的会话提交的新 checkpoint 会被误删） | 改为启动时一次（`server/service/checkpoint_maintenance.py`），在 `BEGIN IMMEDIATE` 内执行，keep-set 偏向顶层行 |
| 所有 checkpointer 连接都缺 `busy_timeout`（与 WAL 不同，它是每连接属性） | 统一走一个助手（`agent/checkpointer/pragmas.py`，30 秒） |
| `delete_thread_history` 无异常保护，数据库被锁时会在消息库已清空之后中断清理——半删除里最糟的那一半 | 每一步都有保护；checkpoint 删除改为**最先**执行，重试总能补完 |
| 终态 run 的交付状态只靠 30 秒周期 flush 落盘；崩溃落在窗口内会让 SQLite 停在 TERMINAL+PENDING——该状态没有重试路径，已完成的结果永久丢失 | 交付状态迁移（`delivered / failed / suspended / in_progress`）即时落盘（`set_run_durable`） |
| auto-turn 驱动继承了空实现的 `apply_hitl_pending`，headless turn 中断后图悬挂但会话看似空闲：回合结束的裁剪可能删掉那个 checkpoint，drain 还会把新消息注入悬挂图 | 驱动改为设置标志；`submit_user_input` 也把 checkpoint 里的 interrupt 当作 busy（探测 fail-open），这正是重启后留下的情形 |

第二批把审计剩余项全部落地，其中三项共享同一个洞察：重启失效的不只是数据，还有**时钟**与**仅存在于内存的标志**。

- **启动对账**（`server/service/boot_recovery.py`，对被 checkpoint 覆盖的会话目录做一次有上限的扫描）：图仍挂起的会话重建 HITL pending 标志（该寄存器只在内存，否则服务端报 IDLE，下一条消息会被当作普通回合派发而不是 `Command(resume=...)`）；转录末尾若有带工具调用却无结果的助手回合，用错误占位符加中断标记一次提交修复（确定性 id）；被**已不可能完成**的回合认领的 `CLAIMED` 队列行退回 `QUEUED`，而不是占着槽位等到 24 小时过期。每一步都失败开放且扫描有上限，坏会话只花掉自己那一次探测。
- **恢复出来的 RUNNING run 不是活的。** 没有任何任务能跨重启存活，因此加载时直接终态化（`pause_reason=process_restart`，outcome TIMEOUT）；继续留着 RUNNING 会让 WAITING flow 扫描以为子代理还活着，让孤儿回收等待一个永远不会到来的失联阈值。它的交付保持 PENDING，由 sweeper 的未交付扫描补发公告——拥有该 step 的 flow 因此能得知结果，而不是一直停在 DISPATCHED。
- **所有持久化时间戳在加载时重基准**（`_rebase_restored_clock`）。`time.monotonic()` 是进程相对的：上个进程写的值在这里可能读成数小时前、也可能读成未来，于是每个 `now - timestamp` 的过期判定都静默失效。截止时间（`archive_at`）给一个新的窗口而不是重基准到“现在”，否则第一次扫描就会归档。
- **会中断的工具在 headless 回合直接拒绝。** 审批门早已对无操作者的回合自动拒绝，但自己调用 `interrupt()` 的工具（`question`、`exit_plan_mode`、自我文件审批、通用 `interrupt_on` 策略、沙箱绕过）仍然挂起——一个无人能答的回合就此中断。现在它们拒绝并告诉模型继续（或停止），绝不重试。
- **trajectory 时间线是真的了。** 钩子事件与帧投影现在共享**同一个**按回合递增的序号（钩子过去传 `seq=0`，于是每个钩子事件都排在该回合第一帧之前），时间戳带毫秒，失败的流在重新抛出前记一条 `error` 事件——一条直接停住的时间线看起来就像“还在运行”。


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

## 轨迹面板

账本也有前端消费者：一个 SESSION 作用域的右栏标签
（`client/app/pages/home/components/TrajectoryPanel.vue`）读取
`GET /sessions/:session_id/trajectory`，按 turn 渲染时间线。它是调试视图，不属于重连协议：
打开时只取最新两页（每页 500 条，共 1000 条，触顶显示截断提示），刷新是按钮而不是
实时订阅。

事件按 `turn_id` 分组，再按 `(ts, seq, event_id)` 重排。原因是 `read()` 按 `seq`
排序，而所有生命周期事件（turn_start / turn_end / compact / model_retry /
invalid_tool_call）的 `seq=0`；不重排的话它们会全部排在工具行之前，`turn_end`
甚至会出现在最前面。工具行显示名称/状态/时长，并可展开参数与结果 JSON；生命周期行显示
kind 徽标与可展开 payload。头部提供过滤式搜索（n/m 计数）、两个折叠开关（工具行默认
折叠）、全部展开/折叠、按 turn 过滤和刷新。工具卡片上的历史按钮通过 `ChatBox.vue`
打开同一个去重标签。

纯函数（`groupTurns`、`summarizeTurns`、`formatDuration`、`previewOf`、
`eventMatches`）在 `client/app/composables/use-trajectory.ts`；bridge 调用是
`bridge/session.ts` 的 `fetchTrajectory`。

## 中断后续写

一轮回答可能在两种情况下被截断：用户点了停止，或心跳空闲超时。两者都会把
`[interrupted] {partial}` 标记写入 checkpoint，并在 MesMemory 里写一行
`[interrupted:<reason>]`。在此之上，被中断的 turn 还可能是**可续写**的。

取消路径会记录 `StateKey.RESUMABLE_BREAKPOINT`（mem + DB 双写，重启后仍在），
内容是标记 id、已生成的文本、以及推导标记 id 时的人类消息计数。只有**纯文本**
中断才记录：标记是本次新写入的、没有愈合的 tool call（悬空 tool call 需要先
决定重跑还是跳过），且确实有内容可续（可见文本或推理）。`stopped` 与 `done`
帧都带一个追加字段 `resumable`；`done` 也带它，因为心跳超时是以正常结束的方式
终止 turn 的。

前端的「继续生成」按钮发送 `continue_generation`。服务端先用实时 transcript 校验
断点（标记仍在末尾、计数未变），不满足则回 `stale`/`busy`；校验通过后**消耗**
断点（一次中断只能续一次），把标记改写成干净的部分文本，然后复用既有的
max_tokens 续写路径：模型看到自己未完成的回答加一条续写提示，继续写完。续写是
新的一轮；提示消息带 `provenance: continuation`，消息持久化会过滤它，不会渲染成
用户气泡。任何新的 turn（用户消息、HITL 恢复、自动 turn）都会清除断点，而续写
自身被中断时会写入新的断点。

同一套 turn 边界机制也支撑**编辑已发送的用户消息**：被编辑的轮次及其之后的内容
会从实时 checkpoint 中删除（`RemoveMessage`，因为只保留最新一个 checkpoint），
并从 MesMemory 硬删，然后用新的一轮重跑编辑后的文本，复用原来的 turn 编号。
客户端同时清掉 Dexie 缓存里的对应行，否则下一次历史合并会把它们复活。

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
