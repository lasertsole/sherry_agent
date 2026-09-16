# Design Pattern Refactoring

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式。
>
> 审查范围：`agent/`、`models/`、`server/`、`runtime/`、`channels/`、`bus/`、`context_engine/`、`plugins/`、`config/`、`client/app/`
>
> 生成日期：2026-09-04；最近更新：2026-09-14
>
> 更新（2026-09-14）：5 个并行审计子代理全栈重审，新增前端深度审计、跨切面重复代码清单、架构方向违反、改进项标注。编号连续重排。

---

## 目录

- [Summary Table](#summary-table)
- [1. Backend: Agent & Middlewares](#1-backend-agent--middlewares)
- [2. Backend: Server & Service & Runtime](#2-backend-server--service--runtime)
- [3. Backend: Context Engine / Models / Bus / Channels](#3-backend-context-engine--models--bus--channels)
- [4. Frontend: Client](#4-frontend-client)
- [5. Cross-cutting: Duplicate Code Inventory](#5-cross-cutting-duplicate-code-inventory)
- [6. Cross-cutting: Global State Inventory](#6-cross-cutting-global-state-inventory)
- [7. Cross-cutting Patterns Summary](#7-cross-cutting-patterns-summary)
- [8. Recommended Refactoring Roadmap](#8-recommended-refactoring-roadmap)

---

## Summary Table

| P      | #   | Location                                                    | Problem                                                             | Pattern                           |
| ------ | --- | ----------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------- |
| **P0** | 1   | `channels/manager.py` _dispatch_outbound vs _consume_loop   | 竞争消费同一队列，消息丢失                                          | 统一消费者                        |
| **P1** | 2   | `context_engine/store/core.py:14` `_db = get_db()`          | 导入即触发 SQLite 连接 + migration                                  | Lazy init                         |
| **P1** | 3   | `state_register_mem` 全局耦合                               | 所有中间件直接依赖全局单例，裸字符串 key                            | SessionState Facade + Enum key    |
| **P1** | 4   | `runtime/session/state_register.py`                         | 每次 SQLite 操作新开连接；无 Protocol                               | 连接池/Repository + Protocol      |
| **P1** | 5   | `summarization/core.py` 2250 行                                  | 15+ 职责的上帝类；12+ sync/async 双路径                             | 拆分为 6 个模块                   |
| **P2** | 6   | ITTT/VTTT/reranker/extract 模块级单例                       | 与 main_llm 工厂模式不一致                                          | 统一工厂函数                      |
| **P2** | 7   | `RepetitionGuardWrapper` + `ContextLimitGuard` 导入私有常量 | 跨模块私有依赖（8 个私有符号）                                      | 依赖倒置 + Protocol               |
| **P2** | 8   | 原始 SQL 泄漏（5 个文件）                                   | checkpointer/store/embeddings/events                                | Repository Pattern                |
| **P2** | 9   | 原始 HTTP requests.post                                     | embed_model/reranker_model                                          | API Client Adapter                |
| **P2** | 10  | 原始 subprocess/Popen                                       | terminal/python_repl/skill_manage                                   | Command Executor 抽象             |
| **P2** | 11  | `handleOperate` switch（11 路）                             | 前端 ad-hoc 对话框管理                                              | Command Registry + Dialog Manager |
| **P2** | 12  | `badgeClass`/`statusLabel`/`statusColor`/`statusKey`        | 状态映射重复 4 处                                                   | Lookup Table                      |
| **P2** | 13  | `config/schema.py` 导入 `models/`                           | 架构方向违反（config→models）                                       | 反转依赖                          |
| **P2** | 14  | `models/LLMs/main_llm.py` 导入 `agent/`                     | 架构方向违反（models→agent）                                        | 提取到 pub/ 或 config/            |
| **P2** | 15  | 3 个 SQLite 存储无共享基类                                  | _connect/_ensure_tables_sync 重复 3 份                              | BaseSQLiteRepository              |
| **P2** | 16  | `_convert_message_to_dict` 重复                             | ITTT/VTTT/base_local_llama 各一份                                   | Template Method 完善              |
| **P2** | 17  | 4 处 JSON content decode 重复                               | 同一 `\x00json:` 前缀解析逻辑 4 份                                  | 提取为 `ContentDecoder`           |
| **P2** | 18  | 5 处 `load_dotenv` + config dict 构建重复                   | 5 个模型文件各自重复 env 加载                                       | 提取 `ModelEnvBuilder`            |
| **P2** | 19  | 前端 `Response.data: unknown`                               | 26 处 `as unknown as` 类型断言根因                                  | `Response<T>` 泛型                |
| **P2** | 20  | 前端 3 个 WebSocket 管理无统一抽象                          | 3 种重连策略各自实现                                                | `WebSocketConnection` 基类        |
| **P2** | 21  | 前端 `upload.ts`/`health.ts` raw fetch                      | 绕过 requestApi，无 token/retry                                     | 统一 API 客户端                   |
| **P2** | 22  | 前端错误处理 4 种策略不一致                                 | catch→null / catch→缓存 / throw / catch→默认                        | Result<T,E> 或统一规范            |
| **P3** | 23  | `build_reasoning_kwargs` provider 分发                      | if-elif 链                                                          | Strategy + Registry               |
| **P3** | 24  | `ToolGuardrails._evaluate` + 消息构造                       | 嵌套 if-else action 决策                                            | Chain of Responsibility           |
| **P3** | 25  | `memory.py` action 分发（5 路）                             | if-elif 链                                                          | Strategy + Registry               |
| **P3** | 26  | `skill_manage.py` action 分发（5+ 路）                      | 嵌套 if-elif 链                                                     | Strategy + Registry               |
| **P3** | 27  | `built_agent()`                                             | 混合事件循环/checkpointer/LLM/中间件                                | Builder Pattern                   |
| **P3** | 28  | `reranker_model/core.py` 701 行                             | FFI+numpy+HTTP+排序混合                                             | 分层架构                          |
| **P3** | 29  | `Summarization` 依赖 agent.tools                            | 中间件层依赖工具层（5 处）                                          | 依赖倒置                          |
| **P3** | 30  | `ContextEngineHook`/nudge 创建子代理                        | 中间件直接依赖 agent 构建                                           | Factory + DI                      |
| **P3** | 31  | `IterationBudget` 依赖私有函数                              | `_is_internal_completion` 跨模块私有                                | Protocol                          |
| **P3** | 32  | 导入时副作用（5 处）                                        | channels/core、subagent/core、embed_model、extract_model、ITTT/VTTT | Explicit initialization           |
| **P3** | 33  | `ws_event_processor_dict` 全局 dict                         | 事件分发无类型安全                                                  | Registry Pattern                  |
| **P3** | 34  | `messages.py` 模块级可变状态                                | `_pending_args`/`_pending_raw` 全局                                 | State Pattern                     |
| **P3** | 35  | `turn_runner.py` 延迟导入                                   | 7 个 lazy import 绕循环依赖                                         | Dependency Injection              |
| **P3** | 36  | `state_register.py` 无公共接口                              | Mem/DB 无 Protocol                                                  | Interface Segregation             |
| **P3** | 37  | `ContextEngineHook._after_agent_impl`                       | 返回 5 元组裸值                                                     | Dataclass + NudgeCounter          |
| **P3** | 38  | `aclean_old_checkpoints`                                    | 70 行原始 SQL                                                       | Repository Pattern                |
| **P3** | 39  | `postAgentStream`                                           | 适配器 + 流编排混合                                                 | Adapter + Error Classifier        |
| **P3** | 40  | `events/store.py` `__import__()` 反模式                     | 为绕循环依赖用动态导入                                              | 延迟导入或 DI                     |
| **P3** | 41  | `curator/orchestrator.py` 708 行                            | sync llm.invoke() 在异步路径                                        | async/await 或 to_thread          |
| **P3** | 42  | 前端 `ChatBox.vue` 883 行                                   | 渲染+复制+滚动+载体+媒体解析                                        | 拆分为多个 composable             |
| **P3** | 43  | 前端 `agent-socket.ts` 540 行                               | WS+消息+重连+上传+队列                                              | 拆分为 ConnectionManager/Router   |
| **P3** | 44  | 前端 `resolveSid`/`isClient`/`safeT` 重复                   | 2-3 处各自重复                                                      | 提取共享工具                      |
| **P3** | 45  | 前端 Pinia 严重欠用                                         | 仅 1 个状态在 Pinia，大量在模块级 ref                               | 迁移到 Pinia store                |

---

## 1. Backend: Agent & Middlewares

### 1.1 硬编码 if-else 链

#### 1.1.1 [CONFIRMED] `build_reasoning_kwargs()` provider 分发

- **文件**: `models/LLMs/reasoning_payload.py:148-216`
- **问题**: 4 分支 if-elif 链分发 deepseek/openai(含 zhipu 子分支)/anthropic/默认
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

#### 1.1.2 [NEW] `memory.py` action 分发

- **文件**: `agent/tools/memory.py:611-641`
- **问题**: 5 分支 if-elif：`action == "replace"` / `"remove"` / `"fact_add"` / `"fact_read"` / `"fact_search"`
- **模式**: Command/Strategy Registry — `MEMORY_ACTIONS = {"replace": ReplaceStrategy(), ...}`

#### 1.1.3 [NEW] `skill_manage.py` action 分发

- **文件**: `agent/tools/skill_tools/skill_manage.py:922-980`
- **问题**: 大型 if-elif 链：`action in {"edit", "patch", "delete", "write_file", "remove_file"}`，内部嵌套 elif
- **模式**: Command/Strategy Registry

#### 1.1.4 [CONFIRMED] `ToolGuardrails._evaluate()` action 决策

- **文件**: `agent/middlewares/tool_guardrails/core.py:124-222, 354-483`
- **问题**: 嵌套 if-else 决定 ALLOW/WARN/BLOCK/HALT，三种病理各有独立阈值；`_wrap_tool_call_impl` 有大型 if/elif 链构造消息
- **模式**: Chain of Responsibility — `ExactFailureDetector`、`SameToolFailureDetector`、`NoProgressDetector`，各自独立评估，最终 action 由最高优先级决定；Message Factory 处理消息构造

#### 1.1.5 [NEW] `announce/delivery.py` 状态路由

- **文件**: `agent/tools/subagent/announce/delivery.py:312-443`
- **问题**: 多个 if/elif 链分发 delivery status（suspended/terminal/error/timeout）
- **模式**: State pattern 或 dispatch table

---

### 1.2 混合关注点 / 上帝类

#### 1.2.1 [CONFIRMED] `summarization/core.py` — 2250 行上帝类（从 1996 增长）

- **文件**: `agent/middlewares/summarization/core.py`
- **职责清单** (15+ 职责):
  1. Token 估算 (693-705)
  2. Budget 计算 (710-716)
  3. 触发检查 (721-732)
  4. 抢占式截断 (734-752)
  5. 4 路由溢出决策 (775-955)
  6. T3 后响应重新检查 (1004-1127)
  7. T4/T5 提供商错误恢复 (1132-1312)
  8. 冷却/降级管理 (1377-1425)
  9. Cutoff 确定 (1430-1462)
  10. Summary prompt 构造 (1496-1541)
  11. LLM summary 创建 (1547-1597)
  12. 非 LLM 策略管道 (1632-1676)
  13. 恢复上下文捕获/注入 (1685-1724)
  14. 文件操作棘轮 (512-616)
  15. 系统提示词重建 (1869-1884)
- **附加问题**: 12+ sync/async 方法对（`_apply_compression`/`_aapply_compression`、`_execute_compact`/`_aexecute_compact` 等全量复制）
- **模式**: 拆分为 `OverflowRouter`、`SummaryGenerator`、`CompressionExecutor`、`DegradationMonitor`、`FileOpsExtractor`、`CooldownManager`

#### 1.2.2 [CONFIRMED] `built_agent()` — 组合根

- **文件**: `agent/core.py:103-199`
- **问题**: 单函数连接 14 个 middleware、3 个 LLM 实例、checkpointer、2 个 graph wrapper
- **改善**: `init()` 提取了导入时副作用（已修复），但 `built_agent()` 本身仍是巨大接线函数
- **模式**: Builder Pattern

#### 1.2.3 [CONFIRMED] `nudge.py` — 777 行多职责模块

- **文件**: `agent/middlewares/context_engine/nudge.py`
- **职责**: nudge agent 创建、memory review、plan extraction、facts section 渲染、compression todo 对账、todo shim 构造
- **模式**: 拆分为 `NudgeAgentFactory`、`MemoryReviewer`、`PlanExtractor`、`CompressionTodoReconciler`

#### 1.2.4 [CONFIRMED] `reranker_model/core.py` — 701 行混合

- **文件**: `models/reranker_model/core.py`
- **问题**: 混合 ctypes FFI、GGUF 二进制解析、numpy 矩阵运算、HTTP API 客户端、排序/过滤逻辑
- **模式**: 分层架构 — `gguf_parser.py`、`llama_encoder.py`、`reranker_service.py`

---

### 1.3 缺失抽象

#### 1.3.1 [CONFIRMED] `state_register_mem` 全局单例直接耦合

- **文件**: 所有中间件文件
- **问题**: 每个中间件 `from runtime import state_register_mem` 并用裸字符串 key（如 `"iteration_budget"`、`"heartbeat_stale"`），key 名称分散，无类型安全
- **模式**: SessionState Facade + Enum key

```python
class SessionStateKey(str, Enum):
    ANSWERING = "answering"
    CURRENT_TOOL_NAME = "current_tool_name"
    CURRENT_TOOL_ID = "current_tool_id"

class SessionState:
    def __init__(self, session_id: str): ...
    def get(self, key: SessionStateKey, default=None): ...
    def set(self, key: SessionStateKey, value): ...
```

#### 1.3.2 [CONFIRMED] 原始 SQL 泄漏（5 个文件）

- `agent/checkpointer/thread_safe_checkpointer.py:196-242`
- `context_engine/store/core.py` (INSERT/SELECT/UPDATE/DELETE，755 行)
- `context_engine/store/db.py` (DDL，CREATE TABLE/INDEX/TRIGGER)
- `context_engine/embeddings/store.py:32-54` (INSERT + SELECT JOIN)
- `context_engine/events/store.py` (append_event + get_events)
- **模式**: Repository Pattern — `MessageRepository`、`EventRepository`、`EmbeddingRepository`

#### 1.3.3 [CONFIRMED] 原始 HTTP 请求泄漏

- `models/embed_model/core.py:128` — `requests.post(url, ..., verify=False)`
- `models/reranker_model/core.py:573,623,679` — `requests.post(..., verify=False)`
- `agent/middlewares/media_pipeline/media_handlers.py:90-91` — `urllib.request.urlopen(req, timeout=30)`
- **模式**: API Client Adapter — `EmbeddingApiClient`、`RerankerApiClient`、`MediaDownloader`

#### 1.3.4 [CONFIRMED] 原始 subprocess/Popen 操作泄漏

- `agent/tools/terminal.py:189,198` — `subprocess.Popen`
- `agent/tools/python_repl.py:121` — `subprocess.Popen`
- `agent/tools/skill_tools/skill_manage.py:327` — `subprocess.run`
- **模式**: Command Executor 抽象 — `CommandExecutor.run(argv, env, timeout) -> ExecutionResult`

---

### 1.4 紧耦合

#### 1.4.1 [CONFIRMED] `RepetitionGuardWrapper` 导入中间件私有常量

- **文件**: `agent/wrapper/repetition_guard.py:55-64`
- **问题**: 直接导入 `_HALTED_KEY`、`_INTERNAL_WARNED_KEY`、`_CHAR_RUN_MIN`、`_MIN_CONTENT_LENGTH`、`_STREAM_WARNING` 等 6 个私有符号
- **模式**: 依赖倒置 — 提取 `RepetitionDetectorProtocol`，Wrapper 通过构造函数注入

#### 1.4.2 [NEW] `ContextLimitGuardWrapper` 导入私有常量

- **文件**: `agent/wrapper/context_limit.py:38`
- **问题**: `from agent.middlewares.summarization.summarization_components import _FORCE_RECOVERY_KEY`
- **模式**: 依赖倒置 — 提取为公共 Protocol

#### 1.4.3 [CONFIRMED] `IterationBudget` 依赖 `subagent_completion_drain` 私有函数

- **文件**: `agent/middlewares/iteration_budget/core.py:32`
- **问题**: `from agent.middlewares.subagent_completion_drain.core import _is_internal_completion`
- **模式**: 提取为公共 Protocol — `CompletionMessageProtocol`

#### 1.4.4 [CONFIRMED] `Summarization` 直接依赖 `agent.tools`（5 处）

- **文件**: `agent/middlewares/summarization/core.py:410-412` 等
- **问题**: 中间件层直接导入工具层的 `memory_store`、`taskflow`、`memory_tiered`，违反分层架构
- **同类问题**: `context_engine/core.py` 导入 `todolist`；`nudge.py` 导入 `subagent`/`todolist`/`memory`；`todo_continuation/core.py` 导入 `todolist`；`subagent_completion_drain/core.py` 导入 `announce`
- **模式**: 依赖倒置 — 定义 `MemorySnapshotProvider` 接口，由 `core.py` 注入

#### 1.4.5 [CONFIRMED] `ContextEngineHook`/nudge 直接创建子代理

- **文件**: `agent/middlewares/context_engine/nudge.py:358-385`
- **问题**: `_create_nudge_agent` 在中间件层创建完整 LangGraph agent，导入 `build_main_llm`、`get_agent_tools`、`create_agent`
- **模式**: Factory + DI — `NudgeAgentFactory` 协议注入

#### 1.4.6 [CONFIRMED] ITTT/VTTT/reranker/extract 模块级单例 vs 工厂不一致

- **文件**: `ITTT_model/core.py:81`; `VTTT_model/core.py:76`; `reranker_model/__init__.py:120`; `extract_model/core.py:194`
- **问题**: 这 4 个模块在导入时创建单例，而 `main_llm`/`auxiliary_llm`/`reasoner_llm` 使用 `build_*()` 工厂函数
- **模式**: 统一使用 `build_*()` 工厂函数

---

## 2. Backend: Server & Service & Runtime

### 2.1 硬编码 if-else / 全局状态

#### 2.1.1 [CONFIRMED] `ws_event_processor_dict` — 全局 dict 事件分发

- **文件**: `server/trigger/core.py:41-75`
- **问题**: 全局 dict 映射事件名到处理器，无类型安全、无生命周期管理
- **模式**: Registry Pattern — 正式化为 `EventDispatcher` 类

#### 2.1.2 [CONFIRMED] `messages.py` 模块级可变状态

- **文件**: `server/service/messages.py:26-29`；`server/service/stream_dispatch.py:52,55`
- **问题**: `_pending_args`/`_pending_raw` 进程全局 dict，隐式全局状态，难测试
- **模式**: State Pattern / Session-scoped context object

#### 2.1.3 [CONFIRMED] `turn_runner.py` 延迟导入缝隙

- **文件**: `server/service/turn_runner.py`（7 个 lazy import：`_iqs`、`get_registry`、`get_websocket_by_session_id`、`set_hitl_pending`、`async_generate`、`get_pending_interrupt`、`_get_active_tasks`）
- **问题**: 纯为绕循环依赖而存在
- **模式**: Dependency Injection

#### 2.1.4 [CONFIRMED] `runtime/session/state_register.py` — 无公共接口 + 每次新开连接

- **文件**: `runtime/session/state_register.py`
- **问题**: `StateRegisterMem` 和 `StateRegisterDB` 方法签名相同但无 Protocol（Register ABC 仅要求 `clear_session()`，太薄）；DB 每次操作 `sqlite3.connect()`；`state_register_db` 在 `__init__` 中调用 `_init_db()` 即触发 SQLite 连接 + CREATE TABLE
- **模式**: Interface Segregation (Protocol) + 连接池 + Lazy init

#### 2.1.5 导入时副作用（5 处）

- `server/trigger/channels/core.py:293-346` — import 即启动线程 + 注册 consumer
- `server/trigger/subagent/core.py:48` — import 即 `_schedule_startup()` 调度任务
- `models/embed_model/core.py:51,91-105` — import 时 `urllib3.disable_warnings()` + `_detect_backend()` + `_ensure_downloaded()`
- `models/extract_model/core.py:22,194` — import 时设 `os.environ` + 创建 singleton
- `models/ITTT_model/core.py` / `VTTT_model/core.py` — import 时读 env + 创建模型实例
- **模式**: Explicit initialization — `setup()` 函数

---

## 3. Backend: Context Engine / Models / Bus / Channels

### 3.1 混合关注点 / 缺失抽象

#### 3.1.1 [CONFIRMED] `context_engine/core.py` 模块级混合

- **问题**: 数据库访问(`_db`, `_lock`)、FTS5 语法(`_sanitize_fts5_query`)、内容编解码、业务逻辑、搜索结果组装全在一个模块
- **改善**: 已引入 Strategy Pattern（`_SearchStrategy` ABC + `_TrigramStrategy`/`_LikeStrategy`/`_FtsStrategy` + `_SearchStrategyFactory`），搜索分发从 if-else 变为多态
- **模式**: 分层架构 + Repository Pattern（部分已改进）

#### 3.1.2 [CONFIRMED] `context_engine/store/core.py` — 755 行 God 模块（从 648 增长）

- **问题**: 混合行构建（3 个 builder）、turn 管理、幂等性、session leaf 树、压缩检查点、session 列举
- **模式**: 拆分为 `message_repo.py` / `checkpoint_repo.py` / `session_repo.py`

#### 3.1.3 [NEW] `context_engine/curator/orchestrator.py` — 708 行

- **问题**: 混合 LLM 审查生成、umbrella 解析、合并执行、源文件迁移、系统 prompt 刷新、状态管理
- **附加**: 第 96/454 行 sync `llm.invoke()` 在异步路径中调用
- **模式**: 拆分为 `review.py` / `consolidation.py` / `refresh.py` + async/await

#### 3.1.4 [CONFIRMED] FTS5 查询语法泄漏

- **文件**: `context_engine/core.py:92-154` — `_sanitize_fts5_query()` 62 行正则处理
- **模式**: `FTSQuerySanitizer` 抽象

#### 3.1.5 [CONFIRMED] 模块级全局状态

- `context_engine/core.py` (`_db`, `_lock`)
- `context_engine/store/core.py` (`_db`, `_turn_assign_lock`, `_turn_assign_lock`, `_last_turn_ms`)；第 14 行 `_db = get_db()` eager init（与 `core.py` 的 lazy `_shared_db()` 不一致）
- `context_engine/store/db.py` (`_db`, `_db_lock`, `_db_path`)
- `context_engine/curator/__init__.py` (`_idle_for_seconds`, `_curator_thread`, `_curator_check_interval`)
- `context_engine/embeddings/indexer.py` (`_embed_fn`)
- `context_engine/curator/orchestrator.py` (`_report_cache`)
- `plugins/channels/qq/core.py` (`_consecutive_install_failures`, `_cooldown_until`)
- **模式**: Dependency Injection

#### 3.1.6 [NEW] `events/store.py` `__import__()` 反模式

- **文件**: `context_engine/events/store.py:22,39`
- **问题**: 使用 `__import__("context_engine.store.db", fromlist=["get_db"]).get_db()` 而非正常 import
- **模式**: 延迟导入或 DI

#### 3.1.7 [NEW] JSON content decode 4 处重复

| 文件                                 | 函数                      | 行号    |
| ------------------------------------ | ------------------------- | ------- |
| `context_engine/core.py`             | `_decode_content()`       | 157-165 |
| `context_engine/store/core.py`       | `_decode_json_columns()`  | 506-527 |
| `context_engine/store/core.py`       | `_decode_title_content()` | 673-695 |
| `context_engine/embeddings/store.py` | `_decode()`               | 91-96   |

- **模式**: 提取为 `ContentDecoder` 共享函数

---

### 3.2 架构方向违反

#### 3.2.1 [CONFIRMED] `config/schema.py` 导入 `models/`

- **文件**: `config/schema.py:185,260`
- `from models.providers.registry import PROVIDERS`（在 `_match_provider` 方法内，延迟导入）
- `from models.providers.registry import find_by_name`（在 `get_api_base` 方法内）
- **问题**: config 层依赖 models 层，违反分层架构（虽不在 `config/features/` 下，AGENTS.md 规则未明确禁止 `config/schema.py`，但依赖方向仍反了）
- **模式**: 反转依赖 — 将 provider 匹配逻辑移到 models 层或新建 resolver 层

#### 3.2.2 [NEW] `models/LLMs/main_llm.py` 导入 `agent/`

- **文件**: `models/LLMs/main_llm.py:138`
- `from agent.middlewares.llm_retry.core import FallbackCandidate` — 在 `build_fallback_chain()` 内
- **问题**: models 层（基础设施）依赖 agent 层（业务逻辑），违反分层架构
- **模式**: 提取 `FallbackCandidate` 到 `pub/` 或 `config/`

---

### 3.3 Bus / Channel 架构

#### 3.3.1 [CONFIRMED] `bus/core.py` 单一全局队列

- 第 31-32 行：inbound 和 outbound 各一个 `asyncio.Queue`，所有 channel 共享
- **改善**: 有界队列已添加（`maxsize=BUS_QUEUE_MAXSIZE`）
- **模式**: 按渠道路由

#### 3.3.2 [NEW] `ChannelManager._consume_loop` 发送到 ALL channels + 竞争消费 bug

- **文件**: `channels/manager.py:88,111,243`
- **问题 1**: `_consume_loop("outbound")` 遍历所有配置渠道，每条消息发送到所有渠道而非仅目标渠道
- **问题 2 (P0 bug)**: `_dispatch_outbound()`（第 243 行，按 msg.channel 路由）和 `_consume_loop("outbound")`（第 88 行）都从 `self._bus.consume_outbound()` 消费。两个 task 竞争同一队列——当 `_consume_loop` 赢得消息时 `_outbound_consumer` 默认为 `None`，消息被消费后丢弃
- **模式**: 统一消费者 — 删除 `_outbound_consume_loop` 或合并为单一消费者

#### 3.3.3 [CONFIRMED] Channel base class `config: Any`

- **文件**: `channels/base.py:16` — `def __init__(self, config: Any, bus: MessageBus)`
- **模式**: 泛型或 TypedDict 约束

#### 3.3.4 [NEW] reranker rank/filter/predict_scores 三方法重复

- `CrossEncoderGGUF` 和 `CloudReranker` 重复实现相同方法签名和逻辑
- **模式**: 提取 `RerankerProtocol` ABC，两个实现继承接口

---

## 4. Frontend: Client

### 4.1 硬编码 if-else / switch 链

#### 4.1.1 [CONFIRMED] `handleOperate` — 11 路 switch（非之前报告的 13 路）

- **文件**: `client/app/pages/home/index.vue:353-392`
- **问题**: 11 个 case（skills, knowledgeGraph, stats, systemConfig, persona, memory, heartbeat, cron, logs, notification, extend），每个仅设置一个 `showXxxDialog.value = true`
- **附加问题**: 10 个独立的 `showXxxDialog = ref(false)` ad-hoc 管理对话框，无集中管理器
- **模式**: Command Registry — `const dialogRegistry: Record<string, Ref<boolean>>` + `useDialogManager()` composable

#### 4.1.2 [NEW] `handleOperate` in `[sid].vue` — 4 路 switch

- **文件**: `client/app/pages/home/index/[sid].vue:681-700`
- **问题**: 4-case：createSession, uploadImage, uploadAudio, uploadVideo

#### 4.1.3 [CONFIRMED] `badgeClass`/`statusLabel` — 硬编码 if-else 映射

- **文件**: `client/app/composables/useSubagentTasks.ts:30-60`
- **问题**: `badgeClass` 6 个条件分支，`statusLabel` 9 个条件分支
- **模式**: Lookup Table — `const STATUS_STYLE: Record<string, {badge, labelKey}>`

#### 4.1.4 [NEW] `statusColorLight`/`statusColorDark`/`statusKey` — 状态映射重复第 2-3 处

- **文件**: `client/app/pages/home/components/SubagentFlowGraph.vue:103-158`
- **问题**: 两个 switch + 一个 if-else 链，将同一 SubagentRun 状态枚举映射到颜色/i18n key。**重复了** `useSubagentTasks.ts` 中 `badgeClass`/`statusLabel` 的逻辑
- **模式**: 提取为共享的 `SUBAGENT_STATUS_META` 查找表

#### 4.1.5 [NEW] CronDialog.vue — 3 个 switch

- **文件**: `client/app/pages/home/components/CronDialog.vue:323,338,486`
- **问题**: `everyToMs()`（4-case）、`buildSchedule()`（3-case）、`describeSchedule()`（3-case）围绕同一调度类型枚举
- **模式**: 合并为一个 Strategy 模式

#### 4.1.6 [NEW] TodoItem.vue — statusIcon switch

- **文件**: `client/app/components/chat/TodoItem.vue:42-52`
- **问题**: 4-case switch 将 todo 状态映射到图标
- **模式**: 小型查找表

---

### 4.2 混合关注点

#### 4.2.1 [CONFIRMED] `agent-socket.ts` — 540 行混合

- **文件**: `client/app/composables/bridge/agent-socket.ts`
- **职责**: WS 连接生命周期、消息解析/路由（7 种事件）、指数退避重连 + 回退定时器、出站消息队列、媒体上传调度、Promise 追踪、会话 turn 追踪、mitt 事件广播
- **模式**: 拆分为 `ConnectionManager`、`MessageRouter`、`SendQueue`、`UploadPipeline`

#### 4.2.2 [CONFIRMED] `ChatBox.vue` — 883 行混合

- **文件**: `client/app/pages/home/components/ChatBox.vue`
- **职责**: 消息渲染（4 种布局）、复制到剪贴板、滚动管理、载体消息分流、图片/音频/视频 src 解析（3 处重复）、图片加载失败处理、工具卡片展开/折叠、思考过程展开、连续消息判断、turn 分组
- **模式**: 提取 `useMessageMedia`、`useCopyMessage`、`useScrollManagement`、`useCardExpansion`

#### 4.2.3 [CONFIRMED] `ws.ts` — 双 WS 管理在一个文件

- **文件**: `client/app/composables/ws.ts` (390 行)
- **问题**: 一个文件管理两个独立 WebSocket 通道（`/sessions/ws` 和 `/subagents/ws`），各自包含连接管理、消息解析、重连、心跳
- **模式**: 拆分为 `session-ws.ts` 和 `subagent-ws.ts`

#### 4.2.4 [NEW] `connection.ts` — WS + 浏览器事件 + toast + i18n 混合

- **文件**: `client/app/composables/connection.ts` (246 行)
- **问题**: 混合 WS 生命周期启动、浏览器 online/offline 事件、toast 通知、i18n 翻译、edge 去重

---

### 4.3 模块级可变全局状态

**约 45 个模块级可变全局状态，分布在 18 个文件中**：

| 文件                    | 变量                                                                                                                                                         | 数量 |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---- |
| `ws.ts`                 | wsInstance, everConnected, heartbeatTimer, pongTimeoutTimer, pendingPong, missedPongs, subagentWsInstance, subagentReady                                     | 8    |
| `agent-socket.ts`       | sockets Map                                                                                                                                                  | 1    |
| `mitt.ts`               | emitter (const 但本质可变单例)                                                                                                                               | 1    |
| `clientLog.ts`          | activeStore, logBuffer, clientLogSubscribers, captureInstalled, origConsole, logDb                                                                           | 5    |
| `db.ts`                 | db (Dexie singleton)                                                                                                                                         | 1    |
| `subagent-state.ts`     | taskRuns, allTaskRuns, taskLoading, lastTasksFetchedAt, subagentWsReady, tasksTabActive                                                                      | 6    |
| `subagent-tree.ts`      | expandedRunId, selectedRunId, focusedRunId                                                                                                                   | 3    |
| `subagent-selection.ts` | selectedRunIds, deletingRunIds                                                                                                                               | 2    |
| `subagent-sync.ts`      | lastLoadedSessionId, subscribed, subagentSessionsLoaded, subagentValidSessionIds                                                                             | 4    |
| `connection.ts`         | clientFlagOverride, isOnline, backendStatus, lastReachable                                                                                                   | 4    |
| `toast.ts`              | clientFlagOverride, toastApi                                                                                                                                 | 2    |
| `use-todo-list.ts`      | todos, currentSid, subscribed                                                                                                                                | 3    |
| `useChatBackground.ts`  | backgroundUrl, backgroundOpacity, backgroundLoaded                                                                                                           | 3    |
| 组件内 let              | GChart.vue(9), ChatBox.vue(1), LogsDialog.vue(4), HeartbeatDialog.vue(1), AvatarCropDialog.vue(1), inputBox.vue(1), ImagePreviewOverlay.vue(1), [sid].vue(1) | 19   |

- **模式**: 迁移到 Pinia store 或 composable 工厂函数

---

### 4.4 缺失抽象

#### 4.4.1 [CONFIRMED] 原始 fetch() 绕过 API 客户端

- `upload.ts:69` — `fetch(...)` 完全绕过 requestApi.ts，无重试、无 token、无 toast
- `health.ts:18` — `fetch(...)` 同上
- **模式**: 统一通过 requestApi.ts

#### 4.4.2 [CONFIRMED] 3 个 WebSocket 管理无统一抽象

| 实现                     | 重连策略            | 心跳                     |
| ------------------------ | ------------------- | ------------------------ |
| `ws.ts::useWs()`         | 5s 固定             | 10s ping/5s pong timeout |
| `ws.ts::useSubagentWs()` | 5s 固定（复制粘贴） | 无                       |
| `agent-socket.ts`        | 指数退避 + 5s 回退  | 无                       |

- **模式**: `WebSocketConnection` 基类 + 可插拔重连策略

#### 4.4.3 [NEW] JSON.parse 无运行时 schema 验证

- `ws-message.ts:18` — `JSON.parse(event.data as string) as T`
- `messages.ts:97` — `res as unknown as CachedMessage[]`
- `message-items.ts:64` — `JSON.parse(calls)`
- `ChannelSettingsDialog.vue:328` — `JSON.parse(trimmed)`
- **模式**: zod 或类似库进行运行时 schema 验证

---

### 4.5 不一致模式

#### 4.5.1 [CONFIRMED] API 调用方式不一致

| 文件          | 方法         | 重试 | Token | Toast |
| ------------- | ------------ | ---- | ----- | ----- |
| requestApi.ts | `$fetch`     | 3 次 | 自动  | 自动  |
| upload.ts     | 原始 `fetch` | 无   | 无    | 无    |
| health.ts     | 原始 `fetch` | 无   | 无    | 无    |

#### 4.5.2 [CONFIRMED] 状态管理位置不一致

| 存储         | 位置                                    | 用途                                           |
| ------------ | --------------------------------------- | ---------------------------------------------- |
| Pinia        | `stores/ui.ts`（仅 1 个）               | sidebarCollapsed                               |
| Dexie        | `db.ts`（8 表）, `clientLog.ts`（1 表） | 消息/角色/会话/草稿/背景/子代理/标题/预设/日志 |
| 模块级 ref   | 5 个 composable 文件                    | subagent 运行时/背景/todo/连接                 |
| localStorage | `requestApi.ts`                         | 仅 token                                       |
| 组件 ref     | 各组件                                  | 局部 UI                                        |

**Pinia 严重欠用** — 大量应共享的状态放在模块级 ref 中。

#### 4.5.3 [CONFIRMED] 错误处理 4 种策略不一致

| 策略                    | 文件                 | 返回值               | 通知       |
| ----------------------- | -------------------- | -------------------- | ---------- |
| catch → null + toast    | requestApi.ts        | `null`               | 自动 toast |
| catch → 缓存回退        | messages.ts          | 本地缓存             | 无         |
| throw                   | upload.ts            | 抛出 Error           | 无         |
| catch → 默认对象        | health.ts            | `{ healthy: false }` | 无         |
| catch → 日志 + 保持现状 | subagent-sync.ts     | 旧数据               | 无         |
| catch → 静默忽略        | ws.ts, ws-message.ts | null                 | 无         |

- **模式**: 统一为 `Result<T, E>` 或明确区分可恢复/不可恢复路径

---

### 4.6 类型安全

#### 4.6.1 [CONFIRMED] `Response.data: unknown` — 26 处类型断言根因

- **文件**: `client/app/types/response.d.ts`
- `data?: unknown` 导致所有 bridge 模块需 `as unknown as Promise<...>` 断言
- **分布**: skills.ts(6), session.ts(5), memory.ts(2), logs.ts(2), curator.ts(3), cron.ts(6), channels.ts(4), system-prompt.ts(2), messages.ts(3), agent-socket.ts(1)
- **模式**: `Response<T>` 泛型 — `export type Response<T = unknown> = { code?: number; data?: T; msg?: string }`

---

### 4.7 重复代码

#### 4.7.1 [CONFIRMED] 重连逻辑重复

- `ws.ts::useWs()` 第 230 行: `setTimeout(() => connect(), 5000)` — 5s 固定
- `ws.ts::useSubagentWs()` 第 365 行: **完全相同的 5s 固定重连**（复制粘贴）
- `agent-socket.ts` 第 425 行: 指数退避 + 5s 回退（不同策略）

#### 4.7.2 [NEW] `resolveSid` 函数重复

- `subagent-sync.ts:70-76` 和 `use-todo-list.ts:85-90` — 几乎相同的 URL pathname 解析逻辑

#### 4.7.3 [NEW] `isClient()`/`safeT()` 重复

- `toast.ts:38-41,71-75` 和 `connection.ts:50-53,84-88` — 相同的 `clientFlagOverride` 变量 + `isClient()` 函数 + `safeT()` 函数

#### 4.7.4 [NEW] 状态→颜色/标签映射重复（4 处）

1. `useSubagentTasks.ts::badgeClass()` — status → CSS class
2. `useSubagentTasks.ts::statusLabel()` — status → i18n key
3. `SubagentFlowGraph.vue::statusColorLight/Dark()` — status → hex color
4. `SubagentFlowGraph.vue::statusKey()` — status → i18n key

---

## 5. Cross-cutting: Duplicate Code Inventory

### 5.1 [CONFIRMED] 三 SQLite 存储无共享基类

| 文件                                            | 重复模式                                                                                                                                           |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent/tools/todolist/registry/store_sqlite.py` | `_DB_DIR`, `_DB_PATH`, `_BUSY_TIMEOUT_MS`, `_CREATE_TABLE_SQL`, `_connect()`, `_ensure_tables_sync()`, `_init_lock`/`_init_loop`/`_sync_init_lock` |
| `agent/tools/taskflow/registry/store_sqlite.py` | 同上                                                                                                                                               |
| `agent/tools/subagent/registry/store_sqlite.py` | 同上                                                                                                                                               |

- **模式**: `BaseSQLiteRepository` 基类 + `SQLiteRepositoryFactory`

### 5.2 [CONFIRMED] `_convert_message_to_dict` 重复

| 文件                              | 行号    | 变体                         |
| --------------------------------- | ------- | ---------------------------- |
| `models/ITTT_model/core.py`       | 131-170 | text + image_url             |
| `models/VTTT_model/core.py`       | 126-174 | text + image_url + video_url |
| `models/LLMs/base_local_llama.py` | 60-70   | text-only（基类默认）        |

- **改善**: `base_local_llama.py` 已提取 Template Method 基类（`LocalLlamaChatBase`）
- **模式**: 完善 Template Method — ITTT/VTTT 的 `_convert_message_to_dict_impl` 提取到基类，通过 hook 扩展

### 5.3 [NEW] 5 处 `load_dotenv` + config dict 构建重复

| 文件                                | 行号      |
| ----------------------------------- | --------- |
| `models/ITTT_model/core.py`         | 43, 58-71 |
| `models/VTTT_model/core.py`         | 46, 61-74 |
| `models/LLMs/main_llm.py`           | 17, 18-88 |
| `models/LLMs/reasoner_llm.py`       | 14, 15-33 |
| `models/LLMs/auxiliary_llm/core.py` | 39        |

- **模式**: 提取 `ModelEnvBuilder` — 加载 env + 构建 config dict + 过滤 None

### 5.4 [NEW] 3 处 GGUF 路径解析重复

- `models/ITTT_model/core.py:46-125`
- `models/VTTT_model/core.py:49-120`
- `models/LLMs/auxiliary_llm/core.py:94-104`（较短变体）

- **模式**: 提取 `resolve_gguf_path(repo_id, filename, fallback_dir)` 共享函数

### 5.5 [CONFIRMED] middleware sync/async 双路径

- **唯一存在全量复制的中间件**: `summarization/core.py`（12+ 方法对）
- **已正确使用共享 impl 的中间件**: media_pipeline、tool_guardrails、iteration_budget、context_engine/core — 都通过 `_xxx_impl` 方法被 sync 和 async 版本共享
- **模式**: 将 summarization 的 sync/async 对重构为共享 `_impl` 模式

---

## 6. Cross-cutting: Global State Inventory

### 后端模块级可变全局（52+ 处 agent/，14+ 处 server/）

| 类别             | 代表变量                                                              | 文件                                 |
| ---------------- | --------------------------------------------------------------------- | ------------------------------------ |
| 子代理运行注册表 | `_runs`, `_lock`                                                      | `registry/memory.py:6-7`             |
| 任务引用         | `_task_refs`                                                          | `registry/task_refs.py:6`            |
| 生命周期管理     | `_terminal_locks`, `_cleanup_generations`, `_deferred_cleanup_timers` | `registry/lifecycle.py:40-42`        |
| HITL 状态        | `_HITL_PENDING`, `_HITL_LOCK`                                         | `registry/session_state.py:98-99`    |
| 交付幂等         | `_delivered_keys`, `_delivery_mirror`                                 | `announce/delivery.py:28-29`         |
| 队列持有         | `_QUEUE_HOLDER`, `_QUEUE_LOCK`                                        | `announce/steering_queue.py:337-338` |
| 事件总线         | `_BUS`                                                                | `events/core.py:105`                 |
| sweeper          | `_sweeper_task`                                                       | `registry/sweeper.py:324`            |
| followup         | `_followup_task`                                                      | `followup/core.py:12`                |
| bridge           | `_bridge_task`                                                        | `events/bridge.py:34`                |
| 压缩 TODO        | `_COMPRESSION_TODO_TASKS`                                             | `nudge.py:296`                       |
| 后台任务         | `_BACKGROUND_TASKS`                                                   | `context_engine/core.py:142`         |
| 冷却会话         | `_RESTORED_COOLDOWN_SESSIONS`                                         | `summarization/core.py:128`               |
| 提醒会话         | `_reminded_sessions`                                                  | `todowrite.py:34`                    |
| 任务流错误       | `_REGISTRY_ERROR`                                                     | `taskflow_wait_all.py:43`            |
| wrapper 工厂     | `_GRAPH_WRAPPER_FACTORIES`                                            | `wrapper/registry.py:45`             |
| 初始化锁         | `_init_lock`, `_init_loop`, `_sync_init_lock`                         | 3 个 SQLite 存储                     |
| 持久化锁         | `_persist_lock`                                                       | `state.py:9`                         |
| 武装会话         | `_armed_sessions`                                                     | `task_intent/core.py:192`                 |
| 停滞追踪         | 5 个 dict                                                             | `stagnation_tracker.py:27-31`        |
| 工具列表缓存     | `_tools`                                                              | `core.py:52`                         |
| 进度钩子         | 4 个 list                                                             | `hooks/progress.py:6-9`              |

### 前端模块级可变全局（~45 处，18 个文件）

见 [4.3 节](#43-模块级可变全局状态)。

---

## 7. Cross-cutting Patterns Summary

| Pattern                     | 适用发现                                               | 核心收益                         |
| --------------------------- | ------------------------------------------------------ | -------------------------------- |
| **Strategy + Registry**     | #1.1.1, #1.1.2, #1.1.3, #1.1.5, #4.1.1, #4.1.5         | 消除 if-elif 链，开闭原则        |
| **Chain of Responsibility** | #1.1.4                                                 | 拆解嵌套分发为独立 handler 链    |
| **Builder**                 | #1.2.2 (built_agent)                                   | 逐步组装复杂对象                 |
| **Facade + Service Layer**  | #1.2.1, #1.2.3, #3.1.1                                 | 中间件委托给服务，只协调         |
| **Repository**              | #1.3.2, #3.1.2, #5.1                                   | 封装 SQL，分离数据访问与业务逻辑 |
| **Dependency Injection**    | #1.4.4, #1.4.5, #2.1.3, #2.1.5, #3.1.5, #3.2.1, #3.2.2 | 消除 lazy import 缝隙和跨层依赖  |
| **Command Registry**        | #4.1.1, #4.1.2, #4.1.6                                 | 消除 switch/case，开闭原则       |
| **Lookup Table**            | #4.1.3, #4.1.4                                         | 消除硬编码映射重复               |
| **Template Method**         | #5.2, #1.2.4                                           | 提取共享骨架，hook 扩展          |
| **Base Class / Mixin**      | #5.1, #5.3, #5.4, #3.3.4                               | 消除重复 boilerplate             |
| **Protocol / Interface**    | #1.4.1, #1.4.2, #1.4.3, #2.1.4, #3.3.4                 | 类型安全的多态替代               |
| **State Pattern**           | #2.1.2                                                 | 封装隐式状态机为显式状态类       |
| **Explicit Initialization** | #2.1.5, #3.1.5                                         | 移除 import 时副作用             |
| **Adapter**                 | #1.3.3, #1.3.4, #4.4.1, #4.4.2                         | 封装第三方 API/协议差异          |
| **Generic Type**            | #4.6.1                                                 | 类型安全替代类型断言             |

---

## 8. Recommended Refactoring Roadmap

### Phase 0: 修复数据丢失 Bug（P0）

| Step | Target                             | Pattern    | Est. Effort |
| ---- | ---------------------------------- | ---------- | ----------- |
| 0.1  | `channels/manager.py` 竞争消费 bug | 统一消费者 | 0.5 天      |

### Phase 1: 消除最大风险 + 架构方向（P1）

| Step | Target                                                 | Pattern                 | Est. Effort |
| ---- | ------------------------------------------------------ | ----------------------- | ----------- |
| 1.1  | `context_engine/store/core.py:14` eager DB → lazy      | Lazy init               | 0.5 天      |
| 1.2  | 中间件 `session_id` 提取 + `state_register_mem` Facade | Mixin + Enum key        | 1 天        |
| 1.3  | `runtime/session/state_register.py` Protocol + 连接池 + lazy   | Interface Seg. + 连接池 | 1-2 天      |
| 1.4  | `summarization/core.py` 拆分（2250 行 → 6 模块）            | 分层 + 共享 impl        | 3-5 天      |

### Phase 2: 拆解 God 模块 + DRY 清理（P2）

| Step | Target                                                     | Pattern               | Est. Effort |
| ---- | ---------------------------------------------------------- | --------------------- | ----------- |
| 2.1  | `BaseSQLiteRepository` 基类提取（3 个 SQLite 存储）        | Base Class            | 1 天        |
| 2.2  | `FileStore` 基类提取 + 工具函数提取                        | Template Method       | 1 天        |
| 2.3  | Repository Pattern（SQL 封装：5 个文件）                   | Repository            | 2-3 天      |
| 2.4  | Command Executor 抽象                                      | Adapter               | 1 天        |
| 2.5  | API Client Adapter（embed/reranker/media HTTP）            | Adapter               | 1 天        |
| 2.6  | 工厂函数统一（ITTT/VTTT/reranker/extract）                 | Factory               | 1 天        |
| 2.7  | `ModelEnvBuilder` 提取（5 处 load_dotenv + config 重复）   | DRY                   | 0.5 天      |
| 2.8  | `ContentDecoder` 提取（4 处 JSON decode 重复）             | DRY                   | 0.5 天      |
| 2.9  | `_convert_message_to_dict` 完善到基类                      | Template Method       | 0.5 天      |
| 2.10 | `RerankerProtocol` ABC 提取                                | Interface Seg.        | 0.5 天      |
| 2.11 | 前端 `Response<T>` 泛型                                    | Generic Type          | 0.5 天      |
| 2.12 | 前端 `WebSocketConnection` 基类                            | Base Class + Strategy | 1-2 天      |
| 2.13 | 前端 Command Registry + Dialog Manager                     | Registry              | 1 天        |
| 2.14 | 前端 `SUBAGENT_STATUS_META` 查找表                         | Lookup Table          | 0.5 天      |
| 2.15 | 前端 `resolveSid`/`isClient`/`safeT` 提取                  | DRY                   | 0.5 天      |
| 2.16 | `config/schema.py` 反转 `models/` 依赖                     | 反转依赖              | 1 天        |
| 2.17 | `models/LLMs/main_llm.py` 提取 `FallbackCandidate` 到 pub/ | 反转依赖              | 0.5 天      |

### Phase 3: 架构清理 + 前端状态管理（P3）

| Step | Target                            | Pattern        | Est. Effort |
| ---- | --------------------------------- | -------------- | ----------- |
| 3.1  | 导入时副作用 → `setup()` 函数     | Explicit init  | 1 天        |
| 3.2  | `turn_runner` 依赖注入            | DI             | 2 天        |
| 3.3  | `built_agent()` Builder           | Builder        | 1 天        |
| 3.4  | `state_register` Protocol 完善    | Interface Seg. | 1 天        |
| 3.5  | `curator/orchestrator.py` 拆分    | 分层架构       | 2 天        |
| 3.6  | `ChatBox.vue` 拆分为多 composable | Separation     | 2 天        |
| 3.7  | `agent-socket.ts` 拆分            | Separation     | 2 天        |
| 3.8  | 前端 subagent 状态迁移到 Pinia    | State Pattern  | 2 天        |
| 3.9  | 前端错误处理统一                  | Result/规范    | 1 天        |
| 3.10 | 前端 upload.ts/health.ts 统一 API | Adapter        | 0.5 天      |
| 3.11 | if-else 链 → Strategy（6 处）     | Strategy       | 2 天        |

---

> **注**: 每步重构应在对应测试通过后合并。建议按 Phase 0 → 1 → 2 → 3 顺序推进，前置 phase 的基础（如 SessionState Facade、BaseSQLiteRepository、Response\<T\> 泛型）是后续步骤的前提。
