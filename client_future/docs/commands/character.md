# Character Commands

Manage the character configuration (identity, personality, appearance, etc.).

## `character_read`

Read the current character configuration.

### Signature

```typescript
const response = await invoke<CharacterResponse>('character_read');
```

### Parameters

None.

### Returns

`CharacterResponse`

| Field | Type | Description |
|-------|------|-------------|
| `character_data` | `Record<string, Record<string, string>>` | Nested section -> field -> value |

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `IO_ERROR` | Failed to read character file | Yes |
| `CONFIG_ERROR` | Malformed character file | No |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { CharacterResponse } from '@/types/backend/CharacterResponse';

const response = await invoke<CharacterResponse>('character_read');
console.log('Name:', response.character_data.identity?.name);
console.log('Personality:', response.character_data.identity?.personality);
```

---

## `character_write`

Overwrite character configuration (full replacement).

### Signature

```typescript
await invoke('character_write', {
  payload: CharacterPayload,
});
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `payload` | [`CharacterPayload`](/types/reference#characterpayload) | Yes | New character data |

### Returns

`void`

### Errors

| Code | Description | Retryable |
|------|-------------|-----------|
| `IO_ERROR` | Failed to write character file | Yes |
| `CONFIG_ERROR` | Invalid data structure | No |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';

await invoke('character_write', {
  payload: {
    character_data: {
      identity: { name: 'Sherry', personality: 'cheerful' },
      appearance: { hair: 'blonde', eyes: 'blue' },
    },
  },
});
```

---

## `character_update`

Partially update character configuration (merge). Only the specified
sections/fields are updated.

### Signature

```typescript
await invoke('character_update', {
  payload: CharacterPayload,
});
```

### Parameters

Same as `character_write`.

### Returns

`void`

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';

// Update only the personality
await invoke('character_update', {
  payload: {
    character_data: {
      identity: { personality: 'energetic' },
    },
  },
});
```
