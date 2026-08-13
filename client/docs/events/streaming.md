# Streaming Events

The agent uses Tauri's event system to stream responses to the frontend in real time.

## How It Works

In the hybrid architecture, streaming works as follows:
1. Frontend calls `invoke('agent_chat', ...)` via Tauri IPC
2. Rust backend sends `POST /sessions/agent/sse` to the Python backend
3. Python backend returns an SSE stream
4. Rust parses each SSE `data:` line and emits a Tauri Event (`agent:stream:chunk`)
5. Frontend listens for Tauri Events and updates the UI in real time

The `bridge.ts` composable handles this transparently in both Tauri and browser modes.

## Event Lifecycle

```
agent:stream:start  -->  agent:stream:chunk  (x N)  -->  agent:stream:end
                              |
                              +-->  agent:stream:error  (on failure)
```

## Event Table

| Event Name | Trigger | Payload Type |
|---|---|---|
| `agent:stream:start` | Agent begins processing a message | [`AgentStreamStart`](#agentstreamstart) |
| `agent:stream:chunk` | Each text fragment is generated | [`AgentStreamChunk`](#agentstreamchunk) |
| `agent:stream:end` | Agent finishes successfully | [`AgentStreamEnd`](#agentstreamend) |
| `agent:stream:error` | Agent encounters a failure | [`AgentStreamError`](#agentstreamerror) |

## Listening to Events

```typescript
import { listen } from '@tauri-apps/api/event';
import type { AgentStreamStart } from '@/types/backend/AgentStreamStart';
import type { AgentStreamChunk } from '@/types/backend/AgentStreamChunk';
import type { AgentStreamEnd } from '@/types/backend/AgentStreamEnd';
import type { AgentStreamError } from '@/types/backend/AgentStreamError';

// Collect unlisten functions for cleanup
const unlisteners: (() => void)[] = [];

// Listen for stream start
unlisteners.push(
  await listen<AgentStreamStart>('agent:stream:start', (event) => {
    console.log(`Stream started: session=${event.payload.session_id}`);
  })
);

// Listen for chunks (main data stream)
unlisteners.push(
  await listen<AgentStreamChunk>('agent:stream:chunk', (event) => {
    const { content } = event.payload;
    appendToChat(content);  // Your UI update function
  })
);

// Listen for stream end
unlisteners.push(
  await listen<AgentStreamEnd>('agent:stream:end', (event) => {
    console.log(`Done! Total chunks: ${event.payload.total_chunks}`);
  })
);

// Listen for errors
unlisteners.push(
  await listen<AgentStreamError>('agent:stream:error', (event) => {
    console.error(`Error: [${event.payload.code}] ${event.payload.message}`);
  })
);

// Cleanup when component unmounts
function cleanup() {
  unlisteners.forEach(fn => fn());
}
```

## Complete Streaming Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { AgentStreamChunk } from '@/types/backend/AgentStreamChunk';

async function streamChat(sessionId: string, message: string) {
  let fullResponse = '';

  // Set up chunk listener BEFORE invoking the command
  const unlisten = await listen<AgentStreamChunk>('agent:stream:chunk', (event) => {
    if (event.payload.session_id === sessionId) {
      fullResponse += event.payload.content;
      updateChatUI(fullResponse);
    }
  });

  try {
    // Trigger the agent (also returns chunks for non-streaming fallback)
    await invoke('agent_chat', {
      request: { session_id: sessionId, text: message, image_base64_list: [] },
    });
  } finally {
    unlisten();  // Always clean up
  }

  return fullResponse;
}
```

## Payload Types

### AgentStreamStart

Emitted once when the agent begins processing.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `string` | The session being processed |
| `message_id` | `string` | Unique ID for this message turn |

### AgentStreamChunk

Emitted for each text fragment.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `string` | The session this chunk belongs to |
| `message_id` | `string` | The message turn this chunk belongs to |
| `content` | `string` | The text fragment |

### AgentStreamEnd

Emitted once when the agent finishes successfully.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `string` | The session that completed |
| `message_id` | `string` | The message turn that completed |
| `total_chunks` | `number` | Total number of chunks emitted |

### AgentStreamError

Emitted when the agent encounters a failure.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `string` | The session that failed |
| `message_id` | `string` | The message turn that failed |
| `code` | `string` | Machine-readable error code |
| `message` | `string` | Human-readable error description |
