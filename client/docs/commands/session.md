# Session Commands

## `session_clear`

Clear all state for a given session, including conversation history, checkpointer data,
and cached context.

### Signature

```typescript
await invoke('session_clear', {
  request: ClearSessionRequest,
});
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request` | [`ClearSessionRequest`](/types/reference#clearsessionrequest) | Yes | Session to clear |

#### ClearSessionRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | `string` | Yes | Session ID to clear |

### Returns

`void` — Resolves on success.

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `SESSION_ERROR` | Session not found or already cleared | No |
| `DATABASE_ERROR` | Failed to delete session data | Yes |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('session_clear', {
  request: { session_id: 'main' },
});
console.log('Session cleared');
```

---

## `session_history`

Retrieve conversation history for a session. Returns the last N turns (or all turns)
as an ordered list of messages, oldest first.

### Signature

```typescript
const messages = await invoke<HistoryMessage[]>('session_history', {
  request: HistoryRequest,
});
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request` | [`HistoryRequest`](/types/reference#historyrequest) | Yes | Query parameters |

#### HistoryRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | `string` | Yes | Session ID to query |
| `last_turn_count` | `number \| null` | No | Number of recent turns (`null` = all) |

### Returns

`HistoryMessage[]` — Ordered list of messages.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `string` | `"user"` or `"assistant"` |
| `content` | `string` | Message text |
| `timestamp` | `string \| undefined` | ISO 8601 timestamp (may be absent) |

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `SESSION_ERROR` | Session not found | No |
| `DATABASE_ERROR` | Failed to query history | Yes |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { HistoryMessage } from '@/types/backend/HistoryMessage';

// Get last 10 turns
const messages = await invoke<HistoryMessage[]>('session_history', {
  request: { session_id: 'main', last_turn_count: 10 },
});

// Render chat UI
messages.forEach(msg => {
  console.log(`[${msg.role}] ${msg.content}`);
});

// Get all history
const allMessages = await invoke<HistoryMessage[]>('session_history', {
  request: { session_id: 'main', last_turn_count: null },
});
```
