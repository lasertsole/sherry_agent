# HumanInTheLoop 中间件闭环集成 — 升级说明书

> 本文档描述如何将旧的 sherry_agent 项目升级，使其在 agent/core.py 的 build_agent 中加入 HumanInTheLoop 中间件，并在 client_future 实现前端交互闭环。Agent 可仅凭本文档完成升级，无需阅读项目源码。

---

## 一、改动文件清单

| # | 文件路径 | 操作 | 说明 |
|---|---------|------|------|
| 1 | `agent/middlewares/HumanInTheLoop/gates.py` | 修改 | 补充缺失的 `import threading` |
| 2 | `agent/core.py` | 修改 | 导入并注册 `HumanInTheLoop` 中间件 |
| 3 | `server/service/messages.py` | 修改 | 新增 `get_pending_interrupt` 和 `resume_agent` 函数 |
| 4 | `server/service/__init__.py` | 修改 | 导出新函数 |
| 5 | `server/trigger/ws/messages.py` | 修改 | WS 处理器支持 hitl_request / hitl_response |
| 6 | `client_future/app/composables/bridge.ts` | 修改 | 新增 HITL 类型、回调、sendHitlResponse |
| 7 | `client_future/app/composables/messages.ts` | 修改 | postAgentStream 透传 onHitl 回调 |
| 8 | `client_future/app/pages/home/type.ts` | 修改 | 新增 HitlRequestData 接口 |
| 9 | `client_future/app/pages/home/index.vue` | 修改 | 新增审批弹窗 UI + HITL 状态管理 |
| 10 | `tests/unit/test_hitl_integration.py` | 新增 | Python 单元测试 (62 个) |
| 11 | `client_future/app/composables/__tests__/hitl-bridge.test.ts` | 新增 | TypeScript 测试 (13 个) |

---

## 二、背景与原理

### 2.1 什么是 HumanInTheLoop

项目已有 `agent/middlewares/HumanInTheLoop/` 目录，内含完整的 HITL 中间件实现（审批管线、中断管理、写入审批门控等）。该中间件通过 `langgraph.types.interrupt()` 暂停 agent 执行，等待人类决策后通过 `Command(resume=...)` 恢复。

### 2.2 闭环数据流

```
用户发送消息
    │
    ▼
WebSocket /sessions/agent/ws
    │
    ▼
async_generate() ──► agent.astream()
    │
    │  (HumanInTheLoop 中间件检测到危险工具调用)
    │  调用 interrupt() 暂停
    │
    ▼
流结束，get_pending_interrupt() 检测到中断
    │
    ▼
服务端发送 {event: "hitl_request", content: {tool_name, tool_args, ...}}
    │
    ▼
前端 bridge.ts onmessage 收到 hitl_request
    │  调用 onHitl 回调
    │  弹出审批弹窗（不关闭 WebSocket）
    │
    ▼
用户点击 Approve / Reject
    │
    ▼
controller.sendHitlResponse({decision, message})
    │  通过同一 WebSocket 发送 {type: "hitl_response", decision, message}
    │
    ▼
服务端收到 hitl_response，调用 resume_agent()
    │  使用 Command(resume={decisions: [{type: decision}]})
    │  继续流式输出
    │
    ▼
前端继续接收 chunk → done
```

### 2.3 WebSocket 协议变更

**服务端 → 客户端新增事件：**

```json
{
  "event": "hitl_request",
  "session_id": "main",
  "content": {
    "tool_name": "terminal",
    "tool_args": {"command": "rm -rf /tmp"},
    "description": "Dangerous command: rm -rf /tmp",
    "allowed_decisions": ["approve", "reject"]
  }
}
```

**客户端 → 服务端新增消息：**

```json
{
  "type": "hitl_response",
  "session_id": "main",
  "decision": "approve",
  "message": "",
  "edited_args": null
}
```

`decision` 可选值：`"approve"` | `"reject"` | `"edit"`（edit 时需带 `edited_args`）。

---

## 三、逐文件改动详情

### 3.1 `agent/middlewares/HumanInTheLoop/gates.py` — 补充 import

**原因：** `InterruptManager.set_interrupt()` 使用了 `threading.Event()`，但文件顶部未导入 `threading`。

**改动：** 在 `from __future__ import annotations` 之后、`import time` 之前，插入一行：

