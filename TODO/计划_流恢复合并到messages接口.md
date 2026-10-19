# 计划：流恢复合并到 messages 接口 + 快照隔离队列

> **状态**：提案
> **范围**：客户端重连/连接协议改造，合并 `resume_stream` 与 `loadSessionHistory`；扩展 `active_turn_snapshot` 和流式帧携带 `turn_id`
> **前置条件**：无（独立于 `计划_agent工具层清理.md` 的 Plan A/B）
> **风险等级**：中-高 —— 涉及流式协议层改动，影响所有重连路径

## 1. 设计问题

现状的流恢复协议有两个问题：

### 1.1 重连需要两次 RPC，且存在可见性缺口

- `resume_stream` WS 帧：拿当前运行中回合快照
- 等当前回合 `done` 时 `loadSessionHistory` HTTP：补断线期间完成的整条消息

可见性缺口：断线期间完成的整条消息要等当前回合结束才显示，用户重连后会看到"我之前问的问题答案呢"的空缺。

### 1.2 客户端靠 `streamingTurn.value` 路由，回合切换时错乱

代码证据：

- `server/service/stream_dispatch.py:485` `turn_id = current_turn_id.get()` —— `turn_id` 是个 contextvar，不是 `StreamTurn` 实例属性
- `StreamTurn` 类只有 `session_id`、`ai_text`、`_frame_seq` 等字段，**没有 turn_num/turn_id**
- `active_turn_snapshot`（`server/service/stream_dispatch.py:253-262`）只返回 `{text, seq, has_reasoning}`，**不带 turn 标识**
- WS 流式帧只有 `frame["seq"] = self._frame_seq`（`:500`），**不带 turn_num**

客户端现状靠 `streamingTurn.value` 假设所有流式帧都属于当前回合。重连期间发生回合切换（用户排队、`PlanContinuationEnforcer` 自动续跑、`subagent completion` 注入、`cron` 触发）时，快照和帧都会路由到错误的回合行。

## 2. 方案核心：快照隔离队列 + 合并 RPC

把 `resume_stream` 和 `loadSessionHistory` 合并：客户端重连/连接时只发一次 `loadSessionHistory?include_active_turn=1`，后端在返回历史的同时附带当前运行中回合的快照。

客户端在等响应期间，**所有 WS 帧入队不 apply**；响应到达后一次性 apply（messages 刷新 + 快照替换 + 弹队列追加）。

### 数据流

```
客户端                              后端
──────                              ────
重连/连接
   │
   ├─→ GET /sessions/:id/messages?include_active_turn=1
   │                                    查 messages 表（持久化的整条消息）
   │                                    调 active_turn_snapshot()（当前运行中回合）
   │                                    返回 {messages: [...], active_turn: {text, seq, has_reasoning, turn_id} | null}
   │←───────────────────────────────────
   │
   │ 期间收到 WS 帧 → 入队（不 apply）
   │
   │ 响应到达：
   │   1. 用 messages 刷新 chatMessages（拿到断线期间完成的整条消息）
   │   2. 如果有 active_turn：
   │      - 按 turn_id 找到/新建对应的 AI 行
   │      - 用 active_turn.text 替换该行
   │      - 设置水位 seq = active_turn.seq
   │   3. 弹队列：
   │      - 帧的 seq <= 水位 → 丢弃
   │      - 帧的 seq > 水位 → 按帧的 turn_id 路由到对应行，追加
   │
   │ 后续 WS 帧 → 正常 apply（seq > 水位才追加）
```

## 3. 扩展 1：`active_turn_snapshot` 返回 `turn_id`

让客户端拿到快照时**知道它属于哪个回合**，正确路由到对应的 chatMessages 行。

### 后端改动

`server/service/stream_dispatch.py`：

```python
class StreamTurn:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.turn_id = current_turn_id.get("")    # 新增：从 contextvar 读
        # ... 原有字段保持不变 ...

def active_turn_snapshot(session_id: str) -> dict[str, Any] | None:
    engine = _ACTIVE_TURNS.get(session_id)
    if engine is None:
        return None
    return {
        "text": engine.ai_text,
        "has_reasoning": engine._has_reasoning,
        "seq": int(getattr(engine, "_frame_seq", 0)),
        "turn_id": engine.turn_id,    # 新增字段
    }
```

