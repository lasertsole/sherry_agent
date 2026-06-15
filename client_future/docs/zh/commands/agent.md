# Agent 命令

## `agent_chat`

向 Agent 发送聊天消息并接收流式响应。

启动一次 Agent 对话轮次。Agent 通过 LangGraph 管道处理消息
（上下文构建 -> LLM 调用 -> 工具执行 -> 响应生成）。

### 调用签名

```typescript
const chunks = await invoke<ChatChunk[]>('agent_chat', {
  request: ChatRequest,
});
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `request` | [`ChatRequest`](/zh/types/reference#chatrequest) | 是 | 多模态消息载体 |

#### ChatRequest

| 字段 | 类型 | 必填 | 说明 |
|-------|------|----------|-------------|
| `session_id` | `string` | 是 | 唯一会话标识符 |
| `text` | `string \| null` | 否 | 文本消息内容 |
| `image_base64_list` | `string[]` | 否 | Base64 编码的图片，用于多模态输入 |

### 返回值

`ChatChunk[]` — 响应块数组。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `content` | `string` | 该块的文本片段 |
| `done` | `boolean` | 是否为最后一个块 |

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `AGENT_ERROR` | Agent 管道失败（工具循环、LangGraph 错误） | 否 |
| `MODEL_ERROR` | LLM API 调用失败（超时、连接拒绝） | 是 |
| `SESSION_ERROR` | 无效或过期的 session ID | 否 |
| `RAG_ERROR` | 知识检索失败 | 否 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { ChatChunk, ChatRequest } from '@/types/backend/ChatRequest';

// 简单文本聊天
const chunks = await invoke<ChatChunk[]>('agent_chat', {
  request: {
    session_id: 'main',
    text: '你好，今天怎么样？',
    image_base64_list: [],
  },
});

// 显示完整响应
const fullResponse = chunks.map(c => c.content).join('');
console.log(fullResponse);

// 带图片的多模态聊天
const chunks = await invoke<ChatChunk[]>('agent_chat', {
  request: {
    session_id: 'main',
    text: '描述这张图片',
    image_base64_list: [base64ImageData],
  },
});
```

### 相关

- [流式事件](/zh/events/streaming) — 实时逐块流式传输
- [Session 命令](/zh/commands/session) — 管理会话状态
