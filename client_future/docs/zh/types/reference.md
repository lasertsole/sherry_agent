# TypeScript 类型参考

所有类型通过 [ts-rs](https://github.com/Aleph-Alpha/ts-rs) 从 Rust 自动生成，
位于 `app/types/backend/` 目录。

## 导入方式

```typescript
import type { ChatRequest } from '@/types/backend/ChatRequest';
import type { ChatChunk } from '@/types/backend/ChatChunk';
import type { FrontendError } from '@/types/backend/FrontendError';
```

---

## Agent 类型

### ChatRequest

发送给 Agent 的多模态消息载体。

```typescript
export type ChatRequest = {
  session_id: string;
  text: string | null;
  image_base64_list: Array<string>;
};
```

### ChatChunk

单个流式响应块。

```typescript
export type ChatChunk = {
  content: string;
  done: boolean;
};
```

---

## Session 类型

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

## System Prompt 类型

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

## Character 类型

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

## System 类型

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

## 错误类型

### FrontendError

```typescript
export type FrontendError = {
  code: string;
  message: string;
};
```

---

## 事件载体类型

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

## 重新生成类型

类型在 `cargo test` 运行时自动重新生成：

```bash
cd client_future/src-tauri
cargo test
```

这会触发 ts-rs 的 `export_bindings_*` 测试，将 `.ts` 文件写入
`app/types/backend/` 目录。