```python
# ---- 改动前 ----
from __future__ import annotations

import time
import uuid

# ---- 改动后 ----
from __future__ import annotations

import threading
import time
import uuid
```

---

### 3.2 `agent/core.py` — 注册 HumanInTheLoop 中间件

**改动 1：** import 行增加 `HumanInTheLoop, HITLConfig`

```python
# ---- 改动前 ----
from .middlewares import (Summarization, ToolCallNormalize, MultimodalProcessor, ContextEngineHook, ToolGuardrails,
                          IterationBudget, HeartbeatStaleness)

# ---- 改动后 ----
from .middlewares import (Summarization, ToolCallNormalize, MultimodalProcessor, ContextEngineHook, ToolGuardrails,
                          IterationBudget, HeartbeatStaleness, HumanInTheLoop, HITLConfig)
```

**改动 2：** 在 `built_agent()` 函数的 `middleware=[...]` 列表中，`HeartbeatStaleness()` 之后、`Summarization(...)` 之前，插入 `HumanInTheLoop()`

```python
# ---- 改动前 ----
            middleware = [
                ContextEngineHook(),
                MultimodalProcessor(),
                IterationBudget(90),
                ToolGuardrails(),
                ToolCallNormalize(),
                HeartbeatStaleness(),
                Summarization(

# ---- 改动后 ----
            middleware = [
                ContextEngineHook(),
                MultimodalProcessor(),
                IterationBudget(90),
                ToolGuardrails(),
                ToolCallNormalize(),
                HeartbeatStaleness(),
                HumanInTheLoop(),
                Summarization(
```

> 位置说明：HumanInTheLoop 必须在 Summarization 之前，因为 Summarization 可能压缩消息，而 HITL 需要读取完整的最近 AI 消息中的 tool_calls。

---

### 3.3 `server/service/messages.py` — 新增中断检测与恢复

**改动 1：** 文件顶部 import 区，在最后一行 `from langchain_core.messages import ...` 之后新增：

```python
from langgraph.types import Command
```

**改动 2：** 在 `async_generate` 函数之后（`"""End response generation logic"""` 之后），新增两个函数。完整代码如下，直接粘贴到文件中：

