# Session 命令

## `session_clear`

清除指定会话的所有状态，包括对话历史、检查点数据和缓存上下文。

### 调用签名

```typescript
await invoke('session_clear', {
  request: ClearSessionRequest,
});
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `request` | [`ClearSessionRequest`](/zh/types/reference#clearsessionrequest) | 是 | 要清除的会话 |

#### ClearSessionRequest

| 字段 | 类型 | 必填 | 说明 |
|-------|------|----------|-------------|
| `session_id` | `string` | 是 | 要清除的会话 ID |

### 返回值

`void` — 成功时 resolve。

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `SESSION_ERROR` | 会话不存在或已被清除 | 否 |
| `DATABASE_ERROR` | 删除会话数据失败 | 是 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('session_clear', {
  request: { session_id: 'main' },
});
console.log('会话已清除');
```

---

## `session_history`

获取会话的对话历史。返回最近 N 轮（或全部）消息，按时间从旧到新排列。

### 调用签名

```typescript
const messages = await invoke<HistoryMessage[]>('session_history', {
  request: HistoryRequest,
});
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `request` | [`HistoryRequest`](/zh/types/reference#historyrequest) | 是 | 查询参数 |

#### HistoryRequest

| 字段 | 类型 | 必填 | 说明 |
|-------|------|----------|-------------|
| `session_id` | `string` | 是 | 要查询的会话 ID |
| `last_turn_count` | `number \| null` | 否 | 最近轮次数（`null` = 全部） |

### 返回值

`HistoryMessage[]` — 有序消息列表。

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `role` | `string` | `"user"` 或 `"assistant"` |
| `content` | `string` | 消息文本 |
| `timestamp` | `string \| undefined` | ISO 8601 时间戳（可能不存在） |

### 可能的错误

| 错误码 | 说明 | 可重试 |
|------|-------------|-----------|
| `SESSION_ERROR` | 会话不存在 | 否 |
| `DATABASE_ERROR` | 查询历史失败 | 是 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { HistoryMessage } from '@/types/backend/HistoryMessage';

// 获取最近 10 轮对话
const messages = await invoke<HistoryMessage[]>('session_history', {
  request: { session_id: 'main', last_turn_count: 10 },
});

// 渲染聊天 UI
messages.forEach(msg => {
  console.log(`[${msg.role}] ${msg.content}`);
});

// 获取全部历史
const allMessages = await invoke<HistoryMessage[]>('session_history', {
  request: { session_id: 'main', last_turn_count: null },
});
```
