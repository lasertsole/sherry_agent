# 截断增强 + 流式上下文守卫计划

> 拆分自 `LLM_ERROR_HANDLING_PLAN.md`。涵盖模块 H（MaxTokensBoost 扩展）+ 模块 I（ContextLimitGuard）+ 集成注册、配置项、实施顺序、测试计划。
> 模块 A-G（错误分类、重试、回退）见 `LLM_ERROR_HANDLING_PLAN.md`。

## 目录

1. [模块 H：截断增强（MaxTokensBoost 扩展）](#模块-h截断增强maxtokensboost-扩展)
2. [模块 I：流式上下文上限守卫（ContextLimitGuard）](#模块-i流式上下文上限守卫contextlimitguard)
3. [集成与注册](#集成与注册)
4. [配置项](#配置项)
5. [实施顺序](#实施顺序)
6. [测试计划](#测试计划)
7. [与现有计划的关系](#与现有计划的关系)

---

## 模块 H：截断增强（MaxTokensBoost 扩展）

> 合并自 `THINKING_TOKEN_BUDGET_PLAN.md` 全部 4 个 Phase + hermes-agent 的截断处理。

### H.1 Thinking budget 主动膨胀（TTBP Phase A）

**场景**：thinking 启用时，thinking tokens 与 output tokens 共享 `max_tokens` 预算。初始 call 未设 `max_tokens` → provider 默认值可能不足。

**改动**：

```python
# models/LLMs/reasoning_payload.py — 新增
_DEFAULT_NON_ANTHROPIC_BUDGET = int(os.getenv("MAIN_LLM_THINKING_BUDGET", "4096"))


def get_thinking_budget(provider: str | None, model_name: str | None, enabled: bool) -> int:
    if not enabled or not provider:
        return 0
    if provider == "anthropic":
        return _DEFAULT_ANTHROPIC_BUDGET if _is_anthropic_reasoning_model(model_name) else 0
    if provider == "deepseek":
        return _DEFAULT_NON_ANTHROPIC_BUDGET
    # ... OpenAI-compatible (GLM, o-series) ...
```

```python
# models/LLMs/main_llm.py — model_config 中
if thinking_budget > 0:
    model_config["max_tokens"] = OUTPUT_MAX_TOKEN + thinking_budget
    # e.g. 16384 + 2000 = 18384 (Anthropic)
    # e.g. 16384 + 4096 = 20480 (DeepSeek/GLM)
```

```python
# max_tokens_boost.py — boost base 从 request 读取（可选优化）
# 不实现此方法时，boost 从固定 _BASE_MAX_TOKENS=16384 起跳：
#   首次 boost = 16384 × 2 = 32768 > 20480 (16384+4096 thinking)
#   即首次 boost 已能覆盖 thinking 场景，仅后续 boost 递增幅度更小
def _get_current_base(self, request) -> int:
    settings = getattr(request, "model_settings", None) or {}
    current = settings.get("max_tokens")
    if isinstance(current, int) and current > 0:
        return current
    return _BASE_MAX_TOKENS
```

| 改动文件                                | 说明                                                   |
| --------------------------------------- | ------------------------------------------------------ |
| `models/LLMs/reasoning_payload.py`      | 新增 `get_thinking_budget()`                           |
| `models/LLMs/main_llm.py`               | thinking 启用时设 `max_tokens = base + budget`         |
| `agent/middlewares/max_tokens_boost.py` | **（可选）** boost base 从 `request.model_settings` 读 |

### H.2 Thinking-budget 耗尽检测 + reasoning-only 专用重试（TTBP Phase B）

**场景**：模型将全部 output tokens 花在 reasoning 上，无可见文本。续传重试"继续之前的内容"对纯思考场景无效 → 模型可能重新思考 → 再次耗尽 → 空转。

**流式层 flag 设置**：

```python
# stream_dispatch.py — StreamTurn.__init__
self._has_reasoning: bool = False
self._has_visible_text: bool = False

# reasoning delta 块
if _reasoning and len(_reasoning) > 0:
    self._has_reasoning = True
    yield {"type": "reasoning", "content": _reasoning}

# text 块
if len(msg_chunk.content) > 0:
    self._has_visible_text = True
```

**区分两种截断**：

```python
# messages.py — _GenerateTurn._should_text_continue
def _should_text_continue(self) -> tuple[bool, bool]:
    """返回 (should_continue, is_reasoning_only)。"""
    if self.meta_finish_reason not in ("length", "max_tokens"):
        return False, False
    if self._has_tool_calls:
        return False, False  # tool-call 截断走 MaxTokensBoost

    if not self._has_visible_text and self._has_reasoning:
        if self._reasoning_only_retries >= _MAX_REASONING_ONLY_RETRIES:
            return False, False
        return True, True  # reasoning-only → 专用 prompt

    if self._continuation_retries >= _MAX_CONTINUATION_RETRIES:
        return False, False
    return True, False  # 文本截断 → 标准续传
```

**专用续传 prompt**：

```python
_CONTINUATION_PROMPT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off.]"
)
_REASONING_ONLY_PROMPT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Only reasoning/thinking was produced with no "
    "visible answer. Produce the answer now — do not re-reason "
    "or restart the thinking process. Output the final answer directly.]"
)
_MAX_CONTINUATION_RETRIES = 4
_MAX_REASONING_ONLY_RETRIES = 2
```

**hermes 的 thinking-budget 耗尽提前终止**（`conversation_loop.py:1820-1867`）在 sherry 中等价于 reasoning-only 重试 2 次后放弃 — 返回截断结果 + 用户提示。

**截断场景互斥矩阵**：

| finish_reason | has_tool_calls | has_visible_text | has_reasoning | 触发                                            |
| ------------- | -------------- | ---------------- | ------------- | ----------------------------------------------- |
| length        | True           | *                | *             | MaxTokensBoost middleware boost                 |
| length        | False          | True             | *             | 文本续传 `_CONTINUATION_PROMPT` (max 4)         |
| length        | False          | False            | True          | reasoning-only `_REASONING_ONLY_PROMPT` (max 2) |
| length        | False          | False            | False         | 文本续传（兜底）                                |
| stop          | *              | *                | *             | 无续传                                          |

| 改动文件                            | 说明                                                                                                 |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `server/service/stream_dispatch.py` | `_has_reasoning`/`_has_visible_text` flag + `_should_text_continue` 返回 tuple                       |
| `server/service/messages.py`        | `_REASONING_ONLY_PROMPT` + `_prepare_continuation(is_reasoning_only)` + `_create_source` 选择 prompt |
| `server/service/stream_dispatch.py` | while 循环适配 tuple 返回                                                                            |

### H.3 网络断流 vs 输出截断区分

**场景**：流式响应中途网络断开。表现为流突然结束，`finish_reason` 可能是 `length` 或空。需要与真正的 `max_tokens` 截断区分 — 网络断流不需要更大的 `max_tokens`。

> **设计决策**：H.3/H.5 的检测逻辑放在 **LLMRetryMiddleware**（下游/内层中间件），不修改 `max_tokens_boost.py`。通过中间件顺序保证 LLMRetryMiddleware 先看到结果，拦截非截断场景后放行，MaxTokensBoost 只收到真正需要 boost 的截断结果。详见 [中间件顺序与职责隔离](#中间件顺序与职责隔离)。

**流式层标记**（StreamTurn 设置 tag）：

```python
# stream_dispatch.py — StreamTurn.__init__
self._is_partial_stream_stub: bool = False


# 流异常或静默结束时
def _on_stream_error(self, error: Exception) -> None:
    if self._text_chunks:  # 已有部分输出
        self.meta_finish_reason = "length"
        self._is_partial_stream_stub = True
```

**结果透传**：StreamTurn 将 tag 写入 AIMessage.response_metadata，中间件链读取：

```python
# stream_dispatch.py — _final_frames 或 chunk 聚合时
if self._is_partial_stream_stub:
    ai_msg.response_metadata["_is_partial_stream_stub"] = True
```

**LLMRetryMiddleware 拦截**（下游/内层，先于 MaxTokensBoost 看到）：

```python
# agent/middlewares/llm_retry.py — awrap_model_call 中
async def awrap_model_call(self, request, handler):
    try:
        result = await handler(request)
    except (ConnectionError, asyncio.TimeoutError, ...) as e:
        # 网络异常 → 重试（无需 tag）
        return await self._retry_with_backoff(request, handler, e)

    # H.3: 静默网络断流 — 从 response_metadata 读 tag
    ai = self._extract_ai_message(result)
    if ai and (ai.response_metadata or {}).get("_is_partial_stream_stub"):
        # 网络问题不需要更大 max_tokens → 重试而非 boost
        return await self._retry_with_backoff(request, handler, None)

    # H.5: content-filter → 走回退
    if ai and (ai.response_metadata or {}).get("_content_filter_terminated"):
        return await self._try_fallback(request, handler)

    return result  # 放行 → MaxTokensBoost（上游/外层）收到
```

**MaxTokensBoost 不改动**：`_is_partial_stream_stub` 的结果在 LLMRetryMiddleware 内层被拦截，不会到达 MaxTokensBoost。MaxTokensBoost 的 `_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})` + `_detect_tool_call_truncation` 天然只处理真正的 output-cap 截断。

**续传 prompt 区分**：

```python
def _get_continuation_prompt(is_partial_stub, dropped_tools=None) -> str:
    if is_partial_stub and dropped_tools:
        return (
            f"[System: Your previous tool call ({', '.join(dropped_tools[:3])}) "
            "was too large and the stream timed out. Do NOT retry the same "
            "large content. Break it into multiple smaller tool calls.]"
        )
    elif is_partial_stub:
        return "[System: The previous response was cut off by a network error. Continue exactly where you left off.]"
    else:
        return "[System: Your previous response was truncated by the output token limit. Continue where you left off.]"
```

### H.4 Dropped tool names 提示

**概念**：流式聚合 tool-call 时，每收到一个 tool-call 的 `function.name` 就记录。如果流在此之后中断，JSON 不完整无法执行，但模型已经开始生成 tool-call。hermes 用这些名称构建特殊续传提示。

**流式层跟踪**：

```python
# stream_dispatch.py — StreamTurn
self._partial_tool_names: list[str] = []


def _on_tool_call_started(self, tool_name: str) -> None:
    """流式 chunk 中检测到新 tool-call 开始时调用。
    对应 hermes chat_completion_helpers.py:2467-2480。"""
    if tool_name and tool_name not in self._partial_tool_names:
        self._partial_tool_names.append(tool_name)


def _build_dropped_tool_warning(self) -> str | None:
    """构建 dropped tool names 的用户可见警告。
    对应 hermes chat_completion_helpers.py:3159-3174。"""
    if not self._partial_tool_names:
        return None
    names = self._partial_tool_names[:3]
    name_str = ", ".join(names)
    if len(self._partial_tool_names) > 3:
        name_str += f", +{len(self._partial_tool_names) - 3} more"
    return (
        f"\n\n⚠ Stream stalled mid tool-call ({name_str}); "
        f"the action was not executed. Ask me to retry if you want to continue."
    )
```

**在 `_on_stream_error` 中使用**：

```python
def _on_stream_error(self, error: Exception) -> None:
    if self._text_chunks:
        self.meta_finish_reason = "length"
        self._is_partial_stream_stub = True

    warning = self._build_dropped_tool_warning()
    if warning:
        # 追加到部分文本，用户可见
        self.ai_text += warning
        # 挂到 response_metadata 供 MaxTokensBoost 读取
```

### H.5 流中断 content-filter → 回退

**场景**：provider 输出层安全过滤在流式输出中途杀死流。hermes 在 stub 上标记 `_content_filter_terminated`，续传循环看到后直接走回退链。

> **设计决策**：content-filter 检测放在 **LLMRetryMiddleware**（Module C 已有），不修改 `max_tokens_boost.py`。StreamTurn 设置 tag → LLMRetryMiddleware 读取后走 Module G 回退链 → MaxTokensBoost 永远看不到。

**流式层检测**（StreamTurn 设置 tag）：

```python
# stream_dispatch.py — _on_stream_error 中
def _on_stream_error(self, error: Exception) -> None:
    if self._text_chunks:
        self.meta_finish_reason = "length"
        self._is_partial_stream_stub = True

    # H.5: 检测 content-filter
    classified = classify_api_error(error)
    if classified.reason == FailoverReason.content_policy_blocked:
        self._content_filter_terminated = True
        state_register_mem.set_state(self.session_id, "llm_content_filter_terminated", True)
```

**结果透传**：StreamTurn 将 tag 写入 AIMessage.response_metadata：

```python
if self._content_filter_terminated:
    ai_msg.response_metadata["_content_filter_terminated"] = True
```

**LLMRetryMiddleware 拦截**（下游/内层，先于 MaxTokensBoost 看到）：

```python
# agent/middlewares/llm_retry.py — awrap_model_call 中
# Module C 已有 content-filter 检测逻辑
ai = self._extract_ai_message(result)
if ai and (ai.response_metadata or {}).get("_content_filter_terminated"):
    # 跳过 boost（MaxTokensBoost 在外层，根本看不到此结果）
    # 直接走 Module G 回退链
    return await self._try_fallback(request, handler)
```

**MaxTokensBoost 不改动**：content-filter 结果在 LLMRetryMiddleware 内层被拦截并走回退，不会到达 MaxTokensBoost。

### H.6 Subagent boost 对齐（TTBP Phase C）

在 subagent 的 middleware 链中添加 `MaxTokensBoostMiddleware()`：

```python
# agent/tools/subagent/spawn/core.py — middleware 列表
middleware = [
    Summarization(...),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    ToolCallNormalize(),
    MaxTokensBoostMiddleware(),  # 新增
    HeartbeatStaleness(),
]
```

### H.7 reasoning_tokens 追踪（TTBP Phase D）

**流式层提取**：

```python
# stream_dispatch.py — StreamTurn.__init__
self.meta_reasoning_tokens: int | None = None

# usage 块提取
_usage = getattr(msg_chunk, "usage_metadata", None)
if _usage:
    _details = _usage.get("output_token_details") or {}
    if _details.get("reasoning_tokens") is not None:
        self.meta_reasoning_tokens = int(_details["reasoning_tokens"])
```

**meta/done frame 透传**：

```python
# messages.py — _final_frames
return [
    {
        "type": "meta",
        "content": "",
        "model_name": self.meta_model_name or "",
        "input_tokens": self.meta_input_tokens or 0,
        "output_tokens": self.meta_output_tokens or 0,
        "reasoning_tokens": self.meta_reasoning_tokens or 0,  # 新增
        "finish_reason": self.meta_finish_reason or "",
    }
]
```

**DB 持久化**（`context_engine/store/core.py`）：

```python
_reasoning_tokens = None
_usage_meta = response_metadata.get("usage_metadata") or {}
_details = _usage_meta.get("output_token_details") or {}
if _details.get("reasoning_tokens") is not None:
    _reasoning_tokens = int(_details["reasoning_tokens"])
# 存入 DB
"reasoning_tokens": _reasoning_tokens,
```

### H.8 改动文件汇总

| 文件                                    | H 子模块    | 说明                                                                                                                                                                   |
| --------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `models/LLMs/reasoning_payload.py`      | H.1         | `get_thinking_budget()`                                                                                                                                                |
| `models/LLMs/main_llm.py`               | H.1         | `max_tokens = base + budget`                                                                                                                                           |
| `agent/middlewares/max_tokens_boost.py` | H.1（可选） | `_get_current_base` 从 `request.model_settings` 读 base（可选优化；不实现则从固定 `_BASE_MAX_TOKENS=16384` 起跳，首次 boost 32768 仍可覆盖 20480）                     |
| `server/service/stream_dispatch.py`     | H.2-H.5,H.7 | `_has_reasoning`/`_has_visible_text`/`_partial_tool_names`/`_is_partial_stream_stub`/`_content_filter_terminated`/`meta_reasoning_tokens` + tag 写入 response_metadata |
| `server/service/messages.py`            | H.2,H.7     | `_REASONING_ONLY_PROMPT` + `_should_text_continue` tuple + `_final_frames` reasoning_tokens                                                                            |
| `server/service/stream_driver.py`       | H.7         | done frame `reasoning_tokens`                                                                                                                                          |
| `context_engine/store/core.py`          | H.7         | 持久化 `reasoning_tokens`                                                                                                                                              |
| `agent/tools/subagent/spawn/core.py`    | H.6         | middleware 链加 `MaxTokensBoostMiddleware()`                                                                                                                           |
| `agent/middlewares/llm_retry.py`        | H.3,H.5     | 读取 `_is_partial_stream_stub` / `_content_filter_terminated` tag → 拦截（不放过到 MaxTokensBoost）                                                                    |

> **`max_tokens_boost.py` 保持原样**：H.3（网络断流）和 H.5（content-filter）的检测逻辑在 LLMRetryMiddleware（下游/内层）拦截，不修改 MaxTokensBoost。H.1 的 `_get_current_base` 为可选优化。
>
> **已完成的调整**：`_BASE_MAX_TOKENS` 默认值 8192 → 16384，`_MAX_CAP` 32768 → 98304（128K 上下文窗口留 30K 给输入）。boost 序列：32768 → 65536 → 98304。

### H.9 完整数据流

```
模型调用返回 finish_reason=length
  → StreamTurn chunk 循环结束
    → H.2: 设置 _has_reasoning / _has_visible_text flag
    → H.3: _is_partial_stream_stub? (网络断流标记) → 写入 response_metadata
    → H.4: _partial_tool_names (dropped tools)
    → H.5: _content_filter_terminated? (content-filter 标记) → 写入 response_metadata
    → H.7: meta_reasoning_tokens 提取
    → E: stream_diag_summary (诊断摘要)
  → _should_text_continue() → (should, is_reasoning_only)
    → (True, False): 文本截断 → _CONTINUATION_PROMPT
    → (True, True): reasoning-only → _REASONING_ONLY_PROMPT (max 2)
    → (False, _): break
  → LLMRetryMiddleware.awrap_model_call（下游/内层，先看到结果）
    → H.3: response_metadata._is_partial_stream_stub? → 重试（不 boost）
    → H.5: response_metadata._content_filter_terminated? → Module G 回退
    → 异常（网络/rate-limit/5xx）→ 重试 + 退避
    → 无匹配 → 放行 ↓
  → MaxTokensBoostMiddleware.awrap_model_call（上游/外层，收到过滤后的干净结果）
    → _detect_tool_call_truncation? (finish_reason ∈ {length, max_tokens} + tool_calls)
      → Yes: boost max_tokens (base × 2^attempt)，重试（boost 重试经过 LLMRetryMiddleware → Summarization → model）
      → No: 放行
  → 结果返回 agent loop
```

---

## 模块 I：流式上下文上限守卫（ContextLimitGuard）

> 合并自 `CONTEXT_LIMIT_GUARD_WRAPPER.md`。流式层防御，解决中间件三重盲区。

### I.1 问题

Summarization 中间件的 T1-T5 覆盖了模型调用的前/后/错误恢复，但**没有一个能在流式输出过程中介入**：

```
astream(stream_mode=["messages", "updates"]) 单次 agent loop:

  before_agent(T1) → wrap_model_call(T2) → [handler 调模型, 流式chunk吐出] → T3 → tool执行 → T2 → ...
                                             ↑
                                    中间件完全看不到这些 chunk
                                    模型的 output 正在膨胀
```

| 盲区                             | 根因                                                                                | 后果                       |
| -------------------------------- | ----------------------------------------------------------------------------------- | -------------------------- |
| T3 检测到溢出但无法压缩          | `_post_response_check` 修改 `request.messages` 后直接 `return response`，修改被丢弃 | 真实 token 溢出信号被浪费  |
| T2 应该压缩但被 anti-thrash 挡住 | `cooldown_active` 和 `attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN` 会跳过            | 下一次调用带着超限 context |
| T2 估算太粗                      | `len(content) // 4`，实际可能差 2-3 倍                                              | T2 认为"没超"→ 实际已超    |

### I.2 方案：双防御

| 防御层                         | 机制                                                                                      | 为什么中间件做不到                                                                                   |
| ------------------------------ | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| model-call 边界 force-compress | 从 `usage_metadata.input_tokens` 拿真实 token，超 80% 窗口就设 `_FORCE_RECOVERY_KEY=True` | T3 也检测到但修改被丢弃；T2 能压缩但被 anti-thrash 挡住。wrapper 在 stream 层写 state，绕过所有 gate |
| mid-stream output 截断         | 累积输出文本估算 token，超窗口 20% 就停止 forward text chunk                              | 中间件完全看不到 mid-stream chunk                                                                    |

### I.3 新文件：`agent/context_limit_guard_wrapper.py`

```python
"""流式上下文上限守卫 — agent.astream() 外层拦截器。

参考 sherry_agent RepetitionGuardWrapper 模式（已证明 wrapper 可行）。
解决 Summarization T1-T5 中间件的三重盲区。
"""


class ContextLimitGuardWrapper:
    """包裹 CompiledStateGraph，在 astream 层逐 chunk 监控。

    两层防御：
    1. model-call 边界：从 final chunk 的 usage_metadata 提取真实 input_tokens，
       超阈值时设 _FORCE_RECOVERY_KEY=True → T2 绕过 anti-thrash gate
    2. mid-stream output：累积 text chunk 估算 token，超预算停止 forward（只影响客户端视图）
    """

    def __init__(
        self, inner, context_window: int, output_cut_ratio: float = 0.20, check_interval: int = 20
    ):
        self._inner = inner
        self._context_window = context_window
        self._output_token_budget = int(context_window * output_cut_ratio)
        self._check_interval = check_interval

    async def astream(self, *args, **kwargs):
        last_input_tokens = 0
        last_output_tokens = 0
        call_output_text = ""
        token_counter = 0
        output_cut = False

        async for mode, chunk in self._inner.astream(*args, **kwargs):
            if mode == "messages":
                # --- mid-stream output 预算截断 ---
                msg_chunk = chunk[1] if isinstance(chunk, tuple) else chunk
                content = getattr(msg_chunk, "content", None)
                if content and isinstance(content, str) and not output_cut:
                    call_output_text += content
                    token_counter += 1
                    if token_counter >= self._check_interval:
                        token_counter = 0
                        est_tokens = len(call_output_text) // 4  # CHARS_PER_TOKEN
                        if est_tokens > self._output_token_budget:
                            output_cut = True  # 后续 text chunk 不 yield

                # --- usage_metadata 提取 ---
                usage = getattr(msg_chunk, "usage_metadata", None)
                if isinstance(usage, dict):
                    if usage.get("input_tokens") is not None:
                        last_input_tokens = int(usage["input_tokens"])
                    if usage.get("output_tokens") is not None:
                        last_output_tokens = int(usage["output_tokens"])

                # 截断决策：output_cut=True 时不 forward 有 content 的 chunk
                # tool_call chunk 不受影响（功能不受损）
                if output_cut and content and not getattr(msg_chunk, "tool_call_chunks", None):
                    continue

            elif mode == "updates":
                # --- model-call 边界 force-compress ---
                self._check_and_force_compress(
                    getattr(chunk, "session_id", ""),  # best-effort
                    last_input_tokens,
                    last_output_tokens,
                )
                # 重置 per-call 累积
                call_output_text = ""
                token_counter = 0
                output_cut = False

            yield chunk

        # 流结束 flush
        self._check_and_force_compress("", last_input_tokens, last_output_tokens)

    def _check_and_force_compress(self, session_id, input_tokens, output_tokens):
        threshold = int(self._context_window * 0.80)  # COMPRESSION_TRIGGER_RATIO
        if input_tokens >= threshold:
            state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, True)
        elif input_tokens + output_tokens >= threshold:
            # 预测性：output 会成为下轮 input 的一部分
            state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, True)

    async def ainvoke(self, *args, **kwargs):
        return await self._inner.ainvoke(*args, **kwargs)
```

### I.4 包裹结构

```
CompiledStateGraph (graph + middleware)
  │
  │  astream(stream_mode=["messages", "updates"])
  ▼
RepetitionGuardWrapper           ← 内层：流式重复检测
  │  过滤重复文本 chunk
  │  透传 usage_metadata
  ▼
ContextLimitGuardWrapper         ← 外层：流式上下文上限检测
  │  1. 捕获 usage_metadata.input_tokens
  │  2. model-call 边界设 _FORCE_RECOVERY_KEY
  │  3. mid-stream output 预算截断
  ▼
Client (stream_dispatch.py)
```

### I.5 与各模块的协作

| 层                             | 职责                                                                | 数据来源                                                                                         |
| ------------------------------ | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 模块 I（流式层）               | 真实 token 检测 + force flag + output 截断                          | `usage_metadata.input_tokens`（真实）                                                            |
| T1（before_agent）             | 回合开始前预检                                                      | `estimate_msg_tokens`（粗估）                                                                    |
| T2（wrap_model_call）          | 每次调用前检查 + 压缩                                               | `estimate_msg_tokens` + `_FORCE_RECOVERY_KEY`                                                    |
| T3（post-response）            | 响应后真实 token 检测（仅日志）                                     | `extract_reported_input_tokens`                                                                  |
| T4/T5（error recovery）        | provider 报错恢复                                                   | `classify_provider_error`                                                                        |
| 模块 B（LLMRetry，内层）       | 异常重试 + content-filter 回退 + 网络断流重试 + stale streak 断路器 | `classify_api_error` + StreamTurn tags（`_is_partial_stream_stub`/`_content_filter_terminated`） |
| 模块 H（MaxTokensBoost，外层） | tool-call 截断 boost（不改动）                                      | `finish_reason` + `tool_calls`（只收到 LLMRetry 放行的干净结果）                                 |

### I.6 数据流

```
Model call N ends → "updates" boundary
  │
  ├─ wrapper 从 final chunk 提取 usage_metadata
  │   → input_tokens=52000, output_tokens=8000
  │
  ├─ _check_and_force_compress(session_id, 52000, 8000, threshold=52428)
  │   ├─ 52000 >= 52428? NO
  │   ├─ predict: 52000 + 8000 = 60000 >= 52428? YES
  │   └─ state_register_mem.set(_FORCE_RECOVERY_KEY, True)
  │      → log: "force-compression ARMED (predictive)"
  │
  ├─ tool executes → context 继续膨胀
  │
  └─ Model call N+1 starts → T2 (wrap_model_call)
      ├─ forced = get(_FORCE_RECOVERY_KEY) = True
      ├─ _should_skip_compression: clears flag, returns False
      ├─ `not forced` = False → 跳过 cooldown/attempt-cap gate
      ├─ _decide_overflow_route → ROUTE_COMPACT_ONLY
      ├─ _execute_compact → 压缩历史消息
      └─ handler 调用 with 压缩后的 context → 不溢出 ✓
```

### I.7 mid-stream output 预算截断细节

```python
call_output_text += content
token_counter += 1
if token_counter >= self._check_interval:  # 默认 20
    token_counter = 0
    call_output_tokens = len(call_output_text) // 4  # CHARS_PER_TOKEN
    if call_output_tokens > self._output_token_budget:
        output_cut = True  # 后续 text chunk 不再 forward
```

- `output_cut=True` 后，后续有 `content` 且无 `tool_calls` 的 chunk 不 yield（客户端看不到）
- graph 仍累积完整 AIMessage（截断只影响客户端视图）
- tool-call chunk 不受影响（功能不受损）
- T2 在下次调用前会压缩掉超长输出

**不漏检保证** — 三处 flush：

| 触发点         | 位置                     | 说明                   |
| -------------- | ------------------------ | ---------------------- |
| `updates` 边界 | `mode == "updates"` 分支 | 模型调用结束时重置     |
| 非 model 节点  | `node != "model"` 分支   | 摘要等节点出现时重置   |
| 流结束         | `async for` 循环之后     | 最后一次模型调用的残留 |

### I.8 改动文件

| 文件                                   | 操作     | 说明                            |
| -------------------------------------- | -------- | ------------------------------- |
| `agent/context_limit_guard_wrapper.py` | **新建** | `ContextLimitGuardWrapper` 类   |
| `agent/__init__.py`                    | **修改** | `__all__` + lazy import         |
| `agent/core.py`                        | **修改** | `built_agent()` 中包裹 `_agent` |

### I.9 已知局限

| 局限                                         | 影响                                                        | 缓解                                                            |
| -------------------------------------------- | ----------------------------------------------------------- | --------------------------------------------------------------- |
| `usage_metadata` 可能被 RepetitionGuard 截断 | final chunk 有 content 且 `call_cut=True` 时 wrapper 拿不到 | final chunk 通常 content 为空；fallback 到 T2 粗估 + T4/T5 兜底 |
| output 截断只影响客户端视图                  | graph 仍累积完整 AIMessage                                  | 设计意图 — 截断的是"会被压缩掉的冗余输出"                       |
| 子代理不包裹                                 | subagent 用 `ainvoke` 不走 `astream`                        | `ainvoke` 是单次调用，T1/T2/T3 已覆盖                           |
| `estimate_msg_tokens` 精度                   | output 预算用 `len//4` 估算                                 | 保守值（20%），实际 token 数通常高于估算                        |
| `usage_metadata` 不存在的 provider           | 无法做真实 token 检测                                       | fallback 到 T2 粗估 + T4/T5 兜底                                |

---

## 集成与注册

### 中间件注册顺序（最终）

> **关键**：MaxTokensBoostMiddleware 必须在 LLMRetryMiddleware **外层**（列表中更靠前），保证 LLMRetryMiddleware 先看到结果，拦截非截断场景后放行，MaxTokensBoost 只收到真正需要 boost 的截断结果。详见 [中间件顺序与职责隔离](#中间件顺序与职责隔离)。

```python
# agent/core.py — create_agent() middleware 列表
# 列表中越靠前 = 越外层（先 wrap）= 结果回流时越后看到

middleware = [
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ToolGuardrails(),
    ToolCallNormalize(),
    SubagentCompletionDrainMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),  # ← 外层：只处理 tool-call 截断 boost
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(...),
    LLMRetryMiddleware(  # ← 内层（下游）：先看到结果
        config=LLMRetryConfig(),
        fallback_chain=_build_fallback_chain(),
    ),
]
```

### Wrapper 包裹顺序

```python
# agent/core.py — built_agent()

_agent = create_agent(model=main_llm, middleware=middleware, ...)
_agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
_agent = ContextLimitGuardWrapper(_agent, context_window=main_llm_max_tokens)
```

### Subagent 中间件对齐

```python
# agent/tools/subagent/spawn/core.py

middleware = [
    Summarization(...),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    ToolCallNormalize(),
    MaxTokensBoostMiddleware(),  # H.6 新增
    HeartbeatStaleness(),
]
```

### 调用链路（改进后）

```
agent.astream() / agent.ainvoke()
  → [ContextLimitGuardWrapper] astream拦截     ← 模块 I：流式 token 检测 + output 截断
    → [RepetitionGuardWrapper] 重复检测         ← 现有
      → [MaxTokensBoostMiddleware] wrap_model_call  ← 外层：tool-call 截断 boost（不改动）
        → [LLMRetryMiddleware] wrap_model_call     ← 内层：先看到结果，拦截非截断场景
          │  → 异常（网络/rate-limit/5xx）→ 重试 + 退避
          │  → H.3: _is_partial_stream_stub tag → 重试（不 boost）
          │  → H.5: _content_filter_terminated tag → Module G 回退
          │  → Module D: stale streak → 断路器
          │  → 无匹配 → 放行 ↓
          → [Summarization] _execute_with_recovery  ← 现有：T4/T5 溢出压缩
            → [NormalizingChatModel] inner._generate
              → OpenAI SDK (max_retries=2)
                → Provider API
```

### 中间件顺序与职责隔离

**问题**：`finish_reason="length"` 是一个入口条件，背后是多种不同场景（tool-call 截断、网络断流、content-filter、reasoning-only）。如果不区分，MaxTokensBoost 会对所有场景都 boost——网络断流和 content-filter 不需要更大 max_tokens，boost 是浪费。

**方案**：三件事配合，缺一不可：

| 步骤                         | 做什么                                                                                                                                           | 在哪做                           | 为什么                                                                                 |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------- | -------------------------------------------------------------------------------------- |
| 1. StreamTurn 打标签         | 静默 content-filter → `response_metadata["_content_filter_terminated"]=True`；静默网络断流 → `response_metadata["_is_partial_stream_stub"]=True` | `stream_dispatch.py`             | StreamTurn 是流消费者，唯一能看到流如何结束的组件                                      |
| 2. LLMRetryMiddleware 读标签 | 看到 tag → 拦截（重试或回退），不放过到 MaxTokensBoost                                                                                           | `agent/middlewares/llm_retry.py` | LLMRetryMiddleware 本身就有错误分类+重试+回退职责（Module B/C/D/G），读 tag 是自然扩展 |
| 3. 中间件顺序                | MaxTokensBoost 在外层（列表靠前），LLMRetryMiddleware 在内层（列表靠后）                                                                         | `agent/core.py`                  | 结果回流时 LLMRetry 先看到 → 拦截 → MaxTokensBoost 只收到干净结果                      |

**职责边界**：

| 中间件                   | 单一职责                                                               | 不做什么                                        |
| ------------------------ | ---------------------------------------------------------------------- | ----------------------------------------------- |
| MaxTokensBoostMiddleware | `finish_reason ∈ {length, max_tokens}` + tool_calls → boost max_tokens | 不检测 content-filter、不检测网络断流、不做回退 |
| LLMRetryMiddleware       | 异常重试 + content-filter 回退 + 网络断流重试 + stale streak 断路器    | 不 boost max_tokens、不处理 tool-call 截断      |

**互相独立**：两个中间件互不引用、互不依赖。MaxTokensBoost 可以在没有 LLMRetryMiddleware 的环境中独立工作（现有行为不变），反之亦然。

**已知约束**：

- MaxTokensBoost 的 boost 重试经过 LLMRetryMiddleware（内层），每次 boost 重试获得独立的 LLMRetry 重试预算。最坏情况 MaxTokensBoost 3 次 × LLMRetry 3 次 = 9 次模型调用，但实践中网络错误在 boost 重试中很少见。
- LLMRetryMiddleware 在 boost 重试中做回退时，MaxTokensBoost 收到回退模型的结果。boost 值（`_BASE_MAX_TOKENS × 2^attempt`）对回退模型仍然有效——boost 只是给更大 max_tokens，对任何模型都安全。

---

## 配置项

### 环境变量

```env
# LLM 重试配置
LLM_RETRY_MAX_RETRIES=3
LLM_RETRY_BACKOFF_BASE=2.0
LLM_RETRY_BACKOFF_MAX=60.0
LLM_RETRY_BACKOFF_JITTER=0.3

# 断路器配置
LLM_STALE_GIVEUP_THRESHOLD=5

# Thinking budget（H.1）
MAIN_LLM_OUTPUT_MAX_TOKEN=16384
MAIN_LLM_THINKING_BUDGET=4096

# 流式上下文守卫（模块 I）
MAIN_LLM_MAX_TOKEN=128000
COMPRESSION_TRIGGER_RATIO=0.80
# output_cut_ratio=0.20, check_interval=20 (构造函数参数)

# 模型回退配置（可选，不配置则不启用）
FALLBACK_LLM_1_PROVIDER=deepseek
FALLBACK_LLM_1_NAME=deepseek-chat
FALLBACK_LLM_1_API_KEY=sk-...
FALLBACK_LLM_1_API_BASE=https://api.deepseek.com/v1
```

### MaxTokensBoost 参数（代码常量）

| 参数               | 值    | 说明                                                    |
| ------------------ | ----- | ------------------------------------------------------- |
| `_BASE_MAX_TOKENS` | 16384 | boost 基准，从 `MAIN_LLM_OUTPUT_MAX_TOKEN` 环境变量读取 |
| `_MAX_CAP`         | 98304 | boost 上限（128K 上下文窗口留 30K 给输入）              |
| `_MAX_RETRIES`     | 3     | 最多 boost 重试次数                                     |

boost 序列：32768 → 65536 → 98304

---

## 实施顺序

| 阶段 | 模块                                              | 估时 | 依赖  |
| ---- | ------------------------------------------------- | ---- | ----- |
| 1    | 模块 F：`pub_func/retry_utils.py`                 | 0.5h | 无    |
| 2    | 模块 A：扩展 `llm_error_classifier.py`            | 2h   | 无    |
| 3    | 模块 B：`agent/middlewares/llm_retry.py`          | 3h   | A + F |
| 4    | 模块 D：断路器（内嵌在 B 中）                     | 0.5h | B     |
| 5    | 模块 E：`server/service/stream_diag.py`           | 1h   | 无    |
| 6    | 模块 C：content_filter 检测（内嵌在 B 中）        | 1h   | B     |
| 7    | 模块 H.1：thinking budget 主动膨胀                | 1h   | 无    |
| 8    | 模块 H.2：reasoning-only 检测+续传                | 1.5h | E     |
| 9    | 模块 H.3+H.5：StreamTurn 打标签 + LLMRetry 读标签 | 1h   | B + C |
| 10   | 模块 H.4：dropped tool names                      | 0.5h | E     |
| 11   | 模块 H.6：subagent boost 对齐                     | 0.5h | 无    |
| 12   | 模块 H.7：reasoning_tokens 追踪                   | 1h   | 无    |
| 13   | 模块 I：`context_limit_guard_wrapper.py`          | 2h   | 无    |
| 14   | 集成：`agent/core.py` 注册 + 中间件顺序           | 0.5h | B + I |
| 15   | 模块 G：模型回退链                                | 2h   | A + B |
| 16   | 测试                                              | 5h   | 全部  |

> **MaxTokensBoost 已调整**：`_BASE_MAX_TOKENS` 8192→16384，`_MAX_CAP` 32768→98304。阶段 8-12 不包含对 `max_tokens_boost.py` 的进一步修改（H.1 的 `_get_current_base` 为可选优化）。H.3/H.5 的检测逻辑在阶段 9 的 LLMRetryMiddleware 中实现。

**总计：~23h**

---

## 测试计划

### 单元测试

| 测试文件                                                   | 覆盖模块    | 关键用例                                                                                              |
| ---------------------------------------------------------- | ----------- | ----------------------------------------------------------------------------------------------------- |
| `tests/pub_func/test_retry_utils.py`                       | F           | `jittered_backoff` 指数增长、cap、抖动范围                                                            |
| `tests/pub_func/test_llm_error_classifier.py`              | A           | 8 步分类管道、每种 `FailoverReason`、异常链展开、向后兼容                                             |
| `tests/agent/middlewares/test_llm_retry.py`                | B+D         | 重试 3 次后 raise、不可重试直接 raise、should_compress 委托给 Summarization、断路器 5 次后中止        |
| `tests/server/service/test_stream_diag.py`                 | E           | `stream_diag_init`、`capture_response`、`flatten_exception_chain` 4 层、`stream_diag_summary`         |
| `tests/server/service/test_content_filter.py`              | C           | `finish_reason=content_filter` 不续传 + state 标记、中流检测关键词                                    |
| `tests/agent/middlewares/test_token_limit_continuation.py` | H（已调整） | tool-call 截断 boost 回归（base=16384, cap=98304）                                                    |
| `tests/agent/middlewares/test_llm_retry.py`                | B+C+H.3+H.5 | 异常重试、content-filter→回退、partial-stream-stub→重试（不到达 MaxTokensBoost）、stale streak 断路器 |
| `tests/agent/test_context_limit_guard_wrapper.py`          | I           | force-compress 触发（当前+预测）、output 截断、三处 flush、usage_metadata 缺失 fallback               |
| `tests/models/test_reasoning_payload.py`                   | H.1         | `get_thinking_budget` 各 provider、env override                                                       |
| `tests/server/service/test_reasoning_tokens.py`            | H.7         | usage_metadata 提取 reasoning_tokens、meta/done frame、DB 持久化                                      |

### 集成测试

| 场景                        | 模拟方式                                                                        | 期望行为                                                                               |
| --------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| 网络超时                    | mock handler 抛 `asyncio.TimeoutError`                                          | 重试 3 次 → raise                                                                      |
| 429 限流                    | mock handler 抛 `openai.RateLimitError`                                         | 重试 + 退避 → 恢复或 raise                                                             |
| content_filter              | mock `finish_reason=content_filter`                                             | 不续传 → 尝试回退或终止                                                                |
| 上下文溢出（provider 报错） | mock handler 抛 `context_length_exceeded`                                       | 委托给 Summarization T4/T5                                                             |
| 上下文溢出（预测性）        | mock `usage_metadata.input_tokens` 超阈值                                       | force-compress → T2 压缩                                                               |
| 连续 5 次超时               | mock handler 连续 5 turn 超时                                                   | 第 6 turn 断路器中止                                                                   |
| 模型回退                    | 配置 2 候选，主模型抛 `auth_permanent`                                          | 切换到候选 1                                                                           |
| 异常链展开                  | mock `APIConnectionError(cause=RemoteProtocolError)`                            | 日志含根因                                                                             |
| Thinking-budget 耗尽        | mock `finish_reason=length` + think tags + 无后续文本                           | 返回提示，不重试                                                                       |
| Reasoning-only 截断         | mock `finish_reason=length` + `_has_reasoning=True` + `_has_visible_text=False` | `_REASONING_ONLY_PROMPT` 注入（max 2）                                                 |
| 网络断流 vs 输出截断        | mock partial-stream-stub vs 正常 length                                         | LLMRetry 拦截 stub → 重试（不到达 MaxTokensBoost）；正常 length → MaxTokensBoost boost |
| Dropped tool names          | mock 流中断时 `partial_tool_names=["write_file"]`                               | 续传提示含 tool name                                                                   |
| 中流 content-filter         | mock 流中断 + `content_policy_blocked`                                          | LLMRetry 拦截 → 走回退链（不到达 MaxTokensBoost）                                      |
| Mid-stream output 截断      | mock `astream` 输出超窗口 20%                                                   | output_cut=True，客户端不收到后续 text chunk                                           |
| Force-compress 预测性       | mock `input=50000 + output=8000 >= 52428`                                       | `_FORCE_RECOVERY_KEY=True` → T2 压缩                                                   |
| Subagent boost              | mock subagent tool-call 截断                                                    | middleware re-call + boost                                                             |
| reasoning_tokens 追踪       | mock `usage_metadata.output_token_details.reasoning_tokens=6000`                | meta/done frame 含字段                                                                 |

### 回归测试

- 现有 `Summarization` T1-T5 测试全绿
- 现有 `MaxTokensBoostMiddleware` 测试全绿（**已调整 base/cap，测试同步更新**）
- 现有 `stream_dispatch` 流式测试全绿
- 现有 `RepetitionGuardWrapper` 测试全绿
- 现有 `_should_text_continue` 调用处适配 tuple 返回

---

## 与现有计划的关系

| 现有计划                         | 关系                                                          |
| -------------------------------- | ------------------------------------------------------------- |
| `failover-model-fallback.md`     | **已删除** — 内容合并到 LLM_ERROR_HANDLING_PLAN.md 模块 A + G |
| `failover-model-breaker.md`      | **已删除** — 内容合并到 LLM_ERROR_HANDLING_PLAN.md 模块 D     |
| `THINKING_TOKEN_BUDGET_PLAN.md`  | **已删除** — 内容合并到本文档模块 H（全部 4 Phase）           |
| `CONTEXT_LIMIT_GUARD_WRAPPER.md` | **已删除** — 内容合并到本文档模块 I                           |
| `QUESTION_TOOL_PLAN.md`          | **独立** — Question 工具 + 心跳旁路不受本计划影响             |
