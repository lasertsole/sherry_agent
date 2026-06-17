# System 命令

Tauri 原生工具命令。`system_info` 为纯 Rust；`system_health` 会 ping Python 后端。

## `system_info`

返回应用元数据。此命令是同步的，不会失败。

### 调用签名

```typescript
const info = await invoke<AppInfo>('system_info');
```

### 参数

无。

### 返回值

`AppInfo`

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `name` | `string` | 应用名称（来自 `Cargo.toml`） |
| `version` | `string` | 语义化版本号（如 `"0.1.0"`） |
| `tauri_version` | `string` | Tauri 运行时版本 |
| `debug` | `boolean` | debug 构建时为 `true` |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { AppInfo } from '@/types/backend/AppInfo';

const info = await invoke<AppInfo>('system_info');
console.log(`EMA AI Agent v${info.version} (${info.debug ? 'debug' : 'release'})`);
console.log(`Tauri: ${info.tauri_version}`);
```

---

## `system_health`

快速健康检查 — 向 Python 后端发送 ping (`GET /system_prompt`) 以验证连接。

Python 后端响应时返回 `healthy: true`，不可达时返回 `healthy: false` 及描述信息。

### 调用签名

```typescript
const health = await invoke<HealthStatus>('system_health');
```

### 参数

无。

### 返回值

`HealthStatus`

| 字段 | 类型 | 说明 |
|-------|------|-------------|
| `healthy` | `boolean` | 所有子系统正常时为 `true` |
| `message` | `string` | 人类可读的状态描述 |

### 示例

```typescript
import { invoke } from '@tauri-apps/api/core';
import type { HealthStatus } from '@/types/backend/HealthStatus';

const health = await invoke<HealthStatus>('system_health');
if (health.healthy) {
  console.log('所有系统正常:', health.message);
} else {
  console.warn('系统降级:', health.message);
}
```