```python
"""HITL interrupt detection — checks agent state for pending interrupts.

When the HumanInTheLoop middleware calls ``interrupt()``, the agent stream
ends and the interrupt payload is stored in the graph state's ``tasks``.
This function inspects the state and returns the interrupt request so
the WebSocket layer can forward it to the client for human approval.
"""
async def get_pending_interrupt(session_id: str) -> dict[str, Any] | None:
    """Return the pending HITL interrupt payload for a session, or ``None``.

    The returned dict has the shape::

        {
            "tool_name": str,
            "tool_args": dict,
            "description": str,
            "allowed_decisions": list[str],
        }
    """
    agent = await built_agent()
    config = build_agent_config(session_id)
    state = await agent.aget_state(config=config)

    for task in getattr(state, "tasks", []):
        if hasattr(task, "interrupts") and task.interrupts:
            for intr in task.interrupts:
                value = getattr(intr, "value", None)
                if value is None:
                    continue
                action_requests = value.get("action_requests", []) if isinstance(value, dict) else []
                review_configs = value.get("review_configs", []) if isinstance(value, dict) else []
                if not action_requests:
                    continue
                ar = action_requests[0]
                rc = review_configs[0] if review_configs else {}
                return {
                    "tool_name": ar.get("name", "unknown"),
                    "tool_args": ar.get("args", {}),
                    "description": ar.get("description", ""),
                    "allowed_decisions": rc.get("allowed_decisions", ["approve", "reject"]),
                }
    return None
"""End HITL interrupt detection"""

"""HITL resume — continues the agent after a human decision.

Called when the client sends back an approval/rejection. Uses
``Command(resume=...)`` to un-pause the graph and streams the
remaining output just like ``async_generate``.
"""
async def resume_agent(
    session_id: str,
    decision: str,
    message: str = "",
    edited_args: dict[str, Any] | None = None,
) -> AsyncGenerator[dict[str, str], None]:
    """Resume the agent after a HITL interrupt.

    Args:
        session_id:  Active session ID.
        decision:    ``"approve"``, ``"reject"``, or ``"edit"``.
        message:     Optional user message accompanying the decision.
        edited_args: When ``decision == "edit"``, the new tool arguments.

    Yields:
        Same chunk format as :func:`async_generate`.
    """
    start_time = time.time()
    logger.info(
        f"Agent resume started: session_id={session_id}, decision={decision}"
    )

    agent = await built_agent()
    config = build_agent_config(session_id)

    resume_value: dict[str, Any] = {"decisions": [{"type": decision, "message": message}]}
    if decision == "edit" and edited_args is not None:
        resume_value["decisions"][0]["edited_action"] = {"args": edited_args}

    state_register_mem.set_state(session_id, "answering", True)

    try:
        yield {"type": "text", "content": f"{ASSISTANT_NAME}:"}

        async for chunk in agent.astream(
            Command(resume=resume_value),
            config=config,
            stream_mode=["messages", "updates"],
        ):
            if state_register_mem.get_state(session_id, "answering") == False:
                raise asyncio.CancelledError

            mode: str = chunk[0]
            data: Any = chunk[1]
            if mode != "messages":
                continue

            msg_chunk: BaseMessage = data[0]
            metadata: dict[str, Any] = data[1]

            if metadata.get("langgraph_node", None) != "model" or metadata.get("lc_source") == "summarization":
                continue

            if isinstance(msg_chunk, AIMessageChunk):
                tool_calls = msg_chunk.tool_calls if msg_chunk.tool_calls and len(msg_chunk.tool_calls) > 0 else msg_chunk.tool_call_chunks
                if len(tool_calls) > 0 or state_register_mem.get_state(session_id, "current_tool_id", "").strip():
                    repeat_flag = True
                    if len(tool_calls) > 0:
                        tool_call = tool_calls[0]
                        if tool_call["name"]:
                            if tool_call["name"].strip() or tool_call["name"].strip() != state_register_mem.get_state(session_id, "current_tool_name"):
                                state_register_mem.set_state(session_id, "current_tool_name", tool_call['name'])
                        if tool_call["id"]:
                            if tool_call["id"].strip() or tool_call["id"].strip() != state_register_mem.get_state(session_id, "current_tool_id"):
                                state_register_mem.set_state(session_id, "current_tool_id", tool_call['id'])
                                repeat_flag = False
                    if not repeat_flag:
                        tool_name = state_register_mem.get_state(session_id, "current_tool_name", "")
                        yield {"type": "tool_start", "content": tool_name}

                if state_register_mem.get_state(session_id, "current_tool_id", "").strip() and msg_chunk.content is not None and msg_chunk.content:
                    tool_name = state_register_mem.get_state(session_id, "current_tool_name", "")
                    yield {"type": "tool_end", "content": tool_name}
                    state_register_mem.set_state(session_id, "current_tool_id", "")

                if len(msg_chunk.content) > 0:
                    yield {"type": "text", "content": msg_chunk.content}

        elapsed = time.time() - start_time
        logger.debug(
            f"Agent resume completed: session_id={session_id}, duration={elapsed:.2f}s"
        )
    except asyncio.CancelledError:
        yield {"type": "text", "content": "Request cancelled"}
        logger.debug(f"Agent resume cancelled: session_id={session_id}")
    except HeartbeatTimeoutError as e:
        yield {"type": "text", "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated."}
        logger.warning(f"Agent resume heartbeat timeout: session_id={session_id}, error={e}")
    except Exception as e:
        logger.error(f"Agent resume failed: session_id={session_id}, error={str(e)}")
        logger.exception(e)
        raise e
    finally:
        state_register_mem.set_state(session_id, "current_tool_name", "")
        state_register_mem.set_state(session_id, "current_tool_id", "")
        state_register_mem.set_state(session_id, "answering", False)
"""End HITL resume"""
```

---

### 3.4 `server/service/__init__.py` — 导出新函数

```python
# ---- 改动前 ----
from .heartbeat import process_heartbeat_task, process_heartbeat_notify
from .messages import async_generate, clear_session, get_history_by_turn_page
from .workplace import (read_system_prompt_file, write_system_prompt_file, update_system_prompt_file, read_character,
                        write_character, update_character)

# ---- 改动后 ----
from .heartbeat import process_heartbeat_task, process_heartbeat_notify
from .messages import async_generate, clear_session, get_history_by_turn_page, get_pending_interrupt, resume_agent
from .workplace import (read_system_prompt_file, write_system_prompt_file, update_system_prompt_file, read_character,
                        write_character, update_character)
```

---

### 3.5 `server/trigger/ws/messages.py` — WebSocket 处理器支持 HITL

