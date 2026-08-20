# 流式事件

Agent 使用 Tauri 事件系统向前端实时流式传输响应。

## 工作原理

在混合架构中，流式传输的工作流程如下：
1. 前端通过 Tauri IPC 调用 `invoke('agent_chat', ...)`
2. Rust 后端向 Python 后端发送 `POST /sessions/agent/sse`
3. Python 后端返回 SSE 流
4. Rust 解析每个 SSE `data:` 行并发出 Tauri Event (`agent:stream:chunk`)
5. 前端监听 Tauri Events 并实时更新 UI

`bridge.ts` 组合式在 Tauri 和浏览器模式下透明地处理这一过程。

## 事件生命周期

```
agent:stream:start  -->  agent:stream:chunk  (x N)  -->  agent:stream:end
                              |
                              +-->  agent:stream:error  (失败时)
```

## 事件表

| 事件名 | 触发条件 | 载体类型 |
|---|---|---|
| `agent:stream:start` | Agent 开始处理消息 | [`AgentStreamStart`](#agentstreamstart) |
| `agent:stream:chunk` | 每个文本片段生成时 | [`AgentStreamChunk`](#agentstreamchunk) |
| `agent:stream:end` | Agent 成功完成 | [`AgentStreamEnd`](#agentstreamend) |
| `agent:stream:error` | Agent 遇到错误 | [`AgentStreamError`](#agentstreamerror) |

## 监听事件

```typescript
import { listen } from '@tauri-apps/api/event';
import type { AgentStreamStart } from '@/types/backend/AgentStreamStart';
import type { AgentStreamChunk } from '@/types/backend/AgentStreamChunk';
import type { AgentStreamEnd } from '@/types/backend/AgentStreamEnd';
import type { AgentStreamError } from '@/types/backend/AgentStreamError';

// 收集取消监听函数以便清理
const unlisteners: (() => void)[] = [];

// 监听流开始
unlisteners.push(
  await listen<AgentStreamStart>('agent:stream:start', (event) => {
    console.log(`流开始: session=${event.payload.session_id}`);
  })
);

// 监听数据块（主要数据流）
unlisteners.push(
  await listen<AgentStreamChunk>('agent:stream:chunk', (event) => {
    const { content } = event.payload;
    appendToChat(content);  // 你的 UI 更新函数
  })
);

// 监听流结束
unlisteners.push(
  await listen<AgentStreamEnd>('agent:stream:end', (event) => {
    console.log(`完成！总块数: ${event.payload.total_chunks}`);
  })
);

// 监听错误
unlisteners.push(
  await listen<AgentStreamError>('agent:stream:error', (event) => {
    console.error(`错误: [${event.payload.code}] ${event.payload.message}`);
  })
);

// 组件卸载时清理
function cleanup() {
  unlisteners.forEach(fn => fn());
}
```

## 完整流式示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { AgentStreamChunk } from '@/types/backend/AgentStreamChunk';

async function streamChat(sessionId: string, message: string) {
  let fullResponse = '';

  // 在调用命令之前设置块监听器
  const unlisten = await listen<AgentStreamChunk>('agent:stream:chunk', (event) => {
    if (event.payload.session_id === sessionId) {
      fullResponse += event.payload.content;
      updateChatUI(fullResponse);
    }
  });

  try {
    // 触发 Agent（同时为非流式回退返回块）
    await invoke('agent_chat', {
      request: { session_id: sessionId, text: message, image_base64_list: [] },
    });
  } finally {
    unlisten();  // 始终清理
  }

  return fullResponse;
}
```

## 载体类型

### AgentStreamStart

Agent 开始处理时触发一次。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `session_id` | `string` | 正在处理的会话 |
| `message_id` | `string` | 此消息轮次的唯一 ID |

### AgentStreamChunk

每个文本片段触发一次。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `session_id` | `string` | 此块所属的会话 |
| `message_id` | `string` | 此块所属的消息轮次 |
| `content` | `string` | 文本片段 |

### AgentStreamEnd

Agent 成功完成时触发一次。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `session_id` | `string` | 完成的会话 |
| `message_id` | `string` | 完成的消息轮次 |
| `total_chunks` | `number` | 发出的总块数 |

### AgentStreamError

Agent 遇到失败时触发。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `session_id` | `string` | 失败的会话 |
| `message_id` | `string` | 失败的消息轮次 |
| `code` | `string` | 机器可读的错误码 |
| `message` | `string` | 人类可读的错误描述 |
