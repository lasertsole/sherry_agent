# Thinking Token Budget — Implementation Plan

> Status: **Draft** (not yet implemented)
> Date: 2026-09-07
> Owner: Agent
> Predecessor: 输出侧 max_tokens 恢复（已实施 — `agent/middlewares/max_tokens_boost.py`)
> Related: 输入侧上下文上限检测原本计划为 `ContextLimitGuardWrapper`
> (`agent/context_limit_guard_wrapper.py`)，但**未实施**——该计划文档
> (`TODO/CONTEXT_LIMIT_GUARD_WRAPPER.md`) 已删除，无对应实现/测试。

---

## 1. Background

### 1.1 问题

Thinking/reasoning 模型（Anthropic extended thinking、DeepSeek thinking、GLM thinking、OpenAI o-series）在产出可见文本前先产生 chain-of-thought (CoT) tokens。这些 thinking tokens 与 output tokens **共享 `max_tokens` 预算**。

当前 sherry_agent 在 4 个层面都没有感知这一点：

| 层面                         | 现状                                                                               | 后果                                                                                                            |
| ---------------------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **初始 max_tokens 未设**     | `main_llm.py` 的 `model_config` 不含 `max_tokens`，用 provider 默认值              | thinking 开启时预算可能不足，thinking 吃完后 output 空间所剩无几                                                |
| **Anthropic budget 硬编码**  | `reasoning_payload.py:67` `_DEFAULT_ANTHROPIC_BUDGET = 2000`，不随 max_tokens 调整 | 2000 对 8192 base 尚可，但对更大的 thinking 需求无法动态扩展                                                    |
| **截断检测不区分 thinking**  | `_TRUNCATION_REASONS={"length","max_tokens"}` 不区分"thinking 耗尽"还是"输出过长"  | thinking-only 截断时续写 prompt 说"继续之前的内容"，但之前只有思考没有内容 → 模型可能重新思考 → 再次耗尽 → 空转 |
| **Subagent 缺 boost 中间件** | `spawn/core.py:762-786` middleware 链无 `MaxTokensBoostMiddleware`                 | subagent 工具调用截断无法自动 boost，静默失败                                                                   |
| **reasoning_tokens 未追踪**  | `output_tokens` 直接用 provider 返回值，不拆分                                     | 无法观测 thinking 占了多少 token                                                                                |
| **boost base 硬编码**        | `max_tokens_boost.py:30` `_BASE_MAX_TOKENS = 8192`，不从 request 读取实际值        | 初始 call 设了更高的 max_tokens 后，boost 仍从 8192 起算，回退                                                  |

### 1.2 跨项目调研结论

| 项目             | thinking budget 膨胀 max_tokens                                         | thinking 耗尽检测                                                                                    | reasoning-only 重试                                                                       | reasoning_tokens 追踪                                                  | subagent 覆盖 |
| ---------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------- |
| **hermes-agent** | `max(effective, budget+4096)` (anthropic_adapter.py:2663)               | think tags 检查 + `⚠️ Thinking Budget Exhausted` 用户提示 (conversation_loop.py:1808-1867)           | Codex `incomplete` 3 次重试 (conversation_loop.py:1637-1645)                              | `normalize_usage()` 提取 `reasoning_tokens` (usage_pricing.py:883-907) | 全局统一      |
| **openclaw**     | `maxTokens = baseMaxTokens + thinkingBudget` (simple-options.ts:72-101) | `assessLastAssistantMessage()` → `"incomplete-thinking"` / `"incomplete-text"` (thinking.ts:351-392) | `DEFAULT_REASONING_ONLY_RETRY_LIMIT=2` + 专用重试指令 (incomplete-turn-recovery.ts:32-35) | `output_tokens_details.reasoning_tokens`                               | 全局统一      |
| **sherry_agent** | **无**                                                                  | **无**                                                                                               | **无**                                                                                    | **无**                                                                 | **缺失**      |

### 1.3 架构约束

1. **thinking tokens 计入 max_tokens**：Anthropic 文档明确 "thinking tokens count toward the limit"（hermes-agent 代码注释同述）。DeepSeek/GLM 的 `extra_body {"thinking": {"type": "enabled"}}` 不提供独立 budget 参数——thinking tokens 与 output tokens 共享 `max_tokens`。

2. **`reasoning_payload.py` 的 provider 映射**：
   - Anthropic → `{"thinking": {"type": "enabled", "budget_tokens": 2000}}`（显式 budget）
   - DeepSeek → `{"extra_body": {"thinking": {"type": "enabled"}}}`（无 budget 参数）
   - GLM → 同 DeepSeek
   - OpenAI o-series → `{"reasoning_effort": "high"}`（无 budget 参数）

3. **`request.model_settings` 与 model config 分离**：`build_reasoning_kwargs()` 返回的 kwargs 合并到 `init_chat_model()` 的 `model_config`，成为模型的 bound kwargs。middleware 的 `request.model_settings` 是 per-request 的，通过 `model.bind(**request.model_settings)` 传递。middleware 无法直接从 request 感知 thinking 是否开启——需要通过 env 或 model config 层传递。

4. **subagent 用 `ainvoke`（非流式）**：`spawn/core.py:560-571` 的 subagent 执行路径是非流式的。`MaxTokensBoostMiddleware` 的 `is_stream_turn` flag 默认 False，走非流式 re-call 路径，callback stripping 不触发——行为正确。

5. **续写 HumanMessage 被 checkpointer 持久化**：续写 prompt 会出现在对话历史中。thinking-only 续写 prompt 需要用 `[System: ...]` 前缀，客户端可识别并隐藏。

6. **`usage_metadata` 中的 reasoning_tokens**：部分 provider（Anthropic、GLM）在 `usage_metadata` 中返回 `output_token_details.reasoning_tokens`。LangChain 的 `usage_metadata` schema 支持此字段但 sherry_agent 从未读取。

---

## 2. 方案概览

| Phase | 目标                                   | 改造层                                          | 复杂度 | 文件数 | 状态  |
| ----- | -------------------------------------- | ----------------------------------------------- | ------ | ------ | ----- |
| A     | 主动膨胀 max_tokens（thinking budget） | reasoning_payload + main_llm + max_tokens_boost | 中     | 3      | Draft |
| B     | Reasoning-only 截断检测 + 专用重试     | stream_dispatch + messages                      | 中     | 2      | Draft |
| C     | Subagent MaxTokensBoost 中间件对齐     | spawn/core                                      | 低     | 1      | Draft |
| D     | reasoning_tokens 单独追踪              | stream_dispatch + messages + store/core         | 低     | 3      | Draft |