### 影响

- `_handle_resume_stream`（WS 路径，`server/trigger/ws/messages.py:365`）的回复**自动**带 `turn_id`（同一个函数）
- HTTP messages handler 调用 `active_turn_snapshot` 时也带 `turn_id`
- 客户端 `onStreamResumed` 的 `ok` 分支用 `turn_id` 路由，不再靠 `streamingTurn.value` 假设

## 4. 扩展 2：WS 流式帧带 `turn_id`

让队列里的帧能按 turn 路由——这样弹队列时能正确分发到不同回合的行。

### 后端改动

`server/service/stream_dispatch.py` 所有 `yield frame` 之前：

```python
frame["seq"] = self._frame_seq
frame["turn_id"] = self.turn_id    # 新增
yield frame
```

涉及 yield 点：`server/service/stream_dispatch.py:501, 533, 536, 544, 547, 614, 619`（grep 已确认）。

### 客户端改动

`client/app/composables/use-chat-stream.ts` 收到流式帧时：

- 读 `frame.turn_id`，按它路由到对应 chatMessages 行（找不到则新建）
- 不再靠 `streamingTurn.value` 假设

### 影响

- 协议层改动，所有流式帧都多一个字段
- 客户端帧处理逻辑要重写路由部分
- **向后兼容**：新增字段，老版本客户端不读它不报错

## 5. 客户端队列隔离逻辑

### 队列结构

```typescript
// client/app/composables/use-chat-stream.ts 新增
const pendingFramesQueue: Frame[] = [];
let isAwaitingSnapshot = false;   // 是否在等 loadSessionHistory 响应

const onWsFrame = (frame: Frame) => {
  if (isAwaitingSnapshot) {
    pendingFramesQueue.push(frame);   // 入队，不 apply
    return;
  }
  applyFrame(frame);   // 正常 apply
};

const onLoadSessionHistoryResponse = (response: { messages, active_turn }) => {
  // 1. 用 messages 刷新 chatMessages
  mergeMessages(response.messages);
  // 2. 如果有 active_turn，应用快照
  if (response.active_turn) {
    applyActiveTurnSnapshot(response.active_turn);
  }
  // 3. 弹队列
  isAwaitingSnapshot = false;
  for (const frame of pendingFramesQueue) {
    if (response.active_turn && frame.seq <= response.active_turn.seq) {
      continue;   // 水位及以下丢弃
    }
    applyFrame(frame);   // 按帧的 turn_id 路由
  }
  pendingFramesQueue.length = 0;
};
```

### 调用时机

- **开始连接**（session 切换）：`client/app/pages/home/index/[sid].vue:937` 已经调 `loadSessionHistory(sid)` → 改成带 `include_active_turn=true` + 设 `isAwaitingSnapshot=true`
- **重连**：WS 重连成功后，**不再发 `resume_stream`**，改成发 `loadSessionHistory?include_active_turn=true` + 设 `isAwaitingSnapshot=true`

### `applyActiveTurnSnapshot` 复用现有逻辑

抽 `use-chat-stream.ts:180-192` 的 `ok` 分支：

```typescript
function applyActiveTurnSnapshot(snapshot: { text, seq, has_reasoning, turn_id }) {
  chunks.flushPendingText?.();
  // 按 turn_id 找到对应 AI 行（找不到则新建）
  let row = chatMessages.value.find(r => r.role === AI && r.turn_id === snapshot.turn_id);
  if (!row) {
    row = { role: AI, turn_id: snapshot.turn_id, turn_num: ???, content: '' };
    chatMessages.value.push(row);
  }
  row.content = snapshot.text;
  // 设置水位
  socketCtx.setSnapshotSeq(snapshot.seq);
  socketCtx.markAllReceivedChunk();
}
```

`turn_num` 是客户端展示用的，`turn_id` 是路由用的。`active_turn` 可以同时返回 `turn_num`（如果后端能拿到），或者客户端按 `turn_id` 找到行后用 messages 里同 `turn_id` 的 `turn_num` 补齐。

