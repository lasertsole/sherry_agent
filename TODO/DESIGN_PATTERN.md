# Design Pattern Refactoring

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式。
>
> 审查范围：`agent/`、`models/`、`server/`、`runtime/`、`channels/`、`bus/`、`context_engine/`、`plugins/`、`config/`、`client/app/`
>
> 生成日期：2026-09-04；最近更新：2026-09-14
>
> 最近核对：2026-09-21（逐条对照源码）：已关闭条目保留编号并以 Status 列标记（不重排编号），行号/计数按当前源码修正。
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

| P      | #   | Location                                                    | Problem                                                             | Pattern                           | Status |
| ------ | --- | ----------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------- | ------ |
| **P0** | 1   | `channels/manager.py` _dispatch_outbound vs _consume_loop   | 已解决（统一消费者）：`_dispatch_outbound` 独占出站队列——解析目标 channel 后先 fail-open 调用 `_outbound_consumer(msg, channel)`（仅目标频道），再 `channel.send(msg)`；删除竞争的第二消费者 `_outbound_consume_loop` / `_consume_loop`，`start_service()` 仅调度 dispatcher + 入站消费者（`channels/manager.py:88,174,243`）。原缺陷：被 `_consume_loop("outbound")` 抢到的消息**静默不投递**（`_process_outbound` 只登记 session、不调用 `channel.send()`，`server/trigger/channels/core.py:317-327`） | 统一消费者                        | Done |
| **P1** | 2   | `context_engine/store/core.py:16` `_db = get_db()` | 导入即触发 SQLite 连接 + migration                                  | Lazy init                         | Open |
| **P1** | 3   | `state_register_mem` 全局耦合                               | 所有中间件直接依赖全局单例，裸字符串 key                            | SessionState Facade + Enum key    | Open |
| **P1** | 4   | `runtime/session/state_register.py`                         | 每次 SQLite 操作新开连接；无 Protocol                               | 连接池/Repository + Protocol      | Open |
| **P1** | 5   | `agent/middlewares/summarization/core.py` 2745 → 573 行 | 已解决：核心拆为 `compression.py`/`overflow.py`/`summary_generation.py`/`thrash.py`（+ `state_aliases.py`），`core.py` 仅留中间件类 + hook 编排 + 进程级接缝（573 行，纯 402 ≤ 800）；sync/async 收敛 4 对（`_execute_compact`/`_dispatch_overflow_route`/`_post_response_check`/`_forced_recovery_request` 共享 `_impl`），未收敛 3 对见 §5.5 | 拆分核心 + 共享 impl | Done |
| **P2** | 6   | ITTT/VTTT/reranker/extract 模块级单例                       | 与 main_llm 工厂模式不一致                                          | 统一工厂函数                      | Open |
| **P2** | 7   | `RepetitionGuardWrapper` + `ContextLimitGuard` 导入私有常量 | 跨模块私有依赖（8 个私有符号）                                      | 依赖倒置 + Protocol               | Open |
| **P2** | 8   | 原始 SQL 泄漏（5 个文件）                                   | checkpointer/store/embeddings/events                                | Repository Pattern                | Open |
| **P2** | 9   | 原始 HTTP requests.post                                     | embed_model/reranker_model                                          | API Client Adapter                | Open |
| **P2** | 10  | 原始 subprocess/Popen                                       | terminal/python_repl/skill_manage                                   | Command Executor 抽象             | Open |
| **P2** | 11  | `handleOperate` switch（11 路）                             | 前端 ad-hoc 对话框管理                                              | Command Registry + Dialog Manager | Open |
| **P2** | 12  | `badgeClass`/`statusLabel`/`statusColor`/`statusKey`        | 状态映射重复 4 处                                                   | Lookup Table                      | Open |
| **P2** | 13  | `config/schema.py` 导入 `models/`                           | 已解决：改为回调注入 `set_provider_registry`（`config/schema.py:24`），models 侧装配时推送元数据；`lint-imports` "config must not import models" KEPT | 反转依赖（已落地） | Done |
| **P2** | 14  | `models/LLMs/main_llm.py` 导入 `agent/`                     | 已解决：`FallbackCandidate` 已迁 `pub/types/llm.py:8`，`models/LLMs/main_llm.py:138` 自 pub 导入 | 提取到 pub/（已落地） | Done |
| **P2** | 15  | 3 个 SQLite 存储无共享基类                                  | `_connect`/`_ensure_tables_sync` 重复 3 份（todolist 381 / taskflow 703 / subagent 300 行；taskflow 已加 `session_id` 会话隔离列） | BaseSQLiteRepository              | Open |
| **P2** | 16  | `_convert_message_to_dict` 重复                             | ITTT↔VTTT 仍各自复制（ITTT `core.py:134-173`、VTTT `core.py:129-177`）；文本默认已上提 `base_local_llama.py:60` | Template Method 完善              | Open |
| **P2** | 17  | 4 处 JSON content decode 重复                               | 同一 `\x00json:` 前缀解析逻辑 4 份                                  | 提取为 `ContentDecoder`           | Open |
| **P2** | 18  | 5 处模型 config 构建重复 | config 已统一为 `config/features/` TypedDict + `LLM_CLIENT_DEFAULTS`；剩余重复收窄为「读 env + 建 dict + 过滤 None」 | 提取 `ModelEnvBuilder`            | Open |
| **P2** | 19  | 前端 `Response.data: unknown`                               | 39 处 `as unknown as` 类型断言根因（生产代码，不含测试） | `Response<T>` 泛型                | Open |
| **P2** | 20  | 前端 3 个 WebSocket 管理无统一抽象                          | 3 种重连策略各自实现                                                | `WebSocketConnection` 基类        | Open |
| **P2** | 21  | 前端 `bridge/upload.ts`/`bridge/health.ts` raw fetch | 绕过 requestApi，无 token/retry                                     | 统一 API 客户端                   | Open |
| **P2** | 22  | 前端错误处理 4 种策略不一致                                 | catch→null / catch→缓存 / throw / catch→默认 —— **需执行前单独复核**（4/5/6 种口径不一致） | Result<T,E> 或统一规范            | Open |
| **P3** | 23  | `build_reasoning_kwargs` provider 分发                      | if-elif 链                                                          | Strategy + Registry               | Open |
| **P3** | 24  | `ToolGuardrails._evaluate` + 消息构造                       | 嵌套 if-else action 决策                                            | Chain of Responsibility           | Open |
| **P3** | 25  | `memory.py` action 分发（5 路）                             | 已过时：facts action 链已删；`agent/tools/memory.py:663-684` 现为 `add/replace/remove` 3 路 | Strategy + Registry（不再单列） | Obsolete |
| **P3** | 26  | `skill_manage.py` action 分发（5+ 路）                      | 嵌套 if-elif 链                                                     | Strategy + Registry               | Open |
| **P3** | 27  | `built_agent()`                                             | 混合事件循环/checkpointer/LLM/中间件（`agent/core.py:122-252`） | Builder Pattern                   | Open |
| **P3** | 28  | `reranker_model/core.py` 701 行                             | FFI+numpy+HTTP+排序混合                                             | 分层架构                          | Open |
| **P3** | 29  | `Summarization` 依赖 agent.tools                            | 中间件层依赖工具层（5 处）                                          | 依赖倒置                          | Open |
| **P3** | 30  | nudge 创建子代理（`agent/middlewares/summarization/nudges.py:549`，891 行） | 中间件直接依赖 agent 构建；`ContextEngineHook` 已不存在（被 `@dynamic_prompt` 取代），但该耦合仍在 | Factory + DI                      | Open |
| **P3** | 31  | `IterationBudget` 依赖私有函数                              | `_is_internal_completion` 跨模块私有                                | Protocol                          | Open |
| **P3** | 32  | 导入时副作用（5 处）                                        | channels/core、subagent/core、embed_model、extract_model、ITTT/VTTT | Explicit initialization           | Open |
| **P3** | 33  | `ws_event_processor_dict` 全局 dict                         | 事件分发无类型安全                                                  | Registry Pattern                  | Open |
| **P3** | 34  | `stream_dispatch.py` 模块级可变状态                         | `_pending_args`/`_pending_raw` 为 session-scoped 模块级 dict        | State Pattern                     | Open |
| **P3** | 35  | `turn_runner.py` 延迟导入                                   | 7 个 lazy import 绕循环依赖                                         | Dependency Injection              | Open |
| **P3** | 36  | `state_register.py` 无公共接口                              | Mem/DB 无 Protocol                                                  | Interface Segregation             | Open |
| **P3** | 37  | `ContextEngineHook._after_agent_impl`                       | 已过时：`ContextEngineHook` 类全仓不存在（被 `@dynamic_prompt` 取代） | Dataclass + NudgeCounter          | Obsolete |
| **P3** | 38  | `aclean_old_checkpoints`                                    | 70 行原始 SQL                                                       | Repository Pattern                | Open |
| **P3** | 39  | `postAgentStream`                                           | 适配器 + 流编排混合 —— **需执行前单独复核** | Adapter + Error Classifier        | Open |
| **P3** | 40  | `events/store.py` `__import__()` 反模式                     | 为绕循环依赖用动态导入                                              | 延迟导入或 DI                     | Open |
| **P3** | 41  | `curator/orchestrator.py` 775 行 | sync `llm.invoke()` 在异步路径（`:98`/`:473`） | async/await 或 to_thread          | Open |
| **P3** | 42  | 前端 `ChatBox.vue` 888 行 | 渲染+复制+滚动+载体+媒体解析                                        | 拆分为多个 composable             | Open |
| **P3** | 43  | 前端 `agent-socket.ts` 554 行 | WS+消息+重连+上传+队列                                              | 拆分为 ConnectionManager/Router   | Open |
| **P3** | 44  | 前端 `resolveSid`/`safeT` 重复 | 缩窄：`resolveSid`（`subagent-sync.ts:68` / `stores/todo.ts:81`）与 `safeT`（`toast.ts:52` / `stores/connection.ts:65`）仍重复；`isClient`/`clientFlagOverride` 已统一到 `utils/client.ts:29`/`:16` | 提取共享工具 | Open |

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

