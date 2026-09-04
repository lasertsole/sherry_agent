# Design Pattern Refactoring — Frontend

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式（前端部分）。
>
> 审查范围：`client/app/`
>
> 生成日期：2026-09-04

---

## 目录

- [4. Frontend: Client](#4-frontend-client)
- [5. Cross-cutting Patterns Summary](#5-cross-cutting-patterns-summary)
- [6. Recommended Refactoring Roadmap](#6-recommended-refactoring-roadmap)

> 后端部分见 [DESIGN_PATTERN_REFACTORING_BACKEND.md](./DESIGN_PATTERN_REFACTORING_BACKEND.md)

---

## 4. Frontend: Client

### 4.1 代码重复

#### 4.1.1 `resolveWsBaseUrl` 函数重复

- **文件**: `composables/bridge.ts:315`; `composables/ws.ts:126`
- **模式**: 提取到 `composables/env.ts` 或新建 `composables/wsUtils.ts`

#### 4.1.2 `closeSocket` 辅助函数重复 3 次

- **文件**: `bridge.ts:520-532`（sendChatMessageWs）; `bridge.ts:796-808`（resumeHitl）; `bridge.ts:1929-1942`（openLogStream）
- **模式**: 提取 `teardownWebSocket(ws)` 公共函数

#### 4.1.3 媒体文件选择逻辑三重复制

- **文件**: `pages/home/index/[sid].vue:1281-1474`
- **问题**: image/audio/video 三组完全同构的 `selected*/read*/remove*/trigger*/on*Selected` 函数，仅常量（MIME 前缀、最大数量、i18n key）不同
- **模式**: Parameterized Factory — `useMediaPicker(kind)`

```typescript
function useMediaPicker(kind: "image" | "audio" | "video") {
  const selected = ref<MediaItem[]>([]);
  const trigger = () => fileInput.value?.click();
  const onSelected = async (e: Event) => {
    /* shared logic with kind-specific constants */
  };
  const remove = (idx: number) => {
    selected.value.splice(idx, 1);
  };
  return { selected, trigger, onSelected, remove };
}
```

#### 4.1.4 WebSocket onmessage 模式重复 6 处

- **文件**: `ws.ts:191-214`; `ws.ts:338-362`; `bridge.ts:687-738`; `bridge.ts:853-885`; `bridge.ts:940-950`; `bridge.ts:1956-1964`
- **模式**: Event Dispatcher — `createWsMessageHandler(handlers: Record<string, (data) => void>)`

#### 4.1.5 `isTauri()` 分支模式重复 14 次

- **文件**: `composables/bridge.ts` 全文（第359, 912, 985, 1060, 1088, 1117, 1169, 1188, 1203, 1221, 1237, 1260, 1277, 1725 行）
- **模式**: Strategy + Factory

```typescript
interface TransportStrategy {
  call(cmd: string, payload: any): Promise<any>
  stream(request: ChatRequest, onChunk: OnChunkCallback): StreamController
}
class TauriTransport implements TransportStrategy { ... }
class BrowserTransport implements TransportStrategy { ... }
function createTransport(): TransportStrategy {
  return isTauri() ? new TauriTransport() : new BrowserTransport()
}
// 所有 bridge 函数只调 transport.call(...)
```

#### 4.1.6 `VITE_API_BACK_URL || 'http://localhost:8080'` 硬编码 9+ 处

- **文件**: `bridge.ts`（481, 789, 926, 1730, 1923）; `ws.ts`（155, 317）; `requestApi.ts`（107）
- **模式**: Configuration Provider — `composables/env.ts` 导出 `API_BASE_URL` 和 `WS_BASE_URL`

---

### 4.2 God 类/函数

#### 4.2.1 `[sid].vue` — 1629 行 God Component

- **文件**: `pages/home/index/[sid].vue`
- **问题**: 10+ 职责（历史加载、流处理、草稿持久化、HITL、媒体上传、角色快照、KeepAlive、重连横幅、队列徽章、删除中断）
- **模式**: Composable 拆分

```typescript
// useChatStream(sid) — 流式发送/chunk处理/停止
// useDraftPersistence(sid) — 草稿写入/去重/定时器
// useHitlApproval(sid) — HITL 审批/resume
// useMediaUpload(kind) — 媒体选择/预览/移除
// useSessionLifecycle(sid) — KeepAlive/历史加载/角色快照
```

#### 4.2.2 `bridge.ts` — 1978 行 God Module

- **文件**: `composables/bridge.ts`
- **问题**: 10+ 独立领域 API（流式聊天、停止、会话管理、系统提示词 CRUD、记忆 CRUD、心跳、定时任务、技能管理、频道管理、日志流、健康检查、子代理）+ 传输基础设施
- **模式**: 模块拆分 + Facade — `bridge/chat.ts`、`bridge/session.ts`、`bridge/systemPrompt.ts`、`bridge/memory.ts`、`bridge/cron.ts`、`bridge/skills.ts` 等

#### 4.2.3 `useSubagentTasks.ts` — 799 行 God Composable

- **文件**: `composables/useSubagentTasks.ts`
- **问题**: 状态管理 + WS 订阅 + 缓存同步 + 类型映射（`toCachedSubagentRun`/`toSubagentRun` 各 40+ 行）+ 过滤 + 分组 + 多选 + 批量删除 + 展开/折叠 + 子树 BFS 遍历
- **模式**: 状态切片 — `useSubagentState`、`useSubagentSync`、`useSubagentSelection`、`useSubagentTree`

---

### 4.3 长方法与深嵌套

#### 4.3.1 `sendChatMessageWs` — 282 行，4 层闭包

- **文件**: `composables/bridge.ts:471-753`
- **问题**: `runStream` → `handleConnectionLoss` → `connect` → `ws.onmessage` → 5 路 if/else 链
- **模式**: Extract Method + Handler Registry

#### 4.3.2 `handleSend` — 140+ 行，5 层回调

- **文件**: `pages/home/index/[sid].vue:1133-1275`
- **问题**: `postAgentStream(sid, req, onStreamChunk, meta => {...}, err => {...}, handleHitlRequest, info => {...})` 每个回调 3-4 层嵌套
- **模式**: State Machine + Callback Decomposition

#### 4.3.3 `loadSessionHistory` — 91 行，3 层 Map 嵌套

- **文件**: `pages/home/index/[sid].vue:472-563`
- **问题**: 构建 `mergedById` Map，遍历 `chatMessages` → 对每个负 id 消息调用 `serverRowFor`（内部又遍历 `historyItems`）→ 再遍历 `drafts`。O(n*m) 嵌套查找
- **模式**: Pre-indexing + Extract Method

#### 4.3.4 `appendStreamChunk` — 115 行，5 路 if/else

- **文件**: `pages/home/index/[sid].vue:868-983`
- **问题**: 按 `type` 分 5 个分支（text/reasoning/tool_start/tool_end/tool_result），每分支 15-30 行
- **模式**: Strategy Registry

```typescript
type ChunkHandler = (msg: MessageItem, chunk: any) => void;
const CHUNK_HANDLERS: Record<string, ChunkHandler> = {
  text: (msg, c) => {
    msg.content += c.content;
  },
  reasoning: (msg, c) => {
    msg.reasoning += c.content;
  },
  tool_start: (msg, c) => {
    /* new tool card */
  },
  tool_end: (msg, c) => {
    /* mark tool done */
  },
  tool_result: (msg, c) => {
    /* fill tool result */
  },
};
// appendStreamChunk 简化为:
const handler = CHUNK_HANDLERS[chunk.type];
if (handler) handler(msg, chunk);
```

---

### 4.4 硬编码 if-else 链

#### 4.4.1 `handleOperate` — 13 路 switch

- **文件**: `pages/home/index.vue:348-385`
- **问题**: 13 个 case，每个只是设置一个 `ref(false)` 为 `true`
- **模式**: Command Registry — `const dialogRegistry: Record<string, Ref<boolean>>`

#### 4.4.2 WebSocket onmessage if/else — 6 路

- **文件**: `bridge.ts:694-738`; `bridge.ts:860-884`
- **模式**: Event Handler Registry — `const handlers: Partial<Record<AgentWsEventType, WsEventHandler>>`

#### 4.4.3 `badgeClass` / `statusLabel` — 硬编码映射

- **文件**: `useSubagentTasks.ts:484-511`
- **模式**: Lookup Table — `const STATUS_STYLE: Record<string, {badge, labelKey}>`

---

### 4.5 缺失抽象 / 紧耦合

#### 4.5.1 WebSocket 协议泄漏到 `[sid].vue`

- **文件**: `[sid].vue:294` — 直接导入 `StreamInterruptedError`、`AgentChunkType`、`QueuedInfo`
- **模式**: Event Aggregator — `useChatStream` 封装所有协议细节

#### 4.5.2 ChatBox 直接构造后端 URL

- **文件**: `pages/home/components/ChatBox.vue:526-556`
- **问题**: `${backendBaseUrl}/media?session_id=...&filename=...` — 展示组件含 API 形状知识
- **模式**: Media URL Resolver Service — `composables/mediaResolver.ts`

#### 4.5.3 `(controller as any).sendHitlResponse` 类型安全破坏

- **文件**: `composables/messages.ts:240`
- **模式**: Type-Safe Interface

```typescript
interface ChatController extends AbortController {
  sendHitlResponse?(response: HitlResponse): void;
}
// 返回 ChatController 而非 AbortController
```

#### 4.5.4 base64 data URL 剥离泄漏

- **文件**: `[sid].vue:1335, 1402, 1467` — 三处 `dataUrl.split(',')[1]`
- **模式**: Media Encoding Utility — `extractBase64(dataUrl: string): string`

#### 4.5.5 IndexedDB 序列化兼容性泄漏

- **文件**: `[sid].vue:1037` — `JSON.parse(JSON.stringify(m))` 为避免 Dexie `DataCloneError`
- **模式**: Repository 封装 — `db.ts` 内部做深拷贝

#### 4.5.6 `useSubagentTasks` 三方紧耦合

- **文件**: `composables/useSubagentTasks.ts:16-18` — 直接依赖 bridge/db/ws + 80+ 行手动 schema 映射
- **模式**: Repository + 统一领域模型

---

## 5. Cross-cutting Patterns Summary

| Pattern                     | 适用发现                          | 核心收益                         |
| --------------------------- | --------------------------------- | -------------------------------- |
| **Template Method**         | #1, #17, #19, 2.1.1, 2.1.3, 3.1.1 | 消除跨函数/跨模块重复，统一骨架  |
| **Strategy + Registry**     | #3, #10, #11, #12, 1.4.1, 4.3.4   | 消除 if-elif 链，开闭原则        |
| **State Pattern**           | #2, #21, 2.3.2                    | 封装隐式状态机为显式状态类       |
| **Chain of Responsibility** | #2, 1.4.4                         | 拆解嵌套分发为独立 handler 链    |
| **Builder**                 | 1.5.3 (built_agent)               | 逐步组装复杂对象                 |
| **Facade + Service Layer**  | 1.5.1, 1.5.2, 3.3.1               | 中间件委托给服务，只协调         |
| **Repository**              | #14, #41, 2.2.3, 3.3.2            | 封装 SQL，分离数据访问与业务逻辑 |
| **Mixin / 工具函数**        | #7, 1.1.6, 1.1.9, 1.1.10          | 消除跨中间件/工具重复            |
| **Dependency Injection**    | 2.3.3, 1.7.3, 1.7.4, 3.3.5        | 消除 lazy import 缝隙和跨层依赖  |
| **Observer / Event Bus**    | 2.1.4, 4.1.4                      | 被动观察者不侵入核心流           |
| **Adapter / Mapper**        | 3.3.4, 4.5.3                      | 隔离框架类型与持久化/展示层      |
| **Command Registry**        | 4.4.1, 4.4.2                      | 消除 switch/case，开闭原则       |
| **Parameterized Factory**   | 1.1.1, 4.1.3                      | 泛化同构函数族                   |

---

## 6. Recommended Refactoring Roadmap

### Phase 1: 消除最大风险（P0）

| Step | Target                                                 | Pattern                            | Est. Effort |
| ---- | ------------------------------------------------------ | ---------------------------------- | ----------- |
| 1.1  | `messages.py` async_generate / resume_agent 统一       | Template Method + StreamDispatcher | 2-3 天      |
| 1.2  | `LocalLlamaChatModel` 基类提取                         | Template Method                    | 1 天        |
| 1.3  | `HumanInTheLoop.after_model` 策略注册表                | Strategy + Registry                | 2 天        |
| 1.4  | 中间件 `session_id` 提取 + `state_register_mem` Facade | Mixin + Enum key                   | 1 天        |
| 1.5  | `bridge.ts` TransportStrategy 抽象                     | Strategy + Factory                 | 1-2 天      |
| 1.6  | `[sid].vue` Composable 拆分                            | Separation of Concerns             | 3-5 天      |

### Phase 2: 拆解 God 模块（P1）

| Step | Target                                  | Pattern          | Est. Effort |
| ---- | --------------------------------------- | ---------------- | ----------- |
| 2.1  | `OutputRepetitionGuard` 拆分            | 组合模式         | 2 天        |
| 2.2  | `MultimodalProcessor` Strategy dispatch | Strategy         | 1-2 天      |
| 2.3  | `search_messages` 三策略拆分            | Strategy         | 2 天        |
| 2.4  | `add_messages` 类型分发拆分             | Strategy/Command | 1-2 天      |
| 2.5  | `skill_scanner.py` SRP 拆分             | SRP              | 2 天        |
| 2.6  | 中间件 sync/async 基类                  | 装饰器/基类      | 1 天        |
| 2.7  | `FileStore` 基类提取                    | Template Method  | 1 天        |
| 2.8  | `useSubagentTasks` 状态切片             | 状态切片         | 2 天        |

### Phase 3: DRY 清理（P2）

| Step | Target                                                       | Pattern               | Est. Effort |
| ---- | ------------------------------------------------------------ | --------------------- | ----------- |
| 3.1  | 工具函数提取（`_read_dotenv`/`_tool_error`/`_args_hash` 等） | DRY                   | 1 天        |
| 3.2  | 前端共享模块（`resolveWsBaseUrl`/`closeSocket`/env）         | DRY                   | 1 天        |
| 3.3  | 媒体选择 `useMediaPicker`                                    | Parameterized Factory | 1 天        |
| 3.4  | 原子写入工具 + HTTP helpers + 序列化模块                     | DRY                   | 1 天        |
| 3.5  | Repository Pattern（SQL 封装）                               | Repository            | 2-3 天      |
| 3.6  | Command Executor 抽象                                        | 抽象                  | 1 天        |
| 3.7  | 前端 Command Registry + Lookup Table                         | Registry              | 1 天        |

### Phase 4: 架构清理（P3）

| Step | Target                             | Pattern        | Est. Effort |
| ---- | ---------------------------------- | -------------- | ----------- |
| 4.1  | 导入时副作用 → `setup()` 函数      | Explicit init  | 1 天        |
| 4.2  | `turn_runner` 依赖注入             | DI             | 2 天        |
| 4.3  | `built_agent()` Builder            | Builder        | 1 天        |
| 4.4  | `curator/__init__.py` Facade       | Facade         | 1 天        |
| 4.5  | `runtime/core.py` bug 修复         | Bug fix        | 0.5 天      |
| 4.6  | `state_register` Protocol + 连接池 | Interface Seg. | 1-2 天      |

---

> **注**: 每步重构应在对应测试通过后合并。建议按 Phase 1 → 2 → 3 → 4 顺序推进，前置 phase 的基础（如 SessionState Facade、TransportStrategy）是后续步骤的前提。
