# Agent Commands

## `agent_chat`

Send a chat message to the agent and receive a streamed response.

Initiates an agent conversation turn. The agent processes the message through its
LangGraph pipeline (context building -> LLM call -> tool execution -> response generation).

### Signature

```typescript
const chunks = await invoke<ChatChunk[]>('agent_chat', {
  request: ChatRequest,
});
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request` | [`ChatRequest`](/types/reference#chatrequest) | Yes | Multi-modal message payload |

#### ChatRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | `string` | Yes | Unique session identifier |
| `text` | `string \| null` | No | Text message content |
| `image_base64_list` | `string[]` | No | Base64-encoded images for multi-modal input |

### Returns

`ChatChunk[]` — Array of response chunks.

| Field | Type | Description |
|-------|------|-------------|
| `content` | `string` | Text fragment for this chunk |
| `done` | `boolean` | `true` if this is the final chunk |

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `AGENT_ERROR` | Agent pipeline failure (tool loop, LangGraph error) | No |
| `MODEL_ERROR` | LLM API call failed (timeout, connection refused) | Yes |
| `SESSION_ERROR` | Invalid or expired session ID | No |
| `RAG_ERROR` | Knowledge retrieval failure | No |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { ChatChunk, ChatRequest } from '@/types/backend/ChatRequest';

// Simple text chat
const chunks = await invoke<ChatChunk[]>('agent_chat', {
  request: {
    session_id: 'main',
    text: 'Hello, how are you?',
    image_base64_list: [],
  },
});

// Display the full response
const fullResponse = chunks.map(c => c.content).join('');
console.log(fullResponse);

// Multi-modal chat with images
const chunks = await invoke<ChatChunk[]>('agent_chat', {
  request: {
    session_id: 'main',
    text: 'Describe this image',
    image_base64_list: [base64ImageData],
  },
});
```

### Related

- [Streaming Events](/events/streaming) — For real-time chunk-by-chunk streaming
- [Session Commands](/commands/session) — Manage session state

---

## `agent_stop`

Stop an ongoing agent generation for the given session.

Sends a cancellation request to the Python backend (`POST /sessions/agent/sse/stop`).
The SSE stream will terminate and emit `agent:stream:end`.

### Signature

```typescript
await invoke('agent_stop', {
  request: StopRequest,
});
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request` | [`StopRequest`](/types/reference#stoprequest) | Yes | Session to stop |

#### StopRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | `string` | Yes | Session whose generation should be cancelled |

### Returns

`void`

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';

// Stop generation for the current session
await invoke('agent_stop', {
  request: { session_id: 'main' },
});
```

### Related

- [`agent_chat`](#agent_chat) — Start a chat (which can be stopped)
- [Streaming Events](/events/streaming) — Event lifecycle including error on stop