### 实施优先级

```
Phase C (1行, 立即) ──→ Phase A (根因, 最高) ──→ Phase B (检测+重试) ──→ Phase D (可观测)
```

### 决策摘要

| 问题                                        | 决策                                                                  | 理由                                                                                    |
| ------------------------------------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| max_tokens 在哪设？                         | `main_llm.py` 的 `model_config`                                       | thinking budget 在 model 构建时已知，直接写入 config 最简洁                             |
| thinking budget 怎么传给 boost middleware？ | middleware 从 `request.model_settings["max_tokens"]` 读 boost base    | 如初始 call 已设 max_tokens=12288，boost 从 12288 起算而非 8192                         |
| DeepSeek/GLM 无显式 budget 怎么办？         | env `MAIN_LLM_THINKING_BUDGET`（默认 4096）                           | 无 API 参数可读，只能估；env 可按需调整                                                 |
| reasoning-only 检测在哪做？                 | `stream_dispatch.py` 累积 `_has_reasoning` / `_has_visible_text` flag | 流式处理时已有 reasoning delta 和 text 分支，设 flag 开销极低                           |
| reasoning-only 重试 prompt？                | 专用 prompt，不复用 `_CONTINUATION_PROMPT`                            | "继续之前的内容"对纯思考场景无效，需"产出答案，不要重新思考"                            |
| subagent 的 boost？                         | 直接加 `MaxTokensBoostMiddleware()` 到中间件链                        | subagent 用 `ainvoke`，is_stream 默认 False，非流式 re-call 路径无需 callback stripping |
| reasoning_tokens 从哪读？                   | `usage_metadata["output_token_details"]["reasoning_tokens"]`          | LangChain schema 已支持，provider 已返回，只是从未读取                                  |

---

## 3. Phase A: 主动膨胀 max_tokens ✏️

### 3.1 目标

当 thinking 启用时，在初始 model call 前主动设置 `max_tokens = output_base + thinking_budget`，使模型有足够预算容纳 thinking + output。同时让 boost middleware 从实际 `max_tokens` 起算，不回退到硬编码 base。

### 3.2 核心流程

```
┌─ 初始 call 路径 ──────────────────────────────────────────────────┐
│                                                                    │
│  main_llm.py                                                       │
│    model_config = {                                                │
│      ...build_reasoning_kwargs(...),                               │
│      "max_tokens": OUTPUT_MAX_TOKEN + thinking_budget,  ← 新增    │
│    }                                                               │
│    model = NormalizingChatModel(init_chat_model(**model_config))   │
│                                                                    │
│  → 模型第一次 call 就有足够预算                                      │
│    thinking 用掉 budget 部分，output 有 OUTPUT_MAX_TOKEN 空间       │
└────────────────────────────────────────────────────────────────────┘

┌─ Boost 路径（截断时）──────────────────────────────────────────────┐
│                                                                    │
│  MaxTokensBoostMiddleware.awrap_model_call                          │
│    result = await handler(request)  ← 使用初始 max_tokens           │
│    if truncation + tool_calls:                                     │
│      current_base = request.model_settings.get("max_tokens")       │
│                     or _BASE_MAX_TOKENS  ← 从 request 读，不硬编码  │
│      for attempt in 1..3:                                          │
│        boosted = min(current_base * 2^attempt, _MAX_CAP)           │
│        _inject_boost(request, boosted)                              │
│        ...                                                         │
└────────────────────────────────────────────────────────────────────┘
```

### 3.3 文件变更

#### `models/LLMs/reasoning_payload.py`

新增函数 `get_thinking_budget()` 和导出预算常量：

```python
# 现有
_DEFAULT_ANTHROPIC_BUDGET = 2000

# 新增
_DEFAULT_NON_ANTHROPIC_BUDGET = int(os.getenv("MAIN_LLM_THINKING_BUDGET", "4096"))


def get_thinking_budget(
    provider: str | None,
    model_name: str | None,
    enabled: bool,
) -> int:
    """Return the estimated thinking token budget for the given provider/model.

    Returns 0 when thinking is disabled or the provider has no thinking support.
    For Anthropic, returns the explicit ``budget_tokens`` value (2000).
    For DeepSeek/GLM/OpenAI o-series (no explicit budget param), returns an
    env-configurable estimate (default 4096) — these providers share thinking
    and output tokens against ``max_tokens`` with no separate budget API.

    Used by ``main_llm.py`` to inflate ``max_tokens`` proactively so that
    thinking + output both fit within the limit.
    """
    if not enabled or not provider:
        return 0

    provider = provider.strip().lower()

    if provider == "anthropic":
        if model_name and _is_anthropic_reasoning_model(model_name):
            return _DEFAULT_ANTHROPIC_BUDGET
        return 0

    if provider == "deepseek":
        return _DEFAULT_NON_ANTHROPIC_BUDGET

    if provider in _OPENAI_COMPATIBLE:
        if model_name and is_zhipu_reasoning_model(model_name):
            return _DEFAULT_NON_ANTHROPIC_BUDGET
        if model_name and is_openai_reasoning_model(model_name):
            return _DEFAULT_NON_ANTHROPIC_BUDGET
        return 0

    return 0
```

更新 `__all__`：

```python
__all__ = [
    "build_reasoning_kwargs",
    "get_thinking_budget",
    "is_openai_reasoning_model",
    "is_zhipu_reasoning_model",
]
```

#### `models/LLMs/main_llm.py`

thinking 启用时主动设置 `max_tokens`：

```python
from reasoning_payload import build_reasoning_kwargs, get_thinking_budget

# 现有逻辑
reasoning_kwargs = build_reasoning_kwargs(
    provider=model_provider,
    model_name=model_name,
    enabled=enable_thinking,
    reasoning_effort=reasoning_effort,
)

# 新增：计算 thinking budget 并膨胀 max_tokens
thinking_budget = get_thinking_budget(
    provider=model_provider,
    model_name=model_name,
    enabled=enable_thinking,
)

model_config = {
    **reasoning_kwargs,
    # ... 其他现有配置
}

if thinking_budget > 0:
    output_base = int(os.getenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "8192"))
    model_config["max_tokens"] = output_base + thinking_budget
    # e.g. 8192 + 2000 = 10192 (Anthropic)
    # e.g. 8192 + 4096 = 12288 (DeepSeek/GLM)
```

#### `agent/middlewares/max_tokens_boost.py`

boost base 从 `request.model_settings` 读取实际 `max_tokens`，不回退到硬编码值：