#### 1.2.1 [RESOLVED] `summarization/core.py` — 2745 行上帝类（从 1996 增长）

- **文件**: `agent/middlewares/summarization/core.py`
- **已完成（2026-09-21）**: 除既有的 6 个兄弟模块（`compaction_lock.py`/`media_offload.py`/`memory_flush.py`/`nudges.py`/`plan_context.py`/`summary_doc.py`）外，核心按职责拆为 4 个新模块 + 1 个共享键别名模块：
  - `compression.py`（421 行）: `_calculate_preserve_budget`/`_determine_cutoff`/`_adjust_for_orphan_pairs`/`_run_non_llm_strategies`/`_aggressive_truncate`/`_capture_recovery_context`/`_inject_recovery_context`/`_truncate_*`/`_preemptive_truncate`/`_apply_compression*`/`_aapply_compression*`
  - `overflow.py`（841 行）: T1–T5 触发环——`_check_trigger`/`_preemptive_check`/`_decide_overflow_route`/`_run_budget_truncation`/`_fast_tail_clip`/`_execute_compact`/`_dispatch_overflow_route`/`_post_response_check`/`_forced_recovery_request`/`_execute_with_recovery`/`_t1_preflight` + `extract_reported_input_tokens`
  - `summary_generation.py`（857 行）: prompt 模板/序列化/文件操作棘轮/静态回退/`_build_*_prompt`/`_structured_runnable`/`_finalize_summary_doc`/`_create_summary`/`_build_new_messages`
  - `thrash.py`（169 行）: 反抖动计数器/冷却 tick/`_monitor_degradation`/`_reset_turn_state`/`_check_last_turn_ratio`
  - `state_aliases.py`（26 行）: `StateKey` 短别名单一来源
- **`core.py` 573 行（纯 402，≤ 800 目标）**: 仅保留 `Summarization` 类（`__init__` + 生命周期 hook）、持久冷却镜像（`state_register_db` 接缝）、进程级接缝（`_rebuild_system_prompt`/`_persist_system_prompt`/`_fire_compression_nudges`/`_taskflow_context`/`_plan_context`/`_resolve_active_plan`），并 re-export 全部被移出的模块级名字（既有测试可零改）。
- **>250 纯 LOC 模块**已按仓库惯例加 `# allow: SIZE_OK` + 理由（`compression`/`overflow`/`summary_generation`）。
- **sync/async**: 4 对收敛为共享 `_impl`（见 §5.5）；3 对保持双份并注明理由。
- **门禁**: 受影响测试 `318 passed`（与拆分前基线逐一致）；`lint-imports` 7 kept；`basedpyright agent/` 0 errors；`ruff check/format` 干净；`check_docs_parity.py` PASS；新增 `tests/agent/middlewares/test_module_boundaries.py`（独立 import + 无环 + 组合根 re-export）。
- **模式**: 已落地 `CompressionMixin`/`OverflowMixin`/`SummaryGenerationMixin`/`ThrashMixin` + 组合根 `Summarization`