**改动 1：** import 行增加 `get_pending_interrupt, resume_agent`

```python
# ---- 改动前 ----
from server.service import async_generate

# ---- 改动后 ----
from server.service import async_generate, get_pending_interrupt, resume_agent
```

**改动 2：** 在 `if obj.get("type") == "stop":` 代码块之后、`multi_modal_message_data = obj.get(...)` 之前，插入 hitl_response 处理分支：

```python
                # HITL resume — client sends back a human decision after an interrupt
                if obj.get("type") == "hitl_response":
                    decision: str = obj.get("decision", "reject")
                    hitl_message: str = obj.get("message", "")
                    edited_args: dict[str, Any] | None = obj.get("edited_args")
                    logger.info(
                        f"Agent WS HITL resume: session_id={session_id}, decision={decision}"
                    )
                    try:
                        async for chunk in resume_agent(
                            session_id, decision, hitl_message, edited_args,
                        ):
                            await websocket.send_text(json.dumps({
                                "event": "chunk", "session_id": session_id,
                                "type": chunk["type"], "content": chunk["content"],
                            }))

                        # Check for another interrupt after resume
                        interrupt_data = await get_pending_interrupt(session_id)
                        if interrupt_data:
                            await websocket.send_text(json.dumps({
                                "event": "hitl_request", "session_id": session_id,
                                "content": interrupt_data,
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "event": "done", "session_id": session_id, "content": "",
                            }))
                    except Exception as e:
                        logger.error(
                            f"Agent WS HITL resume failed: session_id={session_id}, error={str(e)}"
                        )
                        await websocket.send_text(json.dumps({
                            "event": "error", "session_id": session_id, "content": str(e),
                        }))
                    continue
```

**改动 3：** 在正常消息流的 `async for chunk in async_generate(...)` 循环之后，将原来直接发送 `done` 的逻辑改为先检查中断：

```python
# ---- 改动前 ----
                    async for chunk in async_generate(session_id, multi_modal_message):
                        await websocket.send_text(json.dumps({"event": "chunk", "session_id": session_id, "type": chunk["type"], "content": chunk["content"]}))

                    await websocket.send_text(json.dumps({"event": "done", "session_id": session_id, "content": ""}))
                    elapsed = time.time() - start_time
                    logger.info(
                        f"Agent WS request completed: session_id={session_id}, duration={elapsed:.2f}s"
                    )

# ---- 改动后 ----
                    async for chunk in async_generate(session_id, multi_modal_message):
                        await websocket.send_text(json.dumps({"event": "chunk", "session_id": session_id, "type": chunk["type"], "content": chunk["content"]}))

                    # After stream ends, check if the agent paused for HITL approval
                    interrupt_data = await get_pending_interrupt(session_id)
                    if interrupt_data:
                        logger.info(
                            f"Agent WS HITL interrupt detected: session_id={session_id}, "
                            f"tool={interrupt_data.get('tool_name')}"
                        )
                        await websocket.send_text(json.dumps({
                            "event": "hitl_request", "session_id": session_id,
                            "content": interrupt_data,
                        }))
                    else:
                        await websocket.send_text(json.dumps({"event": "done", "session_id": session_id, "content": ""}))
                    elapsed = time.time() - start_time
                    logger.info(
                        f"Agent WS request completed: session_id={session_id}, duration={elapsed:.2f}s"
                    )
```

---

### 3.6 `client_future/app/composables/bridge.ts` — 前端桥接层

**改动 1：** `AgentWsEventType` 增加 `'hitl_request'`

```typescript
// ---- 改动前 ----
export type AgentWsEventType = 'chunk' | 'done' | 'error' | 'stopped';

// ---- 改动后 ----
export type AgentWsEventType = 'chunk' | 'done' | 'error' | 'stopped' | 'hitl_request';
```

**改动 2：** 在 `AgentChunkType` 定义之后、`AgentWsEvent` 之前，新增 HITL 类型定义：

```typescript
/** HITL interrupt payload sent by the server when the agent pauses for human approval. */
export interface HitlInterruptData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}

/** HITL decision sent by the client to resume the agent. */
export interface HitlResponse {
  decision: 'approve' | 'reject' | 'edit';
  message?: string;
  edited_args?: Record<string, unknown>;
}
```

**改动 3：** 在 `OnChunkCallback` 定义之后新增 `OnHitlCallback`