```python
# 现有
_BASE_MAX_TOKENS = int(os.getenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "8192"))


# 新增辅助方法
def _get_current_base(self, request) -> int:
    """Read the effective max_tokens base from the request, falling back
    to the env default. This ensures boost starts from the ACTUAL max_tokens
    (which may have been inflated for thinking budget) rather than a hardcoded
    value that ignores the inflation."""
    settings = getattr(request, "model_settings", None) or {}
    current = settings.get("max_tokens")
    if isinstance(current, int) and current > 0:
        return current
    return _BASE_MAX_TOKENS


# 修改 awrap_model_call 中的 boost 循环
async def awrap_model_call(self, request, handler):
    session_id = self._get_session_id(request)
    is_stream = state_register_mem.get_state(session_id, _STREAM_FLAG, False)

    result = await handler(request)
    if not self._detect_tool_call_truncation(result):
        return result

    base = self._get_current_base(request)  # ← 新增：从 request 读

    saved_callbacks = None
    if is_stream:
        saved_callbacks = self._strip_callbacks(request)

    try:
        for attempt in range(1, _MAX_RETRIES + 1):
            boosted = min(base * (2**attempt), _MAX_CAP)  # ← 用 base 而非 _BASE_MAX_TOKENS
            self._inject_boost(request, boosted)
            logger.debug(
                f"Tool-call truncation retry {attempt}/{_MAX_RETRIES}, "
                f"max_tokens={boosted} (base={base}): session_id={session_id}"
            )
            result = await handler(request)
            if not self._detect_tool_call_truncation(result):
                break
        else:
            logger.warning(
                f"max_tokens boost exhausted after {_MAX_RETRIES} retries "
                f"(base={base}): session_id={session_id}"
            )
    finally:
        if is_stream:
            self._restore_callbacks(request, saved_callbacks)

    return result
```

### 3.4 环境变量

```env
# 现有
MAIN_LLM_OUTPUT_MAX_TOKEN=8192        # output token limit base
MAIN_LLM_MAX_TOKEN=65536              # context window (input)

# 新增
MAIN_LLM_THINKING_BUDGET=4096         # estimated thinking budget for non-Anthropic providers
                                       # (Anthropic uses explicit budget_tokens=2000)
```

### 3.5 效果对比

| 场景               | 现状                                         | Phase A 后                         |
| ------------------ | -------------------------------------------- | ---------------------------------- |
| Anthropic thinking | max_tokens 未设 → provider 默认（可能 4096） | max_tokens = 8192 + 2000 = 10192   |
| DeepSeek thinking  | max_tokens 未设 → provider 默认              | max_tokens = 8192 + 4096 = 12288   |
| GLM thinking       | max_tokens 未设 → provider 默认              | max_tokens = 8192 + 4096 = 12288   |
| 无 thinking        | max_tokens 未设 → provider 默认              | 不变（budget=0，不设 max_tokens）  |
| 截断后 boost       | 从 8192 起算                                 | 从实际 max_tokens 起算（如 12288） |

---

## 4. Phase B: Reasoning-only 截断检测 + 专用重试 ✏️

### 4.1 目标

当 `finish_reason="length"` 且响应**只含 thinking、无可见文本、无 tool calls** 时，使用专用重试 prompt（"产出答案，不要重新思考"），而非通用的续写 prompt（"继续之前的内容"）。

### 4.2 问题场景

```
模型收到用户消息
    │
    ▼
模型开始 thinking → 产出 reasoning_content delta → 消耗 max_tokens
    │
    ├─ thinking 用完预算 → finish_reason="length"
    │   ├─ ai_text = "" (无可见文本)
    │   ├─ has_tool_calls = False
    │   └─ has_reasoning = True
    │
    ▼
当前 _should_text_continue() 返回 True（finish_reason=length + 无 tool_calls）
    │
    ▼
注入 _CONTINUATION_PROMPT: "继续之前的内容"
    │
    ▼
模型收到续写 → 之前的消息只有 thinking → "继续"什么？
    │
    ├─ 可能重新开始 thinking → 再次耗尽 → 空转
    └─ 可能直接产出答案（运气好时）
```

### 4.3 核心流程

```
┌─ StreamTurn.run() while 循环 ──────────────────────────────────────┐
│                                                                    │
│  [stream chunks]                                                   │
│    ├─ text chunk → ai_text += content, _has_visible_text = True    │
│    ├─ reasoning chunk → yield reasoning, _has_reasoning = True     │
│    ├─ tool_call chunk → _has_tool_calls = True                     │
│    └─ final chunk → meta_finish_reason, meta_output_tokens         │
│                                                                    │
│  if _should_text_continue():                                       │
│    is_reasoning_only = (                                                           │
│      self.meta_finish_reason in ("length", "max_tokens")          │
│      and not self._has_tool_calls                                 │
│      and not self._has_visible_text      ← 新增判断                │
│      and self._has_reasoning              ← 新增判断                │
│      and self._continuation_retries < _MAX_CONTINUATION_RETRIES   │
│    )                                                               │
│    _prepare_continuation(is_reasoning_only)                        │
│      ├─ reasoning_only → _is_continuation = True                  │
│      │                    _is_reasoning_only = True                │
│      └─ normal → _is_continuation = True                          │
│                     _is_reasoning_only = False                    │
│    continue                                                        │
│  break                                                             │
└────────────────────────────────────────────────────────────────────┘

┌─ _create_source() ─────────────────────────────────────────────────┐
│                                                                    │
│  if self._is_continuation:                                         │
│    prompt = (_REASONING_ONLY_PROMPT                                │
│              if self._is_reasoning_only                             │
│              else _CONTINUATION_PROMPT)                            │
│    input_dict = {                                                  │
│      "session_id": self.session_id,                                │
│      "messages": [HumanMessage(content=prompt)],                   │
│    }                                                               │
└────────────────────────────────────────────────────────────────────┘
```

### 4.4 文件变更

#### `server/service/stream_dispatch.py`

**`StreamTurn.__init__`** — 新增两个状态 flag：

```python
def __init__(self, session_id: str) -> None:
    self.session_id = session_id
    self.ai_text: str = ""
    self.meta_model_name: str | None = None
    self.meta_input_tokens: int | None = None
    self.meta_output_tokens: int | None = None
    self.meta_finish_reason: str | None = None
    self._has_tool_calls: bool = False
    self._has_reasoning: bool = False  # ← 新增
    self._has_visible_text: bool = False  # ← 新增
```

**reasoning delta 块** (L458-460) — 收到 reasoning 时设 flag：