#### 1.2.2 [CONFIRMED] `built_agent()` — 组合根

- **文件**: `agent/core.py:122-252`
- **问题**: 单函数连接 14 个 middleware、3 个 LLM 实例、checkpointer、2 个 graph wrapper
- **改善**: `init()` 提取了导入时副作用（已修复），但 `built_agent()` 本身仍是巨大接线函数
- **模式**: Builder Pattern

#### 1.2.3 [CONFIRMED] `nudges.py` — 891 行多职责模块

- **文件**: `agent/middlewares/summarization/nudges.py`（原 `context_engine/nudge.py`；`ContextEngineHook` 已不存在）
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
- `context_engine/store/core.py` (INSERT/SELECT/UPDATE/DELETE，872 行)
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

- **文件**: `agent/middlewares/summarization/core.py:376-381`（`taskflow` store/工具函数在函数体内 lazy 导入）、`:823-834`（`memory_store` 已由构造函数注入 `self._memory_store`）
- **问题**: 中间件层仍依赖工具层具体实现——`taskflow` 靠函数内导入、`memory_store` 靠注入但无 Protocol 约束，分层未对话化
- **同类问题**: `context_engine/core.py` 导入 `todolist`；`nudges.py` 导入 `subagent`/`todolist`/`memory`；`todo_continuation/core.py` 导入 `todolist`；`subagent_completion_drain/core.py` 导入 `announce`
- **模式**: 依赖倒置 — 定义 `MemorySnapshotProvider` 接口，由 `core.py` 注入

#### 1.4.5 [CONFIRMED] nudge 直接创建子代理

- **文件**: `agent/middlewares/summarization/nudges.py:549`（`_create_nudge_agent`）
- **问题**: 中间件层创建完整 LangGraph agent，导入 `build_main_llm`、`get_agent_tools`、`create_agent`（`ContextEngineHook` 已不存在，被 `@dynamic_prompt` 取代；耦合本身仍在）
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

#### 2.1.2 [CONFIRMED] `stream_dispatch.py` 模块级可变状态

- **文件**: `server/service/stream_dispatch.py:52,55`
- **问题**: `_pending_args`/`_pending_raw` 为 **session-scoped** module-level dict（按 session_id 键隔离，已不跨会话串），但仍是模块级可变状态，生命周期与测试隔离依赖隐式约定
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

#### 3.1.1 [CONFIRMED] `context_engine/core.py` 模块级混合（现 507 行）

- **问题**: 数据库访问(`_db`, `_lock`)、FTS5 语法、内容编解码、业务逻辑、搜索结果组装仍在同一模块
- **改善**: Strategy Pattern 已落地——`_SearchStrategy` ABC 在 `:177`，`_TrigramStrategy:185` / `_LikeStrategy:252` / `_FtsStrategy:306` + `_SearchStrategyFactory:351`，搜索分发已多态
- **剩余混合点**: FTS5 清洗 `_sanitize_fts5_query():92-154`、decoder `_decode_content():157`
- **模式**: 分层架构 + Repository Pattern（部分已改进）

#### 3.1.2 [CONFIRMED] `context_engine/store/core.py` — 872 行 God 模块（从 648 增长）

- **问题**: 混合行构建（3 个 builder）、turn 管理、幂等性、session leaf 树、压缩检查点、session 列举
- **模式**: 拆分为 `message_repo.py` / `checkpoint_repo.py` / `session_repo.py`

#### 3.1.3 [NEW] `context_engine/curator/orchestrator.py` — 775 行

- **问题**: 混合 LLM 审查生成、umbrella 解析、合并执行、源文件迁移、系统 prompt 刷新、状态管理
- **附加**: `:98` / `:473` sync `llm.invoke()` 在异步路径中调用
- **注（2026-09-21 核对）**: curator 已改为归档语义（`context_engine/curator/usage.py:256 archive_skill`、`:141 delete_skill`），但不改变"orchestrator 混合职责"的判定
- **模式**: 拆分为 `review.py` / `consolidation.py` / `refresh.py` + async/await

#### 3.1.4 [CONFIRMED] FTS5 查询语法泄漏

- **文件**: `context_engine/core.py:92-154` — `_sanitize_fts5_query()` 62 行正则处理
- **模式**: `FTSQuerySanitizer` 抽象

#### 3.1.5 [CONFIRMED] 模块级全局状态

- `context_engine/core.py` (`_db`, `_lock`)
- `context_engine/store/core.py` (`_db`, `_turn_assign_lock`, `_turn_stamp_lock:28`, `_last_turn_ms:29`)；第 16 行 `_db = get_db()` eager init（与 `core.py` 的 lazy `_shared_db()` 不一致）
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
| `context_engine/core.py`             | `_decode_content()`       | 157     |
| `context_engine/store/core.py`       | `_decode_json_columns()`  | 618     |
| `context_engine/store/core.py`       | `_decode_title_content()` | 788     |
| `context_engine/embeddings/store.py` | `_decode()`               | 91      |

- **模式**: 提取为 `ContentDecoder` 共享函数

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
- **Status**: **Done** (2026-09-21) — 出站统一由 `_dispatch_outbound`（`channels/manager.py:243`）独占：解析目标 `channel` 后先 fail-open 调用 `_outbound_consumer(msg, channel)`（仅目标频道，异常记日志不阻断），再 `channel.send(msg)`。删除 `_outbound_consume_loop` 与 `_consume_loop`；`start_service()`（`:174`）只调度 dispatcher + 入站消费者。入站同源缺陷（广播到所有配置渠道）一并修正为按 `msg.channel` 路由（`_inbound_consume_loop`，`:88`）。回归测试：`tests/channels/test_consume_loop.py`

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

