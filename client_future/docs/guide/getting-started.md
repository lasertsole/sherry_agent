# Getting Started

## Overview

The EMA AI Agent backend is a Tauri application that exposes **11 IPC commands** to the frontend.
All commands are called via Tauri's `invoke()` API and return typed results.

## Prerequisites

- [Tauri v2](https://v2.tauri.app/) runtime
- `@tauri-apps/api` npm package (already in `package.json`)
- TypeScript types from `app/types/backend/`

## Calling a Command

```typescript
import { invoke } from '@tauri-apps/api/core';

// Simple command (no parameters)
const info = await invoke<AppInfo>('system_info');
console.log(`Running ${info.name} v${info.version}`);

// Command with parameters
const history = await invoke<HistoryMessage[]>('session_history', {
  request: { session_id: 'main', last_turn_count: 10 },
});
```

## Command Naming Convention

All command names use `snake_case` matching the Rust function names:

| Command Name | Module | Description |
|---|---|---|
| `agent_chat` | agent | Send a message, get agent response |
| `session_clear` | session | Clear session state |
| `session_history` | session | Get conversation history |
| `system_prompt_read` | system_prompt | Read all prompt files |
| `system_prompt_write` | system_prompt | Overwrite prompt files |
| `system_prompt_update` | system_prompt | Merge-update prompt files |
| `character_read` | character | Read character config |
| `character_write` | character | Overwrite character config |
| `character_update` | character | Merge-update character config |
| `system_info` | system | Get app metadata |
| `system_health` | system | Health check |

## Parameter Passing

Tauri commands receive parameters as an object. The parameter name in the Rust function
becomes the key in the invoke call:

```typescript
// Rust: fn session_clear(request: ClearSessionRequest)
// TypeScript:
await invoke('session_clear', {
  request: { session_id: 'main' }  // <-- "request" matches the Rust parameter name
});
```

## Return Types

- **Success**: The resolved value matches the command's return type (e.g., `ChatChunk[]`)
- **Error**: The promise rejects with a [`FrontendError`](/guide/error-handling) object

```typescript
try {
  const chunks = await invoke<ChatChunk[]>('agent_chat', { request });
  // handle success
} catch (error: any) {
  // error.code = "MODEL_ERROR"
  // error.message = "model error: connection refused"
}
```

## Next Steps

- [Error Handling Guide](/guide/error-handling) — How to handle errors from commands
- [Commands Reference](/commands/agent) — Detailed documentation for each command
- [Streaming Events](/events/streaming) — Real-time agent response streaming