```python
_reasoning = _reasoning_delta(msg_chunk)
if _reasoning and len(_reasoning) > 0:
    self._has_reasoning = True  # ← 新增
    yield {"type": "reasoning", "content": _reasoning}
```

**text 块** (L443-446) — 有可见文本时设 flag：

```python
if len(msg_chunk.content) > 0:
    res: str = msg_chunk.content
    self.ai_text += res
    self._has_visible_text = True  # ← 新增
    yield {"type": "text", "content": res}
```

**`_should_text_continue` 虚方法** — 增加返回 reasoning-only 判定：

```python
def _should_text_continue(self) -> tuple[bool, bool]:
    """Return (should_continue, is_reasoning_only).

    is_reasoning_only is only meaningful when should_continue is True.
    """
    return False, False
```

#### `server/service/messages.py`

**新增常量**：

```python
_CONTINUATION_PROMPT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)

# 新增
_REASONING_ONLY_PROMPT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Only reasoning/thinking was produced with no "
    "visible answer. Produce the answer now — do not re-reason "
    "or restart the thinking process. Output the final answer directly.]"
)

_MAX_CONTINUATION_RETRIES = 4
_MAX_REASONING_ONLY_RETRIES = 2  # ← 新增：reasoning-only 重试上限（与 openclaw 对齐）
```

**`_GenerateTurn.__init__`** — 新增字段：

```python
def __init__(self, ...):
    super().__init__(session_id)
    # ... 现有字段
    self._continuation_retries: int = 0
    self._is_continuation: bool = False
    self._is_reasoning_only: bool = False        # ← 新增
    self._reasoning_only_retries: int = 0        # ← 新增
```

**`_should_text_continue` 覆写** — 区分两种截断场景：

```python
def _should_text_continue(self) -> tuple[bool, bool]:
    """Return (should_continue, is_reasoning_only).

    Two truncation scenarios:
    1. Text truncation: finish_reason=length, no tool_calls, has visible text
       → use _CONTINUATION_PROMPT ("continue where you left off")
    2. Reasoning-only truncation: finish_reason=length, no tool_calls,
       no visible text, has reasoning
       → use _REASONING_ONLY_PROMPT ("produce the answer, don't re-reason")
    """
    if self.meta_finish_reason not in ("length", "max_tokens"):
        return False, False
    if self._has_tool_calls:
        return False, False  # Phase 3 handles tool-call truncation

    if not self._has_visible_text and self._has_reasoning:
        # Reasoning-only truncation
        if self._reasoning_only_retries >= self._MAX_REASONING_ONLY_RETRIES:
            return False, False
        return True, True

    # Normal text truncation
    if self._continuation_retries >= self._MAX_CONTINUATION_RETRIES:
        return False, False
    return True, False
```

**`_prepare_continuation` 覆写** — 根据 reasoning-only 选择 prompt：

```python
def _prepare_continuation(self, is_reasoning_only: bool = False) -> None:
    self._is_continuation = True
    self._is_reasoning_only = is_reasoning_only

    if is_reasoning_only:
        self._reasoning_only_retries += 1
        logger.debug(
            f"Reasoning-only continuation retry "
            f"{self._reasoning_only_retries}/"
            f"{self._MAX_REASONING_ONLY_RETRIES}: "
            f"session_id={self.session_id}"
        )
    else:
        self._continuation_retries += 1
        logger.debug(
            f"Text continuation retry {self._continuation_retries}/"
            f"{self._MAX_CONTINUATION_RETRIES}: "
            f"session_id={self.session_id}"
        )

    # Reset per-iteration state
    self.meta_finish_reason = None
    self._has_tool_calls = False
    self._has_reasoning = False
    self._has_visible_text = False
```

**`_create_source` 覆写** — 选择 prompt：

```python
async def _create_source(self) -> tuple[Literal["stream", "invoke"], Any]:
    if self._is_continuation:
        prompt = (
            self._REASONING_ONLY_PROMPT if self._is_reasoning_only else self._CONTINUATION_PROMPT
        )
        input_dict = {
            "session_id": self.session_id,
            "messages": [HumanMessage(content=prompt)],
        }
    else:
        content_list: list[dict[str, str]] = _get_content_list(self.multi_modal_message)
        input_dict = {
            "session_id": self.session_id,
            "messages": [HumanMessage(content=content_list, metadata=self.origin)],
        }
    # ... rest unchanged
```

**`stream_dispatch.py` while 循环调用处** (L464-466) — 适配新的返回签名：

```python
# 现有
if self._should_text_continue():
    self._prepare_continuation()
    continue
break

# 修改后
_should, _is_ro = self._should_text_continue()
if _should:
    self._prepare_continuation(is_reasoning_only=_is_ro)
    continue
break
```

**`_ResumeTurn`** — 继承基类的 `(False, False)` 返回，不受影响。

### 4.5 重试预算对比

| 场景                              | 当前                             | Phase B 后                                             |
| --------------------------------- | -------------------------------- | ------------------------------------------------------ |
| 文本截断                          | 最多 4 次 `_CONTINUATION_PROMPT` | 不变（4 次）                                           |
| Reasoning-only 截断               | 走文本截断路径，最多 4 次        | 最多 2 次 `_REASONING_ONLY_PROMPT`（与 openclaw 对齐） |
| Mixed（先 thinking 后文本被截断） | 走文本截断路径                   | 走文本截断路径（有 visible_text → 非 reasoning-only）  |

### 4.6 续写时 thinking 的行为

| 场景                | 模型行为                                      | 系统行为                                                        |
| ------------------- | --------------------------------------------- | --------------------------------------------------------------- |
| 文本截断续写        | 模型可能重新 thinking（消耗预算）然后继续输出 | 可接受——续写时 thinking 有预算空间（Phase A 膨胀了 max_tokens） |
| Reasoning-only 续写 | 模型被要求"不要重新思考"，直接产出答案        | prompt 明确禁止 re-reason，模型应直接输出                       |

---

## 5. Phase C: Subagent MaxTokensBoost 中间件对齐 ✏️

### 5.1 目标

在 subagent 的 middleware 链中添加 `MaxTokensBoostMiddleware()`，使 subagent 的工具调用截断也能自动 boost max_tokens。

### 5.2 问题场景