## 6. 边界处理

### 6.1 响应超时兜底

```typescript
const SNAPSHOT_TIMEOUT_MS = 10_000;
const onLoadSessionHistoryWithTimeout = async (sid: string) => {
  isAwaitingSnapshot = true;
  try {
    const response = await withTimeout(
      api.get(`/sessions/${sid}/messages`, { params: { include_active_turn: true } }),
      SNAPSHOT_TIMEOUT_MS
    );
    onLoadSessionHistoryResponse(response);
  } catch (e) {
    // 超时或失败：放弃快照路径，直接弹队列（按 turn_id 路由，没有水位保护）
    isAwaitingSnapshot = false;
    for (const frame of pendingFramesQueue) {
      applyFrame(frame);   // 没有 active_turn，按帧自己的 turn_id 路由
    }
    pendingFramesQueue.length = 0;
  }
};
```

### 6.2 队列长度上限

```typescript
const MAX_QUEUE_SIZE = 1000;
const onWsFrame = (frame: Frame) => {
  if (isAwaitingSnapshot) {
    if (pendingFramesQueue.length >= MAX_QUEUE_SIZE) {
      pendingFramesQueue.shift();   // 丢老的
    }
    pendingFramesQueue.push(frame);
    return;
  }
  applyFrame(frame);
};
```

### 6.3 `done` 帧的处理

`done` 帧也入队。响应到达后弹队列时，`done` 帧的处理顺序：先 apply messages（拿到完整答案）→ 再 apply `done` 帧（清空 `streamingTurn`、停止 spinner）。`use-chat-stream.ts:441-444` 的 `loadSessionHistory` 调用保持，但放在弹队列末尾。

### 6.4 用户在等响应期间主动发消息

用户发的消息走 `sendByMsgId`，不走 `applyFrame`——不受队列影响。如果当前回合在跑，服务端会排队；客户端可以乐观显示用户消息（行内 pending 状态），等服务端推送时 dedup。

### 6.5 重连期间多回合切换

`turn_runner` 串行化回合，同一时刻只有一个回合在发帧。所以队列里的帧按到达顺序就是按时间顺序，单回合内 seq 单调。多个回合的帧按 `turn_id` 区分，弹队列时按 `turn_id` 路由到对应行。

## 7. 分阶段执行

### Stage C.0 — 准备（无代码改动）

| 检查项 | 命令 | 预期 |
|---|---|---|
| 前端 baseline | `cd client && pnpm test:unit && pnpm test:integration` | 通过 |
| 后端 baseline | `uv run --no-sync pytest tests/server/test_stream_resume.py -q` | 通过 |
| 审计 `useSessionLifecycle` | grep `loadSessionHistory` 实现 | 找到接口实现位置 |
| 审计流式帧 yield 点 | grep `yield frame` `server/service/stream_dispatch.py` | 列出所有点，确认扩展 2 改动面 |
| 审计 `current_turn_id` contextvar 可用性 | grep `current_turn_id.set` | 确认 StreamTurn 跑在 turn_scope 内 |

### Stage C.1 — 后端：扩展 1 + 扩展 2

**动作：**

1. `StreamTurn.__init__` 加 `self.turn_id = current_turn_id.get("")`
2. `active_turn_snapshot` 返回字段加 `turn_id`
3. 所有 `yield frame` 之前加 `frame["turn_id"] = self.turn_id`（7 个 yield 点）
4. 更新 `tests/server/test_stream_resume.py` 断言：`active_turn_snapshot` 返回带 `turn_id`；流式帧带 `turn_id`

**验证：**

```bash
uv run --no-sync pytest tests/server/test_stream_resume.py tests/server/service/test_stream_dispatch.py -q
uv run --with ruff ruff check server/service/stream_dispatch.py
uv run --no-sync basedpyright server/service/stream_dispatch.py
```

**出口条件**：后端测试全绿；`active_turn_snapshot` 返回的 dict 含 `turn_id` 字段；所有流式帧 yield 点都带 `turn_id`。