- **文件**: `client/app/pages/home/index.vue:396-434`（11 路 switch 自 `:398`）
- **问题**: 11 个 case（skills, knowledgeGraph, stats, systemConfig, persona, memory, heartbeat, cron, logs, notification, extend），每个仅设置一个 `showXxxDialog.value = true`
- **附加问题**: 10 个独立的 `showXxxDialog = ref(false)` ad-hoc 管理对话框（refs 集中在 `:335-362`），无集中管理器
- **模式**: Command Registry — `const dialogRegistry: Record<string, Ref<boolean>>` + `useDialogManager()` composable

#### 4.1.2 [NEW] `handleOperate` in `[sid].vue` — 4 路 switch

- **文件**: `client/app/pages/home/index/[sid].vue:689-708`
- **问题**: 4-case：createSession, uploadImage, uploadAudio, uploadVideo

#### 4.1.3 [CONFIRMED] `badgeClass`/`statusLabel` — 硬编码 if-else 映射

- **文件**: `client/app/composables/useSubagentTasks.ts:50,66`
- **问题**: `badgeClass` 6 个条件分支，`statusLabel` 9 个条件分支
- **模式**: Lookup Table — `const STATUS_STYLE: Record<string, {badge, labelKey}>`

#### 4.1.4 [NEW] `statusColorLight`/`statusColorDark`/`statusKey` — 状态映射重复第 2-3 处

- **文件**: `client/app/pages/home/components/SubagentFlowGraph.vue:103,123,151`
- **问题**: 两个 switch + 一个 if-else 链，将同一 SubagentRun 状态枚举映射到颜色/i18n key。**重复了** `useSubagentTasks.ts` 中 `badgeClass`/`statusLabel` 的逻辑
- **模式**: 提取为共享的 `SUBAGENT_STATUS_META` 查找表

#### 4.1.5 [NEW] CronDialog.vue — 3 个 switch

- **文件**: `client/app/pages/home/components/CronDialog.vue:320,337,484`
- **问题**: `everyToMs()`（4-case）、`buildSchedule()`（3-case）、`describeSchedule()`（3-case）围绕同一调度类型枚举
- **模式**: 合并为一个 Strategy 模式

#### 4.1.6 [NEW] TodoItem.vue — statusIcon switch

- **文件**: `client/app/components/chat/TodoItem.vue:41-52`
- **问题**: 4-case switch 将 todo 状态映射到图标
- **模式**: 小型查找表

---

### 4.2 混合关注点

#### 4.2.1 [CONFIRMED] `agent-socket.ts` — 554 行混合

- **文件**: `client/app/composables/bridge/agent-socket.ts`
- **职责**: WS 连接生命周期、消息解析/路由（7 种事件）、指数退避重连 + 回退定时器、出站消息队列、媒体上传调度、Promise 追踪、会话 turn 追踪、mitt 事件广播
- **模式**: 拆分为 `ConnectionManager`、`MessageRouter`、`SendQueue`、`UploadPipeline`

#### 4.2.2 [CONFIRMED] `ChatBox.vue` — 888 行混合

- **文件**: `client/app/pages/home/components/ChatBox.vue`
- **职责**: 消息渲染（4 种布局）、复制到剪贴板、滚动管理、载体消息分流、图片/音频/视频 src 解析（3 处重复）、图片加载失败处理、工具卡片展开/折叠、思考过程展开、连续消息判断、turn 分组
- **模式**: 提取 `useMessageMedia`、`useCopyMessage`、`useScrollManagement`、`useCardExpansion`

#### 4.2.3 [CONFIRMED] `ws.ts` — 双 WS 管理在一个文件

- **文件**: `client/app/composables/ws.ts` (437 行)
- **问题**: 一个文件管理两个独立 WebSocket 通道（`/sessions/ws` 和 `/subagents/ws`），各自包含连接管理、消息解析、重连、心跳
- **模式**: 拆分为 `session-ws.ts` 和 `subagent-ws.ts`

#### 4.2.4 [NEW] `connection.ts` — WS + 浏览器事件 + toast + i18n 混合

- **文件**: `client/app/stores/connection.ts` (222 行)（原 `composables/connection.ts` 已迁为 Pinia store）
- **问题**: 混合 WS 生命周期启动、浏览器 online/offline 事件、toast 通知、i18n 翻译、edge 去重

---

### 4.3 模块级可变全局状态

**前端共享状态已迁移至 Pinia**（`stores/ui`、`stores/subagent`、`stores/connection`、`stores/todo`、`stores/chat-background`）。

仍保留在模块级的**基础设施单例/缓存（约 10 个文件）**（非响应式状态，按迁移边界不迁）：

| 文件           | 变量（行号）                                                                 |
| -------------- | ---------------------------------------------------------------------------- |
| `ws.ts`        | wsInstance, everConnected, heartbeatTimer, pongTimeoutTimer, pendingPong, missedPongs, subagentWsInstance, subagentReady; `reconnectTimer:65`, `subagentReconnectTimer:319` |
| `agent-socket.ts` | sockets Map                                                              |
| `subagent-sync.ts` | `lastLoadedSessionId:15`, `subscribed:18`, `subagentSessionsLoaded:21`   |
| `toast.ts`     | `toastApi:27`                                                                |
| `model-config.ts` | `cached:26`                                                               |
| `global-error-handler.ts` | `activeCleanup:46`                                                 |
| `utils/client.ts` | `clientFlagOverride:16`                                                  |
| `mitt.ts`      | emitter (const 但本质可变单例)                                               |
| `clientLog.ts` | activeStore, logBuffer, clientLogSubscribers, captureInstalled, origConsole, logDb |
| `db.ts`        | db (Dexie singleton)                                                         |

组件内 `let`（局部 UI 状态）不迁移。

---

### 4.4 缺失抽象

#### 4.4.1 [CONFIRMED] 原始 fetch() 绕过 API 客户端

- `bridge/upload.ts:69` — `fetch(...)` 完全绕过 requestApi.ts，无重试、无 token、无 toast
- `bridge/health.ts:18` — `fetch(...)` 同上
- **模式**: 统一通过 requestApi.ts

#### 4.4.2 [CONFIRMED] 3 个 WebSocket 管理无统一抽象

| 实现                     | 重连策略            | 心跳                     |
| ------------------------ | ------------------- | ------------------------ |
| `ws.ts::useWs()`         | 5s 固定             | 10s ping/5s pong timeout |
| `ws.ts::useSubagentWs()` | 5s 固定（复制粘贴） | 无                       |
| `agent-socket.ts`        | 指数退避 + 5s 回退  | 无                       |