```
Subagent (ORCHESTRATOR/LEAF) 执行
    │
    ├─ 模型启用 thinking → thinking + tool_call JSON 超出 max_tokens
    │   ├─ finish_reason = "length"
    │   ├─ has_tool_calls = True
    │   └─ tool_call JSON 被截断（不完整）
    │
    ▼
当前 subagent middleware 链无 MaxTokensBoostMiddleware
    │
    ▼
截断的 tool_call JSON 传给 ToolNode → JSON 解析失败 → tool 执行错误
    │
    ▼
错误传回模型 → 模型重试（消耗 IterationBudget）→ 可能再次截断 → 恶性循环
```

### 5.3 文件变更

#### `agent/tools/subagent/spawn/core.py`

**imports** (L724-730) — 新增导入：

```python
from agent.middlewares import (
    IterationBudget,
    ToolGuardrails,
    ToolCallNormalize,
    Summarization,
    HeartbeatStaleness,
    MaxTokensBoostMiddleware,  # ← 新增
)
from agent.middlewares.output_repetition_guard import OutputRepetitionGuard
```

**middleware 列表** (L762-786) — 添加 `MaxTokensBoostMiddleware()`：

```python
child_agent = create_agent(
    model=child_llm,
    system_prompt=system_prompt,
    state_schema=StateSchema,
    checkpointer=child_checkpointer,
    tools=filtered_tools,
    middleware=[
        Summarization(
            model=auxiliary_llm,
            main_llm_context_window=main_llm_max_tokens,
            trigger=[
                ("messages", 40),
                ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
            ],
            keep=("messages", 10),
        ),
        IterationBudget(60),
        ToolGuardrails(),
        OutputRepetitionGuard(),
        ToolCallNormalize(),
        MaxTokensBoostMiddleware(),  # ← 新增：在 ToolCallNormalize 之后、HeartbeatStaleness 之前
        HeartbeatStaleness(),
    ],
)
```

### 5.4 注意事项

| 维度                     | 说明                                                                                                                                                                                                                                   |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **is_stream_turn flag**  | subagent 用 `ainvoke`（非流式），`is_stream_turn` 从未被设置 → middleware 的 `is_stream = state_register_mem.get_state(session_id, _STREAM_FLAG, False)` 返回 False → 走非流式 re-call 路径 → callback stripping 不触发 → **行为正确** |
| **IterationBudget 消耗** | 与主 agent 一致：re-call 在 `awrap_model_call` 内完成，IterationBudget 只计 1 次（+0 额外消耗）                                                                                                                                        |
| **boost base**           | Phase A 后，如 subagent 的 LLM 启用了 thinking，`main_llm.py` 已设 `max_tokens = base + budget`。middleware 从 `request.model_settings` 读取此值作为 boost base                                                                        |
| **subagent 的 LLM 选择** | ORCHESTRATOR 用 `build_main_llm()`（含 thinking），LEAF 用 `build_auxiliary_llm()`（可能不含 thinking）。两种情况均安全——无 thinking 时 budget=0，max_tokens 不膨胀                                                                    |

### 5.5 中间件顺序说明

```
Summarization          ← 压缩历史
IterationBudget(60)    ← 迭代计数
ToolGuardrails          ← 工具调用验证
OutputRepetitionGuard  ← 重复检测
ToolCallNormalize      ← 工具调用格式归一化
MaxTokensBoostMiddleware  ← 新增：截断时 boost（在 Normalize 之后，因为需要解析后的 tool_calls 来判断截断）
HeartbeatStaleness     ← 心跳检测
```

**为什么在 ToolCallNormalize 之后**：`_detect_tool_call_truncation` 检查 `ai_msg.tool_calls`，需要 ToolCallNormalize 已处理的规范 tool_calls 格式。

**为什么在 HeartbeatStaleness 之前**：boost re-call 可能耗时较长，不应被 HeartbeatStaleness 误判为卡死。HeartbeatStaleness 在 boost 之后执行，看到的是 boost 完成后的状态。

---

## 6. Phase D: reasoning_tokens 单独追踪 ✏️

### 6.1 目标

从 `usage_metadata` 中提取 `reasoning_tokens`，单独追踪并透传给客户端和 DB，使 thinking token 消耗可观测。

### 6.2 数据来源

部分 provider 在 `usage_metadata` 中返回 `output_token_details`：

```python
# Anthropic 的 usage_metadata 示例
{
    "input_tokens": 1234,
    "output_tokens": 8192,
    "output_token_details": {
        "reasoning_tokens": 6000  # ← 6000 是 thinking, 2192 是实际输出
    },
}

# DeepSeek/GLM 的 usage_metadata 示例（通过 OpenAI-compatible 网关）
{"input_tokens": 1234, "output_tokens": 8192, "output_token_details": {"reasoning_tokens": 4096}}
```

当前 `stream_dispatch.py:330-335` 只读 `input_tokens` 和 `output_tokens`，`output_token_details` 被忽略。

### 6.3 文件变更

#### `server/service/stream_dispatch.py`

**`StreamTurn.__init__`** — 新增字段：

```python
def __init__(self, session_id: str) -> None:
    # ... 现有
    self.meta_reasoning_tokens: int | None = None  # ← 新增
```

**metadata 捕获块** (L330-335) — 提取 reasoning_tokens：

```python
_usage = getattr(msg_chunk, "usage_metadata", None)
if _usage:
    if _usage.get("input_tokens") is not None:
        self.meta_input_tokens = int(_usage["input_tokens"])
    if _usage.get("output_tokens") is not None:
        self.meta_output_tokens = int(_usage["output_tokens"])
    # ← 新增
    _details = _usage.get("output_token_details") or {}
    if _details.get("reasoning_tokens") is not None:
        self.meta_reasoning_tokens = int(_details["reasoning_tokens"])
```

#### `server/service/messages.py`

**`_GenerateTurn._final_frames()`** — meta chunk 增加字段：

```python
return [
    {
        "type": "meta",
        "content": "",
        "model_name": self.meta_model_name or "",
        "input_tokens": self.meta_input_tokens or 0,
        "output_tokens": self.meta_output_tokens or 0,
        "reasoning_tokens": self.meta_reasoning_tokens or 0,  # ← 新增
        "finish_reason": self.meta_finish_reason or "",
    }
]
```

#### `server/service/stream_driver.py`

**done frame** — 增加 `reasoning_tokens` 字段（与 meta chunk 对齐）。

#### `context_engine/store/core.py`

**AI 消息持久化** (L133, L188, L212) — 持久化 reasoning_tokens：

```python
# 在现有 response_metadata 提取处
_reasoning_tokens = None
_usage_meta = response_metadata.get("usage_metadata") or {}
_details = _usage_meta.get("output_token_details") or {}
if _details.get("reasoning_tokens") is not None:
    _reasoning_tokens = int(_details["reasoning_tokens"])

# 存入 DB
"reasoning_tokens": _reasoning_tokens,
```