```typescript
/** HITL callback: invoked when the server signals an interrupt requiring human approval. */
export type OnHitlCallback = (data: HitlInterruptData) => void;
```

**改动 4：** `StreamController` 接口增加 `sendHitlResponse` 方法

```typescript
// ---- 改动前 ----
export interface StreamController {
  readonly closed: boolean;
  abort(): void;
}

// ---- 改动后 ----
export interface StreamController {
  readonly closed: boolean;
  abort(): void;
  /** Send a HITL decision back to the server on the active WebSocket. */
  sendHitlResponse?(response: HitlResponse): void;
}
```

**改动 5：** `streamChatMessage` 函数签名增加 `onHitl` 参数，并传递给 `sendChatMessageWs`

```typescript
// ---- 改动前 ----
export function streamChatMessage(
  request: ChatRequest,
  onChunk: OnChunkCallback,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  if (isTauri()) {
    const promise = sendChatMessageTauri(request, onChunk);
    return {
      controller: { closed: false, abort: () => void stopChatMessage(request.session_id || 'main') },
      promise,
    };
  }
  return sendChatMessageWs(request, onChunk);
}

// ---- 改动后 ----
export function streamChatMessage(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  if (isTauri()) {
    const promise = sendChatMessageTauri(request, onChunk);
    return {
      controller: { closed: false, abort: () => void stopChatMessage(request.session_id || 'main') },
      promise,
    };
  }
  return sendChatMessageWs(request, onChunk, onHitl);
}
```

**改动 6：** `sendChatMessageWs` 函数签名增加 `onHitl` 参数

```typescript
// ---- 改动前 ----
function sendChatMessageWs(
  request: ChatRequest,
  onChunk: OnChunkCallback,
): {

// ---- 改动后 ----
function sendChatMessageWs(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
): {
```

**改动 7：** `controller` 对象增加 `sendHitlResponse` 方法实现

```typescript
// 在 controller 的 abort() 方法之后添加：
    sendHitlResponse: (response: HitlResponse) => {
      if (done || !socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({
        type: 'hitl_response',
        session_id: sessionId,
        decision: response.decision,
        message: response.message ?? '',
        edited_args: response.edited_args,
      }));
    },
```

**改动 8：** `socket.onmessage` 处理器增加 `hitl_request` 分支

```typescript
// 在 if (data.event === 'chunk') {...} 之后、else if (data.event === 'done') 之前插入：
    } else if (data.event === 'hitl_request') {
      // Agent paused for human approval — invoke callback but keep socket open
      // so the client can send back a hitl_response on the same connection.
      if (onHitl && data.content) {
        onHitl(data.content as unknown as HitlInterruptData);
      }
```

---

### 3.7 `client_future/app/composables/messages.ts` — 消息层透传

**改动 1：** import 行增加 `OnHitlCallback, StreamController`

```typescript
// ---- 改动前 ----
import { streamChatMessage, type OnChunkCallback } from './bridge';

// ---- 改动后 ----
import { streamChatMessage, type OnChunkCallback, type OnHitlCallback, type StreamController } from './bridge';
```

**改动 2：** `postAgentStream` 函数签名增加 `onHitl` 参数，并在内部透传

```typescript
// ---- 改动前 ----
export function postAgentStream(
    session_id: string,
    multi_modal_message: MultiModalMessage,
    onData: OnChunkCallback,
    onDone?: () => void,
    onError?: (err: unknown) => void,
): AbortController {
    const controller = new AbortController();
    let stopFn: (() => void) | null = null;

    const { controller: stream, promise } = streamChatMessage(
        {
            session_id,
            text: multi_modal_message.text ?? '',
            image_base64_list: multi_modal_message.image_base64_list,
        },
        onData,
    );
    stopFn = () => stream.abort();

    controller.signal.addEventListener('abort', () => stopFn?.());
    // ... (promise.then/catch 不变)

// ---- 改动后 ----
export function postAgentStream(
    session_id: string,
    multi_modal_message: MultiModalMessage,
    onData: OnChunkCallback,
    onDone?: () => void,
    onError?: (err: unknown) => void,
    onHitl?: OnHitlCallback,
): AbortController {
    const controller = new AbortController();
    let stopFn: (() => void) | null = null;
    let hitlSender: (((response: import('./bridge').HitlResponse) => void) | null) = null;

    const { controller: stream, promise } = streamChatMessage(
        {
            session_id,
            text: multi_modal_message.text ?? '',
            image_base64_list: multi_modal_message.image_base64_list,
        },
        onData,
        onHitl,
    );
    stopFn = () => stream.abort();
    hitlSender = stream.sendHitlResponse ?? null;

    // 将 sendHitlResponse 挂载到返回的 AbortController 上
    (controller as any).sendHitlResponse = hitlSender;

    controller.signal.addEventListener('abort', () => stopFn?.());
    // ... (promise.then/catch 不变)
```

