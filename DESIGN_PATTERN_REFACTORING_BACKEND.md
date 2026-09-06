# Design Pattern Refactoring — Backend

> 全项目代码审查：识别可用设计模式优化的代码异味与反模式（后端部分）。
>
> 审查范围：`agent/`、`models/`、`server/`、`runtime/`、`channels/`、`bus/`、`context_engine/`、`plugins/`
>
> 生成日期：2026-09-04

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
| **P0** | 1   | `server/service/messages.py` async_generate / resume_agent | ~170 行流分发循环完整复制             | Template Method                   |
| **P0** | 2   | `server/service/messages.py` async_generate                | 300 行 God Function，10+ 职责         | Strategy + Chain of Resp. + State |
| **P0** | 3   | `agent/middlewares/humanInTheLoop/core.py` after_model     | 230 行巨型方法，if-else 工具路由      | Strategy + Registry               |
| **P0** | 4   | `models/LLMs/` LocalLlamaChatModel                         | 3 文件近乎逐行复制                    | Template Method 基类              |
| **P0** | 5   | `client/app/pages/home/index/[sid].vue`                    | 1629 行 God Component                 | Composable 拆分                   |
| **P0** | 6   | `client/app/composables/bridge.ts`                         | 1978 行 God Module，14 处 isTauri()   | Strategy + 模块拆分               |
| **P0** | 7   | 全部中间件 session_id 提取                                 | 7+ 个中间件各自实现相同逻辑           | Mixin / 工具函数                  |
| **P0** | 8   | `bridge.ts` isTauri() 分支                                 | 14 处重复运行时检测 + 传输选择        | Strategy + Factory                |
| **P1** | 9   | `output_repetition_guard.py`                               | 727 行类，混合哈希/检测/状态/钩子     | 组合模式拆分                      |
| **P1** | 10  | `multimodal_processor.py`                                  | 210 行方法，5 种媒体类型 if-elif      | Strategy dispatch                 |
| **P1** | 11  | `context_engine/core.py` search_messages                   | 247 行 God Function，3 种搜索策略     | Strategy Pattern                  |
| **P1** | 12  | `context_engine/store/core.py` add_messages                | 222 行，3 种消息类型 if-elif          | Strategy/Command                  |
| **P1** | 13  | `server/service/skill_scanner.py`                          | 827 行模块，8+ 职责                   | SRP 拆分                          |
| **P1** | 14  | `server/queue/user_input_queue.py`                         | 678 行 God class                      | Repository Pattern                |
| **P1** | 15  | 全部中间件 sync/async 钩子                                 | 每个 _impl + sync + async 三方法      | 装饰器/基类自动桥接               |
| **P1** | 16  | `state_register_mem` 全局耦合                              | 所有中间件直接依赖全局单例            | SessionState Facade + Enum key    |
| **P1** | 17  | `_send_ws` / 流驱动逻辑                                    | 3-4 处重复 WS 帧发送 + 流驱动         | Template Method                   |
| **P1** | 18  | `[sid].vue` appendStreamChunk                              | 115 行 5 路 if/else                   | Strategy Registry                 |
| **P1** | 19  | 文件读写校验模式                                           | workplace/memory/heartbeat 三处重复   | Template Method (FileStore)       |
| **P1** | 20  | `runtime/state_register.py`                                | 每次 SQLite 操作新开连接              | 连接池/Repository                 |
| **P1** | 21  | `stream_repetition_guard_wrapper.py` astream               | 200 行嵌套状态机                      | State Pattern                     |
| **P1** | 22  | `summarization.py`                                         | 493 行类，10+ 方法混合多职责          | 组件拆分                          |
| **P1** | 23  | `client/useSubagentTasks.ts`                               | 799 行 God Composable                 | 状态切片拆分                      |
| **P1** | 24  | `orchestrator.py` run_curator_review                       | 189 行 God Function                   | SRP 拆分                          |
| **P1** | 25  | `orchestrator.py` _apply_consolidation                     | 144 行 God Function，4-5 层嵌套       | SRP + Extract Method              |
| **P2** | 26  | `_read_dotenv`                                             | 2 文件完全相同                        | 提取公共工具                      |
| **P2** | 27  | `_convert_message_to_dict`                                 | 3 文件重复                            | 共享工具类                        |
| **P2** | 28  | `_resolve_model_path`                                      | 3 文件重复                            | ModelWeightResolver               |
| **P2** | 29  | `_ClassOrInstanceSchema`                                   | 2 文件重复描述符                      | 工厂函数                          |
| **P2** | 30  | `_deny_sandbox_bypass`                                     | 2 文件重复                            | Mixin                             |
| **P2** | 31  | `_tool_error` / `_args_hash`                               | 多处工具函数重复                      | 提取公共模块                      |
| **P2** | 32  | `resolveWsBaseUrl` / `closeSocket`                         | 前端 2-3 处重复                       | 提取共享模块                      |
| **P2** | 33  | 媒体文件选择逻辑                                           | image/audio/video 三重复制            | Parameterized Factory             |
| **P2** | 34  | `VITE_API_BACK_URL` 硬编码                                 | 9+ 处重复                             | Configuration Provider            |
| **P2** | 35  | 原子文件写入模式                                           | 3-4 处重复                            | 提取 atomic_write 工具            |
| **P2** | 36  | HTTP 响应辅助函数                                          | 多个 HTTP handler 重复                | 提取 http_helpers                 |
| **P2** | 37  | `_serialize_run`                                           | 2 文件重复                            | 提取共享序列化模块                |
| **P2** | 38  | JSON 列解码                                                | store/core.py 2 处重复                | 提取 decode 辅助                  |
| **P2** | 39  | ITTT/VTTT 模块级单例                                       | 与 main_llm 工厂模式不一致            | 统一工厂函数                      |
| **P2** | 40  | `RepetitionGuardWrapper` 导入私有常量                      | 跨模块私有依赖                        | 依赖倒置 + Protocol               |
| **P2** | 41  | 原始 SQL 泄漏                                              | checkpointer/message_search/store     | Repository Pattern                |
| **P2** | 42  | 原始 HTTP requests.post                                    | embed_model/reranker_model            | API Client Adapter                |
| **P2** | 43  | 原始 subprocess/Popen                                      | terminal.py/python_repl.py            | Command Executor 抽象             |
| **P2** | 44  | `handleOperate` switch                                     | 13 路 case                            | Command Registry                  |
| **P2** | 45  | WS onmessage 模式                                          | 6 处重复                              | Event Handler Registry            |
| **P2** | 46  | `badgeClass`/`statusLabel`                                 | 硬编码 if-else 映射                   | Lookup Table                      |
| **P2** | 47  | `[sid].vue` 协议泄漏                                       | 直接导入传输层类型                    | Event Aggregator                  |
| **P2** | 48  | ChatBox 构造后端 URL                                       | 展示组件含 API 形状知识               | Media URL Resolver Service        |
| **P2** | 49  | `(controller as any)`                                      | 类型安全破坏                          | Type-Safe Interface               |
| **P2** | 50  | base64 剥离逻辑                                            | 视图层 3 处 `split(',')[1]`           | Media Encoding Utility            |
| **P3** | 51  | `_build_rename_summary` diff 重复                          | report.py 2 处计算相同                | DRY                               |
| **P3** | 52  | `_skill_dir` 函数重复                                      | orchestrator.py / usage.py            | DRY                               |
| **P3** | 53  | 依赖安装逻辑重复                                           | registry.py / qq/core.py              | DRY                               |
| **P3** | 54  | curator config getter 重复                                 | config.py 6 个同构函数                | 泛型函数                          |
| **P3** | 55  | `build_reasoning_kwargs`                                   | provider if-elif 链                   | Strategy + Registry               |
| **P3** | 56  | `ToolGuardrails._evaluate`                                 | 嵌套 if-else action 决策              | Chain of Responsibility           |
| **P3** | 57  | `built_agent()`                                            | 混合事件循环/checkpointer/LLM/中间件  | Builder Pattern                   |
| **P3** | 58  | `reranker_model/core.py`                                   | FFI+numpy+HTTP+排序混合               | 分层架构                          |
| **P3** | 59  | `Summarization` 依赖 agent.tools                           | 中间件层依赖工具层                    | 依赖倒置                          |
| **P3** | 60  | `ContextEngineHook` 创建子代理                             | 中间件直接依赖 agent 构建             | Factory + DI                      |
| **P3** | 61  | `IterationBudget` 依赖私有函数                             | 跨模块 `_is_internal_completion`      | Protocol                          |
| **P3** | 62  | channels/core.py 导入时副作用                              | import 即启动线程/注册                | Explicit initialization           |
| **P3** | 63  | subagent/core.py 导入时副作用                              | import 即调度任务                     | Explicit initialization           |
| **P3** | 64  | `ws_event_processor_dict`                                  | 全局 dict 事件分发                    | Registry Pattern                  |
| **P3** | 65  | `ws_handler` 混合关注点                                    | 消息解析/分发/响应/生命周期           | Separation of Concerns            |
| **P3** | 66  | `messages.py` 模块级可变状态                               | `_pending_args`/`_pending_raw` 全局   | State Pattern                     |
| **P3** | 67  | `turn_runner.py` 延迟导入缝隙                              | 7 个 lazy import 绕循环依赖           | Dependency Injection              |
| **P3** | 68  | `state_register.py` 无公共接口                             | Mem/DB 无 Protocol                    | Interface Segregation             |
| **P3** | 69  | `runtime/core.py` 错误导入                                 | `from venv import logger`             | Bug fix                           |
| **P3** | 70  | `LocalLlamaChatModel` (auxiliary)                          | 280 行类，5+ 职责                     | Decorator 分离                    |
| **P3** | 71  | `session_search()`                                         | 150 行函数混合多职责                  | 提取类                            |
| **P3** | 72  | `ContextEngineHook._after_agent_impl`                      | 返回 5 元组裸值                       | Dataclass + NudgeCounter          |
| **P3** | 73  | `aclean_old_checkpoints`                                   | 70 行原始 SQL                         | Repository Pattern                |
| **P3** | 74  | `ChannelManager`                                           | God Class，管理生命周期/事件循环/路由 | Separation of Concerns            |
| **P3** | 75  | `handleSend`                                               | 140+ 行 5 层回调嵌套                  | State Machine + 分解              |
| **P3** | 76  | `loadSessionHistory`                                       | 91 行 3 层 Map 嵌套                   | Pre-indexing + Extract            |
| **P3** | 77  | `sendChatMessageWs`                                        | 282 行 4 层闭包                       | Extract Method + Registry         |
| **P3** | 78  | `postAgentStream`                                          | 适配器 + 流编排混合                   | Adapter + Error Classifier        |
| **P3** | 79  | IndexedDB 序列化泄漏                                       | 视图层 `JSON.parse(JSON.stringify)`   | Repository 封装                   |
| **P3** | 80  | `useSubagentTasks` 三方耦合                                | 直接依赖 bridge/db/ws                 | Repository + 统一模型             |