**DB schema** (如 `context_engine/store/core.py` 的 CREATE TABLE)：

```sql
-- 在 messages 表新增列
reasoning_tokens INTEGER,   -- AI 消息：usage_metadata output_token_details.reasoning_tokens
```

### 6.4 客户端可观测性

客户端收到 meta/done frame 后可显示：

```
⚡ Tokens: input 1234 · output 8192 (reasoning 6000 · answer 2192)
```

使 thinking 的 token 消耗可见，帮助用户理解为什么 response 被截断（"thinking 用了 6000/8192"）。

---

## 7. 四 Phase 协同流程

```
用户消息
    │
    ▼
StreamTurn.run()  ─── while True ──────────────────────────┐
    │                                                      │
    ├─ _create_source() → agent.astream/ainvoke             │
    │                                                      │
    │   ┌── middleware chain (per model call) ─────────┐  │
    │   │ IterationBudget.awrap_model_call  (+1 计数)   │  │
    │   │  └→ MaxTokensBoost.awrap_model_call           │  │
    │   │       base = _get_current_base(request)       │  │
    │   │             = request.model_settings["max_tokens"]
    │   │             = OUTPUT_MAX_TOKEN + thinking_budget  ← Phase A
    │   │       result = handler(request)  ← 1st call   │  │
    │   │       if truncation + tool_calls:             │  │
    │   │         strip callbacks (if streaming)        │  │
    │   │         for attempt in 1..3:                  │  │
    │   │           boosted = base * 2^attempt           │  │
    │   │           handler(request) → no streaming      │  │
    │   │           if not truncation: break             │  │
    │   │         restore callbacks (finally)            │  │
    │   │       return final result                      │  │
    │   │ OutputRepetitionGuard                          │  │
    │   │   (tracks reasoning separately)               │  │
    │   └──────────────────────────────────────────────┘  │
    │                                                      │
    │   [agent loop: model → tools → model → ...]          │
    │                                                      │
    │   [stream chunks]                                    │
    │     ├─ text → ai_text +=, _has_visible_text = True  │  ← Phase B
    │     ├─ reasoning → yield, _has_reasoning = True      │  ← Phase B
    │     ├─ tool_call → _has_tool_calls = True            │
    │     └─ final → meta_finish_reason,                  │
    │                meta_output_tokens,                   │
    │                meta_reasoning_tokens                 │  ← Phase D
    │                                                      │
    ├─ _should_text_continue() → (should, is_reasoning_only)  ← Phase B
    │   ├─ (True, False): text truncation                  │
    │   │   → _prepare_continuation(is_reasoning_only=False)│
    │   │   → _CONTINUATION_PROMPT                         │
    │   │   → continue ──────────────────────────────────┤
    │   ├─ (True, True): reasoning-only truncation         │
    │   │   → _prepare_continuation(is_reasoning_only=True) │
    │   │   → _REASONING_ONLY_PROMPT                       │
    │   │   → continue ──────────────────────────────────┤
    │   └─ (False, _): break ─────────────────────────── │
    │                                          │           │
    ├─ yield _final_frames()  (meta chunk)    │           │
    │     includes: reasoning_tokens           │           │  ← Phase D
    └─ _cleanup()  (close source, clear flags) │           │
                                               └───────────┘
```

**Phase 互斥与协同**：

| finish_reason | _has_tool_calls | _has_visible_text | _has_reasoning | 触发                                               |
| ------------- | --------------- | ----------------- | -------------- | -------------------------------------------------- |
| length        | True            | *                 | *              | Phase 3 (middleware boost)                         |
| length        | False           | True              | *              | Phase 2 text continuation                          |
| length        | False           | False             | True           | Phase 2 reasoning-only continuation (Phase B 新增) |
| length        | False           | False             | False          | Phase 2 text continuation (兜底)                   |
| stop          | *               | *                 | *              | 无续写                                             |

---

## 8. 完整文件变更清单

### 修改文件

| 文件                                    | Phase | 变更                                                                                                                                                                                                                                                                                                    |
| --------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `models/LLMs/reasoning_payload.py`      | A     | 新增 `get_thinking_budget()` 函数 + `_DEFAULT_NON_ANTHROPIC_BUDGET` 常量；更新 `__all__`                                                                                                                                                                                                                |
| `models/LLMs/main_llm.py`               | A     | thinking 启用时在 `model_config` 中设 `max_tokens = OUTPUT_MAX_TOKEN + thinking_budget`                                                                                                                                                                                                                 |
| `agent/middlewares/max_tokens_boost.py` | A     | 新增 `_get_current_base()` 方法；boost 循环用 `base` 替代 `_BASE_MAX_TOKENS`                                                                                                                                                                                                                            |
| `server/service/stream_dispatch.py`     | B,D   | `__init__` 加 `_has_reasoning`/`_has_visible_text`/`meta_reasoning_tokens`；reasoning/text 块设 flag；usage 块提取 reasoning_tokens；`_should_text_continue` 返回签名改为 tuple                                                                                                                         |
| `server/service/messages.py`            | B,D   | 新增 `_REASONING_ONLY_PROMPT`/`_MAX_REASONING_ONLY_RETRIES`；`__init__` 加 `_is_reasoning_only`/`_reasoning_only_retries`；覆写 `_should_text_continue`（返回 tuple）；覆写 `_prepare_continuation`（接受 `is_reasoning_only` 参数）；`_create_source` 选择 prompt；`_final_frames` 加 reasoning_tokens |
| `server/service/stream_driver.py`       | D     | done frame 加 `reasoning_tokens` 字段                                                                                                                                                                                                                                                                   |
| `context_engine/store/core.py`          | D     | 提取 `output_token_details.reasoning_tokens` 并持久化；DB schema 加 `reasoning_tokens` 列                                                                                                                                                                                                               |
| `agent/tools/subagent/spawn/core.py`    | C     | imports 加 `MaxTokensBoostMiddleware`；middleware 列表加 `MaxTokensBoostMiddleware()`                                                                                                                                                                                                                   |

### 不变文件