### Stage C.2 — 后端：messages 接口扩展

**动作：**

1. `server/trigger/http/messages.py:63` 的 handler 加 `include_active_turn` query 参数
2. 如果参数为 true，调 `active_turn_snapshot(session_id)`，附加到 response
3. response shape：

   ```json
   {
     "messages": [...],
     "active_turn": {"text": "...", "seq": 8, "has_reasoning": true, "turn_id": "abc-123"} | null
   }
   ```

4. 测试：`tests/server/trigger/test_messages_http.py`（如果有）或新建

**验证：**

```bash
uv run --no-sync pytest tests/server/trigger/ -q -k "messages"
uv run --with ruff ruff check server/trigger/http/messages.py
```

**出口条件**：`GET /sessions/:id/messages?include_active_turn=1` 返回 `active_turn` 字段（有运行中回合时非 null）。

### Stage C.3 — 客户端：队列隔离 + 路由按 `turn_id`

**动作：**

1. `client/app/composables/use-chat-stream.ts`：
   - 加 `pendingFramesQueue`、`isAwaitingSnapshot`、`onWsFrame` 包装
   - 抽 `applyActiveTurnSnapshot` 复用原 `ok` 分支逻辑
   - 改 `loadSessionHistory` 调用：带 `include_active_turn=true` + 设 `isAwaitingSnapshot=true` + 响应到达后弹队列
   - 流式帧处理改为按 `frame.turn_id` 路由，不再靠 `streamingTurn.value` 假设
   - 加超时兜底（10s）+ 队列上限（1000 帧）
2. `client/app/pages/home/index/[sid].vue:937` 的 `loadSessionHistory(sid)` 调用扩展为带 `include_active_turn`
3. `client/app/composables/bridge/agent-socket.ts`：重连时不再发 `resume_stream`，改触发 `loadSessionHistory` 流程
4. 更新 `client/app/composables/__tests__/agent-socket-resume.test.ts` 和相关测试

**验证：**

```bash
cd client && pnpm test:unit && pnpm test:integration && pnpm run dpdm
cd client && pnpm typecheck
```

**出口条件**：前端测试全绿；重连场景测试覆盖：当前回合未切换、回合切换、服务端自动续跑、响应超时。

### Stage C.4 — 废弃 `resume_stream` WS 帧（渐进）

**动作：**

1. 重连路径改走 `loadSessionHistory` 后，客户端不再发 `resume_stream` 帧
2. 保留后端 `_handle_resume_stream` handler 一段时间作为 fallback
3. 在某个后续版本删除 `_handle_resume_stream`（breaking change，需要 release note）

**验证：**

```bash
grep -rn "resume_stream" client/app/   # 期望：零命中（除了类型定义/注释）
```

**出口条件**：客户端不再发送 `resume_stream` 帧；后端 handler 保留但无调用。

### Stage C.5 — 文档 + 全量验证

**动作：**

1. 更新 `docs/stream-recovery/README*.md` 四语言版本，重写协议描述：
   - 移除"resume_stream 帧"机制
   - 新增"messages 接口带 active_turn"机制
   - 新增"客户端队列隔离"机制
2. 更新 `AGENTS.md` 的 Stream Broadcast & Reconnect Resume 段
3. 跑全量验证：

   ```bash
   uv run --no-sync python tests/run_tests_split.py
   uv run --no-sync pytest tests/perf -q -s
   cd client && pnpm test:unit && pnpm test:integration
   ./scripts/verify_feature.sh
   uv run --no-sync python scripts/check_docs_parity.py
   uv run --no-sync python scripts/check_doc_links.py
   ```

**出口条件**：所有 gate 全绿；四语言 README 同步更新；`AGENTS.md` 描述与新协议一致。

## 8. 关键风险与缓解