---

## 1. Backend: Agent & Middlewares

### 1.1 代码重复

> **✅ 完成标记（2026-09-06）**：1.1.1–1.1.10 全部落地。共享件：`models/utils.py`（`read_env_file_value` + `resolve_gguf_path`）、`models/LLMs/base_local_llama.py`（`LocalLlamaChatBase` + `LocalMultimodalLlamaChatBase`）、`agent/tools/pub_base/{schema_utils,tool_utils,sandbox_guard}.py`、`agent/middlewares/base.py`（`require_session_id` + `args_hash`）+ `agent/middlewares/mixins.py`（钩子桥接）。TDD 测试 79 个（unit/skills 15 + unit/middlewares 13+17+… + module 组），回归 145+ 通过。1.1.9 采用范围说明见 1.1.9 条目。

#### 1.1.1 `LocalLlamaChatModel` 类跨 3 文件近乎逐行复制

- **文件**: `models/LLMs/auxiliary_llm/core.py:140-419`; `models/ITTT_model/core.py:219-302`; `models/VTTT_model/core.py:225-308`
- **问题**: 三个文件各自定义几乎相同的 `LocalLlamaChatModel(BaseChatModel)` 类，包含相同的 `_ensure_client`/`_release_client`/`_generate` 方法。
- **实际差异（2026-09-05 核实）**: 三个类均非顶层定义——auxiliary 的嵌套在 `build_auxiliary_llm()` 工厂内并闭包 `temperature`；ITTT/VTTT 定义在模块级 local 分支的 else 块中。auxiliary 为纯文本模型（含 reasoning 提取、`bind_tools`、`with_structured_output`）；ITTT/VTTT 为多模态（`mmproj_path` + `Qwen25VLChatHandler` + `n_batch/n_ubatch`，无 reasoning 提取），二者仅差 VTTT 多一个 `video_url` → 文本占位分支。字段默认值亦不同（`n_ctx` 40960 vs 8192；`max_tokens` 32768 vs 4096）。
- **模式**: Template Method
- **✅ 已完成（2026-09-06）**: `models/LLMs/base_local_llama.py` 已创建（`LocalLlamaChatBase` + `LocalMultimodalLlamaChatBase`），三个模型类缩为钩子子类；行为 pin 测试 `tests/module/test_base_local_llama.py`（13 个，假 client，无 GGUF 下载）。
- **建议**: 提取 `models/LLMs/base_local_llama.py`：`LocalLlamaChatBase`（公共字段/`_release_client`/`_generate` 骨架，reasoning 提取做成 `extract_reasoning` 类开关）+ `LocalMultimodalLlamaChatBase`（`mmproj_path` + chat_handler 版 `_ensure_client`，ITTT/VTTT 共享）。子类仅覆盖 `_convert_message_to_dict`、`_ensure_client`、`_resolve_model_path` 钩子与 `_llm_type`；auxiliary 的 `bind_tools`/`with_structured_output` 保留在子类。

