# Design Pattern Refactoring

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式。
>
> 审查范围：`agent/`、`models/`、`server/`、`runtime/`、`channels/`、`bus/`、`context_engine/`、`plugins/`、`client/app/`
>
> 生成日期：2026-09-04；最近更新：2026-09-10
>
> 更新（2026-09-10）：本文档仅保留当前待办项，已完成条目已移除。

---

## 目录

- [Summary Table](#summary-table)
- [1. Backend: Agent & Middlewares](#1-backend-agent--middlewares)
- [2. Backend: Server & Service & Runtime](#2-backend-server--service--runtime)
- [3. Backend: Channels / Bus / Context Engine](#3-backend-channels--bus--context-engine)
- [4. Frontend: Client（剩余项）](#4-frontend-client剩余项)
- [5. Cross-cutting Patterns Summary](#5-cross-cutting-patterns-summary)
- [6. Recommended Refactoring Roadmap](#6-recommended-refactoring-roadmap)

---

## Summary Table

| P      | #   | Location                                                   | Problem                               | Pattern                           |
| ------ | --- | ---------------------------------------------------------- | ------------------------------------- | --------------------------------- |
| **P1** | 4 | `state_register_mem` 全局耦合 | 所有中间件直接依赖全局单例 | SessionState Facade + Enum key |
| **P1** | 6 | `runtime/state_register.py` | 每次 SQLite 操作新开连接 | 连接池/Repository |
| **P2** | 11 | ITTT/VTTT 模块级单例 | 与 main_llm 工厂模式不一致 | 统一工厂函数 |
| **P2** | 12 | `RepetitionGuardWrapper` 导入私有常量 | 跨模块私有依赖 | 依赖倒置 + Protocol |
| **P2** | 13 | 原始 SQL 泄漏 | checkpointer/message_search/store | Repository Pattern |
| **P2** | 14 | 原始 HTTP requests.post | embed_model/reranker_model | API Client Adapter |
| **P2** | 15 | 原始 subprocess/Popen | terminal.py/python_repl.py | Command Executor 抽象 |
| **P2** | 16 | `handleOperate` switch | 13 路 case | Command Registry |
| **P2** | 18 | `badgeClass`/`statusLabel` | 硬编码 if-else 映射 | Lookup Table |
| **P3** | 23 | `build_reasoning_kwargs` | provider if-elif 链 | Strategy + Registry |
| **P3** | 24 | `ToolGuardrails._evaluate` | 嵌套 if-else action 决策 | Chain of Responsibility |
| **P3** | 25 | `built_agent()` | 混合事件循环/checkpointer/LLM/中间件 | Builder Pattern |
| **P3** | 26 | `reranker_model/core.py` | FFI+numpy+HTTP+排序混合 | 分层架构 |
| **P3** | 27 | `Summarization` 依赖 agent.tools | 中间件层依赖工具层 | 依赖倒置 |
| **P3** | 28 | `ContextEngineHook` 创建子代理 | 中间件直接依赖 agent 构建 | Factory + DI |
| **P3** | 29 | `IterationBudget` 依赖私有函数 | 跨模块 `_is_internal_completion` | Protocol |
| **P3** | 30 | channels/core.py 导入时副作用 | import 即启动线程/注册 | Explicit initialization |
| **P3** | 31 | subagent/core.py 导入时副作用 | import 即调度任务 | Explicit initialization |
| **P3** | 32 | `ws_event_processor_dict` | 全局 dict 事件分发 | Registry Pattern |
| **P3** | 33 | `ws_handler` 混合关注点 | 消息解析/分发/响应/生命周期 | Separation of Concerns |
| **P3** | 34 | `messages.py` 模块级可变状态 | `_pending_args`/`_pending_raw` 全局 | State Pattern |
| **P3** | 35 | `turn_runner.py` 延迟导入缝隙 | 7 个 lazy import 绕循环依赖 | Dependency Injection |
| **P3** | 36 | `state_register.py` 无公共接口 | Mem/DB 无 Protocol | Interface Segregation |
| **P3** | 37 | `ContextEngineHook._after_agent_impl` | 返回 5 元组裸值 | Dataclass + NudgeCounter |
| **P3** | 38 | `aclean_old_checkpoints` | 70 行原始 SQL | Repository Pattern |
| **P3** | 42 | `postAgentStream` | 适配器 + 流编排混合 | Adapter + Error Classifier |

---

## 1. Backend: Agent & Middlewares

### 1.1 硬编码 if-else 链

#### 1.1.1 `build_reasoning_kwargs()` provider 分发

- **文件**: `models/LLMs/reasoning_payload.py:108-176`
- **问题**: if-elif 链分发 deepseek/openai(含 zhipu 子分支)/anthropic/默认
- **模式**: Strategy + Registry

```python
class ReasoningPayloadStrategy(ABC):
    @abstractmethod
    def build(self, **kwargs) -> dict: ...


_PAYLOAD_STRATEGIES: dict[str, ReasoningPayloadStrategy] = {
    "deepseek": DeepSeekStrategy(),
    "openai": OpenAIStrategy(),
    "anthropic": AnthropicStrategy(),
}
# build_reasoning_kwargs 简化为:
strategy = _PAYLOAD_STRATEGIES.get(provider, DefaultStrategy())
return strategy.build(**kwargs)
```

#### 1.1.3 `ContextEngineHook.after_agent` nudge 分发

- **文件**: `agent/middlewares/context_engine/core.py:297-303` (sync), `316-351` (async)
- **问题**: if need_memory and need_skill / if need_memory / if need_skill 三路分发
- **模式**: Strategy — `MemoryNudgeStrategy`、`SkillNudgeStrategy`、`CombinedNudgeStrategy`

#### 1.1.4 `ToolGuardrails._evaluate()` action 决策

- **文件**: `agent/middlewares/tool_guardrails.py:119-200`
- **问题**: 嵌套 if-else 决定 ALLOW/WARN/BLOCK/HALT，三种病理各有独立阈值
- **模式**: Chain of Responsibility — `ExactFailureDetector`、`SameToolFailureDetector`、`NoProgressDetector`，各自独立评估，最终 action 由最高优先级决定

---

### 1.2 混合关注点

#### 1.2.1 `MultimodalProcessor` — I/O + 解码 + 路径管理 + 消息操纵

- **文件**: `agent/middlewares/multimodal_processor.py`
- **问题**: 中间件同时负责 base64 解码(PIL)、HTTP 下载(urllib)、文件系统管理、magic byte 推断、消息内容操纵、历史清理
- **模式**: 分离关注点 — `MediaDownloadService`(I/O)、`MediaFormatDetector`、`MediaHintBuilder`(提示词)

#### 1.2.2 `ContextEngineHook` — prompt 管理 + 计数器 + 持久化 + 子代理

- **文件**: `agent/middlewares/context_engine/core.py`
- **问题**: 同时管理系统 prompt 加载/缓存、nudge 计数器(DB 读写)、消息持久化、HITL 拒绝修复、子代理调度
- **模式**: Facade + Service 层 — `SystemPromptService`、`NudgeCounterService`、`MessagePersistenceService`

#### 1.2.3 `built_agent()` — 事件循环 + 检查点 + LLM + 中间件

- **文件**: `agent/core.py:97-170`
- **问题**: 单函数混合事件循环检测/缓存、checkpointer 构建/清理、LLM 实例创建、中间件列表硬编码、agent 编译、wrapper 包装
- **模式**: Builder Pattern

```python
class AgentBuilder:
    def __init__(self):
        self._middlewares = []
        self._tools = []
        ...

    def with_checkpointer(self, cp): ...
    def with_llm(self, llm): ...
    def with_middleware(self, mw): ...
    def build(self) -> CompiledStateGraph: ...
```

#### 1.2.4 `reranker_model/core.py` — FFI + numpy + HTTP + 排序

- **文件**: `models/reranker_model/core.py` (717 行)
- **问题**: 单文件混合 ctypes FFI、GGUF 二进制解析、numpy 矩阵运算、HTTP API 客户端、排序/过滤逻辑
- **模式**: 分层架构 — `gguf_parser.py`、`llama_encoder.py`、`reranker_service.py`

---

### 1.3 缺失抽象

#### 1.3.1 `state_register_mem` 全局单例直接耦合

- **文件**: 所有中间件文件
- **问题**: 每个中间件 `from runtime import state_register_mem` 并用裸字符串 key（如 `"iteration_budget"`、`"heartbeat_stale"`），key 名称分散，无类型安全
- **模式**: SessionState Facade + Enum key

```python
class SessionStateKey(str, Enum):
    ANSWERING = "answering"
    CURRENT_TOOL_NAME = "current_tool_name"
    CURRENT_TOOL_ID = "current_tool_id"
    # ...


class SessionState:
    def __init__(self, session_id: str): ...
    def get(self, key: SessionStateKey, default=None): ...
    def set(self, key: SessionStateKey, value): ...
```

#### 1.3.2 原始 SQL 泄漏

- **文件**: `checkpointer/thread_safe_checkpointer.py:196-242`; `message_search.py:224-236`; `context_engine/store/core.py`
- **模式**: Repository Pattern — `CheckpointRepository` 封装所有 SQL

#### 1.3.3 原始 HTTP 请求泄漏

- **文件**: `embed_model/core.py:131-147`; `reranker_model/core.py:589-602`
- **问题**: 直接使用 `requests.post`，无重试、无连接池、无超时抽象
- **模式**: API Client Adapter — `EmbeddingApiClient`、`RerankerApiClient`

#### 1.3.4 原始 subprocess/Popen 操作泄漏

- **文件**: `terminal.py:212-263`; `python_repl.py:103-176`
- **问题**: 直接操作 `subprocess.Popen`/`asyncio.create_subprocess_*`，手动管理进程生命周期、编码、超时
- **模式**: Command Executor 抽象 — `CommandExecutor.run(argv, env, timeout) -> ExecutionResult`

---

### 1.4 紧耦合

#### 1.4.1 `RepetitionGuardWrapper` 导入中间件私有常量

- **文件**: `agent/stream_repetition_guard_wrapper.py:55-64`
- **问题**: 直接导入 `_HALTED_KEY`、`_INTERNAL_WARNED_KEY`、`_CHAR_RUN_MIN` 等私有常量，还实例化了一个 `OutputRepetitionGuard` 内部实例
- **模式**: 依赖倒置 — 提取 `RepetitionDetectorProtocol`，Wrapper 通过构造函数注入

#### 1.4.2 `IterationBudget` 依赖 `subagent_completion_drain` 私有函数

- **文件**: `agent/middlewares/iteration_budget.py:30`
- **问题**: `from agent.middlewares.subagent_completion_drain import _is_internal_completion` — 中间件之间通过私有函数耦合
- **模式**: 提取为公共 Protocol — `CompletionMessageProtocol`

#### 1.4.3 `Summarization` 直接依赖 `agent.tools.memory`

- **文件**: `agent/middlewares/summarization.py:410-412`
- **问题**: 中间件层直接导入工具层的 `memory_store` 并调用 `load_from_disk()`，违反分层架构
- **模式**: 依赖倒置 — 定义 `MemorySnapshotProvider` 接口，由 `core.py` 注入

#### 1.4.4 `ContextEngineHook` 直接创建子代理

- **文件**: `agent/middlewares/context_engine/nudge.py:233-248`
- **问题**: nudge 模块直接导入 `build_main_llm`、`get_agent_tools`、`create_agent`
- **模式**: Factory + DI — `NudgeAgentFactory` 协议注入

#### 1.4.5 ITTT/VTTT 模块级单例 vs 工厂不一致

- **文件**: `ITTT_model/core.py:80-84`; `VTTT_model/core.py:79-81`; 对比 `main_llm.py:90-93`
- **问题**: ITTT/VTTT 使用模块级 `init_chat_model(...)` 或 `LocalLlamaChatModel()`，而 main_llm/auxiliary_llm 使用工厂函数 `build_main_llm()`
- **模式**: 统一使用 `build_*()` 工厂函数模式

---

## 2. Backend: Server & Service & Runtime

### 2.1 硬编码 if-else / 全局状态

#### 2.1.1 `ws_event_processor_dict` — 全局 dict 事件分发

- **文件**: `server/trigger/core.py:41-75`
- **问题**: 全局 dict `ws_event_processor_dict` 映射事件名到处理器，无类型安全、无生命周期管理
- **模式**: Registry Pattern — 正式化为 `EventDispatcher` 类

#### 2.1.2 `messages.py` 模块级可变状态

- **文件**: `server/service/messages.py:26-29`
- **问题**: `_pending_args`/`_pending_raw` 进程全局 dict，隐式全局状态，难测试、难推理
- **模式**: State Pattern / Session-scoped context object

#### 2.1.3 `turn_runner.py` 延迟导入缝隙

- **文件**: `server/service/turn_runner.py` (7 个 lazy import 函数: `_iqs`, `get_registry`, `get_websocket_by_session_id`, `set_hitl_pending`, `async_generate`, `get_pending_interrupt`, `_get_active_tasks`)
- **问题**: 纯为绕循环依赖而存在
- **模式**: Dependency Injection

#### 2.1.4 `runtime/state_register.py` — 无公共接口 + 每次新开连接

- **文件**: `runtime/state_register.py`
- **问题**: `StateRegisterMem` 和 `StateRegisterDB` 方法签名相同但无 Protocol；DB 每次操作 `sqlite3.connect()`
- **模式**: Interface Segregation (Protocol) + 连接池

#### 2.1.5 `runtime/core.py` 错误导入

- **文件**: `runtime/core.py:1`
- **问题**: `from venv import logger` — 应为 `from loguru import logger`（疑似 bug）
- **模式**: Bug fix

#### 2.1.6 导入时副作用

- **文件**: `server/trigger/channels/core.py:293-346`（import 即启动线程 + 注册 consumer）；`server/trigger/subagent/core.py:48`（import 即 `_schedule_startup()` 调度任务）
- **模式**: Explicit initialization — `setup()` 函数

---

## 3. Backend: Channels / Bus / Context Engine

### 3.1 混合关注点 / 缺失抽象

#### 3.1.1 `context_engine/core.py` 模块级混合

- **问题**: 数据库访问(`_db`, `_lock`)、FTS5 语法(`_sanitize_fts5_query`)、内容编解码、业务逻辑、搜索结果组装全在一个模块
- **模式**: 分层架构 + Repository Pattern

#### 3.1.2 原始 SQL 泄漏

- **文件**: `context_engine/core.py` (trigram SQL, LIKE SQL, FTS SQL, context SQL); `store/core.py` (INSERT/SELECT/DELETE); `store/db.py` (DDL)
- **模式**: Repository Pattern — `MessageRepository`、`SearchRepository`

#### 3.1.3 FTS5 查询语法泄漏

- **文件**: `context_engine/core.py` — `_sanitize_fts5_query` 50 行处理 FTS5 语法
- **模式**: `FTSQuerySanitizer` 抽象

#### 3.1.5 模块级全局状态

- **文件**: `context_engine/core.py` (`_db`, `_lock`); `store/core.py` (`_db`, `_turn_assign_lock`); `curator/__init__.py` (`_idle_for_seconds`, `_curator_thread`); `plugins/channels/qq/core.py` (`_consecutive_install_failures`, `_cooldown_until`)
- **模式**: Dependency Injection

---

## 4. Frontend: Client（剩余项）

剩余项如下。

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

---

## 6. Recommended Refactoring Roadmap

### Phase 1: 消除最大风险（P0）

| Step | Target                                                 | Pattern                            | Est. Effort |
| ---- | ------------------------------------------------------ | ---------------------------------- | ----------- |
| 1.4  | 中间件 `session_id` 提取 + `state_register_mem` Facade | Mixin + Enum key                   | 1 天        |

### Phase 2: 拆解 God 模块（P1）

| Step | Target                                  | Pattern          | Est. Effort |
| ---- | --------------------------------------- | ---------------- | ----------- |
| 2.6  | 中间件 sync/async 基类                  | 装饰器/基类      | 1 天        |
| 2.7  | `FileStore` 基类提取                    | Template Method  | 1 天        |

### Phase 3: DRY 清理（P2）

| Step | Target                                                       | Pattern               | Est. Effort |
| ---- | ------------------------------------------------------------ | --------------------- | ----------- |
| 3.1  | 工具函数提取（`_read_dotenv`/`_tool_error`/`_args_hash` 等） | DRY                   | 1 天        |
| 3.4  | 原子写入工具 + HTTP helpers + 序列化模块                     | DRY                   | 1 天        |
| 3.5  | Repository Pattern（SQL 封装）                               | Repository            | 2-3 天      |
| 3.6  | Command Executor 抽象                                        | 抽象                  | 1 天        |
| 3.7  | 前端 Command Registry + Lookup Table（对应 4.4.1 / 4.4.3）   | Registry              | 1 天        |

### Phase 4: 架构清理（P3）

| Step | Target                             | Pattern        | Est. Effort |
| ---- | ---------------------------------- | -------------- | ----------- |
| 4.1  | 导入时副作用 → `setup()` 函数      | Explicit init  | 1 天        |
| 4.2  | `turn_runner` 依赖注入             | DI             | 2 天        |
| 4.3  | `built_agent()` Builder            | Builder        | 1 天        |
| 4.5  | `runtime/core.py` bug 修复         | Bug fix        | 0.5 天      |
| 4.6  | `state_register` Protocol + 连接池 | Interface Seg. | 1-2 天      |

---

> **注**: 每步重构应在对应测试通过后合并。建议按 Phase 1 → 2 → 3 → 4 顺序推进，前置 phase 的基础（如 SessionState Facade、TransportStrategy）是后续步骤的前提。
