# Error Handling

## FrontendError Format

When a command fails, the rejected promise contains a `FrontendError` object:

```typescript
interface FrontendError {
  code: string;      // Machine-readable error code
  message: string;   // Human-readable description
}
```

## Error Code Table

| Code | Description | Retryable | Triggered By |
|------|-------------|-----------|--------------|
| `CONFIG_ERROR` | Configuration loading or parsing failure | No | Invalid config files, malformed JSON |
| `DATABASE_ERROR` | Database operation failure | **Yes** | SQLite errors, FTS5 query failures |
| `IO_ERROR` | File-system or generic I/O failure | **Yes** | Disk read/write failures |
| `MODEL_ERROR` | LLM API call failure | **Yes** | DeepSeek/Ollama timeout, connection refused |
| `AGENT_ERROR` | Agent execution failure | No | LangGraph errors, tool loop detection |
| `RAG_ERROR` | RAG pipeline failure | No | Document parsing, embedding, retrieval |
| `CHANNEL_ERROR` | Channel communication error | **Yes** | QQ bot, WebSocket, message bus |
| `SESSION_ERROR` | Session management failure | No | Invalid/expired session ID |
| `TOOL_ERROR` | Tool execution failure | No | Python REPL crash, terminal error |
| `SKILL_ERROR` | Skill loading or registration failure | No | Invalid skill definition |
| `BACKEND_ERROR` | Python backend HTTP bridge failure | **Yes** | Connection refused, timeout, bad response from Python |
| `UNKNOWN_ERROR` | Unexpected error | No | Catch-all for unclassified errors |

## Error Handling Pattern

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { FrontendError } from '@/types/backend/FrontendError';

async function callWithRetry<T>(
  command: string,
  args: Record<string, unknown>,
  maxRetries = 3,
): Promise<T> {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      return await invoke<T>(command, args);
    } catch (error: unknown) {
      const err = error as FrontendError;

      // Non-retryable errors — show to user immediately
      if (!isRetryable(err.code)) {
        throw err;
      }

      // Retryable errors — wait and retry
      if (attempt === maxRetries) throw err;
      await sleep(1000 * attempt); // Exponential backoff
    }
  }
  throw new Error('unreachable');
}

function isRetryable(code: string): boolean {
  return ['IO_ERROR', 'CHANNEL_ERROR', 'MODEL_ERROR', 'DATABASE_ERROR', 'BACKEND_ERROR']
    .includes(code);
}
```

## User-Facing Error Messages

Map error codes to localized messages:

```typescript
const ERROR_MESSAGES: Record<string, string> = {
  CONFIG_ERROR:    'Configuration is invalid. Please check settings.',
  DATABASE_ERROR:  'Database operation failed. Retrying...',
  IO_ERROR:        'File operation failed. Retrying...',
  MODEL_ERROR:     'AI model is unreachable. Please check your connection.',
  AGENT_ERROR:     'Agent encountered an error. Please try again.',
  RAG_ERROR:       'Knowledge retrieval failed. Please try again.',
  CHANNEL_ERROR:   'Communication channel error. Retrying...',
  SESSION_ERROR:   'Session expired. Please start a new conversation.',
  TOOL_ERROR:      'A tool failed to execute.',
  SKILL_ERROR:     'Failed to load a skill.',
  BACKEND_ERROR:   'Backend service is unreachable. Please ensure the Python server is running.',
  UNKNOWN_ERROR:   'An unexpected error occurred.',
};
```