```python
# models/LLMs/base_local_llama.py
class LocalLlamaChatBase(BaseChatModel):
    """Base for all local GGUF chat models."""
    model_path: str = ""
    n_ctx: int = 4096

    def _generate(self, messages, stop=None, **kwargs):
        client = self._ensure_client()
        msg_dicts = [self._convert_message_to_dict(m) for m in messages]
        # ... shared generate logic ...
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def _convert_message_to_dict(self, msg: BaseMessage) -> dict: ...  # hook

# models/ITTT_model/core.py
class LocalLlamaChatModel(LocalMultimodalLlamaChatBase):
    def _convert_message_to_dict(self, msg): ...  # image_url blocks
# models/VTTT_model/core.py
class LocalLlamaChatModel(LocalMultimodalLlamaChatBase):
    def _convert_message_to_dict(self, msg): ...  # + video_url placeholder
```

#### 1.1.2 `_convert_message_to_dict` 重复

- **文件**: `auxiliary_llm/core.py:128-136`（工厂内嵌套、纯文本）; `ITTT_model/core.py:168-208`; `VTTT_model/core.py:165-215`（模块级 local 分支、多模态块转换，VTTT 多 video_url 占位）
- **模式**: 随 1.1.1 的基类一并解决——成为 `LocalLlamaChatBase._convert_message_to_dict` 方法（默认纯文本实现），多模态/视频变体在子类覆盖；不再放入 `models/utils.py` 自由函数（三种变体与各自模型强耦合）。
- **✅ 已完成（2026-09-06）**: 转换逻辑逐字保留（ITTT/VTTT 模块函数重命名为 `_convert_message_to_dict_impl`，子类方法委托），diff 验证零语义变化。

#### 1.1.3 `_resolve_model_path` 下载逻辑重复

- **文件**: `auxiliary_llm/core.py:107-126`（直接 HF 下载，无回退拷贝）; `ITTT_model/core.py:108-142` 与 `VTTT_model/core.py:105-139`（含 auxiliary 目录回退拷贝 + 独立 `_resolve_mmproj_path`，两者除闭包常量外逐行相同）
- **模式**: 提取参数化工具函数 `resolve_gguf_path(local_path, hf_repo, hf_filename, local_dir, fallback_path=None)` 放入 `models/utils.py`——三处调用点各保留一行薄包装（闭包常量作参数传入）。
- **✅ 已完成（2026-09-06）**: `models/utils.py::resolve_gguf_path` 已创建（含 auxiliary 目录回退拷贝分支），测试 4 个（本地命中/回退拷贝/HF 下载/缺 hub 报错）。

#### 1.1.4 `_read_dotenv` 完全重复

- **文件**: `models/embed_model/core.py:17-30`; `models/reranker_model/core.py:29-41`
- **问题**: 两份逐字相同。
- **修正原建议（2026-09-05）**: 原建议"统一使用 config 模块的 `load_dotenv` + `os.getenv`"会**改变行为**——函数 docstring 明确"Parse from .env file only, avoiding os.environ (to skip load_dotenv side effects)"（os.environ 可能含与 .env 不一致的导出值，且 load_dotenv 有全局副作用）。按行为保持原则：提取 `models/utils.py::read_env_file_value(key, default)`（保留 .env 文件解析语义），两个模型模块改为导入使用。
- **✅ 已完成（2026-09-06）**: 测试 10 个全过。**顺带修复发现的原有缺陷**：原实现 `\s*` 跨行匹配，`EMPTY=`（空值行）会吞掉下一行作为值——共享版改用 `[ \t]*` 修复。

#### 1.1.5 `_ClassOrInstanceSchema` 描述符重复

> **✅ 已完成（2026-09-06）**: `agent/tools/pub_base/schema_utils.py::class_or_instance_schema(parent_cls)` 工厂已创建（参数化父类，修复两份副本各自硬编码工具类的问题）；terminal.py / python_repl.py 已接线。注意 pathlib 规范化：`Path` 的 parts 不含开头的 `.`。

- **文件**: `agent/tools/terminal.py:92-109`; `agent/tools/python_repl.py:83-100`
- **模式**: 泛化为 `ClassOrInstanceSchema(parent_cls)` 工厂函数，放到 `agent/tools/pub_base/schema_utils.py`

#### 1.1.6 `_deny_sandbox_bypass` 方法重复

> **✅ 已完成（2026-09-06）**: `agent/tools/pub_base/sandbox_guard.py::SandboxGuardMixin` 已创建（含 read_policy 注入点），terminal.py / python_repl.py 已接线；既有沙箱绕过测试全过。

- **文件**: `agent/tools/terminal.py:151-172`; `agent/tools/python_repl.py:193-213`
- **模式**: Mixin — 定义 `SandboxGuardMixin`，两个工具类继承

```python
# agent/tools/pub_base/sandbox_guard.py
class SandboxGuardMixin:
    def _deny_sandbox_bypass(self, caller_scope: str, tool_name: str) -> str | None:
        """Returns error message if sandbox bypass is denied, None if allowed."""
        # shared implementation
```

#### 1.1.7 `_tool_error` 函数重复

> **✅ 已完成（2026-09-06）**: `agent/tools/pub_base/tool_utils.py::tool_error` 已创建；memory.py / message_search.py 以 `_tool_error = tool_error` 别名接线（调用点零改动）。

- **文件**: `agent/tools/memory.py:474-485`; `agent/tools/message_search.py:31-42`
- **模式**: 提取到 `agent/tools/pub_base/tool_utils.py`

#### 1.1.8 `_args_hash` 函数重复

> **✅ 已完成（2026-09-06）**: `agent/middlewares/base.py::args_hash` 共享实现已创建；tool_guardrails.py 静态方法与 humanInTheLoop/approval.py 模块函数均改为委托。测试：确定性/顺序无关/不可序列化回退/md5 长度。

- **文件**: `agent/middlewares/humanInTheLoop/approval.py:73-79`; `agent/middlewares/tool_guardrails.py:107-113`
- **模式**: 提取到公共工具模块

