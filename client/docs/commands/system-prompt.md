# System Prompt Commands

Manage the system prompt files that define the agent's behavior
(e.g., `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`).

## `system_prompt_read`

Read all system prompt files and return their contents.

### Signature

```typescript
const response = await invoke<PromptFileResponse>('system_prompt_read');
```

### Parameters

None.

### Returns

`PromptFileResponse` — A map of filenames to content.

| Field | Type | Description |
|-------|------|-------------|
| `file_to_content` | `Record<string, string>` | Map of filename to file content |

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `IO_ERROR` | Failed to read prompt files | Yes |
| `CONFIG_ERROR` | Invalid prompt file format | No |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { PromptFileResponse } from '@/types/backend/PromptFileResponse';

const response = await invoke<PromptFileResponse>('system_prompt_read');
console.log(response.file_to_content['AGENTS.md']);
```

---

## `system_prompt_write`

Overwrite system prompt files (full replacement). Files not included in
the payload are left unchanged.

### Signature

```typescript
await invoke('system_prompt_write', {
  payload: PromptFilePayload,
});
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `payload` | [`PromptFilePayload`](/types/reference#promptfilepayload) | Yes | Files to write |

#### PromptFilePayload

| Field | Type | Description |
|-------|------|-------------|
| `file_to_content` | `Record<string, string>` | Map of filename to new content |

### Returns

`void` — Resolves on success.

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `IO_ERROR` | Failed to write prompt files | Yes |
| `CONFIG_ERROR` | Invalid file content | No |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('system_prompt_write', {
  payload: {
    file_to_content: {
      'AGENTS.md': '# Updated Agent Configuration\n...',
      'SOUL.md': '# Updated Soul\n...',
    },
  },
});
```

---

## `system_prompt_update`

Partially update system prompt files (merge). Only the specified files
are updated; existing content in other files is preserved.

### Signature

```typescript
await invoke('system_prompt_update', {
  payload: PromptFilePayload,
});
```

### Parameters

Same as `system_prompt_write`.

### Returns

`void` — Resolves on success.

### Errors

Same as `system_prompt_write`.

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('system_prompt_update', {
  payload: {
    file_to_content: {
      'AGENTS.md': '# Appended content...',
    },
  },
});
```