- **模式**: `WebSocketConnection` 基类 + 可插拔重连策略

#### 4.4.3 [NEW] JSON.parse 无运行时 schema 验证

- `message-items.ts:64` — `JSON.parse(calls)` 无 schema
- `ChannelSettingsDialog.vue:328` — `JSON.parse(trimmed)` 无 schema
- `messages.ts:99` — `res as unknown as CachedMessage[]`（缓存回退路径）
- 已加固（不再是裸断言）：`ws-message.ts:11 isWsObjectFrame` + `:33` 对象守卫；`requestApi.ts:59 isApiPayload`
- **模式**: zod 或类似库进行运行时 schema 验证

---

### 4.5 不一致模式

#### 4.5.1 [CONFIRMED] API 调用方式不一致

| 文件          | 方法         | 重试 | Token | Toast |
| ------------- | ------------ | ---- | ----- | ----- |
| requestApi.ts | `$fetch`     | 3 次 | 自动  | 自动  |
| bridge/upload.ts | 原始 `fetch` | 无 | 无  | 无    |
| bridge/health.ts | 原始 `fetch` | 无 | 无  | 无    |

#### 4.5.2 [CONFIRMED] 状态管理位置不一致

| 存储         | 位置                                                              | 用途                                           |
| ------------ | ----------------------------------------------------------------- | ---------------------------------------------- |
| Pinia        | `stores/`（5 个：ui、subagent、connection、todo、chat-background） | UI 偏好、子代理运行时、连接状态、todo、聊天背景 |
| Dexie        | `db.ts`（8 表）, `clientLog.ts`（1 表）                           | 消息/角色/会话/草稿/背景/子代理/标题/预设/日志 |
| 模块级单例   | `ws.ts`、`agent-socket.ts`、`mitt.ts`、`clientLog.ts`（基础设施） | 连接/事件/日志（非响应式状态，不迁移）         |
| localStorage | `requestApi.ts`                                                   | 仅 token                                       |
| 组件 ref     | 各组件                                                            | 局部 UI                                        |

#### 4.5.3 [CONFIRMED] 错误处理策略不一致（**需执行前单独复核**：口径 4/5/6 种不一）

| 策略                    | 文件                 | 返回值               | 通知       |
| ----------------------- | -------------------- | -------------------- | ---------- |
| catch → null + toast    | requestApi.ts        | `null`               | 自动 toast |
| catch → 缓存回退        | messages.ts          | 本地缓存             | 无         |
| throw                   | bridge/upload.ts     | 抛出 Error           | 无         |
| catch → 默认对象        | bridge/health.ts     | `{ healthy: false }` | 无         |
| catch → 日志 + 保持现状 | subagent-sync.ts     | 旧数据               | 无         |
| catch → 静默忽略        | ws.ts, ws-message.ts | null                 | 无         |

- **模式**: 统一为 `Result<T, E>` 或明确区分可恢复/不可恢复路径

---

### 4.6 类型安全

#### 4.6.1 [CONFIRMED] `Response.data: unknown` — 39 处类型断言根因（生产代码，不含测试）

- **文件**: `client/app/types/response.d.ts`
- `data?: unknown` 导致所有 bridge 模块需 `as unknown as Promise<...>` 断言
- **分布**: bridge/skills.ts(6)、bridge/cron.ts(6)、bridge/session.ts(5)、bridge/channels.ts(4)、bridge/curator.ts(3)、messages.ts(3) 等 39 处；`directives/debounce.ts:73-91` 的 4 处 DOM 断言不计入（全量生产断言 43 处，测试不计）
- **模式**: `Response<T>` 泛型 — `export type Response<T = unknown> = { code?: number; data?: T; msg?: string }`

---

### 4.7 重复代码

#### 4.7.1 [CONFIRMED] 重连逻辑重复

- `ws.ts::useWs()` `:252` — 5s 固定重连（`reconnectTimer`）
- `ws.ts::useSubagentWs()` `:410` — **完全相同的 5s 固定重连**（复制粘贴，`subagentReconnectTimer`）
- `agent-socket.ts:405-413` — 指数退避（`this.attempt += 1`、`scheduleReconnect(wsReconnectDelayMs(...))`；`scheduleReconnect:435`；5s 回退 `WS_FALLBACK_RECONNECT_MS`）

#### 4.7.2 [NEW] `resolveSid` 函数重复

- `subagent-sync.ts:68` 和 `stores/todo.ts:81` — 几乎相同的 URL pathname 解析逻辑（旧 `use-todo-list.ts` 已删除）

#### 4.7.3 [NEW] `isClient()`/`safeT()` 重复

- `isClient()`/`clientFlagOverride` 已统一到 `utils/client.ts:29`/`:16`（不再重复）
- 仍重复：`safeT()` — `toast.ts:52` 与 `stores/connection.ts:65`

#### 4.7.4 [NEW] 状态→颜色/标签映射重复（4 处）

1. `useSubagentTasks.ts:50::badgeClass()` — status → CSS class
2. `useSubagentTasks.ts:66::statusLabel()` — status → i18n key
3. `SubagentFlowGraph.vue:103,123::statusColorLight/Dark()` — status → hex color
4. `SubagentFlowGraph.vue:151::statusKey()` — status → i18n key

---

## 5. Cross-cutting: Duplicate Code Inventory

### 5.1 [CONFIRMED] 三 SQLite 存储无共享基类