#### 1.1.9 中间件 sync/async 钩子模式重复

> **✅ 部分完成（2026-09-06）**：`agent/middlewares/base.py::BeforeAgentHooksMixin` / `AfterAgentHooksMixin`（由 `mixins.py` 实现并再导出）已创建并接入 **multimodal_processor / heartbeat_staleness / output_repetition_guard / iteration_budget**（四处 before/after 钩子对完全符合纯委托形态）。**不采用**并记录原因：summarization（sync/async 是两套独立 impl，非仅差 await）、context_engine（after 钩子含 nudge 分发 + 持久化等额外逻辑，sync/async 互异）、humanInTheLoop（委托目标名不同且无日志行）、全部 wrap_* 钩子族（sync handler 与 async handler 结构性不同，无法仅差 await 桥接）。原文档"sync 和 async 钩子体完全相同（仅差 await）"的断言经核实仅对 before/after 纯委托子集成立。

- **文件**: 所有中间件 (`heartbeat_staleness.py:187-209`; `iteration_budget.py`; `output_repetition_guard.py`; `multimodal_processor.py`; `summarization.py`; `context_engine/core.py`)
- **问题**: 每个中间件遵循 `_impl` + `sync_hook` + `async_hook` 三方法模式，sync 和 async 钩子体完全相同（仅差 `await`）
- **模式**: `AsyncSyncMiddleware` 基类自动生成 async 版本

```python
class AsyncSyncMiddleware(AgentMiddleware):
    """Base: subclass implements _impl only; base auto-bridges sync/async."""

    def before_agent(self, state, runtime):
        return self._before_agent_impl(state)

    async def abefore_agent(self, state, runtime):
        return self._before_agent_impl(state)

    # subclass overrides:
    def _before_agent_impl(self, state): ...
```

#### 1.1.10 `session_id` 提取逻辑重复

> **✅ 已完成（2026-09-06）**: `agent/middlewares/base.py::require_session_id(state, error_message)` 已创建——保留各中间件错误文案（"Not pass session_id" / "Xxx: session_id is required"），context_engine 保留 raise 前的 logger.error。已接入：tool_guardrails / heartbeat_staleness / iteration_budget / context_engine / summarization / multimodal_processor（6 处）。

- **文件**: 7+ 中间件各自实现 `_get_session_id`/`_sid`/`_get_session_or_raise`（`output_repetition_guard.py:157-171`; `heartbeat_staleness.py:90-94`; `iteration_budget.py:54-58`; `tool_guardrails.py:95-99`; `multimodal_processor.py:151-155`; `summarization.py:427-434`; `context_engine/core.py:132-139`）
- **模式**: `SessionStateMixin` 或工具函数 `require_session_id(state)`，放到 `agent/middlewares/base.py`

```python
# agent/middlewares/base.py
def require_session_id(state: dict[str, Any]) -> str:
    session_id = state.get("session_id", "")
    if not session_id.strip():
        raise RuntimeError("session_id is required")
    return session_id
```

---

### 1.2 God 类/函数

#### 1.2.1 `HumanInTheLoop.after_model()` — 230 行巨型方法

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

#### 1.2.2 `MultimodalProcessor._before_agent_impl()` — 210 行

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

#### 1.2.3 `OutputRepetitionGuard` — 727 行类

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

#### 1.2.4 `LocalLlamaChatModel` (auxiliary_llm) — 280 行

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

#### 1.2.5 `Summarization` — 493 行，10+ 方法

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

### 1.3 长方法与深嵌套

#### 1.3.1 `RepetitionGuardWrapper.astream()` — 200 行嵌套状态机

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

#### 1.3.2 `session_search()` — 150 行

- **文件**: `agent/tools/message_search.py:275-423`
- **问题**: 混合输入验证、FTS5 搜索、对话格式化、截断策略、并行摘要、内嵌 `_summarize_all`
- **模式**: 提取 `SessionSearcher`、`ConversationTruncator`、`SessionSummarizer` 三类

---

### 1.4 硬编码 if-else 链

#### 1.4.1 `build_reasoning_kwargs()` provider 分发

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

#### 1.4.2 `MultimodalProcessor` 媒体类型分发

- **文件**: `agent/middlewares/multimodal_processor.py:174-292`
- **问题**: 6 路 if-elif 分发 text/image_url/audio_url/audio_bytes/video_url/video_bytes
- **模式**: Strategy — 见 1.2.2

#### 1.4.3 `ContextEngineHook.after_agent` nudge 分发

- **文件**: `agent/middlewares/context_engine/core.py:297-303` (sync), `316-351` (async)
- **问题**: if need_memory and need_skill / if need_memory / if need_skill 三路分发
- **模式**: Strategy — `MemoryNudgeStrategy`、`SkillNudgeStrategy`、`CombinedNudgeStrategy`

#### 1.4.4 `ToolGuardrails._evaluate()` action 决策

- **文件**: `agent/middlewares/tool_guardrails.py:119-200`
- **问题**: 嵌套 if-else 决定 ALLOW/WARN/BLOCK/HALT，三种病理各有独立阈值
- **模式**: Chain of Responsibility — `ExactFailureDetector`、`SameToolFailureDetector`、`NoProgressDetector`，各自独立评估，最终 action 由最高优先级决定

---

### 1.5 混合关注点

#### 1.5.1 `MultimodalProcessor` — I/O + 解码 + 路径管理 + 消息操纵

- **文件**: `agent/middlewares/multimodal_processor.py`
- **问题**: 中间件同时负责 base64 解码(PIL)、HTTP 下载(urllib)、文件系统管理、magic byte 推断、消息内容操纵、历史清理
- **模式**: 分离关注点 — `MediaDownloadService`(I/O)、`MediaFormatDetector`、`MediaHintBuilder`(提示词)

#### 1.5.2 `ContextEngineHook` — prompt 管理 + 计数器 + 持久化 + 子代理

- **文件**: `agent/middlewares/context_engine/core.py`
- **问题**: 同时管理系统 prompt 加载/缓存、nudge 计数器(DB 读写)、消息持久化、HITL 拒绝修复、子代理调度
- **模式**: Facade + Service 层 — `SystemPromptService`、`NudgeCounterService`、`MessagePersistenceService`

#### 1.5.3 `built_agent()` — 事件循环 + 检查点 + LLM + 中间件

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

#### 1.5.4 `reranker_model/core.py` — FFI + numpy + HTTP + 排序

- **文件**: `models/reranker_model/core.py` (717 行)
- **问题**: 单文件混合 ctypes FFI、GGUF 二进制解析、numpy 矩阵运算、HTTP API 客户端、排序/过滤逻辑
- **模式**: 分层架构 — `gguf_parser.py`、`llama_encoder.py`、`reranker_service.py`

