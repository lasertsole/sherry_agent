# Character 命令

管理角色配置（身份、性格、外观等）。

## `character_read`

读取当前角色配置。

### 调用签名

```typescript
const response = await invoke<CharacterResponse>('character_read');
```

### 参数

无。

### 返回值

`CharacterResponse`

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `character_data` | `Record<string, Record<string, string>>` | 嵌套的 分区 -> 字段 -> 值 |

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `IO_ERROR` | 读取角色文件失败 | 是 |
| `CONFIG_ERROR` | 角色文件格式错误 | 否 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { CharacterResponse } from '@/types/backend/CharacterResponse';

const response = await invoke<CharacterResponse>('character_read');
console.log('名字:', response.character_data.identity?.name);
console.log('性格:', response.character_data.identity?.personality);
```

---

## `character_write`

覆写角色配置（全量替换）。

### 调用签名

```typescript
await invoke('character_write', {
  payload: CharacterPayload,
});
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `payload` | [`CharacterPayload`](/zh/types/reference#characterpayload) | 是 | 新的角色数据 |

### 返回值

`void`

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `IO_ERROR` | 写入角色文件失败 | 是 |
| `CONFIG_ERROR` | 数据结构无效 | 否 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('character_write', {
  payload: {
    character_data: {
      identity: { name: '雪莉', personality: '开朗' },
      appearance: { hair: '金色', eyes: '蓝色' },
    },
  },
});
```

---

## `character_update`

增量更新角色配置（合并）。仅更新指定的分区/字段。

### 调用签名

```typescript
await invoke('character_update', {
  payload: CharacterPayload,
});
```

### 参数

同 `character_write`。

### 返回值

`void`

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';

// 仅更新性格
await invoke('character_update', {
  payload: {
    character_data: {
      identity: { personality: '活泼' },
    },
  },
});
```