| 文件                                           | 原因                                                                                   |
| ---------------------------------------------- | -------------------------------------------------------------------------------------- |
| `models/LLMs/reasoning_normalizer.py`          | 归一化逻辑不变——仍将 reasoning_content 统一到 `additional_kwargs["reasoning_content"]` |
| `models/LLMs/reasoning_openai.py`              | OpenAI-compatible reasoning 提取逻辑不变                                               |
| `agent/middlewares/output_repetition_guard.py` | 已有 reasoning 独立追踪，不涉及 token budget                                           |
| `agent/middlewares/iteration_budget.py`        | boost re-call 在 middleware 内完成，+0 额外消耗                                        |
| `agent/stream_repetition_guard_wrapper.py`     | 不涉及 token limit                                                                     |
| `_ResumeTurn` (messages.py)                    | 继承基类 `(False, False)`，不受 Phase B 影响                                           |

---

## 9. 环境变量

```env
# ── 现有 ──────────────────────────────────────────────────────────
MAIN_LLM_MAX_TOKEN=65536                # context window (input)
MAIN_LLM_OUTPUT_MAX_TOKEN=8192          # output token limit base
MAIN_LLM_ENABLE_THINKING=false           # universal thinking switch
MAIN_LLM_REASONING_EFFORT=high           # OpenAI o-series reasoning effort

# ── 新增 ──────────────────────────────────────────────────────────
MAIN_LLM_THINKING_BUDGET=4096           # estimated thinking budget for non-Anthropic
                                        # providers (DeepSeek/GLM/OpenAI o-series).
                                        # Anthropic uses explicit budget_tokens=2000.
                                        # Only used when MAIN_LLM_ENABLE_THINKING=true.
```

---

## 10. 测试计划

### 10.1 Phase A 测试

| 测试名                                             | 描述                                                           |
| -------------------------------------------------- | -------------------------------------------------------------- |
| `test_get_thinking_budget_anthropic`               | Anthropic + thinking enabled → 2000                            |
| `test_get_thinking_budget_anthropic_non_reasoning` | Anthropic + non-reasoning model → 0                            |
| `test_get_thinking_budget_deepseek`                | DeepSeek + thinking enabled → 4096                             |
| `test_get_thinking_budget_glm`                     | GLM + thinking enabled → 4096                                  |
| `test_get_thinking_budget_openai_o_series`         | o3 + thinking enabled → 4096                                   |
| `test_get_thinking_budget_disabled`                | thinking disabled → 0                                          |
| `test_get_thinking_budget_env_override`            | `MAIN_LLM_THINKING_BUDGET=8192` → 8192                         |
| `test_main_llm_sets_max_tokens_with_thinking`      | thinking enabled → model_config["max_tokens"] == 8192 + budget |
| `test_main_llm_no_max_tokens_without_thinking`     | thinking disabled → model_config 无 max_tokens                 |
| `test_boost_reads_current_base_from_request`       | request.model_settings["max_tokens"]=12288 → boost base=12288  |
| `test_boost_falls_back_to_env_default`             | request 无 max_tokens → boost base=8192                        |
| `test_boost_progressive_from_inflated_base`        | base=12288 → 24576, 32768(cap), 32768(cap)                     |

### 10.2 Phase B 测试

| 测试名                                                    | 描述                                                                                    |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `test_should_continue_text_truncation`                    | finish=length + has_text + no_tool_calls → (True, False)                                |
| `test_should_continue_reasoning_only`                     | finish=length + no_text + has_reasoning → (True, True)                                  |
| `test_should_not_continue_reasoning_only_exhausted`       | reasoning_only_retries >= 2 → (False, False)                                            |
| `test_should_not_continue_with_tool_calls`                | finish=length + has_tool_calls → (False, False)                                         |
| `test_should_not_continue_on_stop`                        | finish=stop → (False, False)                                                            |
| `test_prepare_continuation_reasoning_only`                | is_reasoning_only=True → _is_reasoning_only=True, _reasoning_only_retries += 1          |
| `test_prepare_continuation_text`                          | is_reasoning_only=False → _is_reasoning_only=False, _continuation_retries += 1          |
| `test_create_source_reasoning_only_prompt`                | _is_reasoning_only=True → input 含 _REASONING_ONLY_PROMPT                               |
| `test_create_source_text_continuation_prompt`             | _is_reasoning_only=False → input 含 _CONTINUATION_PROMPT                                |
| `test_has_reasoning_set_on_reasoning_chunk`               | 收到 reasoning delta → _has_reasoning=True                                              |
| `test_has_visible_text_set_on_text_chunk`                 | 收到 text content → _has_visible_text=True                                              |
| `test_flags_reset_after_continuation`                     | _prepare_continuation 后 _has_reasoning/_has_visible_text 重置                          |
| `test_reasoning_only_retries_independent_of_text_retries` | reasoning_only_retries=2 但 continuation_retries=0 → reasoning-only 不触发，text 可触发 |

### 10.3 Phase C 测试

| 测试名                                          | 描述                                                                     |
| ----------------------------------------------- | ------------------------------------------------------------------------ |
| `test_subagent_has_max_tokens_boost_middleware` | `_build_child_agent` 返回的 agent middleware 含 MaxTokensBoostMiddleware |
| `test_subagent_boost_on_tool_call_truncation`   | subagent tool-call 截断 → handler re-call + max_tokens boost             |
| `test_subagent_no_callback_strip`               | subagent (ainvoke) → is_stream=False → callbacks 不 strip                |

### 10.4 Phase D 测试

| 测试名                                      | 描述                                                                                      |
| ------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `test_extract_reasoning_tokens_from_usage`  | usage_metadata 含 output_token_details.reasoning_tokens=6000 → meta_reasoning_tokens=6000 |
| `test_reasoning_tokens_none_when_absent`    | usage_metadata 无 output_token_details → meta_reasoning_tokens=None                       |
| `test_meta_chunk_includes_reasoning_tokens` | _final_frames() meta chunk 含 "reasoning_tokens" 字段                                     |
| `test_done_frame_includes_reasoning_tokens` | stream_driver done frame 含 "reasoning_tokens" 字段                                       |
| `test_db_persists_reasoning_tokens`         | store/core.py 持久化 reasoning_tokens 到 DB                                               |

### 10.5 集成测试

| 测试名                                                 | 描述                                                                     |
| ------------------------------------------------------ | ------------------------------------------------------------------------ |
| `test_thinking_enabled_inflates_max_tokens`            | end-to-end: thinking enabled → 模型收到 max_tokens = base + budget       |
| `test_reasoning_only_truncation_uses_reasoning_prompt` | end-to-end: finish=length + only reasoning → _REASONING_ONLY_PROMPT 注入 |
| `test_text_truncation_uses_continuation_prompt`        | end-to-end: finish=length + text → _CONTINUATION_PROMPT 注入             |
| `test_phase_a_reduces_truncation_rate`                 | thinking enabled + Phase A → 截断率低于无 Phase A                        |