---

### 3.8 `client_future/app/pages/home/type.ts` — 新增类型

在文件末尾（`CHAT_ROLE` 枚举之后）新增：

```typescript
/** HITL 审批请求（对应后端 HitlInterruptData） */
export interface HitlRequestData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}
```

---

### 3.9 `client_future/app/pages/home/index.vue` — 审批 UI

**改动 1：** `<script>` 区 import 增加 HITL 类型

```typescript
// ---- 改动前 ----
import type { SessionRecord, MessageItem } from './type.ts';
import { CHAT_ROLE } from './type.ts';
import type { CachedMessage } from '@/composables/db';
import { tools, headerTools } from './config';
import { Menu } from 'primevue';
import { readCharacter } from '@/composables/bridge';
import type { ChatRequest } from '@/composables/bridge';
import type { AgentChunkType } from '@/composables/bridge';
import { useI18n } from 'vue-i18n';

// ---- 改动后 ----
import type { SessionRecord, MessageItem, HitlRequestData } from './type.ts';
import { CHAT_ROLE } from './type.ts';
import type { CachedMessage } from '@/composables/db';
import { tools, headerTools } from './config';
import { Menu } from 'primevue';
import { readCharacter } from '@/composables/bridge';
import type { ChatRequest, AgentChunkType, HitlResponse } from '@/composables/bridge';
import { useI18n } from 'vue-i18n';
```

**改动 2：** 在 `let tempIdCounter = 0;` 之后，新增 HITL 状态和处理函数：

```typescript
/** HITL 审批请求（当 agent 暂停等待人工审批时设置） */
const hitlRequest = ref<HitlRequestData | null>(null);

/** 处理 HITL 审批请求：显示审批弹窗 */
const handleHitlRequest = (data: HitlRequestData) => {
  hitlRequest.value = data;
};

/** 用户审批/拒绝 HITL 请求 */
const handleHitlDecision = (decision: 'approve' | 'reject', message: string = '') => {
  if (!activeAgentController) return;
  const sender = (activeAgentController as any).sendHitlResponse as
    | ((response: HitlResponse) => void) | null;
  if (sender) {
    sender({ decision, message });
  }
  hitlRequest.value = null;
};
```

**改动 3：** `postAgentStream` 调用增加第 6 个参数 `handleHitlRequest`

```typescript
// ---- 改动前 ----
    activeAgentController = postAgentStream(
      sessionId,
      req,
      onStreamChunk,
      () => {
        activeAgentController = null;
        isSending.value = false;
      },
      (err) => {
        activeAgentController = null;
        aiMsg.content = t('errors.replyFailed', { reason: String(err) });
        isSending.value = false;
      }
    );

// ---- 改动后 ----
    activeAgentController = postAgentStream(
      sessionId,
      req,
      onStreamChunk,
      () => {
        activeAgentController = null;
        isSending.value = false;
      },
      (err) => {
        activeAgentController = null;
        aiMsg.content = t('errors.replyFailed', { reason: String(err) });
        isSending.value = false;
      },
      handleHitlRequest,
    );
```

**改动 4：** 在 `<template>` 中，右侧会话主体区域的 `</div>` 之后、根元素 `</div>` 之前，新增审批弹窗：

```html
    <!-- HITL 审批弹窗 -->
    <Dialog
      v-model:visible="hitlRequest"
      :header="t('hitl.title', 'Action Requires Approval')"
      :modal="true"
      :closable="false"
      class="w-[90vw] md:w-[500px]">
      <div class="flex flex-col gap-3">
        <div class="text-sm text-gray-500">{{ t('hitl.tool', 'Tool') }}: <span class="font-bold">{{ hitlRequest?.tool_name }}</span></div>
        <div v-if="hitlRequest?.description" class="text-sm whitespace-pre-wrap">{{ hitlRequest.description }}</div>
        <div v-if="hitlRequest?.tool_args && Object.keys(hitlRequest.tool_args).length > 0" class="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded-lg overflow-auto max-h-40">
          <pre class="m-0">{{ JSON.stringify(hitlRequest.tool_args, null, 2) }}</pre>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button
            :label="t('hitl.reject', 'Reject')"
            icon="pi pi-times"
            severity="danger"
            @click="handleHitlDecision('reject')" />
          <Button
            :label="t('hitl.approve', 'Approve')"
            icon="pi pi-check"
            @click="handleHitlDecision('approve')" />
        </div>
      </template>
    </Dialog>
```

