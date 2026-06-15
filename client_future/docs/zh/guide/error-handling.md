# 错误处理

## FrontendError 格式

当命令执行失败时，reject 的 Promise 包含一个 `FrontendError` 对象：

```typescript
interface FrontendError {
  code: string;      // 机器可读的错误码
  message: string;   // 人类可读的描述
}
```

## 错误码表

| 错误码 | 说明 | 可重试 | 触发场景 |
|------|-------------|-----------|--------------|
| `CONFIG_ERROR` | 配置加载或解析失败 | 否 | 配置文件无效、JSON 格式错误 |
| `DATABASE_ERROR` | 数据库操作失败 | **是** | SQLite 错误、FTS5 查询失败 |
| `IO_ERROR` | 文件系统或通用 I/O 失败 | **是** | 磁盘读写失败 |
| `MODEL_ERROR` | LLM API 调用失败 | **是** | DeepSeek/Ollama 超时、连接拒绝 |
| `AGENT_ERROR` | Agent 执行失败 | 否 | LangGraph 错误、工具循环检测 |
| `RAG_ERROR` | RAG 管道失败 | 否 | 文档解析、嵌入、检索失败 |
| `CHANNEL_ERROR` | 通道通信错误 | **是** | QQ 机器人、WebSocket、消息总线 |
| `SESSION_ERROR` | 会话管理失败 | 否 | 无效/过期的 session ID |
| `TOOL_ERROR` | 工具执行失败 | 否 | Python REPL 崩溃、终端错误 |
| `SKILL_ERROR` | 技能加载或注册失败 | 否 | 无效的技能定义 |
| `UNKNOWN_ERROR` | 未知错误 | 否 | 未分类错误的兜底 |

## 错误处理模式

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

      // 不可重试的错误 — 立即展示给用户
      if (!isRetryable(err.code)) {
        throw err;
      }

      // 可重试的错误 — 等待后重试
      if (attempt === maxRetries) throw err;
      await sleep(1000 * attempt); // 指数退避
    }
  }
  throw new Error('unreachable');
}

function isRetryable(code: string): boolean {
  return ['IO_ERROR', 'CHANNEL_ERROR', 'MODEL_ERROR', 'DATABASE_ERROR']
    .includes(code);
}
```

## 用户友好的错误消息

将错误码映射为本地化消息：

```typescript
const ERROR_MESSAGES: Record<string, string> = {
  CONFIG_ERROR:    '配置无效，请检查设置。',
  DATABASE_ERROR:  '数据库操作失败，正在重试...',
  IO_ERROR:        '文件操作失败，正在重试...',
  MODEL_ERROR:     'AI 模型不可达，请检查网络连接。',
  AGENT_ERROR:     'Agent 执行出错，请重试。',
  RAG_ERROR:       '知识检索失败，请重试。',
  CHANNEL_ERROR:   '通信通道出错，正在重试...',
  SESSION_ERROR:   '会话已过期，请开始新对话。',
  TOOL_ERROR:      '工具执行失败。',
  SKILL_ERROR:     '技能加载失败。',
  UNKNOWN_ERROR:   '发生了未知错误。',
};
```