| 风险 | 可能性 | 缓解 |
|---|---|---|
| 扩展 2 是协议层改动，影响所有流式帧 | 中 | Stage C.1 加 `turn_id` 字段时是**新增**字段，客户端老版本不读它不报错（向后兼容） |
| 客户端队列逻辑复杂，边界多 | 中 | Stage C.0 充分审计；Stage C.3 写单元测试覆盖所有边界（4 种重连场景 + 超时 + 队列上限） |
| 重连路径改动影响所有用户 | 高 | 渐进废弃 `resume_stream`——C.3 完成后 `resume_stream` 仍可用作 fallback，C.4 才正式停发 |
| 响应超时时队列积压 | 低 | 10s 超时 + 1000 帧上限兜底 |
| 扩展 1 依赖 `current_turn_id` contextvar 可用 | 低 | `current_turn_id.get("")` 用默认值空串，StreamTurn 跑在 `turn_scope` 内必然能拿到 |
| 客户端按 `turn_id` 路由找不到行 | 中 | `applyActiveTurnSnapshot` 找不到则新建行（带 `turn_id` + `turn_num` 从 messages 同步） |
| `useSessionLifecycle` 接口破坏 | 中 | Stage C.0 审计其签名；扩展 `loadSessionHistory` 用可选参数，不破坏现有调用 |
| 多观看者场景下 A 的队列阻塞 B | 低 | 队列是客户端本地状态，不影响服务端广播；B 实时收帧不受影响 |

## 9. 明确不在范围

- `taskflow_refresh` / `todo_refresh` / `search_backend_fallback` 等其他 WS 帧——不受影响
- `MessagePersistence` 中间件——不动（消息持久化逻辑不变）
- `active_turn_snapshot` 的读取逻辑本身——只加字段，不改读取实现
- 服务端自动续跑回合不发 `turn_started` 的问题——本方案让流式帧带 `turn_id` 后，自动续跑的帧也能正确路由（不再依赖 `turn_started`）
- `Plan A`（evidence-ledger 抽出）/ `Plan B`（wave 削减）——独立，可任意顺序跑

## 10. 回滚

每个阶段是一个 commit（推荐）。

- Stage C.1+C.2 是后端扩展，向后兼容（新增字段），单独 revert 不影响现有功能
- Stage C.3 是客户端协议切换，revert 后回到 `resume_stream` 路径
- Stage C.4 是废弃动作，revert 恢复 `resume_stream` 调用
- Stage C.5 是文档，revert 是无害的

最坏情况：Stage C.3 上线后发现问题，revert 回 Stage C.2 状态——后端 `turn_id` 字段仍在（无害），客户端继续走 `resume_stream` 老路径。

## 11. 验收清单

- [ ] Stage C.0：baseline 全绿；`useSessionLifecycle` 实现审计完成；yield 点清单确认
- [ ] Stage C.1：`active_turn_snapshot` 返回 `turn_id`；所有流式帧 yield 点带 `turn_id`；`tests/server/test_stream_resume.py` 更新
- [ ] Stage C.2：`GET /sessions/:id/messages?include_active_turn=1` 返回 `active_turn` 字段
- [ ] Stage C.3：客户端队列隔离逻辑实现；超时兜底（10s）；队列上限（1000）；流式帧按 `turn_id` 路由
- [ ] Stage C.3 测试覆盖：当前回合未切换、回合切换、服务端自动续跑、响应超时
- [ ] Stage C.4：客户端不再发送 `resume_stream` 帧（后端 handler 保留）
- [ ] Stage C.5：四语言 README + AGENTS.md 更新；`tests/run_tests_split.py` + perf + `verify_feature.sh` + doc gates 全绿
- [ ] 重连场景 e2e：用户排队多消息 + 断线重连，所有消息完整显示，无错乱
- [ ] 重连场景 e2e：服务端自动续跑期间用户断线重连，自动续跑的内容正确显示

## 12. 与 `计划_agent工具层清理.md` 的关系

完全独立。`Plan A`（evidence-ledger 抽出）和 `Plan B`（wave 削减）都是 agent/tools 工程清理，跟流恢复协议无关。本计划（Plan C）是 client/server 协议层改动，可以前后任意顺序跑，互不影响。

如果一定要排顺序：建议先做 Plan B（wave 削减会让 `taskflow_updated` payload 简化），再做 Plan C（流恢复重写时不用考虑 wave 字段）；但这个顺序约束很弱，不强制。