---

### 1.6 缺失抽象

#### 1.6.1 `state_register_mem` 全局单例直接耦合

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

#### 1.6.2 原始 SQL 泄漏

- **文件**: `checkpointer/thread_safe_checkpointer.py:196-242`; `message_search.py:224-236`; `context_engine/store/core.py`
- **模式**: Repository Pattern — `CheckpointRepository` 封装所有 SQL

#### 1.6.3 原始 HTTP 请求泄漏

- **文件**: `embed_model/core.py:131-147`; `reranker_model/core.py:589-602`
- **问题**: 直接使用 `requests.post`，无重试、无连接池、无超时抽象
- **模式**: API Client Adapter — `EmbeddingApiClient`、`RerankerApiClient`

#### 1.6.4 原始 subprocess/Popen 操作泄漏

- **文件**: `terminal.py:212-263`; `python_repl.py:103-176`
- **问题**: 直接操作 `subprocess.Popen`/`asyncio.create_subprocess_*`，手动管理进程生命周期、编码、超时
- **模式**: Command Executor 抽象 — `CommandExecutor.run(argv, env, timeout) -> ExecutionResult`

---

### 1.7 紧耦合

#### 1.7.1 `RepetitionGuardWrapper` 导入中间件私有常量

- **文件**: `agent/stream_repetition_guard_wrapper.py:55-64`
- **问题**: 直接导入 `_HALTED_KEY`、`_INTERNAL_WARNED_KEY`、`_CHAR_RUN_MIN` 等私有常量，还实例化了一个 `OutputRepetitionGuard` 内部实例
- **模式**: 依赖倒置 — 提取 `RepetitionDetectorProtocol`，Wrapper 通过构造函数注入

#### 1.7.2 `IterationBudget` 依赖 `subagent_completion_drain` 私有函数

- **文件**: `agent/middlewares/iteration_budget.py:30`
- **问题**: `from agent.middlewares.subagent_completion_drain import _is_internal_completion` — 中间件之间通过私有函数耦合
- **模式**: 提取为公共 Protocol — `CompletionMessageProtocol`

#### 1.7.3 `Summarization` 直接依赖 `agent.tools.memory`

- **文件**: `agent/middlewares/summarization.py:410-412`
- **问题**: 中间件层直接导入工具层的 `memory_store` 并调用 `load_from_disk()`，违反分层架构
- **模式**: 依赖倒置 — 定义 `MemorySnapshotProvider` 接口，由 `core.py` 注入

#### 1.7.4 `ContextEngineHook` 直接创建子代理

- **文件**: `agent/middlewares/context_engine/nudge.py:233-248`
- **问题**: nudge 模块直接导入 `build_main_llm`、`get_agent_tools`、`create_agent`
- **模式**: Factory + DI — `NudgeAgentFactory` 协议注入

#### 1.7.5 ITTT/VTTT 模块级单例 vs 工厂不一致

- **文件**: `ITTT_model/core.py:80-84`; `VTTT_model/core.py:79-81`; 对比 `main_llm.py:90-93`
- **问题**: ITTT/VTTT 使用模块级 `init_chat_model(...)` 或 `LocalLlamaChatModel()`，而 main_llm/auxiliary_llm 使用工厂函数 `build_main_llm()`
- **模式**: 统一使用 `build_*()` 工厂函数模式

---

## 2. Backend: Server & Service & Runtime

### 2.1 代码重复

> **✅ 完成标记（2026-09-06）**：2.1.1–2.1.8 全部落地。共享件：`server/service/stream_dispatch.py`（`StreamTurn` 模板引擎 + pending-args 状态/helpers）、`server/service/stream_driver.py`（`StreamDriver` 模板）、`server/utils/ws_helpers.py`（`send_ws_json`）、`server/utils/atomic_io.py`（`atomic_write_text`）、`server/trigger/http/helpers.py`（响应辅助）、`server/trigger/subagent_serialize.py`（`serialize_run`）、`server/service/file_store.py`（`FileStore` 模板）、`server/trigger/ws/push_channel.py`（`WSPushChannel`）。行为 pin 测试 122 个（tests/unit/server 7 文件 + tests/unit/service + tests/unit/channels 2 文件 + tests/unit/context_engine 2 文件）。回归：tests/unit 1742+ 通过；tests/integration+system+module 795 通过；tests/regression 74 通过。两个既有 flaky（`test_store_timestamp_ordering.py::test_display_stamp_is_14_chars` 的 `_last_turn_ms` 跨测试泄漏、`test_ws_queue_integration.py::test_hitl_interrupt_sets_hitl_pending` 的断言竞态）与两个环境性收集失败（test_rag_anything_integration / test_snkv_storage 的网络依赖）均在未改动的 HEAD 上复现，与本次改动无关。各条目差异说明见对应小节。

#### 2.1.1 `async_generate` / `resume_agent` 流分发循环完整复制

> **✅ 已完成（2026-09-06）**：`server/service/stream_dispatch.py` 创建——`StreamTurn` 模板方法 + 从 messages.py 迁入的 pending-args 状态机（`_pending_args`/`_pending_raw` + accumulate/get/pop/clear）与 `_reasoning_delta`/`_normalize_text`（messages.py 以别名 re-import，模块状态对象恒等，既有 `tests/unit/test_messages_pending_args.py` 零改动通过）。`messages.py` 中 `_GenerateTurn`/`_ResumeTurn` 仅覆盖真实差异钩子：`_create_source`（astream vs `Command(resume=...)` / ainvoke）、`_extra_messages_frames`（resume 独有的 HITL 拒绝 tool_result 路径）、`_note_tool_start`（generate 独有的 ai_text 工具标注）、`_invoke_frames`（非流分支）、`_final_frames`（generate 独有 meta chunk）、`_on_cancelled`/`_on_heartbeat_timeout`（generate 独有中断标记）、`_cleanup`（generate 独有 aclose）。公共 API 签名与帧序逐帧保持；行为 pin 测试 `tests/unit/server/test_stream_dispatch.py`（24 个）。

- **文件**: `server/service/messages.py:328-511` vs `711-859`
- **问题**: updates-mode tool 处理、messages-mode tool_call 跟踪 + args 累积、text/reasoning 发射、finally 清理——几乎逐行复制
- **模式**: Template Method + StreamDispatcher

```python
class TurnRunner(ABC):
    @abstractmethod
    async def _create_generator(self, session_id, agent, config): ...
    async def run(self, session_id):
        agent = await built_agent(force_rebuild=True)
        generator = await self._create_generator(session_id, agent, ...)
        dispatcher = StreamDispatcher(session_id)
        async for chunk in dispatcher.dispatch(generator):
            yield chunk

class GenerateTurn(TurnRunner):
    async def _create_generator(self, ...):
        return agent.astream(input_dict, stream_mode=["messages","updates"])

class ResumeTurn(TurnRunner):
    async def _create_generator(self, ...):
        return agent.astream(Command(resume=...), stream_mode=["messages","updates"])
```