| 文件                                            | 重复模式（规模/行号，2026-09-21 核对）                                                                                                             |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent/tools/todolist/registry/store_sqlite.py` | 381 行；`_connect():167`、`_ensure_tables_sync():271`、init 锁 `_init_lock:95`/`_sync_init_lock:102`、`_DB_DIR`/`_DB_PATH`/`_BUSY_TIMEOUT_MS`/`_CREATE_TABLE_SQL` |
| `agent/tools/taskflow/registry/store_sqlite.py` | 703 行；`_connect():290`、`_ensure_tables_sync():376`、init 锁 `:188-195`；已加 `session_id` 会话隔离列及迁移（`:137-169`）——基类化须参数化 DDL |
| `agent/tools/subagent/registry/store_sqlite.py` | 300 行；`_connect():85`、`_ensure_tables_sync():166`、init 锁 `:64-71`                                                                              |

- **模式**: `BaseSQLiteRepository` 基类 + `SQLiteRepositoryFactory`

### 5.2 [CONFIRMED] `_convert_message_to_dict` 重复（ITTT↔VTTT 仍各自复制）

| 文件                              | 行号    | 变体                         |
| --------------------------------- | ------- | ---------------------------- |
| `models/ITTT_model/core.py`       | 134-173 | text + image_url             |
| `models/VTTT_model/core.py`       | 129-177 | text + image_url + video_url |
| `models/LLMs/base_local_llama.py` | 60      | text-only（基类默认，已上提） |

- **改善**: `base_local_llama.py` 已提取 Template Method 基类（`LocalLlamaChatBase`）
- **模式**: 完善 Template Method — ITTT/VTTT 的 `_convert_message_to_dict_impl` 提取到基类，通过 hook 扩展

### 5.3 [NEW] 5 处模型 config 构建重复（读 env + 建 dict + 过滤 None）

| 文件                                | 行号      |
| ----------------------------------- | --------- |
| `models/ITTT_model/core.py`         | 44, 64-74 |
| `models/VTTT_model/core.py`         | 47, 67-77 |
| `models/LLMs/main_llm.py`           | 17, 64-88 |
| `models/LLMs/reasoner_llm.py`       | 14, 23-36 |
| `models/LLMs/auxiliary_llm/core.py` | 39        |

- **已统一**: `config/features/` TypedDict + `LLM_CLIENT_DEFAULTS`（`config/features/agent_side/llm_client_defaults.py:32`）——常量散落已消解
- **模式**: 提取 `ModelEnvBuilder` — 读 env + 构建 config dict + 过滤 None

### 5.5 [RESOLVED] middleware sync/async 双路径

- **已完成（2026-09-21）**: `summarization` 的 sync/async 对按「共享同步实现 + async 薄包装」收敛 **4 对**（仅当语义等价时）：
  - `_execute_compact` / `_aexecute_compact` → 共享 `_finish_compact`
  - `_dispatch_overflow_route` / `_adispatch_overflow_route` → 共享 `_prepare_overflow_dispatch`
  - `_post_response_check` / `_apost_response_check` → 共享 `_evaluate_post_response` + `_log_post_response`
  - `_forced_recovery_request` / `_aforced_recovery_request` → 共享 `_begin_forced_recovery` / `_finish_forced_recovery`
- **未收敛（3 对，附理由）**:
  - `_apply_compression_under_lock` / `_aapply_compression_under_lock` — sync 走 `run_memory_flush_sync`，async `await run_memory_flush`；折叠会改变 await/取消语义（任务显式要求保留）
  - `_execute_with_recovery` / `_aexecute_with_recovery` — 非目标异常的 `raise`（bare re-raise，保留原始 traceback）与 handler 的同步/异步调用必须各自成立
  - `_before_agent_impl` / `_abefore_agent_impl` — T1 preflight 的 `_t1_preflight` / `await _at1_preflight` 差异
- **已正确使用共享 impl 的中间件（不变）**: media_pipeline、tool_guardrails、iteration_budget、context_engine/core
- **覆盖**: 收敛后的每条路径仍由既有 sync + async 两侧测试覆盖（`test_summarization_comprehensive` 的 `TestSummarizationAsync` 等），未删任何一侧测试

---

## 6. Cross-cutting: Global State Inventory

### 后端模块级可变全局（60+ 处 agent/，10+ 处 server/；口径：AST 顶层可变赋值 + `global` 声明，排除 tests/evals/scripts）

| 类别             | 代表变量                                                              | 文件                                 |
| ---------------- | --------------------------------------------------------------------- | ------------------------------------ |
| 子代理运行注册表 | `_runs`, `_lock`                                                      | `registry/memory.py:6-7`             |
| 任务引用         | `_task_refs`                                                          | `registry/task_refs.py:6`            |
| 生命周期管理     | `_terminal_locks`, `_cleanup_generations`, `_deferred_cleanup_timers` | `registry/lifecycle.py:40-42`        |
| HITL 状态        | `_HITL_PENDING`, `_HITL_LOCK`                                         | `registry/session_state.py:104-105`    |
| 交付幂等         | `_delivered_keys`, `_delivery_mirror`                                 | `announce/delivery.py:32-33`         |
| 队列持有         | `_QUEUE_HOLDER`, `_QUEUE_LOCK`                                        | `announce/steering_queue.py:337-338` |
| 事件总线         | `_BUS`                                                                | `events/core.py:105`                 |
| sweeper          | `_sweeper_task`                                                       | `registry/sweeper.py:331`            |
| followup         | `_followup_task`                                                      | `followup/core.py:12`                |
| bridge           | `_bridge_task`                                                        | `events/bridge.py:34`                |
| 压缩 TODO        | `_COMPRESSION_TODO_TASKS`                                             | `summarization/nudges.py:355`                       |
| 冷却会话         | `_RESTORED_COOLDOWN_SESSIONS`                                         | `summarization/core.py:105`               |
| 提醒会话         | `_reminded_sessions`                                                  | `todowrite.py:34`                    |
| 任务流错误       | `_REGISTRY_ERROR`                                                     | `taskflow_wait_all.py:43`            |
| wrapper 工厂     | `_GRAPH_WRAPPER_FACTORIES`                                            | `wrapper/registry.py:45`（有意为之的进程级可插拔注册表：公共 API `register_graph_wrapper`/`unregister_graph_wrapper`/`reset_graph_wrappers`，非待消除 smell）             |
| 初始化锁         | `_init_lock`, `_init_loop`, `_sync_init_lock`                         | 3 个 SQLite 存储                     |
| 持久化锁         | `_persist_lock`                                                       | `state.py:9`                         |
| 武装会话         | `_armed_sessions`                                                     | `task_intent/core.py:198`                 |
| 停滞追踪         | 5 个 dict                                                             | `stagnation_tracker.py:27-31`        |
| 工具列表缓存     | `_tools`                                                              | `core.py:52`                         |
| 进度钩子         | 4 个 list                                                             | `hooks/progress.py:6-9`              |

### 前端模块级可变全局（基础设施单例，18 → 约 10 文件）

共享 UI 状态已迁移至 Pinia（`stores/` 5 个，见 4.5.2 节）。仍保留在模块级的是**基础设施单例/缓存（约 10 个文件**：`ws.ts`、`agent-socket.ts`、`subagent-sync.ts`、`toast.ts`、`model-config.ts`、`global-error-handler.ts`、`utils/client.ts`、`mitt.ts`、`clientLog.ts`、`db.ts`，见 4.3 节）与组件内局部 `let`。

---

## 7. Cross-cutting Patterns Summary

| Pattern                     | 适用发现                                               | 核心收益                         |
| --------------------------- | ------------------------------------------------------ | -------------------------------- |
| **Strategy + Registry**     | #1.1.1, #1.1.3, #1.1.5, #4.1.1, #4.1.5         | 消除 if-elif 链，开闭原则        |
| **Chain of Responsibility** | #1.1.4                                                 | 拆解嵌套分发为独立 handler 链    |
| **Builder**                 | #1.2.2 (built_agent)                                   | 逐步组装复杂对象                 |
| **Facade + Service Layer**  | #1.2.1, #1.2.3, #3.1.1                                 | 中间件委托给服务，只协调         |
| **Repository**              | #1.3.2, #3.1.2, #5.1                                   | 封装 SQL，分离数据访问与业务逻辑 |
| **Dependency Injection**    | #1.4.4, #1.4.5, #2.1.3, #2.1.5, #3.1.5 | 消除 lazy import 缝隙和跨层依赖  |
| **Command Registry**        | #4.1.1, #4.1.2, #4.1.6                                 | 消除 switch/case，开闭原则       |
| **Lookup Table**            | #4.1.3, #4.1.4                                         | 消除硬编码映射重复               |
| **Template Method**         | #5.2, #1.2.4                                           | 提取共享骨架，hook 扩展          |
| **Base Class / Mixin**      | #5.1, #5.3, #3.3.4                               | 消除重复 boilerplate             |
| **Protocol / Interface**    | #1.4.1, #1.4.2, #1.4.3, #2.1.4, #3.3.4                 | 类型安全的多态替代               |
| **State Pattern**           | #2.1.2                                                 | 封装隐式状态机为显式状态类       |
| **Explicit Initialization** | #2.1.5, #3.1.5                                         | 移除 import 时副作用             |
| **Adapter**                 | #1.3.3, #1.3.4, #4.4.1, #4.4.2                         | 封装第三方 API/协议差异          |
| **Generic Type**            | #4.6.1（39 处断言根因）                                                 | 类型安全替代类型断言             |

---

## 8. Recommended Refactoring Roadmap

> **2026-09-21 核对**：已完成仅 **2.16 / 2.17 / 3.8**（3.8 Pinia 迁移已落地，该步已从列表删除）；其余待做。每步附当前 file:line 定位，可直接执行。

### Phase 0: 修复数据丢失 Bug（P0）

| Step | Target                             | Pattern    | Est. Effort | Status |
| ---- | ---------------------------------- | ---------- | ----------- | ------ |
| 0.1  | `channels/manager.py` 竞争消费 bug | 统一消费者 | 0.5 天      | Done   |

- 已修复（2026-09-21）：`_dispatch_outbound`（`channels/manager.py:243`）为唯一出站消费者；删除 `_outbound_consume_loop`/`_consume_loop`；`start_service()` 仅调度 dispatcher + 入站消费者（`:174`）。原静默丢失点在 `server/trigger/channels/core.py:317-327`（`_process_outbound` 只登记 session、不调用 `channel.send()`）；其副作用改为在 `_dispatch_outbound` 内对目标频道 fail-open 调用。

### Phase 1: 消除最大风险 + 架构方向（P1）

| Step | Target                                                 | Pattern                 | Est. Effort | Status |
| ---- | ------------------------------------------------------ | ----------------------- | ----------- | ------ |
| 1.1  | `context_engine/store/core.py:16` eager DB → lazy      | Lazy init               | 0.5 天      | Open   |
| 1.2  | 中间件 `session_id` 提取 + `state_register_mem` Facade | Mixin + Enum key        | 1 天        | Open   |
| 1.3  | `runtime/session/state_register.py` Protocol + 连接池 + lazy | Interface Seg. + 连接池 | 1-2 天      | Open   |
| 1.4  | `summarization/core.py` 拆分（2745 → 573 行 + 共享 impl） | 分层 + 共享 impl        | 3-5 天      | Done   |

- 1.1 定位：`context_engine/store/core.py:16`（`_db = get_db()`）。
- 1.2/1.3 定位：`runtime/session/state_register.py`（`StateRegisterMem`/`StateRegisterDB` 无 Protocol；DB 每次 `sqlite3.connect()`）。
- 1.4 定位（已完成 2026-09-21）：`agent/middlewares/summarization/core.py` 2745 → 573 行；核心拆为 `compression.py`(421)/`overflow.py`(841)/`summary_generation.py`(857)/`thrash.py`(169)/`state_aliases.py`(26)，`core.py` 仅留类 + hook 编排 + 进程级接缝；sync/async 收敛 4 对（未收敛 3 对见 §5.5）。

### Phase 2: 拆解 God 模块 + DRY 清理（P2）

| Step | Target                                                     | Pattern               | Est. Effort | Status |
| ---- | ---------------------------------------------------------- | --------------------- | ----------- | ------ |
| 2.1  | `BaseSQLiteRepository` 基类提取（3 个 SQLite 存储）        | Base Class            | 1 天        | Open   |
| 2.2  | `FileStore` 基类提取 + 工具函数提取                        | Template Method       | 1 天        | Open   |
| 2.3  | Repository Pattern（SQL 封装：5 个文件）                   | Repository            | 2-3 天      | Open   |
| 2.4  | Command Executor 抽象                                      | Adapter               | 1 天        | Open   |
| 2.5  | API Client Adapter（embed/reranker/media HTTP）            | Adapter               | 1 天        | Open   |
| 2.6  | 工厂函数统一（ITTT/VTTT/reranker/extract）                 | Factory               | 1 天        | Open   |
| 2.7  | `ModelEnvBuilder` 提取（5 处模型 config 构建重复）         | DRY                   | 0.5 天      | Open   |
| 2.8  | `ContentDecoder` 提取（4 处 JSON decode 重复）             | DRY                   | 0.5 天      | Open   |
| 2.9  | `_convert_message_to_dict` 完善到基类                      | Template Method       | 0.5 天      | Open   |
| 2.10 | `RerankerProtocol` ABC 提取                                | Interface Seg.        | 0.5 天      | Open   |
| 2.11 | 前端 `Response<T>` 泛型                                    | Generic Type          | 0.5 天      | Open   |
| 2.12 | 前端 `WebSocketConnection` 基类                            | Base Class + Strategy | 1-2 天      | Open   |
| 2.13 | 前端 Command Registry + Dialog Manager                     | Registry              | 1 天        | Open   |
| 2.14 | 前端 `SUBAGENT_STATUS_META` 查找表                         | Lookup Table          | 0.5 天      | Open   |
| 2.15 | 前端 `resolveSid`/`safeT` 提取                             | DRY                   | 0.5 天      | Open   |
| 2.16 | `config/schema.py` 反转 `models/` 依赖                     | 反转依赖              | 1 天        | Done   |
| 2.17 | `models/LLMs/main_llm.py` 提取 `FallbackCandidate` 到 pub/ | 反转依赖              | 0.5 天      | Done   |

- 2.1 定位：`agent/tools/{todolist,taskflow,subagent}/registry/store_sqlite.py`（381/703/300 行）；`_connect` :167/:290/:85，`_ensure_tables_sync` :271/:376/:166，init 锁 :95-102/:188-195/:64-71；taskflow 已有 `session_id` 列与迁移（`:137-169`），基类化须参数化 DDL。
- 2.2 定位：`server/service/file_store.py:28`（`class FileStore`）。
- 2.3 定位：`agent/checkpointer/thread_safe_checkpointer.py:196-242`；`context_engine/store/core.py`（872 行）；`context_engine/store/db.py`（DDL）；`context_engine/embeddings/store.py:32-54`；`context_engine/events/store.py`。
- 2.4 定位：`agent/tools/terminal.py:189,198`；`agent/tools/python_repl.py:121`；`agent/tools/skill_tools/skill_manage.py:327`。
- 2.5 定位：`models/embed_model/core.py:128`；`models/reranker_model/core.py:573,623,679`；`agent/middlewares/media_pipeline/media_handlers.py:90-91`。
- 2.6 定位：`models/ITTT_model/core.py:81`；`models/VTTT_model/core.py:76`；`models/reranker_model/__init__.py:120`；`models/extract_model/core.py:194`。
- 2.7 定位：ITTT `core.py:44,64-74`；VTTT `core.py:47,67-77`；`main_llm.py:17,64-88`；`reasoner_llm.py:14,23-36`；`auxiliary_llm/core.py:39`。
- 2.8 定位：`context_engine/core.py:157`；`context_engine/store/core.py:618`；`context_engine/store/core.py:788`；`context_engine/embeddings/store.py:91`。
- 2.9 定位：ITTT `core.py:134-173`；VTTT `core.py:129-177`；基类 `models/LLMs/base_local_llama.py:60`。
- 2.10 定位：`models/reranker_model/core.py:264`（`CrossEncoderGGUF`）/ `:537`（`CloudReranker`）。
- 2.11 定位：`client/app/types/response.d.ts`；39 处断言分布见 §4.6.1。
- 2.12 定位：`client/app/composables/ws.ts:252,410`；`client/app/composables/bridge/agent-socket.ts:405-441`。
- 2.13 定位：`client/app/pages/home/index.vue:396-434`；`client/app/pages/home/index/[sid].vue:689-708`。
- 2.14 定位：`client/app/composables/useSubagentTasks.ts:50,66`；`client/app/pages/home/components/SubagentFlowGraph.vue:103,123,151`。
- 2.15 定位：`client/app/utils/client.ts:16,29`；`subagent-sync.ts:68` ↔ `stores/todo.ts:81`；`toast.ts:52` ↔ `stores/connection.ts:65`。
- 2.16/2.17 Done 依据：`config/schema.py:24 set_provider_registry`（`lint-imports` KEPT）；`pub/types/llm.py:8` + `models/LLMs/main_llm.py:138`。

### Phase 3: 架构清理 + 前端状态管理（P3）

| Step | Target                            | Pattern        | Est. Effort | Status |
| ---- | --------------------------------- | -------------- | ----------- | ------ |
| 3.1  | 导入时副作用 → `setup()` 函数     | Explicit init  | 1 天        | Open   |
| 3.2  | `turn_runner` 依赖注入            | DI             | 2 天        | Open   |
| 3.3  | `built_agent()` Builder           | Builder        | 1 天        | Open   |
| 3.4  | `state_register` Protocol 完善    | Interface Seg. | 1 天        | Open   |
| 3.5  | `curator/orchestrator.py` 拆分    | 分层架构       | 2 天        | Open   |
| 3.6  | `ChatBox.vue` 拆分为多 composable | Separation     | 2 天        | Open   |
| 3.7  | `agent-socket.ts` 拆分            | Separation     | 2 天        | Open   |
| 3.9  | 前端错误处理统一                  | Result/规范    | 1 天        | Open   |
| 3.10 | 前端 upload.ts/health.ts 统一 API | Adapter        | 0.5 天      | Open   |
| 3.11 | if-else 链 → Strategy（后端剩余 4 处 + 前端 §4.1.x） | Strategy | 2 天 | Open |

- 3.1 定位：5 处导入时副作用见 §2.1.5。
- 3.2 定位：`server/service/turn_runner.py`（7 个 lazy import）。
- 3.3 定位：`agent/core.py:122-252`。
- 3.4 定位：`runtime/session/state_register.py`。
- 3.5 定位：`context_engine/curator/orchestrator.py`（775 行；`llm.invoke` 在 `:98`/`:473`）。
- 3.6 定位：`client/app/pages/home/components/ChatBox.vue`（888 行）。
- 3.7 定位：`client/app/composables/bridge/agent-socket.ts`（554 行）。
- 3.9 定位：§4.5.3 的 6 行策略表（**需执行前单独复核**）。
- 3.10 定位：`client/app/composables/bridge/upload.ts:69` / `bridge/health.ts:18`。
- 3.11 定位：`models/LLMs/reasoning_payload.py:148-216`；`agent/tools/skill_tools/skill_manage.py:922-980`；`agent/middlewares/tool_guardrails/core.py:124-222`；`agent/tools/subagent/announce/delivery.py:312-443`（原 `memory.py` facts action 链已消解，该节已删除）。

---

> **注**: 每步重构应在对应测试通过后合并。建议按 Phase 0 → 1 → 2 → 3 顺序推进，前置 phase 的基础（如 SessionState Facade、BaseSQLiteRepository、Response\<T\> 泛型）是后续步骤的前提。