> 弹窗使用 PrimeVue 的 `Dialog` 和 `Button` 组件，项目已引入 PrimeVue，无需额外安装。`t()` 的第二个参数为 fallback 默认值，当 i18n key 不存在时使用。

---

## 四、测试

### 4.1 Python 测试

文件：`tests/unit/test_hitl_integration.py`（新增）

运行命令：

```bash
cd D:\selfProj\sherry_agent
python -m pytest tests/unit/test_hitl_integration.py -v
```

预期：62 passed

测试覆盖：
- HITLConfig 默认值与自定义配置
- ApprovalResult dataclass 行为
- 命令检测（hardline / dangerous / safe）
- ApprovalPipeline 6 层审批管线（安全命令通过、硬线阻断、危险命令升级、YOLO 旁路、deny rules、session/permanent allowlist、smart approve、tool approval）
- InterruptManager 中断设置/清除/会话隔离
- WriteApprovalGate 写入暂存与审批
- KanbanTriage 失败计数与升级
- PairingStore 用户授权
- SlashConfirm 破坏性命令确认
- MCPElicitationConsent 默认拒绝
- agent/core.py 中 HumanInTheLoop 注册验证
- server/service/messages.py 中新函数存在性验证
- server/trigger/ws/messages.py 中 HITL 事件处理验证
- client_future bridge.ts / messages.ts / index.vue / type.ts 中 HITL 支持验证

### 4.2 TypeScript 测试

文件：`client_future/app/composables/__tests__/hitl-bridge.test.ts`（新增）

运行命令：

```bash
cd D:\selfProj\sherry_agent\client_future
npx vitest run app/composables/__tests__/hitl-bridge.test.ts
```

预期：13 passed

测试覆盖：
- HITL 类型导出（HitlInterruptData / HitlResponse / OnHitlCallback / AgentWsEventType）
- hitl_request 事件触发 onHitl 回调
- 无 onHitl 回调时静默忽略
- sendHitlResponse 发送 approve / reject / edit 帧
- sendHitlResponse 在 socket 关闭/abort 时为 no-op
- chunk → hitl_request → chunk → done 交叉序列
- 连续多次 hitl_request

### 4.3 全量测试

```bash
# Python
cd D:\selfProj\sherry_agent
python -m pytest tests/unit/test_hitl_integration.py -v

# TypeScript
cd D:\selfProj\sherry_agent\client_future
npx vitest run
```

---

## 五、注意事项

1. **中间件顺序**：`HumanInTheLoop()` 必须在 `Summarization(...)` 之前注册，因为 Summarization 会压缩消息历史，而 HITL 的 `after_model` 钩子需要读取最近 AIMessage 的 `tool_calls` 字段。

2. **WebSocket 连接保持**：当服务端发送 `hitl_request` 事件后，不会发送 `done`，WebSocket 连接保持打开。客户端需在同一连接上发送 `hitl_response` 消息恢复 agent。服务端 `resume_agent` 完成后才会发送 `done` 或另一个 `hitl_request`。

3. **langgraph 版本要求**：`resume_agent` 使用 `from langgraph.types import Command`，需确保 langgraph 已安装且版本支持 `Command(resume=...)`。

4. **gates.py bug 修复**：原文件 `InterruptManager.set_interrupt()` 内部使用了 `threading.Event()` 但未导入 `threading`，导致运行时 `NameError`。本次升级修复了此 bug。

5. **Tauri 模式**：本次 HITL 交互仅在浏览器 WebSocket 模式下实现。Tauri 模式的 `sendChatMessageTauri` 未传递 `onHitl`，如需 Tauri 支持，需在 Rust 侧增加对应的事件转发。

6. **i18n**：审批弹窗使用了 `t('hitl.title', '...')` 等 i18n key，第二个参数为 fallback。如需正式国际化，需在 i18n locale 文件中添加 `hitl` 命名空间。
