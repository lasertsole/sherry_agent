# Summarization 重构变更记录 — Part 2：中间件重写与集成

> 本文档记录 summarization 中间件、agent core、测试和环境的变更（第 9-16 节）。
> 配置与消息处理工具部分见 [Part 1](SUMMARIZATION_CHANGES_PART1.md)。

---

## 目录

9. [agent/middlewares/summarization.py](#9-agentmiddlewaressummarizationpy)
10. [agent/core.py](#10-agentcorepy)
11. [agent/tools/subagent/spawn/core.py](#11-agenttoolssubagentspawncorepy)
12. [tests/unit/conftest.py（修改）](#12-testsunitconftestpy修改)
13. [tests/test_summarization_comprehensive.py（新建）](#13-teststest_summarization_comprehensivepy新建)
14. [tests/test_e2e_summarization.py（新建）](#14-teststest_e2e_summarizationpy新建)
15. [.env（配置）](#15-env配置)
16. [环境版本升级](#16-环境版本升级)

---

## 9. agent/middlewares/summarization.py

### 变更 9.1：import 部分

#### 旧代码

```python
import math
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from langchain.agents.middleware.types import ResponseT
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from typing import Any, Callable, Awaitable, Sequence, cast
from langchain.agents.middleware import (
    SummarizationMiddleware,
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    SystemMessage,
    AIMessage,
    HumanMessage,
    ToolMessage,
    RemoveMessage,
)
```

#### 新代码

```python
import math
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from langchain.agents.middleware.types import ResponseT
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from typing import Any, Callable, Awaitable, Sequence, cast
from langchain.agents.middleware import (
    SummarizationMiddleware,
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    SystemMessage,
    AIMessage,
    HumanMessage,
    ToolMessage,
    RemoveMessage,
)
from config.num import (
    PREEMPTIVE_TRUNCATE_RATIO,
    COMPRESSION_TRIGGER_RATIO,
)
```

### 变更 9.2：常量部分

#### 旧代码

```python
_LAST_TURN_RATIO_THRESHOLD = 0.5
_LAST_USER_QUESTION_KEY = "summarization_last_user_question"

_MAX_COMPRESSION_ATTEMPTS = 3
_INEFFECTIVE_THRESHOLD = 2
_MIN_EFFECTIVENESS_PCT = 0.05
_MAX_CONTENT_CHARS = 8000
_CONTENT_HEAD_RATIO = 0.3
_CONTENT_TAIL_RATIO = 0.3
_OMISSION_MARKER = "...[omitted {omitted} chars]..."
_COMPRESSION_COUNT_KEY = "summarization_compression_count"
_COMPRESSION_INEFFECTIVE_KEY = "summarization_compression_ineffective"
_COMPRESSION_LAST_TOKENS_KEY = "summarization_compression_last_tokens"
```

#### 新代码

```python
_LAST_TURN_RATIO_THRESHOLD = 0.5
_LAST_USER_QUESTION_KEY = "summarization_last_user_question"

_MAX_COMPRESSION_ATTEMPTS = 3
_INEFFECTIVE_THRESHOLD = 2
_MIN_EFFECTIVENESS_PCT = 0.05
_MAX_CONTENT_CHARS = 8000
_CONTENT_HEAD_RATIO = 0.3
_CONTENT_TAIL_RATIO = 0.3
_OMISSION_MARKER = "...[omitted {omitted} chars]..."
_COMPRESSION_COUNT_KEY = "summarization_compression_count"
_COMPRESSION_INEFFECTIVE_KEY = "summarization_compression_ineffective"
_COMPRESSION_LAST_TOKENS_KEY = "summarization_compression_last_tokens"
_PREEMPTIVE_TRUNCATE_MAX_CHARS = 2000
_PREEMPTIVE_PROTECTED_TOOLS = frozenset({"memory", "skill_view", "skill_list"})
```

### 变更 9.3：**init** 方法

#### 旧代码

```python
class Summarization(SummarizationMiddleware):
    def __init__(self, need_update_system_prompt: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._need_update_system_prompt: bool = need_update_system_prompt
        self._compress_last_turn: bool = False
```

#### 新代码

```python
class Summarization(SummarizationMiddleware):
    def __init__(
        self,
        need_update_system_prompt: bool = False,
        main_llm_context_window: int | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._need_update_system_prompt: bool = need_update_system_prompt
        self._compress_last_turn: bool = False
        self._main_llm_context_window: int | None = main_llm_context_window

    # ------------------------------------------------------------------
    # Preemptive trigger: check token pressure BEFORE sending to model
    # ------------------------------------------------------------------

    def _get_reported_tokens(self, messages: list[AnyMessage]) -> int:
        """Get total_tokens from last AIMessage.usage_metadata (real API count)."""
        last_ai = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai and last_ai.usage_metadata:
            return int(last_ai.usage_metadata.get("total_tokens", 0))
        return 0

    def _preemptive_check(
        self, messages: list[AnyMessage], session_id: str
    ) -> str | None:
        """Pre-prompt token pressure estimation.

        Returns:
            None — prompt fits, no action needed
            'truncate_only' — pressure moderate, preemptively truncate
                              large tool outputs (no LLM call)
            'compact' — pressure critical, full compression needed
        """
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            ctx_window = self._get_profile_limits()
            if not ctx_window or ctx_window <= 0:
                return None

        local_est = self._estimate_tokens(messages)
        reported = self._get_reported_tokens(messages)
        effective = max(local_est, reported) if reported > 0 else local_est

        pressure = effective / ctx_window

        if pressure >= COMPRESSION_TRIGGER_RATIO:
            return "compact"
        if pressure >= PREEMPTIVE_TRUNCATE_RATIO:
            return "truncate_only"
        return None

    def _preemptive_truncate(
        self, messages: list[BaseMessage], session_id: str
    ) -> list[BaseMessage]:
        """Truncate large tool outputs preemptively (no LLM call, very fast).

        Walks backward, truncates each oversized ToolMessage to head+tail.
        Skips protected tools. Idempotent: already-truncated messages are
        short and won't be re-truncated.
        """
        result: list[BaseMessage] = []
        truncated_count = 0

        for m in messages:
            if isinstance(m, ToolMessage):
                tc_id = getattr(m, "tool_call_id", "")
                tool_name = self._find_tool_name(messages, m, tc_id)
                if tool_name in _PREEMPTIVE_PROTECTED_TOOLS:
                    result.append(m)
                    continue
                content = str(getattr(m, "content", ""))
                if len(content) > _PREEMPTIVE_TRUNCATE_MAX_CHARS:
                    head = content[: int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * _CONTENT_HEAD_RATIO)]
                    tail = content[-int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * _CONTENT_TAIL_RATIO):]
                    omitted = len(content) - len(head) - len(tail)
                    truncated = f"{head}{_OMISSION_MARKER.format(omitted=omitted)}{tail}"
                    result.append(m.model_copy(update={"content": truncated}))
                    truncated_count += 1
                else:
                    result.append(m)
            else:
                result.append(m)

        if truncated_count > 0:
            logger.debug(
                "Preemptive truncation: truncated {} tool outputs, session={}",
                truncated_count,
                session_id,
            )
        return result

    @staticmethod
    def _find_tool_name(
        messages: list[BaseMessage], tool_msg: ToolMessage, tc_id: str
    ) -> str:
        """Find the tool name for a ToolMessage by looking up the AIMessage that issued the call."""
        if not tc_id:
            return ""
        idx = messages.index(tool_msg)
        for i in range(idx - 1, -1, -1):
            m = messages[i]
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                for tc in m.tool_calls:
                    if tc.get("id") == tc_id:
                        return tc.get("name", "")
        return ""
```

### 变更 9.4：新增 `_should_summarize_based_on_reported_tokens` 覆写

#### 旧代码

无此覆写方法（使用基类实现）。

基类原始实现（位于 `langchain/agents/middleware/summarization.py:368-385`）：

```python
def _should_summarize_based_on_reported_tokens(
    self, messages: list[AnyMessage], threshold: float
) -> bool:
    last_ai_message = next(
        (msg for msg in reversed(messages) if isinstance(msg, AIMessage)),
        None,
    )
    if isinstance(last_ai_message, AIMessage):
        usage = last_ai_message.usage_metadata
        if usage is not None:
            reported = usage.get("total_tokens", -1)
            if reported and reported >= threshold:
                message_provider = (
                    last_ai_message.usage_metadata.get("provider", "")
                    if isinstance(last_ai_message.usage_metadata, dict)
                    else ""
                )
                # BUG: 检查 last_ai_message 的 provider 是否等于 self.model（辅助 LLM）的 provider
                # 但 last_ai_message 来自主 LLM，provider 通常不匹配，导致此路径永远 False
                and message_provider == self.model._get_ls_params().get("ls_provider")
                ...
```

#### 新代码

```python
    @override
    def _should_summarize_based_on_reported_tokens(
        self, messages: list[AnyMessage], threshold: float
    ) -> bool:
        """Override: don't check provider match.

        The base class checks if the last AIMessage's provider matches
        self.model (the auxiliary/summary LLM) provider. But the last
        AIMessage was generated by the MAIN LLM, which may have a different
        provider. We just check if reported total_tokens >= threshold.
        """
        last_ai_message = next(
            (msg for msg in reversed(messages) if isinstance(msg, AIMessage)),
            None,
        )
        if isinstance(last_ai_message, AIMessage):
            usage = last_ai_message.usage_metadata
            if usage is not None:
                reported = usage.get("total_tokens", -1)
                if reported and reported >= threshold:
                    return True
        return False
```

### 变更 9.5：`wrap_model_call` 方法

#### 旧代码

```python
    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} wrap_model_call hook fired", type(self).__name__)
        session_id = self._get_session_or_raise(request.state)
        self._check_last_turn_ratio(request.state.get("messages", []), session_id)
        res: dict[str, Any] | None = super().before_model(
            request.state, cast("Runtime[None]", request.runtime)
        )
        overridden = self._wrap_model_call_impl(request, res, session_id)
        return handler(overridden if overridden is not None else request)
```

#### 新代码

```python
    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} wrap_model_call hook fired", type(self).__name__)
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        action = self._preemptive_check(messages, session_id)
        if action == "truncate_only":
            truncated = self._preemptive_truncate(messages, session_id)
            request = request.override(
                messages=cast("list[AnyMessage]", truncated)
            )

        res: dict[str, Any] | None = super().before_model(
            request.state, cast("Runtime[None]", request.runtime)
        )
        overridden = self._wrap_model_call_impl(request, res, session_id)
        return handler(overridden if overridden is not None else request)
```

### 变更 9.6：`awrap_model_call` 方法

#### 旧代码

```python
    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        session_id = self._get_session_or_raise(request.state)
        self._check_last_turn_ratio(request.state.get("messages", []), session_id)
        res: dict[str, Any] | None = await super().abefore_model(
            request.state, cast("Runtime[None]", request.runtime)
        )
        overridden = self._wrap_model_call_impl(request, res, session_id)
        return await handler(overridden if overridden is not None else request)
```

#### 新代码

```python
    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        action = self._preemptive_check(messages, session_id)
        if action == "truncate_only":
            truncated = self._preemptive_truncate(messages, session_id)
            request = request.override(
                messages=cast("list[AnyMessage]", truncated)
            )

        res: dict[str, Any] | None = await super().abefore_model(
            request.state, cast("Runtime[None]", request.runtime)
        )
        overridden = self._wrap_model_call_impl(request, res, session_id)
        return await handler(overridden if overridden is not None else request)
```

### 变更说明（summarization.py 整体）

| 变更点                                            | 解决的问题                                                                                            |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| 新增 `main_llm_context_window` 参数               | 让中间件知道主 LLM 的上下文窗口大小，用于计算 token 压力                                              |
| 新增 `_get_reported_tokens`                       | 从 `usage_metadata` 提取 API 返回的真实 token 数                                                      |
| 新增 `_preemptive_check`                          | 在 prompt 发给 LLM 之前估算 token 压力，路由到 None/truncate_only/compact                             |
| 新增 `_preemptive_truncate`                       | 无 LLM 调用、快速截断大型工具输出（head+tail），幂等                                                  |
| 新增 `_find_tool_name`                            | 通过 `tool_call_id` 反查 AIMessage 获取工具名                                                         |
| 覆写 `_should_summarize_based_on_reported_tokens` | **修复 P7**：基类检查 provider 匹配，但主 LLM 和辅助 LLM provider 不同导致 reported tokens 永远不触发 |
| 修改 `wrap_model_call`/`awrap_model_call`         | 在调用 `super().before_model()` 之前加入抢占式检查和截断                                              |

### 变更 9.7：Phase 2 — 完全重写（继承 `AgentMiddleware`，放弃 `SummarizationMiddleware`）

#### 旧代码（Phase 1 修改后状态，约 649 行）

> Phase 1 修改后的文件仍继承 `SummarizationMiddleware`，依赖基类的 `before_model`/`abefore_model` 做压缩判断和消息替换。各变更片段见上方 9.1–9.6 新代码块。关键结构如下：

```python
class Summarization(SummarizationMiddleware):
    """继承 SummarizationMiddleware，覆写部分方法。"""

    def __init__(
        self,
        need_update_system_prompt: bool = False,
        main_llm_context_window: int | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._need_update_system_prompt: bool = need_update_system_prompt
        self._compress_last_turn: bool = False
        self._main_llm_context_window: int | None = main_llm_context_window

    # 依赖基类实现的方法（未覆写）：
    # - before_model / abefore_model     → 基类做 trigger 判断 + cutoff + summary + 消息替换
    # - _create_summary / _acreate_summary → 基类用 self.model 生成摘要
    # - _determine_cutoff_index           → 基类从尾部保留 keep 条消息
    # - _build_new_messages               → 基类构建 SystemMessage + RemoveMessage
    # - _wrap_model_call_impl             → 基类处理 res 返回值
    # - _get_session_or_raise             → 从 state 获取 session_id
    # - _check_last_turn_ratio            → 最后一轮占比检测
    # - _slice_last_turn                  → 切分最后一轮
    # - _get_profile_limits               → 从 model profile 获取上下文窗口
    # - _estimate_tokens                  → 估算消息列表 token 数

    # Phase 1 新增/覆写的方法：
    def _get_reported_tokens(self, messages): ...
    def _preemptive_check(self, messages, session_id) -> str | None: ...
    def _preemptive_truncate(self, messages, session_id) -> list: ...
    @staticmethod
    def _find_tool_name(messages, tool_msg, tc_id) -> str: ...

    @override
    def _should_summarize_based_on_reported_tokens(self, messages, threshold) -> bool:
        # 修复 P7：不检查 provider 匹配
        ...

    @override
    def wrap_model_call(self, request, handler):
        # 新增抢占式检查
        ...
        res = super().before_model(request.state, ...)  # 依赖基类压缩
        ...

    @override
    async def awrap_model_call(self, request, handler):
        # 同步版本镜像
        ...
```

**存在的问题（Phase 1 未解决）：**

| 问题编号 | 描述                                                                                                                               |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| P1       | 基类 `_should_summarize_based_on_reported_tokens` 的 provider 检查虽然已覆写，但基类 `before_model` 内部仍有其他 provider 相关逻辑 |
| P2       | 反抖动机制简单（3 次后放弃），没有渐进升级                                                                                         |
| P3       | 无多策略管线，只依赖 LLM 压缩                                                                                                      |
| P4       | cutoff 逻辑依赖基类 `_determine_cutoff_index`，无法做预算式 tail 选择                                                              |
| P5       | 无前次摘要链接，每次压缩都是独立摘要                                                                                               |
| P6       | 无文件操作 ratchet，跨压缩丢失文件上下文                                                                                           |
| P8       | 无确定性回退，LLM 失败时无本地提取                                                                                                 |

#### 新代码（Phase 2 完全重写，约 1262 行）

> 继承 `AgentMiddleware`（不再继承 `SummarizationMiddleware`），所有压缩逻辑自包含。
> 类名保持 `Summarization`，`__init__` 签名兼容现有调用方式。

```python
import re
import json
import hashlib
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ResponseT
from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from typing import Any, Callable, Awaitable, Sequence, cast
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    SystemMessage,
    AIMessage,
    HumanMessage,
    ToolMessage,
)
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens, estimate_messages_tokens
from pub_func.message.turn_utils import split_into_turns, split_turn
from pub_func.message.tool_output_dedup import dedup_tool_outputs
from pub_func.message.tool_output_prune import prune_tool_outputs
from pub_func.message.target_truncation import target_truncate_tool_outputs
from config.num import (
    PREEMPTIVE_TRUNCATE_RATIO,
    COMPRESSION_TRIGGER_RATIO,
    MIN_PRESERVE_TOKENS,
    MAX_PRESERVE_TOKENS,
    PRESERVE_RATIO,
    PRUNE_PROTECT_TOKENS,
    PRUNE_MIN_REDUCTION_TOKENS,
    TARGET_TRUNCATE_RATIO,
    MIN_OUTPUT_CHARS_TO_TRUNCATE,
    MAX_TOOL_OUTPUT_CHARS,
    AGGRESSIVE_TRUNCATE_CHARS,
    SUMMARY_TRIM_TOKENS,
    SUMMARY_TOTAL_MAX_CHARS,
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    DEGRADATION_NO_TEXT_THRESHOLD,
    MAX_RECOVERY_ATTEMPTS,
    MAX_TOTAL_COMPRESSION_ATTEMPTS,
    INEFFECTIVE_THRESHOLD,
    MIN_EFFECTIVENESS_PCT,
    PROTECTED_TOOLS,
    LAST_TURN_RATIO_THRESHOLD,
    COMPLETED_MAX_ITEMS,
    KEY_DECISIONS_MAX_ITEMS,
    CRITICAL_CONTEXT_MAX_ITEMS,
    FILE_OPS_LIST_MAX_CHARS,
    LATEST_USER_REQUEST_MAX_CHARS,
    AUTO_CONTINUE_PROMPT,
)


# ======================================================================
# State Keys
# ======================================================================

_LAST_USER_QUESTION_KEY = "summarization_last_user_question"
_COMPRESSION_COUNT_KEY = "summarization_compression_count"
_COMPRESSION_INEFFECTIVE_KEY = "summarization_compression_ineffective"
_COMPRESSION_LAST_TOKENS_KEY = "summarization_compression_last_tokens"
_LAST_STRATEGY_KEY = "summarization_last_strategy"
_SKIP_LLM_KEY = "summarization_skip_llm"
_DEGRADATION_NO_TEXT_KEY = "summarization_degradation_no_text"
_RECOVERY_ATTEMPTS_KEY = "summarization_recovery_attempts"
_FORCE_RECOVERY_KEY = "summarization_force_recovery"
_PREVIOUS_FILE_OPS_KEY = "summarization_previous_file_ops"
_PREEMPTIVE_TRUNCATE_MAX_CHARS = 2000

_SUMMARY_LC_SOURCE = "summarization"


# ======================================================================
# Summary Templates
# ======================================================================

_SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. Treat it as background reference, NOT as active "
    "instructions. Do NOT answer questions mentioned in this summary. "
    "Respond ONLY to the latest user message that appears AFTER this summary."
)
_SUMMARY_SUFFIX = (
    "\n\n--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
_SUMMARY_OPEN_TAG = "<summary>"
_SUMMARY_CLOSE_TAG = "</summary>"

_SUMMARY_TEMPLATE = (
    "Output exactly the Markdown structure below. Keep every section, even when empty.\n"
    "Use terse bullets, not prose paragraphs.\n"
    "Preserve exact file paths, commands, error strings, identifiers.\n\n"
    f"## Latest Unresolved User Request\n"
    f"- Quote the user's most recent unanswered request (max {LATEST_USER_REQUEST_MAX_CHARS} chars), or \"(none)\"\n\n"
    "## Goal\n"
    "- [one or two brief sentences, or \"(none)\"]\n\n"
    "## Constraints & Preferences\n"
    "- [constraints/preferences/decisions, or \"(none)\"]\n\n"
    "## Progress\n"
    f"### Completed (most recent {COMPLETED_MAX_ITEMS})\n"
    "- [finished work, or \"(none)\"]\n\n"
    "### In Progress\n"
    "- [current work, or \"(none)\"]\n\n"
    "### Blocked\n"
    "- [blockers, or \"(none)\"]\n\n"
    f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})\n"
    "- **[decision]**: [reason, or \"(none)\"]\n\n"
    "## Next Steps\n"
    "1. [immediate action, or \"(none)\"]\n\n"
    f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})\n"
    "- [exact values, error strings, config, or \"(none)\"]\n\n"
    "## Relevant Files\n"
    "- [file path: why it matters, or \"(none)\"]\n\n"
    "Rules:\n"
    "- Keep every section, even when empty.\n"
    f"- For \"Completed\" and \"Key Decisions\", keep only the most recent "
    f"{COMPLETED_MAX_ITEMS}/{KEY_DECISIONS_MAX_ITEMS} items.\n"
    '  Append "(N earlier items omitted for brevity)" when truncating.\n'
    "- Do not mention the summary process or that context was compacted."
)

_SUMMARY_UPDATE_INSTRUCTIONS = (
    "The <prior-summary> summarizes everything that happened before the <conversation>.\n"
    "Construct a new summary that combines both. The <prior-summary> is discarded after this:\n"
    "anything you do not carry into the new summary is lost.\n\n"
    "When combining:\n"
    "- Carry forward objectives, constraints, decisions from <prior-summary> even when\n"
    "  the <conversation> does not mention them.\n"
    "- The <conversation> is more recent. Where they conflict, the conversation wins.\n"
    '- Move completed work from "In Progress" to "Completed".\n'
    f"- Apply FIFO limits: keep only the most recent {COMPLETED_MAX_ITEMS} items in \"Completed\"\n"
    f'  and {KEY_DECISIONS_MAX_ITEMS} in "Key Decisions". Append "(N earlier items omitted)".\n'
    '- Remove items that are finished and no longer needed from "In Progress" and "Blocked".'
)

_SUMMARY_PROMPT_FIRST = (
    "You are a summarization agent creating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    "Create a new anchored summary from the conversation history above.\n\n"
    f"{_SUMMARY_TEMPLATE}"
)

_SUMMARY_PROMPT_UPDATE = (
    "You are a summarization agent updating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    f"{_SUMMARY_UPDATE_INSTRUCTIONS}\n\n"
    f"{_SUMMARY_TEMPLATE}"
)


# ======================================================================
# Serialization for summary LLM
# ======================================================================

def _serialize_for_summary(messages: list[AnyMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage):
            text = content[:2000] if len(content) > 2000 else content
            lines.append(f"[User]: {text}")
        elif isinstance(msg, AIMessage):
            if content.strip():
                lines.append(f"[Assistant]: {content[:2000]}")
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                args = str(tc.get("args", ""))[:500]
                lines.append(f"[Assistant tool call]: {name}({args})")
        elif isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", "")
            status = getattr(msg, "status", "")
            output = content
            if len(output) > 2000:
                output = output[:1800] + f"...[truncated {len(output) - 1800} chars]..."
            if status == "error":
                lines.append(f"[Tool error] ({tc_id}): {output}")
            else:
                lines.append(f"[Tool result] ({tc_id}): {output}")
    return "\n\n".join(lines)


# ======================================================================
# Deterministic Fallback (inspired by hermes-agent)
# ======================================================================

def _build_static_fallback_summary(messages: list[AnyMessage]) -> str:
    user_requests: list[str] = []
    completed_actions: list[str] = []
    decisions: list[str] = []
    key_files: set[str] = set()
    errors: list[str] = []

    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage) and content.strip():
            user_requests.append(content[:500])
        elif isinstance(msg, AIMessage):
            if content.strip():
                lower = content.lower()
                if any(kw in lower for kw in ("decided", "choosing", "because", "therefore")):
                    decisions.append(content[:300])
                else:
                    completed_actions.append(content[:300])
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                completed_actions.append(f"- {name}({args_str[:200]})")
                for word in args_str.replace("'", " ").replace('"', " ").split():
                    cleaned = word.strip("'\".,;:()[]{}")
                    if "/" in cleaned or "\\" in cleaned or cleaned.endswith(
                        (".py", ".md", ".js", ".ts", ".json")
                    ):
                        if len(cleaned) > 2 and not cleaned.startswith(("http", "//")):
                            key_files.add(cleaned)
        elif isinstance(msg, ToolMessage):
            if getattr(msg, "status", "") == "error":
                errors.append(content[:300])

    parts: list[str] = [
        "## Latest Unresolved User Request",
        f"- {user_requests[-1]}" if user_requests else "- (none)",
        "",
        "## Goal",
        f"- {user_requests[0][:200]}" if user_requests else "- (unknown)",
        "",
        "## Constraints & Preferences",
        "- (none)",
        "",
        f"### Completed (most recent {COMPLETED_MAX_ITEMS})",
    ]
    for action in completed_actions[-COMPLETED_MAX_ITEMS:]:
        parts.append(f"- {action}")
    if len(completed_actions) > COMPLETED_MAX_ITEMS:
        parts.append(
            f"({len(completed_actions) - COMPLETED_MAX_ITEMS} earlier completed actions omitted for brevity)"
        )
    parts.extend([
        "",
        "### In Progress",
        "- (continue previous work)",
        "",
        "### Blocked",
        f"- {errors[-1]}" if errors else "- (none)",
        "",
        f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})",
    ])
    for d in decisions[-KEY_DECISIONS_MAX_ITEMS:]:
        parts.append(f"- {d}")
    parts.extend([
        "",
        "## Next Steps",
        "1. (continue previous work)",
        "",
        f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})",
    ])
    for e in errors[-CRITICAL_CONTEXT_MAX_ITEMS:]:
        parts.append(f"- {e}")
    parts.extend(["", "## Relevant Files"])
    for f in list(key_files)[:10]:
        parts.append(f"- {f}")
    if not key_files:
        parts.append("- (none)")

    return "\n".join(parts)


# ======================================================================
# FIFO Enforcement
# ======================================================================

def _enforce_fifo_limits(summary_text: str) -> str:
    def _fifo_section(text: str, header_pattern: str, max_items: int) -> str:
        match = re.search(header_pattern, text)
        if not match:
            return text
        header_end = match.end()
        next_section = re.search(r"\n#{2,3} ", text[header_end:])
        block_end = header_end + next_section.start() if next_section else len(text)
        block = text[header_end:block_end]
        items = [line for line in block.split("\n") if line.strip().startswith("-")]
        if len(items) <= max_items:
            return text
        kept = items[-max_items:]
        omitted = len(items) - max_items
        omitted_line = f"({omitted} earlier items omitted for brevity)"
        new_block = "\n".join(kept) + "\n" + omitted_line + "\n"
        return text[:header_end] + new_block + text[block_end:]

    summary_text = _fifo_section(
        summary_text, r"### Completed[^\n]*\n", COMPLETED_MAX_ITEMS
    )
    summary_text = _fifo_section(
        summary_text, r"## Key Decisions[^\n]*\n", KEY_DECISIONS_MAX_ITEMS
    )
    summary_text = _fifo_section(
        summary_text, r"## Critical Context[^\n]*\n", CRITICAL_CONTEXT_MAX_ITEMS
    )
    return summary_text


# ======================================================================
# File Operations Ratchet (inspired by openclaw)
# ======================================================================

def _extract_file_operations(messages: list[AnyMessage]) -> dict[str, list[str]]:
    read_files: set[str] = set()
    modified_files: set[str] = set()

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                paths: set[str] = set()
                for word in args_str.replace("'", " ").replace('"', " ").replace(",", " ").split():
                    cleaned = word.strip("'\".,;:()[]{}")
                    if "/" in cleaned or "\\" in cleaned or cleaned.endswith(
                        (".py", ".md", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".cfg")
                    ):
                        if len(cleaned) > 2 and not cleaned.startswith(("http", "//")):
                            paths.add(cleaned)
                if name in ("read_file", "read", "cat", "view", "edit", "write_file", "write", "patch_file", "create_file"):
                    if name in ("write_file", "write", "patch_file", "edit", "create_file"):
                        modified_files.update(paths)
                        read_files.update(paths)
                    else:
                        read_files.update(paths)

    read_only = read_files - modified_files
    return {
        "read_files": sorted(read_only),
        "modified_files": sorted(modified_files),
    }


def _format_file_ops(file_ops: dict[str, list[str]], previous: dict | None = None) -> str:
    if previous:
        prev_read = set(previous.get("read_files", []))
        prev_mod = set(previous.get("modified_files", []))
        all_modified = prev_mod | set(file_ops.get("modified_files", []))
        all_read = (prev_read | set(file_ops.get("read_files", []))) - all_modified
    else:
        all_read = set(file_ops.get("read_files", []))
        all_modified = set(file_ops.get("modified_files", []))

    def _fmt(files: set[str], max_chars: int) -> str:
        lines = [f"- {f}" for f in sorted(files)]
        total = sum(len(l) for l in lines)
        while total > max_chars and lines:
            dropped = lines.pop(0)
            total -= len(dropped)
        if not lines and files:
            lines.append(f"- (file list truncated, {len(files)} files)")
        return "\n".join(lines)

    read_section = _fmt(all_read, FILE_OPS_LIST_MAX_CHARS)
    mod_section = _fmt(all_modified, FILE_OPS_LIST_MAX_CHARS)

    result = "<read-files>\n"
    result += read_section if read_section else "- (none)"
    result += "\n</read-files>\n"
    result += "<modified-files>\n"
    result += mod_section if mod_section else "- (none)"
    result += "\n</modified-files>"
    return result


def _parse_file_ops_from_summary(summary_text: str) -> dict | None:
    read_match = re.search(r"<read-files>\n?(.*?)\n?</read-files>", summary_text, re.DOTALL)
    mod_match = re.search(r"<modified-files>\n?(.*?)\n?</modified-files>", summary_text, re.DOTALL)
    if not read_match and not mod_match:
        return None
    read_files = [
        line.strip("- ").strip()
        for line in (read_match.group(1) if read_match else "").split("\n")
        if line.strip().startswith("-")
    ]
    mod_files = [
        line.strip("- ").strip()
        for line in (mod_match.group(1) if mod_match else "").split("\n")
        if line.strip().startswith("-")
    ]
    return {"read_files": read_files, "modified_files": mod_files}


# ======================================================================
# Main Middleware Class
# ======================================================================

class Summarization(AgentMiddleware):
    """Context compaction middleware — written from scratch.

    Does NOT inherit from SummarizationMiddleware. All compression logic
    is self-contained: trigger checking, cutoff determination, summary
    generation, multi-strategy pipeline, degradation monitoring.

    Post-compression format: HumanMessage("What did we do so far?") +
    AIMessage(summary, lc_source="summarization") pair. No consecutive
    same-role messages, no _fix_consecutive_human_messages needed.
    """

    def __init__(
        self,
        model,
        trigger: list | None = None,
        keep: tuple = ("messages", 10),
        main_llm_context_window: int | None = None,
        need_update_system_prompt: bool = False,
        **kwargs,
    ):
        self._model = model
        self._trigger = trigger or [("tokens", 80_000)]
        self._keep = keep
        self._main_llm_context_window = main_llm_context_window
        self._need_update_system_prompt = need_update_system_prompt
        self._compress_last_turn: bool = False
        self._compaction_just_happened: bool = False

    # ------------------------------------------------------------------
    # Session validation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_session_or_raise(state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        return session_id

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_msg_tokens(msg: BaseMessage) -> int:
        return estimate_msg_tokens(msg)

    def _estimate_tokens(self, messages: Sequence[BaseMessage]) -> int:
        return estimate_messages_tokens(list(messages))

    def _get_reported_tokens(self, messages: list[AnyMessage]) -> int:
        last_ai = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai and last_ai.usage_metadata:
            return int(last_ai.usage_metadata.get("total_tokens", 0))
        return 0

    # ------------------------------------------------------------------
    # Budget calculation
    # ------------------------------------------------------------------

    def _calculate_preserve_budget(self) -> int:
        ctx = self._main_llm_context_window
        if ctx:
            budget = int(ctx * PRESERVE_RATIO)
            return min(MAX_PRESERVE_TOKENS, max(MIN_PRESERVE_TOKENS, budget))
        return MIN_PRESERVE_TOKENS

    # ------------------------------------------------------------------
    # Trigger checking
    # ------------------------------------------------------------------

    def _check_trigger(self, messages: list[AnyMessage]) -> bool:
        """Check if any trigger condition is met."""
        for trigger_type, threshold in self._trigger:
            if trigger_type == "messages" and len(messages) >= threshold:
                return True
            if trigger_type == "tokens":
                local_est = self._estimate_tokens(messages)
                reported = self._get_reported_tokens(messages)
                effective = max(local_est, reported) if reported > 0 else local_est
                if effective >= threshold:
                    return True
        return False

    def _preemptive_check(
        self, messages: list[AnyMessage], session_id: str
    ) -> str | None:
        """Pre-prompt token pressure estimation.

        Returns None / 'truncate_only' / 'compact'.
        """
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            return None

        local_est = self._estimate_tokens(messages)
        reported = self._get_reported_tokens(messages)
        effective = max(local_est, reported) if reported > 0 else local_est
        pressure = effective / ctx_window

        if pressure >= COMPRESSION_TRIGGER_RATIO:
            return "compact"
        if pressure >= PREEMPTIVE_TRUNCATE_RATIO:
            return "truncate_only"
        return None

    # ------------------------------------------------------------------
    # Preemptive truncation (no LLM call)
    # ------------------------------------------------------------------

    def _preemptive_truncate(
        self, messages: list[BaseMessage], session_id: str
    ) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        truncated_count = 0

        for m in messages:
            if isinstance(m, ToolMessage):
                tc_id = getattr(m, "tool_call_id", "")
                tool_name = self._find_tool_name(messages, m, tc_id)
                if tool_name in PROTECTED_TOOLS:
                    result.append(m)
                    continue
                content = str(getattr(m, "content", ""))
                if len(content) > _PREEMPTIVE_TRUNCATE_MAX_CHARS:
                    head = content[: int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * CONTENT_HEAD_RATIO)]
                    tail = content[-int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * CONTENT_TAIL_RATIO):]
                    omitted = len(content) - len(head) - len(tail)
                    truncated = f"{head}...[omitted {omitted} chars]...{tail}"
                    result.append(m.model_copy(update={"content": truncated}))
                    truncated_count += 1
                else:
                    result.append(m)
            else:
                result.append(m)

        if truncated_count > 0:
            logger.debug(
                "Preemptive truncation: {} tool outputs, session={}",
                truncated_count, session_id,
            )
        return result

    @staticmethod
    def _find_tool_name(
        messages: list[BaseMessage], tool_msg: ToolMessage, tc_id: str
    ) -> str:
        if not tc_id:
            return ""
        idx = messages.index(tool_msg)
        for i in range(idx - 1, -1, -1):
            m = messages[i]
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                for tc in m.tool_calls:
                    if tc.get("id") == tc_id:
                        return tc.get("name", "")
        return ""

    # ------------------------------------------------------------------
    # Last-turn detection
    # ------------------------------------------------------------------

    @staticmethod
    def _slice_last_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
        if not messages:
            return []
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_user_idx is None:
            return []
        return messages[last_user_idx:]

    def _check_last_turn_ratio(self, messages: list[AnyMessage], session_id: str) -> bool:
        total_tokens = self._estimate_tokens(messages)
        if total_tokens <= 0:
            self._compress_last_turn = False
            return False
        last_turn = self._slice_last_turn(messages)
        last_turn_tokens = self._estimate_tokens(last_turn)
        ratio = last_turn_tokens / total_tokens
        compress = ratio >= LAST_TURN_RATIO_THRESHOLD
        self._compress_last_turn = compress
        if compress:
            last_user_msg = next(
                (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
            )
            question = (
                last_user_msg.content
                if last_user_msg and isinstance(last_user_msg.content, str)
                else ""
            )
            state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, question)
        else:
            state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")
        logger.debug(
            "Compaction: last-turn ratio={:.1f}%, compress_last_turn={}, session={}",
            ratio * 100, compress, session_id,
        )
        return compress

    # ------------------------------------------------------------------
    # Anti-thrashing: progressive escalation
    # ------------------------------------------------------------------

    def _should_skip_compression(self, session_id: str) -> bool:
        if state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False):
            state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, False)
            state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)
            state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            return False

        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        if attempts >= MAX_TOTAL_COMPRESSION_ATTEMPTS:
            logger.debug("Max compression attempts ({}) reached", MAX_TOTAL_COMPRESSION_ATTEMPTS)
            return True

        ineffective = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        if ineffective >= INEFFECTIVE_THRESHOLD:
            if not state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False):
                state_register_mem.set_state(session_id, _SKIP_LLM_KEY, True)
                logger.debug("LLM summary ineffective, switching to non-LLM strategies only")
            return False

        return False

    def _record_compression(
        self,
        session_id: str,
        before_messages: Sequence[BaseMessage],
        after_messages: Sequence[BaseMessage],
        strategy_used: str = "",
    ) -> None:
        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0) + 1
        state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, attempts)
        state_register_mem.set_state(session_id, _LAST_STRATEGY_KEY, strategy_used or "unknown")

        before_tokens = self._estimate_tokens(before_messages)
        after_tokens = self._estimate_tokens(after_messages)
        msg_reduced = len(after_messages) < len(before_messages)
        token_reduction_pct = (
            (before_tokens - after_tokens) / before_tokens if before_tokens > 0 else 0.0
        )
        effective = msg_reduced or token_reduction_pct >= MIN_EFFECTIVENESS_PCT

        if not effective:
            ineffective = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0) + 1
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, ineffective)
        else:
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            if strategy_used in ("dedup", "prune", "truncate", "fallback", "aggressive"):
                state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)

        state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, after_tokens)

    # ------------------------------------------------------------------
    # Cutoff determination (budget-based tail selection)
    # ------------------------------------------------------------------

    def _determine_cutoff(self, messages: list[AnyMessage]) -> int:
        budget = self._calculate_preserve_budget()
        turns = split_into_turns(messages)

        total = 0
        cutoff = 0
        for turn in reversed(turns):
            size = self._estimate_tokens(turn.messages)
            if total + size <= budget:
                total += size
                cutoff = turn.start_idx
            else:
                remaining = budget - total
                split_idx = split_turn(turn, remaining, lambda msgs: self._estimate_tokens(msgs))
                if split_idx is not None:
                    cutoff = split_idx
                break

        cutoff = self._adjust_for_orphan_pairs(messages, cutoff)

        if not self._compress_last_turn:
            last_user_idx = next(
                (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
                None,
            )
            if last_user_idx is not None and cutoff > last_user_idx:
                cutoff = last_user_idx

        return max(cutoff, 0)

    def _adjust_for_orphan_pairs(self, messages: list[AnyMessage], cutoff: int) -> int:
        adjusted = cutoff
        while adjusted > 0:
            orphan_ids: set[str] = set()
            for m in messages[adjusted:]:
                if isinstance(m, ToolMessage) and m.tool_call_id:
                    orphan_ids.add(m.tool_call_id)
            for m in messages[adjusted:]:
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        orphan_ids.discard(tc.get("id"))
            if not orphan_ids:
                break

            earliest_orphan_ai = len(messages)
            for i in range(adjusted):
                m = messages[i]
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    if any(tc.get("id") in orphan_ids for tc in m.tool_calls):
                        earliest_orphan_ai = min(earliest_orphan_ai, i)
            if earliest_orphan_ai < adjusted:
                adjusted = earliest_orphan_ai
            else:
                prev_user_idx = next(
                    (i for i in range(adjusted - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
                    None,
                )
                if prev_user_idx is None:
                    break
                adjusted = prev_user_idx
        return adjusted

    # ------------------------------------------------------------------
    # Previous summary chaining
    # ------------------------------------------------------------------

    def _extract_previous_summary(self, messages: list[AnyMessage]) -> str | None:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if _SUMMARY_CLOSE_TAG in content:
                    start = content.find(_SUMMARY_OPEN_TAG)
                    end = content.find(_SUMMARY_CLOSE_TAG)
                    if start >= 0 and end > start:
                        return content[start + len(_SUMMARY_OPEN_TAG):end].strip()
                return content
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage) and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                return msg.content if isinstance(msg.content, str) else str(msg.content)
        return None

    # ------------------------------------------------------------------
    # Summary prompt construction
    # ------------------------------------------------------------------

    def _build_summary_prompt(self, messages_text: str, previous_summary: str | None) -> str:
        conversation = f"Here is the conversation so far:\n\n<conversation>\n{messages_text}\n</conversation>"
        if previous_summary:
            return "\n\n".join([
                conversation,
                f"Here is the summary of the conversation before the <conversation> above:\n\n"
                f"<prior-summary>\n{previous_summary}\n</prior-summary>",
                _SUMMARY_PROMPT_UPDATE,
            ])
        return "\n\n".join([conversation, _SUMMARY_PROMPT_FIRST])

    # ------------------------------------------------------------------
    # LLM summary creation (sync / async)
    # ------------------------------------------------------------------

    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."

        previous_summary = self._extract_previous_summary(messages_to_summarize)
        serialized = _serialize_for_summary(messages_to_summarize)
        if not serialized.strip():
            return "No previous conversation history."

        prompt = self._build_summary_prompt(serialized, previous_summary)

        try:
            response = self._model.invoke(
                prompt,
                config={"metadata": {"lc_source": _SUMMARY_LC_SOURCE}},
            )
            summary = response.text.strip()
            if not summary or len(summary) < 50:
                logger.warning("Summary too short, using fallback")
                return _build_static_fallback_summary(messages_to_summarize)
            return summary
        except Exception as e:
            logger.error("LLM summary failed: {}, using fallback", e)
            return _build_static_fallback_summary(messages_to_summarize)

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."

        previous_summary = self._extract_previous_summary(messages_to_summarize)
        serialized = _serialize_for_summary(messages_to_summarize)
        if not serialized.strip():
            return "No previous conversation history."

        prompt = self._build_summary_prompt(serialized, previous_summary)

        try:
            response = await self._model.ainvoke(
                prompt,
                config={"metadata": {"lc_source": _SUMMARY_LC_SOURCE}},
            )
            summary = response.text.strip()
            if not summary or len(summary) < 50:
                logger.warning("Summary too short, using fallback")
                return _build_static_fallback_summary(messages_to_summarize)
            return summary
        except Exception as e:
            logger.error("LLM summary failed: {}, using fallback", e)
            return _build_static_fallback_summary(messages_to_summarize)

    # ------------------------------------------------------------------
    # Build new messages (HumanMessage + AIMessage pair)
    # ------------------------------------------------------------------

    def _build_new_messages(self, summary: str) -> list[BaseMessage]:
        summary = _enforce_fifo_limits(summary)

        if len(summary) > SUMMARY_TOTAL_MAX_CHARS:
            head = summary[: int(SUMMARY_TOTAL_MAX_CHARS * CONTENT_HEAD_RATIO)]
            tail = summary[-int(SUMMARY_TOTAL_MAX_CHARS * CONTENT_TAIL_RATIO):]
            omitted = len(summary) - len(head) - len(tail)
            summary = f"{head}...[summary truncated, omitted {omitted} chars]...{tail}"

        full_content = (
            f"{_SUMMARY_PREFIX}\n\n"
            f"{_SUMMARY_OPEN_TAG}\n"
            f"{summary}\n"
            f"{_SUMMARY_CLOSE_TAG}"
            f"{_SUMMARY_SUFFIX}"
        )

        return [
            HumanMessage(content="What did we do so far?"),
            AIMessage(
                content=full_content,
                additional_kwargs={"lc_source": _SUMMARY_LC_SOURCE},
            ),
        ]

    # ------------------------------------------------------------------
    # Multi-strategy pipeline (non-LLM strategies)
    # ------------------------------------------------------------------

    def _run_non_llm_strategies(
        self, messages: list[BaseMessage], session_id: str
    ) -> tuple[list[BaseMessage], int]:
        current = list(messages)
        total_reduced = 0

        current, reduced = dedup_tool_outputs(current, set(PROTECTED_TOOLS))
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Dedup reduced ~{} tokens, session={}", reduced, session_id)

        current, reduced = prune_tool_outputs(
            current,
            protect_tokens=PRUNE_PROTECT_TOKENS,
            min_reduction_tokens=PRUNE_MIN_REDUCTION_TOKENS,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Prune reduced ~{} tokens, session={}", reduced, session_id)

        current_tokens = self._estimate_tokens(current)
        target = int(current_tokens * TARGET_TRUNCATE_RATIO)
        current, reduced = target_truncate_tool_outputs(
            current,
            target_reduction_tokens=target,
            min_output_chars=MIN_OUTPUT_CHARS_TO_TRUNCATE,
            max_output_chars=MAX_TOOL_OUTPUT_CHARS,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Target truncation reduced ~{} tokens, session={}", reduced, session_id)

        return current, total_reduced

    def _aggressive_truncate(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for msg in messages:
            if isinstance(msg, ToolMessage):
                content = str(getattr(msg, "content", ""))
                if len(content) > AGGRESSIVE_TRUNCATE_CHARS:
                    truncated = content[:AGGRESSIVE_TRUNCATE_CHARS] + (
                        f"...[aggressively truncated, {len(content) - AGGRESSIVE_TRUNCATE_CHARS} chars omitted]"
                    )
                    msg = msg.model_copy(update={"content": truncated})
            result.append(msg)
        return result

    # ------------------------------------------------------------------
    # Recovery context capture & injection
    # ------------------------------------------------------------------

    def _capture_recovery_context(self, messages: list[BaseMessage], session_id: str) -> dict:
        last_human = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
        )
        user_intent = ""
        if last_human and isinstance(last_human.content, str):
            user_intent = last_human.content[:LATEST_USER_REQUEST_MAX_CHARS]

        file_ops = _extract_file_operations(messages)
        previous_file_ops = state_register_mem.get_state(session_id, _PREVIOUS_FILE_OPS_KEY, None)
        state_register_mem.set_state(session_id, _PREVIOUS_FILE_OPS_KEY, file_ops)

        return {
            "user_intent": user_intent,
            "file_ops": file_ops,
            "previous_file_ops": previous_file_ops,
        }

    def _inject_recovery_context(
        self, messages: list[BaseMessage], ctx: dict, session_id: str
    ) -> list[BaseMessage]:
        file_ops_section = _format_file_ops(ctx.get("file_ops", {}), ctx.get("previous_file_ops"))

        for i, m in enumerate(messages):
            if isinstance(m, AIMessage) and getattr(m, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                existing = m.content if isinstance(m.content, str) else str(m.content)
                pattern = r"## Relevant Files\n.*?(?=\n---|\n</summary>|\Z)"
                if re.search(pattern, existing, re.DOTALL):
                    replacement = f"## Relevant Files\n{file_ops_section}"
                    new_content = re.sub(pattern, replacement, existing, flags=re.DOTALL)
                else:
                    new_content = existing.replace(
                        _SUMMARY_CLOSE_TAG,
                        f"\n## Relevant Files\n{file_ops_section}\n{_SUMMARY_CLOSE_TAG}",
                    )
                messages[i] = m.model_copy(update={"content": new_content})
                break

        return messages

    # ------------------------------------------------------------------
    # Truncation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_content(content: str, max_chars: int) -> str:
        if len(content) <= max_chars:
            return content
        head = content[: int(max_chars * CONTENT_HEAD_RATIO)]
        tail = content[-int(max_chars * CONTENT_TAIL_RATIO):]
        omitted = len(content) - len(head) - len(tail)
        return f"{head}...[omitted {omitted} chars]...{tail}"

    def _truncate_summary_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for m in messages:
            if getattr(m, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                content = getattr(m, "content", "")
                if isinstance(content, str) and len(content) > SUMMARY_TOTAL_MAX_CHARS:
                    truncated = self._truncate_content(content, SUMMARY_TOTAL_MAX_CHARS)
                    m = m.model_copy(update={"content": truncated})
            result.append(m)
        return result

    # ------------------------------------------------------------------
    # Degradation monitoring
    # ------------------------------------------------------------------

    @staticmethod
    def _is_empty_response(response) -> bool:
        if response is None:
            return True
        if isinstance(response, AIMessage):
            content = response.content
            if isinstance(content, str):
                return not content.strip()
            if isinstance(content, list):
                return not any(p.get("text", "").strip() for p in content if isinstance(p, dict))
        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, str):
                return not content.strip()
        return False

    def _monitor_degradation(self, response, session_id: str):
        if not self._compaction_just_happened:
            return
        self._compaction_just_happened = False

        if self._is_empty_response(response):
            count = state_register_mem.get_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0) + 1
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, count)

            if count >= DEGRADATION_NO_TEXT_THRESHOLD:
                attempts = state_register_mem.get_state(session_id, _RECOVERY_ATTEMPTS_KEY, 0)
                if attempts < MAX_RECOVERY_ATTEMPTS:
                    state_register_mem.set_state(session_id, _RECOVERY_ATTEMPTS_KEY, attempts + 1)
                    state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, True)
                    state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
                    state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
                    logger.warning(
                        "Degradation detected ({} empty responses), forcing recovery",
                        count,
                    )
        else:
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0)

    # ------------------------------------------------------------------
    # Core compression application (sync)
    # ------------------------------------------------------------------

    def _apply_compression(
        self, request: ModelRequest[ContextT], session_id: str,
    ) -> ModelRequest[ContextT]:
        original_messages: list[AnyMessage] = request.state.get("messages", [])
        recovery_ctx = self._capture_recovery_context(original_messages, session_id)

        current_messages, non_llm_reduced = self._run_non_llm_strategies(
            list(original_messages), session_id
        )
        strategy_used = "non_llm" if non_llm_reduced > 0 else None

        current_tokens = self._estimate_tokens(current_messages)
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        budget = self._calculate_preserve_budget()

        if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
            cutoff = self._determine_cutoff(current_messages)
            if cutoff > 0:
                messages_to_summarize = current_messages[:cutoff]
                preserved = current_messages[cutoff:]

                if skip_llm:
                    summary_text = _build_static_fallback_summary(messages_to_summarize)
                    strategy_used = "fallback"
                else:
                    summary_text = self._create_summary(messages_to_summarize)
                    strategy_used = "llm_summary"

                new_messages = self._build_new_messages(summary_text)
                final_messages = [*new_messages, *preserved]
            else:
                final_messages = current_messages
                strategy_used = strategy_used or "noop"
        else:
            final_messages = current_messages
            strategy_used = strategy_used or "non_llm_sufficient"

        if self._estimate_tokens(final_messages) > budget * 2:
            final_messages = self._aggressive_truncate(final_messages)
            strategy_used = "aggressive"

        final_messages = self._truncate_summary_messages(final_messages)

        if recovery_ctx:
            final_messages = self._inject_recovery_context(
                final_messages, recovery_ctx, session_id
            )

        self._record_compression(session_id, original_messages, final_messages, strategy_used)
        self._compaction_just_happened = True
        self._compress_last_turn = False
        state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            from agent.tools import memory_store
            memory_store.load_from_disk()
            system_prompt = build_system_prompt(session_id=session_id)
            state_register_mem.set_state(session_id, "system_prompt", system_prompt)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", final_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    # ------------------------------------------------------------------
    # Core compression application (async)
    # ------------------------------------------------------------------

    async def _aapply_compression(
        self, request: ModelRequest[ContextT], session_id: str,
    ) -> ModelRequest[ContextT]:
        original_messages: list[AnyMessage] = request.state.get("messages", [])
        recovery_ctx = self._capture_recovery_context(original_messages, session_id)

        current_messages, non_llm_reduced = self._run_non_llm_strategies(
            list(original_messages), session_id
        )
        strategy_used = "non_llm" if non_llm_reduced > 0 else None

        current_tokens = self._estimate_tokens(current_messages)
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        budget = self._calculate_preserve_budget()

        if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
            cutoff = self._determine_cutoff(current_messages)
            if cutoff > 0:
                messages_to_summarize = current_messages[:cutoff]
                preserved = current_messages[cutoff:]

                if skip_llm:
                    summary_text = _build_static_fallback_summary(messages_to_summarize)
                    strategy_used = "fallback"
                else:
                    summary_text = await self._acreate_summary(messages_to_summarize)
                    strategy_used = "llm_summary"

                new_messages = self._build_new_messages(summary_text)
                final_messages = [*new_messages, *preserved]
            else:
                final_messages = current_messages
                strategy_used = strategy_used or "noop"
        else:
            final_messages = current_messages
            strategy_used = strategy_used or "non_llm_sufficient"

        if self._estimate_tokens(final_messages) > budget * 2:
            final_messages = self._aggressive_truncate(final_messages)
            strategy_used = "aggressive"

        final_messages = self._truncate_summary_messages(final_messages)

        if recovery_ctx:
            final_messages = self._inject_recovery_context(
                final_messages, recovery_ctx, session_id
            )

        self._record_compression(session_id, original_messages, final_messages, strategy_used)
        self._compaction_just_happened = True
        self._compress_last_turn = False
        state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            from agent.tools import memory_store
            memory_store.load_from_disk()
            system_prompt = build_system_prompt(session_id=session_id)
            state_register_mem.set_state(session_id, "system_prompt", system_prompt)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", final_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    # ------------------------------------------------------------------
    # before_agent: reset state
    # ------------------------------------------------------------------

    def _before_agent_impl(self, state: AgentState) -> None:
        session_id = state.get("session_id", "")
        if session_id.strip():
            state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, None)
            state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)
            state_register_mem.set_state(session_id, _LAST_STRATEGY_KEY, "")
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0)
            state_register_mem.set_state(session_id, _RECOVERY_ATTEMPTS_KEY, 0)
            state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, False)
            state_register_mem.set_state(session_id, _PREVIOUS_FILE_OPS_KEY, None)

    def before_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        logger.debug("Compaction before_agent hook fired")
        self._before_agent_impl(state)
        return None

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        logger.debug("Compaction abefore_agent hook fired")
        self._before_agent_impl(state)
        return None

    # ------------------------------------------------------------------
    # wrap_model_call (sync)
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("Compaction wrap_model_call hook fired")
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        if self._should_skip_compression(session_id):
            self._compress_last_turn = False
            self._compaction_just_happened = False
            response = handler(request)
            self._monitor_degradation(response, session_id)
            return response

        action = self._preemptive_check(messages, session_id)
        if action in ("truncate_only", "compact"):
            truncated = self._preemptive_truncate(messages, session_id)
            request = request.override(messages=cast("list[AnyMessage]", truncated))

        need_compress = (action == "compact") or self._check_trigger(
            request.state.get("messages", [])
        )

        if need_compress:
            try:
                request = self._apply_compression(request, session_id)
            except Exception as e:
                logger.error("Compression failed: {}", e)

        response = handler(request)
        self._monitor_degradation(response, session_id)
        return response

    # ------------------------------------------------------------------
    # awrap_model_call (async)
    # ------------------------------------------------------------------

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("Compaction awrap_model_call hook fired")
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        if self._should_skip_compression(session_id):
            self._compress_last_turn = False
            self._compaction_just_happened = False
            response = await handler(request)
            self._monitor_degradation(response, session_id)
            return response

        action = self._preemptive_check(messages, session_id)
        if action in ("truncate_only", "compact"):
            truncated = self._preemptive_truncate(messages, session_id)
            request = request.override(messages=cast("list[AnyMessage]", truncated))

        need_compress = (action == "compact") or self._check_trigger(
            request.state.get("messages", [])
        )

        if need_compress:
            try:
                request = await self._aapply_compression(request, session_id)
            except Exception as e:
                logger.error("Compression failed: {}", e)

        response = await handler(request)
        self._monitor_degradation(response, session_id)
        return response
```

### Phase 2 变更说明（summarization.py 整体）

| 变更点           | 旧（Phase 1 状态）                                                      | 新（Phase 2）                                                                    | 解决的问题                                             |
| ---------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 基类             | `SummarizationMiddleware`                                               | `AgentMiddleware`                                                                | **P1**: 不再依赖基类，消除 provider 检查等所有基类 bug |
| 触发检查         | 基类 `before_model` + 覆写 `_should_summarize_based_on_reported_tokens` | 自建 `_check_trigger` + `_preemptive_check`                                      | 完全自控，不遗漏                                       |
| Cutoff           | 基类 `_determine_cutoff_index`（保留 keep 条）                          | `_determine_cutoff`（预算式 tail + turn 切分 + orphan pair 修复）                | **P4**: 预算式保留尾部消息                             |
| 摘要生成         | 基类 `_create_summary`                                                  | `_create_summary`/`_acreate_summary` + 8 节模板 + `<prior-summary>` 链           | **P5**: 前次摘要链接，不丢失上下文                     |
| 消息格式         | 基类 `_build_new_messages`（SystemMessage + RemoveMessage）             | `_build_new_messages`（HumanMessage + AIMessage 对）                             | 消除连续同角色消息问题                                 |
| 反抖动           | 3 次后放弃                                                              | 渐进升级：LLM→skip LLM→非 LLM 策略→aggressive→fallback                           | **P2**: 不放弃，逐步降级                               |
| 多策略管线       | 无                                                                      | `_run_non_llm_strategies`（dedup→prune→truncate）                                | **P3**: 先非 LLM 压缩，可能不需要 LLM                  |
| 确定性回退       | 无                                                                      | `_build_static_fallback_summary`                                                 | **P8**: LLM 失败时本地提取关键信息                     |
| 文件操作         | 无                                                                      | `_extract_file_operations` + `_format_file_ops` + `_parse_file_ops_from_summary` | **P6**: 跨压缩 ratchet，累积文件上下文                 |
| FIFO 限制        | 无                                                                      | `_enforce_fifo_limits`（Completed 5/Decisions 5/Context 3）                      | 防止摘要膨胀                                           |
| 退化监控         | 无                                                                      | `_monitor_degradation` + `_is_empty_response` + `_FORCE_RECOVERY`                | 空响应检测→强制恢复                                    |
| 摘要模板         | 基类默认                                                                | 8 节 Markdown 结构 + 防注入前缀/后缀                                             | 结构化 + 安全                                          |
| 序列化           | 基类默认                                                                | `_serialize_for_summary`（角色标签 + 截断）                                      | 控制输入 token                                         |
| Recovery context | 无                                                                      | `_capture_recovery_context` + `_inject_recovery_context`                         | 压缩后注入文件操作上下文                               |

---

## 10. agent/core.py

### 变更 10.1：import 部分

#### 旧代码

```python
from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
from agent.tools import memory_store, build_main_tools
```

#### 新代码

```python
from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
from config.num import COMPRESSION_TRIGGER_RATIO
from agent.tools import memory_store, build_main_tools
```

### 变更 10.2：Summarization 实例化

#### 旧代码

```python
                Summarization(
                    need_update_system_prompt=True,
                    model=auxiliary_llm,
                    trigger=[("tokens", int(main_llm_max_tokens / 2))],
                    keep=("messages", 10),
                ),
```

#### 新代码

```python
                Summarization(
                    need_update_system_prompt=True,
                    model=auxiliary_llm,
                    main_llm_context_window=main_llm_max_tokens,
                    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
                    keep=("messages", 10),
                ),
```

### 变更说明

- trigger 阈值从 `main_llm_max_tokens / 2`（50%）改为 `main_llm_max_tokens * 0.80`（80%），保留更多上下文
- 新增 `main_llm_context_window=main_llm_max_tokens` 参数，传递主 LLM 上下文窗口大小给中间件

---

## 11. agent/tools/subagent/spawn/core.py

### 变更 11.1：import 部分（函数内部 import）

#### 旧代码

```python
    from agent.core import StateSchema
    from langchain.agents import create_agent
    from models import build_main_llm, build_auxiliary_llm
    from agent.checkpointer import build_async_sqlite_checkpointer
    from agent.middlewares import (
        IterationBudget,
        ToolGuardrails,
        ToolCallNormalize,
        Summarization,
        HeartbeatStaleness,
    )
```

#### 新代码

```python
    from agent.core import StateSchema
    from langchain.agents import create_agent
    from models import build_main_llm, build_auxiliary_llm
    from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
    from config.num import COMPRESSION_TRIGGER_RATIO
    from agent.checkpointer import build_async_sqlite_checkpointer
    from agent.middlewares import (
        IterationBudget,
        ToolGuardrails,
        ToolCallNormalize,
        Summarization,
        HeartbeatStaleness,
    )
```

### 变更 11.2：Summarization 实例化

#### 旧代码

```python
            Summarization(
                model=auxiliary_llm,
                trigger=[("messages", 40), ("tokens", 30000)],
                keep=("messages", 10),
            ),
```

#### 新代码

```python
            Summarization(
                model=auxiliary_llm,
                main_llm_context_window=main_llm_max_tokens,
                trigger=[
                    ("messages", 40),
                    ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
                ],
                keep=("messages", 10),
            ),
```

### 变更说明

- trigger 的 token 阈值从固定 `30000` 改为 `main_llm_max_tokens * 0.80`（动态适配主 LLM 上下文窗口）
- 新增 `main_llm_context_window=main_llm_max_tokens` 参数

---

## 12. tests/unit/conftest.py（修改）

### 变更说明

`_isolated_skill_scan_cache` autouse fixture 在 `server.service.skill_scanner` 导入失败时（如 langgraph `ExecutionInfo` 环境问题）会导致所有单元测试全部报错。添加 try/except 保护，导入失败时跳过 patch，使无关单元测试正常运行。

### 旧代码

```python
@pytest.fixture(autouse=True)
def _isolated_skill_scan_cache(tmp_path):
    import server.service.skill_scanner  # noqa: F401
    with (
        patch(
            "server.service.skill_scanner._CACHE_PATH",
            tmp_path / "skills_scan_cache.json",
        ),
        patch(
            "server.service.skill_scanner._scanner_version_fingerprint",
            return_value="SkillSpector v-unit-stable",
        ),
    ):
        yield
```

### 新代码

```python
@pytest.fixture(autouse=True)
def _isolated_skill_scan_cache(tmp_path):
    try:
        import server.service.skill_scanner  # noqa: F401
    except Exception:
        yield
        return
    with (
        patch(
            "server.service.skill_scanner._CACHE_PATH",
            tmp_path / "skills_scan_cache.json",
        ),
        patch(
            "server.service.skill_scanner._scanner_version_fingerprint",
            return_value="SkillSpector v-unit-stable",
        ),
    ):
        yield
```

---

## 13. tests/test_summarization_comprehensive.py（新建）

### 文件信息

- 行数：~1009 行
- 测试组：10 个
- 测试用例：172 个（全部通过）
- 运行方式：`python tests/test_summarization_comprehensive.py`
- 说明：使用 stub 模块绕过 langgraph `ExecutionInfo` 导入问题，独立运行无需 pytest

### 测试覆盖

| 测试组                         | 用例数 | 覆盖内容                                        |
| ------------------------------ | ------ | ----------------------------------------------- |
| `TestEstimateMsgTokens`        | 5      | content + tool_calls + tool_call_id token 估算  |
| `TestTurnUtils`                | 4      | `split_into_turns` 正确切分、turn 内切分        |
| `TestToolOutputDedup`          | 6      | 去重保留最新、protected_tools 跳过、签名分组    |
| `TestToolOutputPrune`          | 5      | 保护窗口逻辑、summary 停止、最小缩减量          |
| `TestTargetTruncation`         | 5      | 大小降序排列、达到目标停止、小输出跳过          |
| `TestSummarizationConfig`      | 5      | 预算计算、触发阈值、FIFO 限制                   |
| `TestSummarizationCore`        | 49     | cutoff 逻辑、预算 tail、新消息格式、触发检查    |
| `TestSummarizationCompression` | 26     | apply_compression、多策略管线、preemptive check |
| `TestSummarizationFallback`    | 22     | 静态回退、文件操作 ratchet、恢复上下文          |
| `TestSummarizationAsync`       | 45     | 异步镜像测试、orphan pair 处理                  |

### 修复记录（10 个用例修复）

1. preemptive_check token 估算不足 — 增大测试消息量
2. cutoff 消息量不足 — 增加消息数量使 cutoff 能选中足够消息
3. `apply_compression` 非工具消息太小 — 导致 non-LLM 策略后低于 budget*2
4. async test AIMessage 缺少 `tool_calls` — 导致 `_adjust_for_orphan_pairs` 将 cutoff 降到 0

---

## 14. tests/test_e2e_summarization.py（新建）

### 文件信息

- 行数：97 行
- 运行方式：`python tests/test_e2e_summarization.py`
- 说明：端到端测试，验证完整压缩管线在真实 agent 中运行

### 测试流程

1. 构建 90 条消息（30 轮 Human+AI+Tool），每条 ~5000 字符
2. 调用 `built_agent()` 构建真实 agent graph
3. 清除 summarization 状态
4. `agent.ainvoke()` 触发 `awrap_model_call` hook
5. 验证：preemptive truncation → prune → target truncate → LLM attempt → static fallback

### 验证结果

| 步骤                                    | 结果    | 说明                                      |
| --------------------------------------- | ------- | ----------------------------------------- |
| `awrap_model_call` hook 触发            | PASS    | 中间件正确拦截 model call                 |
| Preemptive truncation: 30 tool outputs  | PASS    | `_preemptive_truncate` 处理所有工具输出   |
| Prune reduced ~20000 tokens             | PASS    | `_prune_tool_outputs` 显著缩减            |
| Target truncation reduced ~33860 tokens | PASS    | `_target_truncate` 缩减至目标预算         |
| LLM summary attempt                     | BLOCKED | HIS Proxy 网络限制返回 HTML 页面          |
| Static fallback activated               | PASS    | `_build_static_fallback_summary` 正确激活 |
| Compressed messages contain summary     | PASS    | HumanMessage + AIMessage(summary) 格式    |

---

## 15. .env（配置）

### 关键配置

```env
## Chat model
MAIN_LLM_PROVIDER = openai
MAIN_LLM_NAME = maas-glm-5-aliyun-codeagent
MAIN_LLM_API_BASE = http://7.183.252.114:3005/codemate/v1
MAIN_LLM_API_KEY = t00627517
MAIN_LLM_MAX_TOKEN = 128000

## Reasoner model
REASONER_LLM_PROVIDER = openai
REASONER_LLM_NAME = maas-glm-5-aliyun-codeagent
REASONER_LLM_API_BASE = http://7.183.252.114:3005/codemate/v1
REASONER_LLM_API_KEY = t00627517
REASONER_LLM_MAX_TOKEN = 128000

## Auxiliary model
AUXILIARY_LLM_MODEL_LOCAL = false
AUXILIARY_LLM_PROVIDER = openai
AUXILIARY_LLM_API_NAME = maas-glm-5-aliyun-codeagent
AUXILIARY_LLM_API_BASE = http://7.183.252.114:3005/codemate/v1
AUXILIARY_LLM_API_KEY = t00627517
AUXILIARY_LLM_MAX_TOKEN = 128000
```

### 已知限制

LLM API 调用受 HIS Proxy 网络限制（`http://7.183.252.114:3005/codemate/v1` 返回 HIS Proxy 通知页面），无法验证 LLM summary 成功路径。Static fallback 路径已完全验证。

---

## 16. 环境版本升级

### 变更说明

升级 langgraph/langchain 相关包版本，解决 `cannot import name 'ExecutionInfo' from 'langgraph.runtime'` ImportError。

### 版本变更

| 包名                 | 旧版本 | 新版本 |
| -------------------- | ------ | ------ |
| `langgraph`          | 1.0.10 | 1.2.11 |
| `langgraph-prebuilt` | 1.0.13 | 1.1.0  |
| `langchain-openai`   | 1.1.9  | 1.6.0  |
| `langchain-core`     | 1.4.8  | 1.6.1  |
| `openai`             | 2.21.0 | 3.7.0  |

### 效果

升级后 `from langchain.agents import create_agent` 正常工作，不再需要 stub 模块。完整 agent 上下文可用于 e2e 测试。

---

## 文件清单汇总

| 文件路径                                    | 操作     | 行数变化                               |
| ------------------------------------------- | -------- | -------------------------------------- |
| `config/num.py`                             | 重写     | 12 → 65                                |
| `pub_func/message/estimate_msg_tokens.py`   | 重写     | 13 → 29                                |
| `pub_func/message/turn_utils.py`            | 新建     | 0 → 40                                 |
| `pub_func/message/tool_output_dedup.py`     | 新建     | 0 → 77                                 |
| `pub_func/message/tool_output_prune.py`     | 新建     | 0 → 77                                 |
| `pub_func/message/target_truncation.py`     | 新建     | 0 → 76                                 |
| `pub_func/message/__init__.py`              | 修改     | 5 → 18                                 |
| `tests/unit/test_message_utils.py`          | 修改     | 5 行断言更新                           |
| `agent/middlewares/summarization.py`        | 完全重写 | 493 → 649（Phase 1）→ 1262（Phase 2）  |
| `agent/core.py`                             | 修改     | +1 import, 改 3 行（Phase 1 触发阈值） |
| `agent/tools/subagent/spawn/core.py`        | 修改     | +2 import, 改 4 行（Phase 1 触发阈值） |
| `tests/unit/conftest.py`                    | 修改     | 添加 try/except 保护（+6 行）          |
| `tests/test_summarization_comprehensive.py` | 新建     | 0 → ~1009（172 测试用例）              |
| `tests/test_e2e_summarization.py`           | 新建     | 0 → 97                                 |
| 环境包升级                                  | 升级     | langgraph 1.0.10→1.2.11 等 5 个包      |