#### 2.1.2 `_send_ws` 函数跨多模块重复

> **✅ 已完成（2026-09-06）**：`server/utils/ws_helpers.py::send_ws_json` 已创建——None-socket 跳过 + 异常吞掉告警 + `ensure_ascii`/`warn_prefix` 参数化，三处调用点各自保留原日志文案（"Agent WS send failed" / "TurnRunner: ws send failed" / "auto_turn: websocket send failed"）与 auto_turn 的 `ensure_ascii=False`。三模块 `_send_ws` 名字保留为薄委托。

- **文件**: `server/trigger/ws/messages.py:33`; `server/service/turn_runner.py:129`; `server/service/auto_turn.py:63`
- **模式**: 提取到 `server/utils/ws_helpers.py`

#### 2.1.3 流驱动逻辑在 3 处重复

> **✅ 已完成（2026-09-06）**：`server/service/stream_driver.py::StreamDriver` 模板已创建（仅 stdlib+loguru，turn_runner 可安全 eager import）。三处各自保留 8-15 行子类仅覆盖站点差异：send_frame（各自 _send_ws）、check_interrupt（各自 import 缝隙/模块全局，测试 monkeypatch 点不变）、apply_hitl_pending（auto_turn 保持不置位）、done 帧缺省（ws 路径 ""/0 vs auto_turn null —— 原线上形状）、log 钩子（原文案与级别）、on_finish（ws 释放任务槽 + on_turn_finished(claim_row_id)；executor 无（cleanup 在 execute 的 finally）；auto_turn on_turn_finished(bare)）。行为 pin 测试 `tests/unit/server/test_stream_driver.py`（13 个）。

- **文件**: `ws/messages.py::_run_stream:41-138`; `auto_turn.py::_drive_turn:161-208`; `turn_runner.py::WsTurnExecutor._drive:339-402`
- **问题**: 三者都做"遍历 async_generate → 转发 chunk → 检查 HITL → 发 done/error/stopped → on_turn_finished"
- **模式**: Template Method — `StreamDriver` 基类

#### 2.1.4 WS push 广播模式重复

> **✅ 已完成（2026-09-06）**：`server/trigger/ws/push_channel.py::WSPushChannel` 已创建（锁保护订阅集 + 有界 deque 丢旧 + sender task + serve() 处理骨架：注册 → ready 帧 → receive 循环 → 反注册/取消）。logs.py / subagent_ws.py 各自保留序列化与幂等注册钩子（loguru sink / subagent hooks）及日志文案。行为 pin 测试 `tests/unit/server/test_push_channel.py`（8 个）。

- **文件**: `server/trigger/ws/logs.py` 和 `server/trigger/ws/subagent_ws.py`
- **问题**: 相同的 `_subscribers`/`_subscribers_lock`/`_pending`/`_MAX_PENDING`/`_broadcast`/`_sender`/`_ensure_*_registered`
- **模式**: Observer — `WSPushChannel` 基类

#### 2.1.5 `_serialize_run` 和 `_PUBLIC_FIELDS` 重复

> **✅ 已完成（2026-09-06）**：`server/trigger/subagent_serialize.py` 已创建（`PUBLIC_FIELDS` + `serialize_run`），http/subagent.py 与 ws/subagent_ws.py 以原名别名引用（调用点零改动）。

- **文件**: `server/trigger/http/subagent.py:33-61`; `server/trigger/ws/subagent_ws.py:38-88`
- **模式**: 提取到共享序列化模块

#### 2.1.6 HTTP 响应辅助函数重复

> **✅ 已完成（2026-09-06）**：`server/trigger/http/helpers.py` 已创建（`to_text_response`/`ok`/`bad_request`/`not_found`/`read_body`），subagent.py / cron.py 以原私有名别名引用，skills.py `_json_response` 改为委托。测试 `tests/unit/server/test_http_helpers.py`（11 个，含别名恒等断言）。

- **文件**: `server/trigger/http/subagent.py:69-105`; `cron.py:28-122`; `skills.py:446-451`
- **问题**: 相同的 `_to_text_response`/`_ok`/`_bad_request`/`_not_found`/`_read_body` 模式
- **模式**: 提取 `server/trigger/http/helpers.py`

#### 2.1.7 文件读写校验模式重复

> **✅ 已完成（2026-09-06）**：`server/service/file_store.py::FileStore` 模板已创建。三服务保留原公共函数名并委托 store 实例。语义逐项保持：错误文案按 `error_noun`（"file"/"memory file"/"heartbeat file"）；校验与写按条目交错（无效后条目不回滚先写条目——原行为）；workplace 的 update 走 `update_files`（合并后全量重校验——原行为）；memory 的 write 走 `merge_files`（仅校验提供条目、不重校验存量——原行为）；heartbeat 的长度预算按任务文本计（`_content_length` 钩子）；`_file_path` 调用时解析（monkeypatch 路径仍生效）。测试 `tests/unit/service/test_file_store.py`（18 个）。

- **文件**: `server/service/workplace.py`; `memory.py`; `heartbeat.py`
- **问题**: 三者都有相同的 `read_*_file()` → 遍历允许文件名 + open/read，`write_*_file()` → 验证文件名/类型/非空/长度 + write
- **模式**: Template Method — `FileStore` 基类

```python
class FileStore(ABC):
    allowed_files: list[str]
    max_length: int

    def read_files(self) -> dict[str, str]:
        return {name: self._read(name) for name in self.allowed_files if self._exists(name)}

    def write_files(self, file_to_content: dict[str, str]):
        self._validate(file_to_content)
        for name, content in file_to_content.items():
            self._write(name, content)

    def update_files(self, file_to_content: dict[str, str]):
        existing = self.read_files()
        existing.update(file_to_content)
        self.write_files(existing)
```

#### 2.1.8 原子文件写入模式重复

> **✅ 已完成（2026-09-06）**：`server/utils/atomic_io.py::atomic_write_text` 已创建（同目录 tempfile + os.replace + 可选 fsync + 失败清理后重抛），channels.py（fsync=True）/ skill_scanner.py（fail-open 告警）/ skills.py（重抛）已接线。**修正原清单（2026-09-06）**：`env.py::write_env_file` 并非该模式——它是 `.bak` 备份 + 原地整写（无 tempfile/os.replace），改造会改变其崩溃/备份语义，故不纳入（同 1.1.4 的行为保持原则）。临时文件命名统一为 `.{name}.tmp.*`（瞬态文件，无行为影响）。测试 `tests/unit/server/test_atomic_io.py`（6 个）。

