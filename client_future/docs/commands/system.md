# System Commands

Tauri-native utility commands for app metadata and health checking.

## `system_info`

Return application metadata. This command is synchronous and never fails.

### Signature

```typescript
const info = await invoke<AppInfo>('system_info');
```

### Parameters

None.

### Returns

`AppInfo`

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Application name (from `Cargo.toml`) |
| `version` | `string` | Semantic version (e.g., `"0.1.0"`) |
| `tauri_version` | `string` | Tauri runtime version |
| `debug` | `boolean` | `true` in debug builds |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { AppInfo } from '@/types/backend/AppInfo';

const info = await invoke<AppInfo>('system_info');
console.log(`EMA AI Agent v${info.version} (${info.debug ? 'debug' : 'release'})`);
console.log(`Tauri: ${info.tauri_version}`);
```

---

## `system_health`

Quick health check — verifies core subsystems are reachable.

### Signature

```typescript
const health = await invoke<HealthStatus>('system_health');
```

### Parameters

None.

### Returns

`HealthStatus`

| Field | Type | Description |
|-------|------|-------------|
| `healthy` | `boolean` | `true` if all subsystems are operational |
| `message` | `string` | Human-readable status description |

### Example

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { HealthStatus } from '@/types/backend/HealthStatus';

const health = await invoke<HealthStatus>('system_health');
if (health.healthy) {
  console.log('All systems OK:', health.message);
} else {
  console.warn('Degraded:', health.message);
}
```
