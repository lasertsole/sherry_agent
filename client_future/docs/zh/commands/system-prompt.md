# System Prompt 命令

管理定义 Agent 行为的系统提示词文件
（如 `AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md`）。

## `system_prompt_read`

读取所有系统提示词文件并返回其内容。

### 调用签名

```typescript
const response = await invoke<PromptFileResponse>('system_prompt_read');
```

### 参数

无。

### 返回值

`PromptFileResponse` — 文件名到内容的映射。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `file_to_content` | `Record<string, string>` | 文件名到文件内容的映射 |

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `IO_ERROR` | 读取提示词文件失败 | 是 |
| `CONFIG_ERROR` | 提示词文件格式无效 | 否 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { PromptFileResponse } from '@/types/backend/PromptFileResponse';

const response = await invoke<PromptFileResponse>('system_prompt_read');
console.log(response.file_to_content['AGENTS.md']);
```

---

## `system_prompt_write`

覆写系统提示词文件（全量替换）。未在载体中包含的文件保持不变。

### 调用签名

```typescript
await invoke('system_prompt_write', {
  payload: PromptFilePayload,
});
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `payload` | [`PromptFilePayload`](/zh/types/reference#promptfilepayload) | 是 | 要写入的文件 |

#### PromptFilePayload

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `file_to_content` | `Record<string, string>` | 文件名到新内容的映射 |

### 返回值

`void` — 成功时 resolve。

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `IO_ERROR` | 写入提示词文件失败 | 是 |
| `CONFIG_ERROR` | 文件内容无效 | 否 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('system_prompt_write', {
  payload: {
    file_to_content: {
      'AGENTS.md': '# 更新后的 Agent 配置\n...',
      'SOUL.md': '# 更新后的灵魂\n...',
    },
  },
});
```

---

## `system_prompt_update`

增量更新系统提示词文件（合并）。仅更新指定的文件，其他文件的现有内容保留。

### 调用签名

```typescript
await invoke('system_prompt_update', {
  payload: PromptFilePayload,
});
```

### 参数

同 `system_prompt_write`。

### 返回值

`void` — 成功时 resolve。

### 可能的错误

同 `system_prompt_write`。

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('system_prompt_update', {
  payload: {
    file_to_content: {
      'AGENTS.md': '# 追加的内容...',
    },
  },
});
```