- **文件**: `channels.py::_save_channel_config:22-50`; `skill_scanner.py::_store_scan_cache:658-700`; `skills.py::_write_skills_state:171-188`; `env.py::write_env_file:116-179`
- **模式**: 提取 `atomic_write(path, content)` 工具函数

---

### 2.2 God 类/函数

#### 2.2.1 `async_generate` — 300 行 God Function

- **文件**: `server/service/messages.py:295-599`
- **问题**: 10+ 职责（Agent 构建、多模态组装、流模式选择、chunk 处理、工具调用跟踪、参数累积、元数据捕获、文本/推理输出、取消/超时/异常处理、状态管理、中断标记写入、生成器清理）
- **模式**: Strategy + Chain of Responsibility + State — 详见 Summary Table #2

#### 2.2.2 `skill_scanner.py` — 827 行 God 模块

- **文件**: `server/service/skill_scanner.py`
- **问题**: CLI 后端扫描、Python API 扫描、可用性探测、结果标准化、序列化、内容寻址缓存、策略决策、环境变量管理
- **模式**: SRP 拆分 — `ScannerBackend`、`ScanResultNormalizer`、`ScanCache`、`ScanPolicy`

#### 2.2.3 `user_input_queue.py` — 678 行 God class

- **文件**: `server/queue/user_input_queue.py`
- **问题**: DB 初始化、连接管理、事务管理、入队去重、claim 原子性、终端转换、活跃计数、崩溃恢复、客户端 msg_id 查找
- **模式**: Repository Pattern — `ConnectionManager`、`SchemaManager`、`QueueRepository`

#### 2.2.4 `skills.py` — 540 行 handler 混合关注点

- **文件**: `server/trigger/http/skills.py`
- **问题**: 单文件处理技能列表、文件树、读取、状态读写、快照重建、上传扫描、切换、删除、pin
- **模式**: 按资源拆分 controller + Command Pattern

---

### 2.3 硬编码 if-else / 全局状态

#### 2.3.1 `ws_event_processor_dict` — 全局 dict 事件分发

- **文件**: `server/trigger/core.py:41-75`
- **问题**: 全局 dict `ws_event_processor_dict` 映射事件名到处理器，无类型安全、无生命周期管理
- **模式**: Registry Pattern — 正式化为 `EventDispatcher` 类

#### 2.3.2 `messages.py` 模块级可变状态

- **文件**: `server/service/messages.py:26-29`
- **问题**: `_pending_args`/`_pending_raw` 进程全局 dict，隐式全局状态，难测试、难推理
- **模式**: State Pattern / Session-scoped context object

#### 2.3.3 `turn_runner.py` 延迟导入缝隙

- **文件**: `server/service/turn_runner.py` (7 个 lazy import 函数: `_iqs`, `get_registry`, `get_websocket_by_session_id`, `set_hitl_pending`, `async_generate`, `get_pending_interrupt`, `_get_active_tasks`)
- **问题**: 纯为绕循环依赖而存在
- **模式**: Dependency Injection

#### 2.3.4 `runtime/state_register.py` — 无公共接口 + 每次新开连接

- **文件**: `runtime/state_register.py`
- **问题**: `StateRegisterMem` 和 `StateRegisterDB` 方法签名相同但无 Protocol；DB 每次操作 `sqlite3.connect()`
- **模式**: Interface Segregation (Protocol) + 连接池

#### 2.3.5 `runtime/core.py` 错误导入

- **文件**: `runtime/core.py:1`
- **问题**: `from venv import logger` — 应为 `from loguru import logger`（疑似 bug）
- **模式**: Bug fix

#### 2.3.6 导入时副作用

- **文件**: `server/trigger/channels/core.py:293-346`（import 即启动线程 + 注册 consumer）；`server/trigger/subagent/core.py:48`（import 即 `_schedule_startup()` 调度任务）
- **模式**: Explicit initialization — `setup()` 函数

---

## 3. Backend: Channels / Bus / Context Engine

### 3.1 代码重复

> **✅ 完成标记（2026-09-06）**：3.1.1–3.1.7 全部落地。共享件：`channels/deps.py`（`install_requirements`）、`context_engine/curator/helpers.py::_skill_dir`（规范实现）、`context_engine/curator/report.py`（`_compute_diff` + `_classify_archived`）、`context_engine/curator/classify.py::_parse_skill_manage_args`、`context_engine/curator/config.py::_get_config_value`、`context_engine/store/core.py::_decode_json_columns`、`channels/manager.py::_consume_loop`。行为 pin 测试 30 个（tests/unit/channels 2 文件 + tests/unit/context_engine 2 文件）。回归同 2.1 完成标记（1743 + 795 + 74 通过，环境性失败与 HEAD 一致）。

#### 3.1.1 ChannelManager 消费循环重复

> **✅ 已完成（2026-09-06）**：泛化为 `_consume_loop(direction)`；`_inbound_consume_loop`/`_outbound_consume_loop` 保留为薄委托（外部引用兼容）。入/出站 debug 日志字段与 warning 文案逐字保持（loguru `{}` 与 f-string 输出等价，统一为 f-string）；consumer 仍每轮循环内解析（原行为）。测试 `tests/unit/channels/test_consume_loop.py`（4 个）。

- **文件**: `channels/manager.py:32-46` (`_inbound_consume_loop`) vs `48-63` (`_outbound_consume_loop`)
- **问题**: 近乎相同，仅 bus 方法/消息类型/consumer 不同
- **模式**: Template Method — 泛化 `_consume_loop(direction)`

- **文件**: `channels/manager.py:32-46` (`_inbound_consume_loop`) vs `48-63` (`_outbound_consume_loop`)
- **问题**: 近乎相同，仅 bus 方法/消息类型/consumer 不同
- **模式**: Template Method — 泛化 `_consume_loop(direction)`

#### 3.1.2 curator report.py diff 计算重复

> **✅ 已完成（2026-09-06）**：提取 `_compute_diff(before_names, after_report)`（after_names/removed/added）与 `_classify_archived(...)`（heuristic + model block + absorbed_into 声明的完整分类管道），`_build_rename_summary` 与 `_write_run_report` 共用；run.json 内容逐字段一致。测试并入 `tests/unit/context_engine/test_curator_shared.py`。

- **文件**: `context_engine/curator/report.py:24-25` (`_build_rename_summary`) vs `93-96` (`_write_run_report`)
- **问题**: 两处独立计算 `after_names`/`removed`/`added` 并调用相同的分类管道
- **模式**: DRY — 提取 `_compute_diff(before, after)` 公共函数

