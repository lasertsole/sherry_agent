# 快速开始

## 概述

EMA AI Agent 采用**混合架构**：Tauri/Rust 后端暴露 **12 个 IPC 命令**，通过 HTTP 将所有请求代理到 Python 后端。前端使用 `bridge.ts` 自动检测运行环境（Tauri IPC 或浏览器 HTTP）。

所有 IPC 命令通过 Tauri 的 `invoke()` API 调用，返回带类型的结果。

## 前提条件

- [Tauri v2](https://v2.tauri.app/) 运行时
- `@tauri-apps/api` npm 包（已包含在 `package.json` 中）
- `app/types/backend/` 中的 TypeScript 类型定义

## 调用命令

```typescript
import { invoke } from '@tauri-apps/api/core';

// 简单命令（无参数）
const info = await invoke<AppInfo>('system_info');
console.log(`运行 ${info.name} v${info.version}`);

// 带参数的命令
const history = await invoke<HistoryMessage[]>('session_history', {
  request: { session_id: 'main', last_turn_count: 10 },
});
```

## 命令命名约定

所有命令名使用 `snake_case`，与 Rust 函数名一致：

| 命令名 | 模块 | 说明 |
|---|---|---|
| `agent_chat` | agent | 发送消息，获取流式 Agent 回复 |
| `agent_stop` | agent | 停止正在进行的 Agent 生成 |
| `session_clear` | session | 清除会话状态 |
| `session_history` | session | 获取对话历史 |
| `system_prompt_read` | system_prompt | 读取所有提示词文件 |
| `system_prompt_write` | system_prompt | 覆写提示词文件 |
| `system_prompt_update` | system_prompt | 增量更新提示词文件 |
| `character_read` | character | 读取角色配置 |
| `character_write` | character | 覆写角色配置 |
| `character_update` | character | 增量更新角色配置 |
| `system_info` | system | 获取应用元数据 |
| `system_health` | system | 健康检查 |

## 参数传递

Tauri 命令以对象形式接收参数。Rust 函数中的参数名即为 invoke 调用中的 key：

```typescript
// Rust: fn session_clear(request: ClearSessionRequest)
// TypeScript:
await invoke('session_clear', {
  request: { session_id: 'main' }  // <-- "request" 与 Rust 参数名对应
});
```

## 返回类型

- **成功**：resolve 的值与命令的返回类型匹配（如 `ChatChunk[]`）
- **失败**：Promise reject，包含 [`FrontendError`](/zh/guide/error-handling) 对象

```typescript
try {
  const chunks = await invoke<ChatChunk[]>('agent_chat', { request });
  // 处理成功结果
} catch (error: any) {
  // error.code = "MODEL_ERROR"
  // error.message = "model error: connection refused"
}
```

## 桥接层 (`bridge.ts`)

生产环境建议使用统一的 `bridge.ts` 组合式，而非直接调用 `invoke()`。
它自动检测 Tauri 桌面模式和浏览器开发模式：

```typescript
import { sendChatMessage, clearSession, checkHealth } from '~/composables/bridge';

// Tauri 桌面模式和浏览器开发模式均可使用
await sendChatMessage(
  { session_id: 'main', text: '你好！', image_base64_list: [] },
  (chunk) => appendToChat(chunk),
);
```

**Tauri 模式**下，`bridge.ts` 调用 `invoke()` 并通过 Tauri Events 接收流式数据。
**浏览器模式**下，它通过 `fetch()` 直接调用 Python 后端并使用 SSE。

## 下一步

- [错误处理指南](/zh/guide/error-handling) — 如何处理命令返回的错误
- [命令参考](/zh/commands/agent) — 每个命令的详细文档
- [流式事件](/zh/events/streaming) — 实时 Agent 响应流
