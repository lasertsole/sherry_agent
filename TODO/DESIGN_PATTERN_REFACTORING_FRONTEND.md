# Design Pattern Refactoring — Frontend

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式（前端部分）。
>
> 审查范围：`client/app/`
>
> 生成日期：2026-09-04；最近更新：2026-09-09（已完成项已移除，详见文末「已完成记录」）
>
> 剩余未完成项集中在 **4.4 硬编码 if-else 链** 与路线图 Phase 4。

---

## 目录

- [4. Frontend: Client（剩余项）](#4-frontend-client剩余项)
- [5. Cross-cutting Patterns Summary](#5-cross-cutting-patterns-summary)
- [6. Recommended Refactoring Roadmap](#6-recommended-refactoring-roadmap)
- [7. 已完成记录（2026-09-09）](#7-已完成记录2026-09-09)

> 后端部分见 [DESIGN_PATTERN_REFACTORING_BACKEND.md](./DESIGN_PATTERN_REFACTORING_BACKEND.md)

---

## 4. Frontend: Client（剩余项）

原 4.1（代码重复）、4.2（God 类/函数）、4.3（长方法与深嵌套）、4.5（缺失抽象/紧耦合）各子项已全部完成并从本文档移除（见第 7 节）。剩余项如下。

### 4.4 硬编码 if-else 链

#### 4.4.1 `handleOperate` — 13 路 switch

- **文件**: `pages/home/index.vue:348-385`
- **问题**: 13 个 case，每个只是设置一个 `ref(false)` 为 `true`
- **模式**: Command Registry — `const dialogRegistry: Record<string, Ref<boolean>>`

#### 4.4.3 `badgeClass` / `statusLabel` — 硬编码映射

- **文件**: `useSubagentTasks.ts`（facade 内的 `badgeClass` / `statusLabel`）
- **模式**: Lookup Table — `const STATUS_STYLE: Record<string, {badge, labelKey}>`

---

## 5. Cross-cutting Patterns Summary

| Pattern                     | 适用发现                          | 核心收益                         |
| --------------------------- | --------------------------------- | -------------------------------- |
| **Strategy + Registry**     | 1.1.1                             | 消除 if-elif 链，开闭原则        |
| **State Pattern**           | 2.1.2                             | 封装隐式状态机为显式状态类       |
| **Chain of Responsibility** | 1.1.4                             | 拆解嵌套分发为独立 handler 链    |
| **Builder**                 | 1.2.3 (built_agent)               | 逐步组装复杂对象                 |
| **Facade + Service Layer**  | 1.2.1, 1.2.2, 3.1.1               | 中间件委托给服务，只协调         |
| **Repository**              | #13, 3.1.2                        | 封装 SQL，分离数据访问与业务逻辑 |
| **Dependency Injection**    | 2.1.3, 1.4.3, 1.4.4, 3.1.5        | 消除 lazy import 缝隙和跨层依赖  |
| **Command Registry**        | 4.4.1                             | 消除 switch/case，开闭原则       |
| **Adapter / Mapper**        | 3.1.4                             | 隔离框架类型与持久化/展示层      |

---

## 6. Recommended Refactoring Roadmap

### Phase 1: 消除最大风险（P0）

| Step | Target                                                 | Pattern                            | Est. Effort |
| ---- | ------------------------------------------------------ | ---------------------------------- | ----------- |
| 1.4  | 中间件 `session_id` 提取 + `state_register_mem` Facade | Mixin + Enum key                   | 1 天        |

> 1.1（`messages.py` async_generate 分解）、1.2（`LocalLlamaChatModel` 基类提取）、1.3（HITL 策略注册表）已完成；1.5（bridge.ts TransportStrategy）与 1.6（`[sid].vue` Composable 拆分）已完成，见第 7 节。

### Phase 2: 拆解 God 模块（P1）

| Step | Target                                  | Pattern          | Est. Effort |
| ---- | --------------------------------------- | ---------------- | ----------- |
| 2.6  | 中间件 sync/async 基类                  | 装饰器/基类      | 1 天        |
| 2.7  | `FileStore` 基类提取                    | Template Method  | 1 天        |

> 2.1（`OutputRepetitionGuard` 拆分）、2.2（`MultimodalProcessor` Strategy dispatch）、2.3（`search_messages` 三策略拆分）、2.4（`add_messages` 类型分发拆分）、2.5（`skill_scanner.py` SRP 拆分）已完成；2.8（`useSubagentTasks` 状态切片）已完成，见第 7 节。

### Phase 3: DRY 清理（P2）

| Step | Target                                                       | Pattern               | Est. Effort |
| ---- | ------------------------------------------------------------ | --------------------- | ----------- |
| 3.1  | 工具函数提取（`_read_dotenv`/`_tool_error`/`_args_hash` 等） | DRY                   | 1 天        |
| 3.4  | 原子写入工具 + HTTP helpers + 序列化模块                     | DRY                   | 1 天        |
| 3.5  | Repository Pattern（SQL 封装）                               | Repository            | 2-3 天      |
| 3.6  | Command Executor 抽象                                        | 抽象                  | 1 天        |
| 3.7  | 前端 Command Registry + Lookup Table（对应 4.4.1 / 4.4.3）   | Registry              | 1 天        |

> 3.2（前端共享模块 `resolveWsBaseUrl`/`closeSocket`/env）与 3.3（`useMediaPicker`）已完成，见第 7 节。

### Phase 4: 架构清理（P3）

| Step | Target                             | Pattern        | Est. Effort |
| ---- | ---------------------------------- | -------------- | ----------- |
| 4.1  | 导入时副作用 → `setup()` 函数      | Explicit init  | 1 天        |
| 4.2  | `turn_runner` 依赖注入             | DI             | 2 天        |
| 4.3  | `built_agent()` Builder            | Builder        | 1 天        |
| 4.5  | `runtime/core.py` bug 修复         | Bug fix        | 0.5 天      |
| 4.6  | `state_register` Protocol + 连接池 | Interface Seg. | 1-2 天      |

> 4.4（`curator/__init__.py` Facade）已完成，见第 7 节。

---

> **注**: 每步重构应在对应测试通过后合并。建议按 Phase 1 → 2 → 3 → 4 顺序推进，前置 phase 的基础（如 SessionState Facade、TransportStrategy）是后续步骤的前提。前端侧的基础重构（TransportStrategy、`bridge/*` 模块拆分、`[sid].vue` Composable 化、`useSubagentTasks` 状态切片 + Repository）已就位。

---

## 7. 已完成记录（2026-09-09）

以下子项已完成（行为保持不变的重构，eslint 0 errors / typecheck / vitest / build 全部通过），对应条目已从上文移除：

| 原编号 | 内容 | 落地位置 |
| ------ | ---- | -------- |
| 4.1.1 | `resolveWsBaseUrl` 提取 | `composables/env.ts` |
| 4.1.2 | `teardownWebSocket(ws)` 公共函数 | `composables/bridge/transport.ts` |
| 4.1.3 | `useMediaPicker(kind)` Parameterized Factory | `composables/useMediaPicker.ts` |
| 4.1.4 | `createWsMessageHandler` Event Dispatcher（6 处 onmessage） | `composables/wsMessage.ts`（ws.ts / bridge/* 接入） |
| 4.1.5 | `TransportStrategy` + `createTransport()`（14 处 `isTauri()` 收敛） | `composables/bridge/chat.ts` + `bridge/transport.ts` |
| 4.1.6 | `API_BASE_URL` / `WS_BASE_URL` Configuration Provider（8 处硬编码） | `composables/env.ts` |
| 4.2.1 | `[sid].vue` Composable 拆分（God Component 1629 行 → 模板 + 207 行纯逻辑接线） | `useSessionLifecycle` / `useDraftPersistence` / `useStreamChunks` / `useChatStream` / `useHitlApproval` / `useMediaPicker` |
| 4.2.2 | `bridge.ts` 模块拆分 + Facade（1978 行 God Module → 15 个领域模块） | `composables/bridge/{transport,chatTypes,upload,chat,wsStream,chatResume,session,systemPrompt,memory,cron,skills,curator,channels,logs,health}.ts`，`bridge.ts` 为显式具名 re-export Facade |
| 4.2.3 | `useSubagentTasks` 状态切片 | `subagentState` / `subagentTree` / `subagentSelection` / `subagentSync`，`useSubagentTasks.ts` 为 Facade |
| 4.3.1 | `sendChatMessageWs` Extract Method | `bridge/wsStream.ts`（`uploadMediaList` 上传分解 + `createWsMessageHandler` 分发） |
| 4.3.2 | `handleSend` 回调分解 | `useChatStream.ts`（`onStreamDone` / `onStreamError` / `onStreamQueued` / `attachDoneMeta`） |
| 4.3.3 | `loadSessionHistory` Pre-indexing | `useSessionLifecycle.ts`（服务行逻辑键 Map + 合并逻辑键 Set，O(n·m) → O(n)） |
| 4.3.4 | `appendStreamChunk` 5 路 if/else → Strategy Registry | `useStreamChunks.ts`（`CHUNK_HANDLERS`，对 `AgentChunkType` 穷举） |
| 4.4.2 | bridge onmessage if/else（与 4.1.4 同源代码位） | 已随 `createWsMessageHandler` 完成 |
| 4.5.1 | WebSocket 协议类型不再泄漏进 `[sid].vue` | 协议类型与流处理封装于 `useChatStream` / `useStreamChunks` / `useHitlApproval` |
| 4.5.2 | ChatBox 后端 URL 构造 → Media URL Resolver | `composables/mediaResolver.ts`（`mediaUrl`） |
| 4.5.3 | `(controller as any).sendHitlResponse` → 类型安全接口 | `composables/messages.ts`（`ChatController extends AbortController`） |
| 4.5.4 | base64 data URL 剥离 → `extractBase64` | `composables/mediaResolver.ts` |
| 4.5.5 | IndexedDB 深拷贝下沉 Repository | `composables/db.ts`（`saveDraftTurn` 内部深拷贝） |
| 4.5.6 | `useSubagentTasks` 三方耦合 → Repository | `composables/subagentRepository.ts`（bridge/db 数据访问 + `toCachedSubagentRun`/`toSubagentRun` schema 映射；WS 事件订阅保留在 `subagentSync` 观察者切片） |

后端部分（2026-09-10 完成，tests/server 325 + tests/context_engine 167 + tests/agent/middlewares 556 全绿）：

| 路线图步骤 | 内容 | 落地位置 |
| ---------- | ---- | -------- |
| 1.1 | `messages.py` async_generate 分解 | `server/service/messages.py`（协调器 + 步骤函数） |
| 1.2 | `LocalLlamaChatModel` 基类提取 | `models/LLMs/auxiliary_llm/local_adapters.py`（`LocalToolBinder` / `LocalStructuredOutput`） |
| 1.3 | HITL 策略注册表 | `agent/middlewares/humanInTheLoop/strategies.py`（`ApprovalHandlerRegistry` + 各审批 Handler） |
| 2.1 | `OutputRepetitionGuard` 拆分 | `repetition_detectors.py` + `repetition_state.py` + 瘦中间件壳 |
| 2.2 | `MultimodalProcessor` Strategy dispatch | `agent/middlewares/media_handlers.py`（`_MEDIA_HANDLERS` 注册表） |
| 2.3 | `search_messages` 三策略拆分 | `context_engine/core.py`（Trigram/Like/Fts Strategy + Factory） |
| 2.4 | `add_messages` 类型分发拆分 | `context_engine/store/core.py`（`MessageRowBuilder` 注册表） |
| 2.5 | `skill_scanner.py` SRP 拆分 | `server/service/skill_scan_{backend,cache,model,policy}.py` |
| 4.4 | `curator/__init__.py` Facade | `context_engine/curator/__init__.py`（公共 API + `__all__`） |

> 兼容性说明：`bridge.ts` Facade 与 `useSubagentTasks.ts` 保留全部历史导出符号（显式具名 re-export），`vi.mock('~/composables/bridge')` 等既有测试 mock 语义不受影响。