#### 3.1.3 `_skill_dir` 函数重复

> **✅ 已完成（2026-09-06）**：规范实现迁入 `context_engine/curator/helpers.py::_skill_dir`；`usage._skill_dir`（`curator/__init__.py` 再导出路径不变）与 `orchestrator._resolve_skill_dir` 均为同对象别名。扁平/嵌套/缺失/SKILL.md 缺失四种解析路径 pin 测试并入 `tests/unit/context_engine/test_curator_shared.py`。

- **文件**: `context_engine/curator/orchestrator.py:13-31`; `context_engine/curator/usage.py:20-41`
- **模式**: DRY — 提取到公共模块

#### 3.1.4 依赖安装逻辑重复

> **✅ 已完成（2026-09-06）**：`channels/deps.py::install_requirements` 已创建（uv 优先 / pip 回退 / 120s 超时 / 成功后 invalidate_caches），registry 与 qq 插件各自保留预检、日志文案与失败策略（registry：直接失败；qq：cooldown 记账 + 无 requirements.txt 返回 False——原差异保持）。测试 `tests/unit/channels/test_deps.py`（8 个，含 qq 插件按原加载方式 spec 加载验证）。

- **文件**: `channels/registry.py:27-64`; `plugins/channels/qq/core.py:74-122`
- **问题**: 相同的 requirements.txt 检查 + uv/pip 选择 + subprocess + timeout + invalidate_caches
- **模式**: DRY — 提取 `ensure_deps(requirements_path)`

#### 3.1.5 JSON 列解码重复

> **✅ 已完成（2026-09-06）**：提取 `_decode_json_columns(row)`（content/tool_calls/images/audios/videos 五列 + 弹出 ts_ms），`get_turns_by_turn_num_scope` 与 `get_history_by_turn_page` 共用；测试 `tests/unit/context_engine/test_store_decode.py`（5 个，含真实 SQLite 端到端轮次查询）。

- **文件**: `context_engine/store/core.py:296-309` 和 `365-378`
- **问题**: 相同的 `json.loads` 对 content/tool_calls/images/audios/videos 列的解码块
- **模式**: 提取 `_decode_json_columns(row)` 辅助函数

#### 3.1.6 tool_call 参数解析重复

> **✅ 已完成（2026-09-06）**：提取 `classify.py::_parse_skill_manage_args(tool_calls, *, keep_raw_on_error)`——分类器以 `keep_raw_on_error=True` 保留 `{"_raw": ...}` 证据（原行为），声明提取器跳过解析失败（原行为）。

- **文件**: `context_engine/curator/classify.py:16-30` 和 `128-139`
- **模式**: 提取 `parse_skill_manage_args(tool_calls)` 辅助函数

#### 3.1.7 curator config getter 重复

> **✅ 已完成（2026-09-06）**：6 个 getter 收敛为 `_get_config_value(key, type_, default)`——bool 强转不 try（原 bool() 不捕获）；int/float 捕获 (TypeError, ValueError) 回落默认（如 yaml 值为 null）；is_enabled 缺省 True、get_consolidate 缺省 DEFAULT_CONSOLIDATE。测试并入 `tests/unit/context_engine/test_curator_shared.py`。

- **文件**: `context_engine/curator/config.py:43-76`（6 个同构函数）
- **模式**: 泛型函数 `get_config_value(key, type_, default)`

---

### 3.2 God 类/函数

#### 3.2.1 `orchestrator.py::run_curator_review` — 189 行 God Function

- **文件**: `context_engine/curator/orchestrator.py:119-307`
- **问题**: dry_run 检查、自动转换、摘要构建、状态加载/保存、报告构建、LLM 调用、重命名摘要、报告写入、回调、结果返回——7+ 职责
- **模式**: SRP 拆分

#### 3.2.2 `orchestrator.py::_apply_consolidation` — 144 行 God Function

- **文件**: `context_engine/curator/orchestrator.py:512-655`
- **问题**: LLM 输出解析、umbrella 查找/创建、源内容读取、文件清单、LLM 生成 umbrella、创建技能、文件迁移、删除、prune、刷新——6+ 职责，4-5 层嵌套
- **模式**: SRP + Extract Method

#### 3.2.3 `context_engine/core.py::search_messages` — 247 行 God Function

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

#### 3.2.4 `store/core.py::add_messages` — 222 行 God Function

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

#### 3.2.5 `ChannelManager` — God Class

- **文件**: `channels/manager.py`
- **问题**: 生命周期管理、事件循环管理、消息路由、consumer 注册、配置加载、插件发现、allow-from 验证
- **模式**: Separation of Concerns

---

### 3.3 混合关注点 / 缺失抽象

#### 3.3.1 `context_engine/core.py` 模块级混合

- **问题**: 数据库访问(`_db`, `_lock`)、FTS5 语法(`_sanitize_fts5_query`)、内容编解码、业务逻辑、搜索结果组装全在一个模块
- **模式**: 分层架构 + Repository Pattern

#### 3.3.2 原始 SQL 泄漏

- **文件**: `context_engine/core.py` (trigram SQL, LIKE SQL, FTS SQL, context SQL); `store/core.py` (INSERT/SELECT/DELETE); `store/db.py` (DDL)
- **模式**: Repository Pattern — `MessageRepository`、`SearchRepository`

#### 3.3.3 FTS5 查询语法泄漏

- **文件**: `context_engine/core.py` — `_sanitize_fts5_query` 50 行处理 FTS5 语法
- **模式**: `FTSQuerySanitizer` 抽象

#### 3.3.4 LangChain 消息类型泄漏到 store

- **文件**: `context_engine/store/core.py::add_messages` — 直接访问 `m.type`/`m.response_metadata`/`m.usage_metadata`/`m.additional_kwargs`/`m.tool_calls`
- **模式**: Adapter/Mapper — `MessageRowMapper`

#### 3.3.5 模块级全局状态

- **文件**: `context_engine/core.py` (`_db`, `_lock`); `store/core.py` (`_db`, `_turn_assign_lock`); `curator/__init__.py` (`_idle_for_seconds`, `_curator_thread`); `plugins/channels/qq/core.py` (`_consecutive_install_failures`, `_cooldown_until`)
- **模式**: Dependency Injection

#### 3.3.6 `curator/__init__.py` 重导出私有函数

- **问题**: re-export 几十个 `_` 前缀的"私有"函数，公共 API 不清晰
- **模式**: Facade — 定义清晰公共接口

---

> 跨横切模式总结与重构路线图见 [DESIGN_PATTERN_REFACTORING_FRONTEND.md](./DESIGN_PATTERN_REFACTORING_FRONTEND.md)
