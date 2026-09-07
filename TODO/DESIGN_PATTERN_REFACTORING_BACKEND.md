# Design Pattern Refactoring — Backend

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式（后端部分）。
>
> 审查范围：`agent/`、`models/`、`server/`、`runtime/`、`channels/`、`bus/`、`context_engine/`、`plugins/`
>
> 生成日期：2026-09-04
>
> 更新（2026-09-07）：已完成条目（原 1.1 / 2.1 / 3.1 全部小节）已从本报告移除，其余条目重编号为连续编号（章节 1.1–1.6 / 2.1–2.2 / 3.1–3.2，Summary Table #1–#59），正文交叉引用已同步。

---

## 目录

- [Summary Table](#summary-table)
- [1. Backend: Agent & Middlewares](#1-backend-agent--middlewares)
- [2. Backend: Server & Service & Runtime](#2-backend-server--service--runtime)
- [3. Backend: Channels / Bus / Context Engine](#3-backend-channels--bus--context-engine)

> 前端部分见 [DESIGN_PATTERN_REFACTORING_FRONTEND.md](./DESIGN_PATTERN_REFACTORING_FRONTEND.md)

---

## Summary Table

| P      | #   | Location                                                   | Problem                               | Pattern                           |
| ------ | --- | ---------------------------------------------------------- | ------------------------------------- | --------------------------------- |
| **P0** | 1   | `server/service/messages.py` async_generate                | 300 行 God Function，10+ 职责         | Strategy + Chain of Resp. + State |
| **P0** | 2   | `agent/middlewares/humanInTheLoop/core.py` after_model     | 230 行巨型方法，if-else 工具路由      | Strategy + Registry               |
| **P0** | 3   | `client/app/pages/home/index/[sid].vue`                    | 1629 行 God Component                 | Composable 拆分                   |
| **P0** | 4   | `client/app/composables/bridge.ts`                         | 1978 行 God Module，14 处 isTauri()   | Strategy + 模块拆分               |
| **P0** | 5   | `bridge.ts` isTauri() 分支                                 | 14 处重复运行时检测 + 传输选择        | Strategy + Factory                |
| **P1** | 6   | `output_repetition_guard.py`                               | 727 行类，混合哈希/检测/状态/钩子     | 组合模式拆分                      |
| **P1** | 7   | `multimodal_processor.py`                                  | 210 行方法，5 种媒体类型 if-elif      | Strategy dispatch                 |
| **P1** | 8   | `context_engine/core.py` search_messages                   | 247 行 God Function，3 种搜索策略     | Strategy Pattern                  |
| **P1** | 9   | `context_engine/store/core.py` add_messages                | 222 行，3 种消息类型 if-elif          | Strategy/Command                  |
| **P1** | 10  | `server/service/skill_scanner.py`                          | 827 行模块，8+ 职责                   | SRP 拆分                          |
| **P1** | 11  | `server/queue/user_input_queue.py`                         | 678 行 God class                      | Repository Pattern                |
| **P1** | 12  | `state_register_mem` 全局耦合                              | 所有中间件直接依赖全局单例            | SessionState Facade + Enum key    |
| **P1** | 13  | `[sid].vue` appendStreamChunk                              | 115 行 5 路 if/else                   | Strategy Registry                 |
| **P1** | 14  | `runtime/state_register.py`                                | 每次 SQLite 操作新开连接              | 连接池/Repository                 |
| **P1** | 15  | `stream_repetition_guard_wrapper.py` astream               | 200 行嵌套状态机                      | State Pattern                     |
| **P1** | 16  | `summarization.py`                                         | 493 行类，10+ 方法混合多职责          | 组件拆分                          |
| **P1** | 17  | `client/useSubagentTasks.ts`                               | 799 行 God Composable                 | 状态切片拆分                      |
| **P1** | 18  | `orchestrator.py` run_curator_review                       | 189 行 God Function                   | SRP 拆分                          |
| **P1** | 19  | `orchestrator.py` _apply_consolidation                     | 144 行 God Function，4-5 层嵌套       | SRP + Extract Method              |
| **P2** | 20  | `resolveWsBaseUrl` / `closeSocket`                         | 前端 2-3 处重复                       | 提取共享模块                      |
| **P2** | 21  | 媒体文件选择逻辑                                           | image/audio/video 三重复制            | Parameterized Factory             |
| **P2** | 22  | `VITE_API_BACK_URL` 硬编码                                 | 9+ 处重复                             | Configuration Provider            |
| **P2** | 23  | ITTT/VTTT 模块级单例                                       | 与 main_llm 工厂模式不一致            | 统一工厂函数                      |
| **P2** | 24  | `RepetitionGuardWrapper` 导入私有常量                      | 跨模块私有依赖                        | 依赖倒置 + Protocol               |
| **P2** | 25  | 原始 SQL 泄漏                                              | checkpointer/message_search/store     | Repository Pattern                |
| **P2** | 26  | 原始 HTTP requests.post                                    | embed_model/reranker_model            | API Client Adapter                |
| **P2** | 27  | 原始 subprocess/Popen                                      | terminal.py/python_repl.py            | Command Executor 抽象             |
| **P2** | 28  | `handleOperate` switch                                     | 13 路 case                            | Command Registry                  |
| **P2** | 29  | WS onmessage 模式                                          | 6 处重复                              | Event Handler Registry            |
| **P2** | 30  | `badgeClass`/`statusLabel`                                 | 硬编码 if-else 映射                   | Lookup Table                      |
| **P2** | 31  | `[sid].vue` 协议泄漏                                       | 直接导入传输层类型                    | Event Aggregator                  |
| **P2** | 32  | ChatBox 构造后端 URL                                       | 展示组件含 API 形状知识               | Media URL Resolver Service        |
| **P2** | 33  | `(controller as any)`                                      | 类型安全破坏                          | Type-Safe Interface               |
| **P2** | 34  | base64 剥离逻辑                                            | 视图层 3 处 `split(',')[1]`           | Media Encoding Utility            |
| **P3** | 35  | `build_reasoning_kwargs`                                   | provider if-elif 链                   | Strategy + Registry               |
| **P3** | 36  | `ToolGuardrails._evaluate`                                 | 嵌套 if-else action 决策              | Chain of Responsibility           |
| **P3** | 37  | `built_agent()`                                            | 混合事件循环/checkpointer/LLM/中间件  | Builder Pattern                   |
| **P3** | 38  | `reranker_model/core.py`                                   | FFI+numpy+HTTP+排序混合               | 分层架构                          |
| **P3** | 39  | `Summarization` 依赖 agent.tools                           | 中间件层依赖工具层                    | 依赖倒置                          |
| **P3** | 40  | `ContextEngineHook` 创建子代理                             | 中间件直接依赖 agent 构建             | Factory + DI                      |
| **P3** | 41  | `IterationBudget` 依赖私有函数                             | 跨模块 `_is_internal_completion`      | Protocol                          |
| **P3** | 42  | channels/core.py 导入时副作用                              | import 即启动线程/注册                | Explicit initialization           |
| **P3** | 43  | subagent/core.py 导入时副作用                              | import 即调度任务                     | Explicit initialization           |
| **P3** | 44  | `ws_event_processor_dict`                                  | 全局 dict 事件分发                    | Registry Pattern                  |
| **P3** | 45  | `ws_handler` 混合关注点                                    | 消息解析/分发/响应/生命周期           | Separation of Concerns            |
| **P3** | 46  | `messages.py` 模块级可变状态                               | `_pending_args`/`_pending_raw` 全局   | State Pattern                     |
| **P3** | 47  | `turn_runner.py` 延迟导入缝隙                              | 7 个 lazy import 绕循环依赖           | Dependency Injection              |
| **P3** | 48  | `state_register.py` 无公共接口                             | Mem/DB 无 Protocol                    | Interface Segregation             |
| **P3** | 49  | `LocalLlamaChatModel` (auxiliary)                          | 280 行类，5+ 职责                     | Decorator 分离                    |
| **P3** | 50  | `session_search()`                                         | 150 行函数混合多职责                  | 提取类                            |
| **P3** | 51  | `ContextEngineHook._after_agent_impl`                      | 返回 5 元组裸值                       | Dataclass + NudgeCounter          |
| **P3** | 52  | `aclean_old_checkpoints`                                   | 70 行原始 SQL                         | Repository Pattern                |
| **P3** | 53  | `ChannelManager`                                           | God Class，管理生命周期/事件循环/路由 | Separation of Concerns            |
| **P3** | 54  | `handleSend`                                               | 140+ 行 5 层回调嵌套                  | State Machine + 分解              |
| **P3** | 55  | `loadSessionHistory`                                       | 91 行 3 层 Map 嵌套                   | Pre-indexing + Extract            |
| **P3** | 56  | `sendChatMessageWs`                                        | 282 行 4 层闭包                       | Extract Method + Registry         |
| **P3** | 57  | `postAgentStream`                                          | 适配器 + 流编排混合                   | Adapter + Error Classifier        |
| **P3** | 58  | IndexedDB 序列化泄漏                                       | 视图层 `JSON.parse(JSON.stringify)`   | Repository 封装                   |
| **P3** | 59  | `useSubagentTasks` 三方耦合                                | 直接依赖 bridge/db/ws                 | Repository + 统一模型             |

---

## 1. Backend: Agent & Middlewares

### 1.1 God 类/函数

#### 1.1.1 `HumanInTheLoop.after_model()` — 230 行巨型方法

- **文件**: `agent/middlewares/humanInTheLoop/core.py:257-488`
- **问题**: 单方法承担 4 种工具类型审批路由（terminal 审批、memory 写审批、interrupt_on 配置工具、plugin 工具），每种有不同审批逻辑和消息构建，嵌套 4-5 层
- **模式**: Strategy + Registry

```python
class ToolApprovalHandler(ABC):
    @abstractmethod
    def matches(self, tool_name: str, config) -> bool: ...
    @abstractmethod
    def build_action_requests(self, tool_call, config) -> list[dict]: ...

class TerminalApprovalHandler(ToolApprovalHandler): ...
class MemoryWriteApprovalHandler(ToolApprovalHandler): ...
class InterruptOnApprovalHandler(ToolApprovalHandler): ...

class ApprovalHandlerRegistry:
    def __init__(self):
        self._handlers: list[ToolApprovalHandler] = []
    def register(self, handler): self._handlers.append(handler)
    def resolve(self, tool_name, config):
        return next(h for h in self._handlers if h.matches(tool_name, config))

# after_model 简化为:
def after_model(self, state, runtime):
    for tc in tool_calls:
        handler = self._registry.resolve(tc["name"], self._config)
        action_requests = handler.build_action_requests(tc, self._config)
        ...
```

#### 1.1.2 `MultimodalProcessor._before_agent_impl()` — 210 行

- **文件**: `agent/middlewares/multimodal_processor.py:150-363`
- **问题**: 单方法处理 image_url/audio_url/audio_bytes/video_url/video_bytes 五种媒体类型，每种含 base64 解码/文件下载/临时文件写入/路径管理/提示词注入
- **模式**: Strategy — 定义 `MediaItemHandler` 接口，按 type 注册

```python
_MEDIA_HANDLERS: dict[str, MediaItemHandler] = {
    "image_url": ImageUrlHandler(),
    "audio_url": AudioUrlHandler(),
    "audio_bytes": AudioBytesHandler(),
    "video_url": VideoUrlHandler(),
    "video_bytes": VideoBytesHandler(),
}
# _before_agent_impl 简化为:
for item in content:
    handler = _MEDIA_HANDLERS.get(item.get("type"))
    if handler:
        handler.process(item, session_id, hints)
```

#### 1.1.3 `OutputRepetitionGuard` — 727 行类

- **文件**: `agent/middlewares/output_repetition_guard.py:116-727`
- **问题**: 混合哈希计算(`_normalize_for_hash`, `_content_hash`)、三种内部重复检测器(`_detect_sentence_repetition`, `_detect_char_run`, `_detect_phrase_repetition`)、推理文本提取(`_extract_reasoning`, `_extract_inline_reasoning`)、跨调用检测(`_check_text_repetition`)、中间件钩子、模块级单例和流级检查函数
- **模式**: 组合拆分

```python
# 拆分为:
# repetition_detectors.py — 纯检测逻辑
class SentenceRepetitionDetector: ...
class CharRunDetector: ...
class PhraseRepetitionDetector: ...
# repetition_state.py — 状态管理
class RepetitionState: ...
# output_repetition_guard.py — 中间件壳，只含钩子
class OutputRepetitionGuard(AgentMiddleware):
    def __init__(self):
        self._detectors = [SentenceRepetitionDetector(), CharRunDetector(), ...]
```

#### 1.1.4 `LocalLlamaChatModel` (auxiliary_llm) — 280 行

- **文件**: `models/LLMs/auxiliary_llm/core.py:140-419`
- **问题**: 类同时承担模型加载(`_ensure_client`)、消息转换、`_generate` 推理、`bind_tools`（含 JSON 解析和 prompt 注入）、`with_structured_output`（instructor 集成）
- **模式**: Decorator/Adapter 分离

```python
class LocalLlamaChatModel(BaseChatModel):
    # 只保留 _generate / _ensure_client
class LocalToolBinder:
    """Wraps a model with tool binding via prompt injection."""
    def __init__(self, model): self._model = model
    def bind_tools(self, tools): ...
class LocalStructuredOutput:
    """Wraps a model with instructor for structured output."""
    def __init__(self, model): self._model = model
    def with_structured_output(self, schema): ...
```

#### 1.1.5 `Summarization` — 493 行，10+ 方法

- **文件**: `agent/middlewares/summarization.py:43-493`
- **问题**: 混合 cutoff 索引调整（orphan AI/Tool 修复）、token 估算、last-turn 比例检测、压缩有效性追踪（反抖动）、消息截断、连续 HumanMessage 合并、system prompt 重建
- **模式**: 组件拆分

```python
class CompressionEffectivenessTracker:
    """Anti-flutter: track whether last compression actually saved tokens."""
class MessageTruncator:
    """Cut messages at cutoff index, merge consecutive HumanMessages."""
class OrphanPairRepairer:
    """Fix orphan AI/Tool messages at the cutoff boundary."""
class Summarization(AgentMiddleware):
    # 只保留钩子协调，委托给上述组件
```

---

### 1.2 长方法与深嵌套

#### 1.2.1 `RepetitionGuardWrapper.astream()` — 200 行嵌套状态机

- **文件**: `agent/stream_repetition_guard_wrapper.py:201-395`
- **问题**: 5 层嵌套 if/elif，管理 `saw_updates`/`phantom_dropped`/`call_text`/`call_cut` 等状态变量
- **模式**: State Pattern — 提取 `StreamGuardState` 类

```python
class StreamGuardState(ABC):
    @abstractmethod
    def on_chunk(self, chunk, ctx) -> list[dict]: ...

class FreshState(StreamGuardState): ...
class UpdatesSeenState(StreamGuardState): ...
class ModelTextState(StreamGuardState): ...
class CutState(StreamGuardState): ...
```

#### 1.2.2 `session_search()` — 150 行

- **文件**: `agent/tools/message_search.py:275-423`
- **问题**: 混合输入验证、FTS5 搜索、对话格式化、截断策略、并行摘要、内嵌 `_summarize_all`
- **模式**: 提取 `SessionSearcher`、`ConversationTruncator`、`SessionSummarizer` 三类

---

### 1.3 硬编码 if-else 链

#### 1.3.1 `build_reasoning_kwargs()` provider 分发

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

#### 1.3.2 `MultimodalProcessor` 媒体类型分发

- **文件**: `agent/middlewares/multimodal_processor.py:174-292`
- **问题**: 6 路 if-elif 分发 text/image_url/audio_url/audio_bytes/video_url/video_bytes
- **模式**: Strategy — 见 1.1.2

#### 1.3.3 `ContextEngineHook.after_agent` nudge 分发

- **文件**: `agent/middlewares/context_engine/core.py:297-303` (sync), `316-351` (async)
- **问题**: if need_memory and need_skill / if need_memory / if need_skill 三路分发
- **模式**: Strategy — `MemoryNudgeStrategy`、`SkillNudgeStrategy`、`CombinedNudgeStrategy`

#### 1.3.4 `ToolGuardrails._evaluate()` action 决策

- **文件**: `agent/middlewares/tool_guardrails.py:119-200`
- **问题**: 嵌套 if-else 决定 ALLOW/WARN/BLOCK/HALT，三种病理各有独立阈值
- **模式**: Chain of Responsibility — `ExactFailureDetector`、`SameToolFailureDetector`、`NoProgressDetector`，各自独立评估，最终 action 由最高优先级决定

---

### 1.4 混合关注点

#### 1.4.1 `MultimodalProcessor` — I/O + 解码 + 路径管理 + 消息操纵

- **文件**: `agent/middlewares/multimodal_processor.py`
- **问题**: 中间件同时负责 base64 解码(PIL)、HTTP 下载(urllib)、文件系统管理、magic byte 推断、消息内容操纵、历史清理
- **模式**: 分离关注点 — `MediaDownloadService`(I/O)、`MediaFormatDetector`、`MediaHintBuilder`(提示词)

#### 1.4.2 `ContextEngineHook` — prompt 管理 + 计数器 + 持久化 + 子代理

- **文件**: `agent/middlewares/context_engine/core.py`
- **问题**: 同时管理系统 prompt 加载/缓存、nudge 计数器(DB 读写)、消息持久化、HITL 拒绝修复、子代理调度
- **模式**: Facade + Service 层 — `SystemPromptService`、`NudgeCounterService`、`MessagePersistenceService`

#### 1.4.3 `built_agent()` — 事件循环 + 检查点 + LLM + 中间件

- **文件**: `agent/core.py:97-170`
- **问题**: 单函数混合事件循环检测/缓存、checkpointer 构建/清理、LLM 实例创建、中间件列表硬编码、agent 编译、wrapper 包装
- **模式**: Builder Pattern

```python
class AgentBuilder:
    def __init__(self): self._middlewares = []; self._tools = []; ...
    def with_checkpointer(self, cp): ...
    def with_llm(self, llm): ...
    def with_middleware(self, mw): ...
    def build(self) -> CompiledStateGraph: ...
```

#### 1.4.4 `reranker_model/core.py` — FFI + numpy + HTTP + 排序

- **文件**: `models/reranker_model/core.py` (717 行)
- **问题**: 单文件混合 ctypes FFI、GGUF 二进制解析、numpy 矩阵运算、HTTP API 客户端、排序/过滤逻辑
- **模式**: 分层架构 — `gguf_parser.py`、`llama_encoder.py`、`reranker_service.py`

---

### 1.5 缺失抽象

#### 1.5.1 `state_register_mem` 全局单例直接耦合

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

#### 1.5.2 原始 SQL 泄漏

- **文件**: `checkpointer/thread_safe_checkpointer.py:196-242`; `message_search.py:224-236`; `context_engine/store/core.py`
- **模式**: Repository Pattern — `CheckpointRepository` 封装所有 SQL

#### 1.5.3 原始 HTTP 请求泄漏

- **文件**: `embed_model/core.py:131-147`; `reranker_model/core.py:589-602`
- **问题**: 直接使用 `requests.post`，无重试、无连接池、无超时抽象
- **模式**: API Client Adapter — `EmbeddingApiClient`、`RerankerApiClient`

#### 1.5.4 原始 subprocess/Popen 操作泄漏

- **文件**: `terminal.py:212-263`; `python_repl.py:103-176`
- **问题**: 直接操作 `subprocess.Popen`/`asyncio.create_subprocess_*`，手动管理进程生命周期、编码、超时
- **模式**: Command Executor 抽象 — `CommandExecutor.run(argv, env, timeout) -> ExecutionResult`

---

### 1.6 紧耦合

#### 1.6.1 `RepetitionGuardWrapper` 导入中间件私有常量

- **文件**: `agent/stream_repetition_guard_wrapper.py:55-64`
- **问题**: 直接导入 `_HALTED_KEY`、`_INTERNAL_WARNED_KEY`、`_CHAR_RUN_MIN` 等私有常量，还实例化了一个 `OutputRepetitionGuard` 内部实例
- **模式**: 依赖倒置 — 提取 `RepetitionDetectorProtocol`，Wrapper 通过构造函数注入

#### 1.6.2 `IterationBudget` 依赖 `subagent_completion_drain` 私有函数

- **文件**: `agent/middlewares/iteration_budget.py:30`
- **问题**: `from agent.middlewares.subagent_completion_drain import _is_internal_completion` — 中间件之间通过私有函数耦合
- **模式**: 提取为公共 Protocol — `CompletionMessageProtocol`

#### 1.6.3 `Summarization` 直接依赖 `agent.tools.memory`

- **文件**: `agent/middlewares/summarization.py:410-412`
- **问题**: 中间件层直接导入工具层的 `memory_store` 并调用 `load_from_disk()`，违反分层架构
- **模式**: 依赖倒置 — 定义 `MemorySnapshotProvider` 接口，由 `core.py` 注入

#### 1.6.4 `ContextEngineHook` 直接创建子代理

- **文件**: `agent/middlewares/context_engine/nudge.py:233-248`
- **问题**: nudge 模块直接导入 `build_main_llm`、`get_agent_tools`、`create_agent`
- **模式**: Factory + DI — `NudgeAgentFactory` 协议注入

#### 1.6.5 ITTT/VTTT 模块级单例 vs 工厂不一致

- **文件**: `ITTT_model/core.py:80-84`; `VTTT_model/core.py:79-81`; 对比 `main_llm.py:90-93`
- **问题**: ITTT/VTTT 使用模块级 `init_chat_model(...)` 或 `LocalLlamaChatModel()`，而 main_llm/auxiliary_llm 使用工厂函数 `build_main_llm()`
- **模式**: 统一使用 `build_*()` 工厂函数模式

---

## 2. Backend: Server & Service & Runtime

### 2.1 God 类/函数

#### 2.1.1 `async_generate` — 300 行 God Function

- **文件**: `server/service/messages.py:295-599`
- **问题**: 10+ 职责（Agent 构建、多模态组装、流模式选择、chunk 处理、工具调用跟踪、参数累积、元数据捕获、文本/推理输出、取消/超时/异常处理、状态管理、中断标记写入、生成器清理）
- **模式**: Strategy + Chain of Responsibility + State — 详见 Summary Table #1

#### 2.1.2 `skill_scanner.py` — 827 行 God 模块

- **文件**: `server/service/skill_scanner.py`
- **问题**: CLI 后端扫描、Python API 扫描、可用性探测、结果标准化、序列化、内容寻址缓存、策略决策、环境变量管理
- **模式**: SRP 拆分 — `ScannerBackend`、`ScanResultNormalizer`、`ScanCache`、`ScanPolicy`

#### 2.1.3 `user_input_queue.py` — 678 行 God class

- **文件**: `server/queue/user_input_queue.py`
- **问题**: DB 初始化、连接管理、事务管理、入队去重、claim 原子性、终端转换、活跃计数、崩溃恢复、客户端 msg_id 查找
- **模式**: Repository Pattern — `ConnectionManager`、`SchemaManager`、`QueueRepository`

#### 2.1.4 `skills.py` — 540 行 handler 混合关注点

- **文件**: `server/trigger/http/skills.py`
- **问题**: 单文件处理技能列表、文件树、读取、状态读写、快照重建、上传扫描、切换、删除、pin
- **模式**: 按资源拆分 controller + Command Pattern

---

### 2.2 硬编码 if-else / 全局状态

#### 2.2.1 `ws_event_processor_dict` — 全局 dict 事件分发

- **文件**: `server/trigger/core.py:41-75`
- **问题**: 全局 dict `ws_event_processor_dict` 映射事件名到处理器，无类型安全、无生命周期管理
- **模式**: Registry Pattern — 正式化为 `EventDispatcher` 类

#### 2.2.2 `messages.py` 模块级可变状态

- **文件**: `server/service/messages.py:26-29`
- **问题**: `_pending_args`/`_pending_raw` 进程全局 dict，隐式全局状态，难测试、难推理
- **模式**: State Pattern / Session-scoped context object

#### 2.2.3 `turn_runner.py` 延迟导入缝隙

- **文件**: `server/service/turn_runner.py` (7 个 lazy import 函数: `_iqs`, `get_registry`, `get_websocket_by_session_id`, `set_hitl_pending`, `async_generate`, `get_pending_interrupt`, `_get_active_tasks`)
- **问题**: 纯为绕循环依赖而存在
- **模式**: Dependency Injection

#### 2.2.4 `runtime/state_register.py` — 无公共接口 + 每次新开连接

- **文件**: `runtime/state_register.py`
- **问题**: `StateRegisterMem` 和 `StateRegisterDB` 方法签名相同但无 Protocol；DB 每次操作 `sqlite3.connect()`
- **模式**: Interface Segregation (Protocol) + 连接池

#### 2.2.5 `runtime/core.py` 错误导入

- **文件**: `runtime/core.py:1`
- **问题**: `from venv import logger` — 应为 `from loguru import logger`（疑似 bug）
- **模式**: Bug fix

#### 2.2.6 导入时副作用

- **文件**: `server/trigger/channels/core.py:293-346`（import 即启动线程 + 注册 consumer）；`server/trigger/subagent/core.py:48`（import 即 `_schedule_startup()` 调度任务）
- **模式**: Explicit initialization — `setup()` 函数

---

## 3. Backend: Channels / Bus / Context Engine

### 3.1 God 类/函数

#### 3.1.1 `orchestrator.py::run_curator_review` — 189 行 God Function

- **文件**: `context_engine/curator/orchestrator.py:119-307`
- **问题**: dry_run 检查、自动转换、摘要构建、状态加载/保存、报告构建、LLM 调用、重命名摘要、报告写入、回调、结果返回——7+ 职责
- **模式**: SRP 拆分

#### 3.1.2 `orchestrator.py::_apply_consolidation` — 144 行 God Function

- **文件**: `context_engine/curator/orchestrator.py:512-655`
- **问题**: LLM 输出解析、umbrella 查找/创建、源内容读取、文件清单、LLM 生成 umbrella、创建技能、文件迁移、删除、prune、刷新——6+ 职责，4-5 层嵌套
- **模式**: SRP + Extract Method

#### 3.1.3 `context_engine/core.py::search_messages` — 247 行 God Function

- **文件**: `context_engine/core.py:128-374`
- **问题**: FTS5 查询净化、CJK 检测路由、trigram FTS5、LIKE fallback、非 CJK FTS5、上下文消息检索、多模态解码、结果后处理——三种搜索策略交织
- **模式**: Strategy Pattern

```python
class SearchStrategy(ABC):
    @abstractmethod
    def matches(self, query: str) -> bool: ...
    @abstractmethod
    def search(self, query: str, limit: int) -> list[dict]: ...

class TrigramStrategy(SearchStrategy): ...
class LikeStrategy(SearchStrategy): ...
class FtsStrategy(SearchStrategy): ...

class SearchStrategyFactory:
    def select(self, query: str) -> SearchStrategy:
        for s in [TrigramStrategy(), LikeStrategy(), FtsStrategy()]:
            if s.matches(query): return s
```

#### 3.1.4 `store/core.py::add_messages` — 222 行 God Function

- **文件**: `context_engine/store/core.py:33-255`
- **问题**: turn 编号、AI 消息规范化（model name/token/reasoning/tool_calls）、Human 消息规范化（filtering/media/subagent origin）、Tool 消息规范化、原子 turn 分配、批量 SQL
- **模式**: Strategy/Command — `MessageRowBuilder` per type

```python
class MessageRowBuilder(ABC):
    @abstractmethod
    def build(self, msg, turn_num) -> dict: ...

class AIMessageRowBuilder(MessageRowBuilder): ...
class HumanMessageRowBuilder(MessageRowBuilder): ...
class ToolMessageRowBuilder(MessageRowBuilder): ...

_BUILDERS: dict[str, MessageRowBuilder] = {
    "ai": AIMessageRowBuilder(),
    "human": HumanMessageRowBuilder(),
    "tool": ToolMessageRowBuilder(),
}
```

#### 3.1.5 `ChannelManager` — God Class

- **文件**: `channels/manager.py`
- **问题**: 生命周期管理、事件循环管理、消息路由、consumer 注册、配置加载、插件发现、allow-from 验证
- **模式**: Separation of Concerns

---

### 3.2 混合关注点 / 缺失抽象

#### 3.2.1 `context_engine/core.py` 模块级混合

- **问题**: 数据库访问(`_db`, `_lock`)、FTS5 语法(`_sanitize_fts5_query`)、内容编解码、业务逻辑、搜索结果组装全在一个模块
- **模式**: 分层架构 + Repository Pattern

#### 3.2.2 原始 SQL 泄漏

- **文件**: `context_engine/core.py` (trigram SQL, LIKE SQL, FTS SQL, context SQL); `store/core.py` (INSERT/SELECT/DELETE); `store/db.py` (DDL)
- **模式**: Repository Pattern — `MessageRepository`、`SearchRepository`

#### 3.2.3 FTS5 查询语法泄漏

- **文件**: `context_engine/core.py` — `_sanitize_fts5_query` 50 行处理 FTS5 语法
- **模式**: `FTSQuerySanitizer` 抽象

#### 3.2.4 LangChain 消息类型泄漏到 store

- **文件**: `context_engine/store/core.py::add_messages` — 直接访问 `m.type`/`m.response_metadata`/`m.usage_metadata`/`m.additional_kwargs`/`m.tool_calls`
- **模式**: Adapter/Mapper — `MessageRowMapper`

#### 3.2.5 模块级全局状态

- **文件**: `context_engine/core.py` (`_db`, `_lock`); `store/core.py` (`_db`, `_turn_assign_lock`); `curator/__init__.py` (`_idle_for_seconds`, `_curator_thread`); `plugins/channels/qq/core.py` (`_consecutive_install_failures`, `_cooldown_until`)
- **模式**: Dependency Injection

#### 3.2.6 `curator/__init__.py` 重导出私有函数

- **问题**: re-export 几十个 `_` 前缀的"私有"函数，公共 API 不清晰
- **模式**: Facade — 定义清晰公共接口

---

> 跨横切模式总结与重构路线图见 [DESIGN_PATTERN_REFACTORING_FRONTEND.md](./DESIGN_PATTERN_REFACTORING_FRONTEND.md)