### 10.6 现有测试更新

| 测试文件                                      | 变更                                                                      |
| --------------------------------------------- | ------------------------------------------------------------------------- |
| `tests/unit/test_token_limit_continuation.py` | `_should_text_continue` 返回签名改为 tuple；所有调用处适配                |
| `tests/unit/server/test_stream_dispatch.py`   | `_has_reasoning`/`_has_visible_text` 断言；meta chunk 含 reasoning_tokens |
| `tests/unit/server/test_stream_driver.py`     | done frame 含 reasoning_tokens 断言                                       |

---

## 11. 风险与缓解

| 风险                                          | 缓解                                                                                                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `max_tokens = base + budget` 超过模型上限     | provider API 返回 400 → 需在 `main_llm.py` 中 cap 到 model 的 `max_output_tokens`（如 Anthropic Opus 4 = 32768）。可从 `profile` 读上限     |
| DeepSeek/GLM 的 thinking budget 是估算值      | env `MAIN_LLM_THINKING_BUDGET` 可调；默认 4096 是保守值（实测 DeepSeek thinking 约 2000-6000 tokens）                                       |
| `_should_text_continue` 返回签名改为 tuple    | 所有调用处只有 2 处（`stream_dispatch.py` while 循环 + `_ResumeTurn`），改动面小                                                            |
| reasoning-only 重试时模型仍重新思考           | prompt 明确禁止 re-reason；max_tokens 已膨胀（Phase A）；最多 2 次重试后放弃                                                                |
| subagent boost 可能影响 IterationBudget       | re-call 在 `awrap_model_call` 内完成，+0 额外消耗（与主 agent 一致）                                                                        |
| `output_token_details` 字段名因 provider 而异 | LangChain 的 `usage_metadata` schema 已统一为 `output_token_details.reasoning_tokens`；provider 差异由 langchain-openai/chat-anthropic 处理 |
| Phase A 与 Phase 3 (max_tokens boost) 交互    | Phase A 设了初始 max_tokens=12288，Phase 3 boost 从 12288 起算 → 24576, 32768(cap) → **协同正确**                                           |
| Phase B 与 Phase 3 互斥                       | `finish_reason=length` + 有 tool_calls → Phase 3（middleware boost）；+ 无 tool_calls → Phase B（续写/重试）。与现有逻辑一致                |
| reasoning-only 重试耗尽后行为                 | 2 次后放弃，返回截断结果。客户端可显示 "⚠️ Thinking budget exhausted — increase max_tokens or disable thinking"                             |

---

## 12. 与 hermes-agent / openclaw 的对齐

| 维度                            | hermes-agent                                                         | openclaw                                                       | 本方案                                                                                              |
| ------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| thinking budget 膨胀 max_tokens | `max(effective, budget+4096)`                                        | `baseMaxTokens + thinkingBudget`                               | `OUTPUT_MAX_TOKEN + thinking_budget`（Phase A）                                                     |
| Anthropic budget 分级           | xhigh:32000, high:16000, medium:8000, low:4000                       | minimal:1024, low:2048, medium:8192, high:16384, max:32768     | 固定 2000（初版，后续可扩展为分级）                                                                 |
| thinking 耗尽检测               | think tags 检查 + `⚠️ Thinking Budget Exhausted`                     | `assessLastAssistantMessage()` → `"incomplete-thinking"`       | `_has_reasoning && !_has_visible_text` flag（Phase B）                                              |
| reasoning-only 重试             | Codex `incomplete` 3 次                                              | `DEFAULT_REASONING_ONLY_RETRY_LIMIT=2`                         | 2 次（与 openclaw 对齐）                                                                            |
| reasoning-only 重试 prompt      | "Continue from that partial turn and produce the visible answer now" | "produce the visible answer now. Do not restate the reasoning" | "Produce the answer now — do not re-reason or restart the thinking process"                         |
| reasoning_tokens 追踪           | `normalize_usage()` 提取                                             | `output_tokens_details.reasoning_tokens`                       | `usage_metadata["output_token_details"]["reasoning_tokens"]`（Phase D）                             |
| output_cap 错误解析             | `parse_available_output_tokens_from_error()`                         | N/A                                                            | 不实现（初版）                                                                                      |
| thinking-only 轮次清理          | `drop_thinking_only_and_merge_users()`                               | `dropThinkingBlocks()`                                         | 不实现（初版——langchain create_agent 的 checkpointer 行为不同，thinking-only 轮次不会成为最后一块） |
| Anthropic adaptive thinking     | `_supports_adaptive_thinking()`                                      | `thinking: {type:"adaptive"}`                                  | 不实现（初版）                                                                                      |
| progressive boost               | 2×, 4×, 8×, 16×, cap 32768                                           | N/A                                                            | `base * 2^attempt`, cap 32768（Phase A 后 base 从 request 读）                                      |
| subagent 覆盖                   | 全局统一                                                             | 全局统一                                                       | Phase C 在 subagent middleware 加 MaxTokensBoostMiddleware                                          |

---

## 13. 实施记录

```
Phase C (Subagent middleware) ✏️ Draft
  └─ 改 spawn/core.py: imports + middleware 列表
  └─ 写测试 (3 tests)
  └─ 全量回归
       │
Phase A (max_tokens 膨胀) ✏️ Draft
  └─ 改 reasoning_payload.py: get_thinking_budget()
  └─ 改 main_llm.py: model_config["max_tokens"] = base + budget
  └─ 改 max_tokens_boost.py: _get_current_base() + boost 循环用 base
  └─ 写测试 (12 tests)
  └─ 全量回归
       │
Phase B (Reasoning-only 检测 + 重试) ✏️ Draft
  └─ 改 stream_dispatch.py: _has_reasoning/_has_visible_text flags + _should_text_continue 返回 tuple
  └─ 改 messages.py: _REASONING_ONLY_PROMPT + _prepare_continuation + _create_source 选择 prompt
  └─ 改 stream_dispatch.py while 循环: 适配 tuple 返回
  └─ 写测试 (13 tests)
  └─ 全量回归
       │
Phase D (reasoning_tokens 追踪) ✏️ Draft
  └─ 改 stream_dispatch.py: meta_reasoning_tokens + usage 块提取
  └─ 改 messages.py: _final_frames 加 reasoning_tokens
  └─ 改 stream_driver.py: done frame 加 reasoning_tokens
  └─ 改 store/core.py: 持久化 reasoning_tokens
  └─ 写测试 (5 tests)
  └─ 全量回归
       │
Lint ✏️ Draft
  └─ pyflakes clean
```
