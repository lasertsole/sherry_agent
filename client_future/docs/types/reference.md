# TypeScript Type Reference

All types are auto-generated from Rust via [ts-rs](https://github.com/Aleph-Alpha/ts-rs)
and located in `app/types/backend/`.

## Import Pattern

```typescript
import type { ChatRequest } from '@/types/backend/ChatRequest';
import type { ChatChunk } from '@/types/backend/ChatChunk';
import type { FrontendError } from '@/types/backend/FrontendError';
```

---

## Agent Types

### ChatRequest

Multi-modal message payload sent to the agent.

```typescript
export type ChatRequest = {
  session_id: string;
  text: string | null;
  image_base64_list: Array<string>;
};
```

### ChatChunk

A single streaming response chunk.

```typescript
export type ChatChunk = {
  content: string;
  done: boolean;
};
```

---

## Session Types

### ClearSessionRequest

```typescript
export type ClearSessionRequest = {
  session_id: string;
};
```

### HistoryRequest

```typescript
export type HistoryRequest = {
  session_id: string;
  last_turn_count: number | null;
};
```

### HistoryMessage

```typescript
export type HistoryMessage = {
  role: string;
  content: string;
  timestamp?: string;
};
```

---

## System Prompt Types

### PromptFilePayload

```typescript
export type PromptFilePayload = {
  file_to_content: Record<string, string>;
};
```

### PromptFileResponse

```typescript
export type PromptFileResponse = {
  file_to_content: Record<string, string>;
};
```

---

## Character Types

### CharacterPayload

```typescript
export type CharacterPayload = {
  character_data: Record<string, Record<string, string>>;
};
```

### CharacterResponse

```typescript
export type CharacterResponse = {
  character_data: Record<string, Record<string, string>>;
};
```

---

## System Types

### AppInfo

```typescript
export type AppInfo = {
  name: string;
  version: string;
  tauri_version: string;
  debug: boolean;
};
```

### HealthStatus

```typescript
export type HealthStatus = {
  healthy: boolean;
  message: string;
};
```

---

## Error Types

### FrontendError

```typescript
export type FrontendError = {
  code: string;
  message: string;
};
```

---

## Event Payload Types

### AgentStreamStart

```typescript
export type AgentStreamStart = {
  session_id: string;
  message_id: string;
};
```

### AgentStreamChunk

```typescript
export type AgentStreamChunk = {
  session_id: string;
  message_id: string;
  content: string;
};
```

### AgentStreamEnd

```typescript
export type AgentStreamEnd = {
  session_id: string;
  message_id: string;
  total_chunks: number;
};
```

### AgentStreamError

```typescript
export type AgentStreamError = {
  session_id: string;
  message_id: string;
  code: string;
  message: string;
};
```

---

## Regenerating Types

Types are regenerated when `cargo test` runs:

```bash
cd client_future/src-tauri
cargo test
```

This triggers ts-rs `export_bindings_*` tests that write `.ts` files to
`app/types/backend/`.
