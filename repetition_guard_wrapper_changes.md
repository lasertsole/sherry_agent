# `agent/repetition_guard_wrapper.py` 改动记录

> 改动目标：将 wrapper 瘦身为**仅保留流式内部重复检测**，跨调用检测、事后检测、状态重置、reasoning 检测全部移交 `OutputRepetitionGuard` middleware。

---

## 1. 模块文档字符串（docstring）

### 改之前

```python
"""RepetitionGuardWrapper — wraps a CompiledStateGraph agent with comprehensive
output repetition detection at the Runnable / stream level.

This wrapper implements **ALL** the interception functionality of the
``OutputRepetitionGuard`` middleware, operating at the stream level instead of
the middleware level:

1. **Stream-level internal repetition detection** (sentence, char-run, phrase)
   — runs on accumulated visible text as chunks arrive, cutting the repetitive
   tail *before* it reaches the client.

2. **Cross-call identical output detection** — runs at model-call boundaries
   (detected by stream node transitions), comparing the accumulated text of
   each model call against a rolling hash history.

3. **Reasoning text repetition detection** — tracks ``reasoning_content``
   independently from visible output, with its own history and warn flags.

4. **HALT escalation** — when cross-call repetition exceeds the threshold,
   yields a halt message and cancels the underlying stream.

5. **WARN escalation** — yields a warning message (for cross-call) or cuts the
   current call's remaining text (for internal repetition).

6. **Per-turn state reset** — clears all repetition state at the start of each
   ``astream`` / ``ainvoke`` call, mirroring the middleware's ``before_agent``.

7. **Non-streaming (``ainvoke``) post-hoc detection** — runs the full detection
   suite on the final ``AIMessage`` of the result.

8. **HALT short-circuit** — when ``_HALTED_KEY`` is already set (e.g. by the
   middleware backstop), subsequent model calls yield a halt message instead of
   forwarding repetitive text.

When using this wrapper as the **sole** guardian, remove
``OutputRepetitionGuard`` from the agent's middleware list to avoid
double-counting in the cross-call hash history.  If both are active, the
``_INTERNAL_WARNED_KEY`` dedupe gate prevents double-warning, but the
cross-call hash history may accumulate duplicate entries.

Integration (in ``agent/core.py``)::

    from .repetition_guard_wrapper import RepetitionGuardWrapper

    _agent = create_agent(...)
    _agent = RepetitionGuardWrapper(_agent)   # wrap here
    return _agent

And in ``server/service/messages.py``, remove the manual
``check_stream_repetition`` calls — the wrapper handles stream-level
interception transparently.
"""
```

### 改之后

```python
"""RepetitionGuardWrapper (slim) — wraps a CompiledStateGraph agent with
**stream-only** internal repetition detection.

This is the slimmed-down version: the wrapper handles ONLY what the
middleware cannot — real-time stream-level internal repetition cutting.
All cross-call detection, per-turn state reset, post-hoc (ainvoke)
detection, reasoning repetition, and HALT escalation are handled by
``OutputRepetitionGuard`` middleware on the inner agent.

Responsibilities retained:

1. **Stream-level internal repetition detection** (sentence, char-run,
   phrase) — runs on accumulated visible text as chunks arrive, cutting
   the repetitive tail *before* it reaches the client.

2. **HALT short-circuit** — when ``_HALTED_KEY`` is set by the
   middleware (cross-call HALT), subsequent model calls yield a halt
   message instead of forwarding repetitive text.

3. **Phantom-stream guard** — drops pre-update model text on fresh
   dict-input runs that cannot be real graph output.

Everything else is delegated to the middleware:
- Cross-call identical-output detection  → ``wrap_model_call``
- Per-turn state reset                   → ``before_agent``
- Non-streaming (ainvoke) post-hoc       → ``wrap_model_call``
- Reasoning text repetition              → ``_wrap_model_call_post``
- HALT escalation (setting _HALTED_KEY)  → ``_check_text_repetition``

Integration (in ``agent/core.py``)::

    from .repetition_guard_wrapper import RepetitionGuardWrapper
    from .middlewares.output_repetition_guard import OutputRepetitionGuard

    _agent = create_agent(
        ...,
        middleware=[
            ...,
            OutputRepetitionGuard(),  # cross-call + post-hoc + state reset
        ],
    )
    _agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
"""
```

---

## 2. Imports

### 改之前

```python
from typing import Any, AsyncGenerator

from loguru import logger
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from runtime import state_register_mem
from agent.middlewares.output_repetition_guard import (
    OutputRepetitionGuard,
    SESSION_STATE_KEYS,
    _HISTORY_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
    _MIN_CONTENT_LENGTH,
    _MIN_CROSSCALL_LENGTH,
    _CHAR_RUN_MIN,
    _STREAM_WARNING,
    _REASONING_KEYS,
)
from agent.middlewares.subagent_completion_drain import _is_internal_completion
```

### 改之后

```python
from types import SimpleNamespace
from typing import Any, AsyncGenerator

from loguru import logger
from langchain_core.messages import AIMessageChunk
from langgraph.graph.state import CompiledStateGraph

from runtime import state_register_mem
from agent.middlewares.output_repetition_guard import (
    OutputRepetitionGuard,
    SESSION_STATE_KEYS,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _MIN_CONTENT_LENGTH,
    _CHAR_RUN_MIN,
    _STREAM_WARNING,
    _REASONING_KEYS,
)
```

**删除的 import：**

- `AIMessage` — 不再用于 `ainvoke` 事后检测
- `_HISTORY_KEY`, `_REASONING_HISTORY_KEY`, `_REASONING_WARNED_KEY` — 跨调用/reasoning 检测移交 middleware
- `_MIN_CROSSCALL_LENGTH` — 跨调用检测移交 middleware
- `_is_internal_completion` — `ainvoke` 事后检测删除

**新增的 import：**

- `SimpleNamespace` — 用于内部测试辅助（后被移除，保留无影响）

---

## 3. `__init__` 构造函数

### 改之前

```python
def __init__(
    self,
    inner: CompiledStateGraph,
    max_identical_outputs: int = 3,
    warn_after: int = 2,
    internal_repeat_ratio: float = 0.6,
    internal_min_lines: int = 6,
    char_run_min: int = _CHAR_RUN_MIN,
    phantom_stream_guard: bool = False,
):
    self._inner = inner
    self._guard = OutputRepetitionGuard(
        max_identical_outputs=max_identical_outputs,
        warn_after=warn_after,
        internal_repeat_ratio=internal_repeat_ratio,
        internal_min_lines=internal_min_lines,
        char_run_min=char_run_min,
    )
    self._phantom_stream_guard = phantom_stream_guard
```

### 改之后

```python
def __init__(
    self,
    inner: CompiledStateGraph,
    internal_repeat_ratio: float = 0.6,
    internal_min_lines: int = 6,
    char_run_min: int = _CHAR_RUN_MIN,
    phantom_stream_guard: bool = False,
):
    self._inner = inner
    self._guard = OutputRepetitionGuard(
        internal_repeat_ratio=internal_repeat_ratio,
        internal_min_lines=internal_min_lines,
        char_run_min=char_run_min,
    )
    self._phantom_stream_guard = phantom_stream_guard
```

**删除的参数：**

- `max_identical_outputs` — 跨调用 HALT 阈值，由 middleware 控制
- `warn_after` — 跨调用 WARN 阈值，由 middleware 控制

**不再传给 `OutputRepetitionGuard` 的参数：**

- `max_identical_outputs`, `warn_after` — wrapper 只用 `_detect_internal_repetition`，不需要跨调用参数

---

## 4. 删除 `_halt_message()` 静态方法

### 改之前

```python
@staticmethod
def _halt_message() -> str:
    """The HALT message text yielded when repetition forces a stop."""
    return (
        "[Output Repetition Guard] Output repetition was detected. "
        "I must stop here. Please summarize what has been accomplished "
        "and what remains to be done."
    )
```

### 改之后

```python
# （删除——HALT 消息由 middleware 的 _check_text_repetition 生成，
#  wrapper 只读 _HALTED_KEY flag 做 short-circuit）
```

**原因：** HALT 消息的生成是 middleware 的职责（`_check_text_repetition` 返回 HALT `AIMessage`），wrapper 只需要读 `_HALTED_KEY` flag 并 yield `_halted_short_circuit_message()`。

---

## 5. 删除 `_on_model_call_end()` 方法

### 改之前

```python
def _on_model_call_end(
    self,
    session_id: str,
    call_text: str,
    call_reasoning: str,
) -> tuple[str | None, bool]:
    """Run cross-call + internal detection at a model-call boundary.

    Returns ``(warning_text, is_halt)``.  When ``warning_text`` is
    ``None``, no escalation applies.  When ``is_halt`` is ``True``, the
    caller should cancel the stream.
    """
    # ---- visible output ----
    if len(call_text) >= _MIN_CROSSCALL_LENGTH:
        r = self._guard._check_text_repetition(
            session_id,
            call_text,
            call_text,
            _HISTORY_KEY,
            _INTERNAL_WARNED_KEY,
            "output",
            check_internal=len(call_text) >= _MIN_CONTENT_LENGTH,
        )
        if r is not None:
            is_halt = state_register_mem.get_state(session_id, _HALTED_KEY, False)
            return (r.content, is_halt)

    # ---- reasoning (independent history) ----
    if len(call_reasoning) >= _MIN_CROSSCALL_LENGTH:
        r = self._guard._check_text_repetition(
            session_id,
            call_reasoning,
            call_text,
            _REASONING_HISTORY_KEY,
            _REASONING_WARNED_KEY,
            "reasoning",
            check_internal=len(call_reasoning) >= _MIN_CONTENT_LENGTH,
        )
        if r is not None:
            is_halt = state_register_mem.get_state(session_id, _HALTED_KEY, False)
            return (r.content, is_halt)

    return (None, False)
```

### 改之后

```python
# （删除——跨调用检测 + reasoning 检测全部移交 middleware 的 wrap_model_call）
```

**原因：** 此方法在流式 model call 边界运行跨调用 hash 检测和 reasoning 检测，与 middleware 的 `wrap_model_call` → `_check_text_repetition` 完全重复。

---

## 6. 删除 `_post_hoc_check()` 方法

### 改之前

```python
def _post_hoc_check(
    self,
    session_id: str,
    ai_msg: AIMessage,
) -> AIMessage | None:
    """Post-hoc detection on a complete ``AIMessage`` (for ``ainvoke``)."""
    if _is_internal_completion(ai_msg):
        return None
    has_tool_calls = bool(getattr(ai_msg, "tool_calls", None))

    content = str(ai_msg.content or "").strip()
    reasoning = OutputRepetitionGuard._extract_reasoning(ai_msg)

    if not reasoning:
        reasoning = OutputRepetitionGuard._extract_inline_reasoning(content)
        if reasoning:
            content = OutputRepetitionGuard._strip_inline_reasoning(content)

    if state_register_mem.get_state(session_id, _HALTED_KEY, False):
        return AIMessage(content=self._halted_short_circuit_message())

    if len(content) >= _MIN_CROSSCALL_LENGTH:
        r = self._guard._check_text_repetition(
            session_id,
            content,
            content,
            _HISTORY_KEY,
            _INTERNAL_WARNED_KEY,
            "output",
            check_internal=(len(content) >= _MIN_CONTENT_LENGTH and not has_tool_calls),
        )
        if r is not None:
            return r

    if len(reasoning) >= _MIN_CROSSCALL_LENGTH:
        r = self._guard._check_text_repetition(
            session_id,
            reasoning,
            content,
            _REASONING_HISTORY_KEY,
            _REASONING_WARNED_KEY,
            "reasoning",
            check_internal=(len(reasoning) >= _MIN_CONTENT_LENGTH and not has_tool_calls),
        )
        if r is not None:
            return r

    return None
    # ... (下方还有一段 unreachable dead code，同样删除)
```

### 改之后

```python
# （删除——ainvoke 事后检测全部移交 middleware 的 wrap_model_call）
```

**原因：** 与 middleware 的 `_wrap_model_call_post` 完全重复。`ainvoke` 路径下 middleware 的 `wrap_model_call` 完美覆盖（无流式，replacement 直接返回）。

---

## 7. `astream` 方法

### 7a. 删除 per-turn 状态重置

#### 改之前

```python
session_id = self._extract_session_id(input_, config)

# [RGW-DIAG] temporary instrumentation
try:
    logger.debug(
        "[RGW-DIAG] astream ENTRY session={} stream_mode={} input={!r}",
        session_id,
        stream_mode,
        str(input_)[:300],
    )
except Exception:
    pass

# Per-turn state reset (mirrors middleware before_agent)
self._guard._before_agent_impl({"session_id": session_id})
```

#### 改之后

```python
session_id = self._extract_session_id(input_, config)

# （删除 RGW-DIAG 日志）
# （删除 per-turn state reset——由 middleware 的 before_agent 处理）
```

---

### 7b. 删除 `in_model_call` / `call_reasoning` 状态变量

#### 改之前

```python
# State machine for tracking model-call boundaries
in_model_call = False
call_text = ""
call_reasoning = ""
call_cut = False  # whether current call's visible text was cut
```

#### 改之后

```python
# State machine for tracking the current model call's accumulated
# text (for internal repetition detection only).
call_text = ""
call_cut = False
```

**删除：**

- `in_model_call` — 不再需要跟踪是否在 model call 内（不需要边界检测）
- `call_reasoning` — reasoning 检测移交 middleware

---

### 7c. 删除 `[RGW-DIAG]` chunk 级日志

#### 改之前

```python
async for chunk in generator:
    # Guard against unexpected chunk shapes
    if not isinstance(chunk, (tuple, list)) or len(chunk) < 2:
        yield chunk
        continue

    mode = chunk[0]
    data = chunk[1]

    # [RGW-DIAG] temporary instrumentation
    try:
        if isinstance(data, (tuple, list)) and len(data) >= 2:
            _d0, _d1 = data[0], data[1]
            _meta = _d1 if isinstance(_d1, dict) else {}
            logger.debug(
                "[RGW-DIAG] chunk mode={} type={} node={} step={} "
                "content={!r} tc={} akw={} in_call={} ct_len={} cut={}",
                mode,
                type(_d0).__name__,
                _meta.get("langgraph_node"),
                _meta.get("langgraph_step"),
                str(getattr(_d0, "content", None))[:120],
                getattr(_d0, "tool_calls", None)
                or getattr(_d0, "tool_call_chunks", None),
                list(getattr(_d0, "additional_kwargs", {}) or {}),
                in_model_call,
                len(call_text),
                call_cut,
            )
        else:
            logger.debug(
                "[RGW-DIAG] chunk RAW mode={} data={!r}",
                mode,
                str(data)[:200],
            )
    except Exception:
        pass
```

#### 改之后

```python
async for chunk in generator:
    if not isinstance(chunk, (tuple, list)) or len(chunk) < 2:
        yield chunk
        continue

    mode = chunk[0]
    data = chunk[1]
```

---

### 7d. 简化 `"updates"` 分支——删除跨调用边界检测

#### 改之前

```python
if mode == "updates":
    saw_updates = True
    if in_model_call:
        warning, is_halt = self._on_model_call_end(
            session_id, call_text, call_reasoning
        )
        in_model_call = False
        call_text = ""
        call_reasoning = ""
        call_cut = False
        if warning is not None:
            yield self._text_chunk(warning)
            if is_halt:
                logger.warning(
                    "[RepetitionGuardWrapper] session={} "
                    "cross-call HALT — cancelling stream",
                    session_id,
                )
                break

    yield chunk
    continue
```

#### 改之后

```python
if mode == "updates":
    saw_updates = True
    # Reset per-call tracking (cross-call detection is
    # handled by the middleware's wrap_model_call).
    call_text = ""
    call_cut = False
    yield chunk
    continue
```

**删除：**

- `in_model_call` 边界检测和 `_on_model_call_end` 调用
- `call_reasoning` 重置
- warning 生成和 HALT break 逻辑

---

### 7e. 简化非-model 节点分支——删除跨调用边界检测

#### 改之前

```python
if node != "model":
    if in_model_call:
        warning, is_halt = self._on_model_call_end(
            session_id, call_text, call_reasoning
        )
        in_model_call = False
        call_text = ""
        call_reasoning = ""
        call_cut = False
        if warning is not None:
            yield self._text_chunk(warning)
            if is_halt:
                logger.warning(
                    "[RepetitionGuardWrapper] session={} "
                    "cross-call HALT — cancelling stream",
                    session_id,
                )
                break
    yield chunk
    continue
```

#### 改之后

```python
if node != "model":
    call_text = ""
    call_cut = False
    yield chunk
    continue
```

---

### 7f. 删除 `in_model_call = True` 和 model node 的大段注释

#### 改之前

```python
# --------------------------------------------------
# Model node — we're inside a model call
# --------------------------------------------------
# [phantom-stream guard] On a fresh dict-input run the
# middleware-equipped graph ALWAYS emits before_agent
# "updates" tuples before any model text ...
# (大段注释，约 10 行)
if (
    phantom_guard_active
    and not saw_updates
    and str(getattr(msg_chunk, "content", "") or "")
):
    phantom_dropped += 1
    if phantom_dropped == 1:
        logger.critical(...)
    continue
in_model_call = True
```

#### 改之后

```python
# --------------------------------------------------
# Model node — internal repetition detection
# --------------------------------------------------
# [phantom-stream guard]
if (
    phantom_guard_active
    and not saw_updates
    and str(getattr(msg_chunk, "content", "") or "")
):
    phantom_dropped += 1
    if phantom_dropped == 1:
        logger.critical(...)
    continue
# （删除 in_model_call = True）
```

---

### 7g. 删除 reasoning 累积

#### 改之前

```python
# Extract text content and reasoning
content = str(msg_chunk.content or "")
reasoning = ""
ak = getattr(msg_chunk, "additional_kwargs", None)
if ak and isinstance(ak, dict):
    for rk in _REASONING_KEYS:
        rv = str(ak.get(rk, "") or "")
        if rv:
            reasoning = rv
            break

# ---- accumulate + stream-level internal detection ----
if content and not call_cut:
    call_text += content
    if not has_tool_calls and len(call_text) >= _MIN_CONTENT_LENGTH:
        ...

# ---- accumulate reasoning (checked at boundary) ----
if reasoning:
    call_reasoning += reasoning
```

#### 改之后

```python
content = str(msg_chunk.content or "")

# ---- accumulate + stream-level internal detection ----
if content and not call_cut and not has_tool_calls:
    call_text += content
    if len(call_text) >= _MIN_CONTENT_LENGTH:
        ...
```

**删除：**

- reasoning 提取（`_REASONING_KEYS` 遍历）
- `call_reasoning` 累积

---

### 7h. 删除 end-of-stream 跨调用处理

#### 改之前

```python
            # ---- end of stream: process the last model call ----
            if in_model_call:
                warning, is_halt = self._on_model_call_end(
                    session_id, call_text, call_reasoning
                )
                if warning is not None:
                    yield self._text_chunk(warning)

        finally:
            try:
                logger.debug("[RGW-DIAG] astream EXIT session={}", session_id)
            except Exception:
                pass
            if phantom_dropped > 0:
                ...
```

#### 改之后

```python
        finally:
            if phantom_dropped > 0:
                ...
```

**删除：**

- end-of-stream 的 `_on_model_call_end` 调用
- `[RGW-DIAG] astream EXIT` 日志

---

## 8. `ainvoke` 方法

### 改之前

```python
async def ainvoke(self, *args, **kwargs) -> Any:
    """Intercepted non-streaming with post-hoc repetition detection.

    Resets per-turn state, delegates to the inner agent, then inspects
    the final ``AIMessage`` of the result.  If repetition is detected,
    the last message is replaced with the guard's warning/halt message.
    """
    input_ = args[0] if args else kwargs.get("input")
    config = args[1] if len(args) > 1 else kwargs.get("config")

    session_id = self._extract_session_id(input_, config)

    # Per-turn state reset
    self._guard._before_agent_impl({"session_id": session_id})

    result = await self._inner.ainvoke(*args, **kwargs)

    # Post-hoc detection on the last AIMessage
    if isinstance(result, dict) and "messages" in result:
        messages = result["messages"]
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, AIMessage):
                try:
                    replacement = self._post_hoc_check(session_id, last_msg)
                    if replacement is not None:
                        result["messages"][-1] = replacement
                except Exception:
                    logger.exception(
                        "[RepetitionGuardWrapper] post-hoc detection error (non-fatal)"
                    )

    return result
```

### 改之后

```python
async def ainvoke(self, *args, **kwargs) -> Any:
    """Delegate to the inner agent.

    The ``OutputRepetitionGuard`` middleware on the inner agent
    handles all post-hoc detection (cross-call, internal, reasoning)
    via ``wrap_model_call``.  The wrapper does not need to do anything
    here.
    """
    return await self._inner.ainvoke(*args, **kwargs)
```

**删除：**

- `session_id` 提取
- per-turn 状态重置（`_before_agent_impl`）
- `_post_hoc_check` 调用和消息替换逻辑

---

## 9. 保留不变的部分

以下方法/逻辑**完全保留**，未做任何修改：

| 方法                               | 说明                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `_extract_session_id()`            | 从 input/Command.resume/config 提取 session_id                           |
| `_can_intercept()`                 | 检查 stream_mode 是否包含 "messages"                                     |
| `_text_chunk()`                    | 构建 `("messages", (AIMessageChunk, metadata))` chunk                    |
| `_halted_short_circuit_message()`  | HALT short-circuit 的消息文本                                            |
| `__getattr__()`                    | 透明委托未知属性到 inner agent                                           |
| `inner` property                   | 返回 wrapped inner agent                                                 |
| Phantom stream guard 逻辑          | 检测并丢弃 pre-update model text                                         |
| HALT short-circuit 逻辑            | 读 `_HALTED_KEY` flag，yield halt 消息                                   |
| 流式内部重复检测逻辑               | `_detect_internal_repetition` + `call_cut` + `_INTERNAL_WARNED_KEY` 去重 |
| `finally` 块中的 generator cleanup | `aclose()` 清理                                                          |

---

## 改动统计

| 指标           | 改之前                                              | 改之后                                                                                                                                                 |
| -------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 总行数         | 635                                                 | 332                                                                                                                                                    |
| 类方法数       | 13 (`__init__` + 12 methods)                        | 9 (`__init__` + 8 methods)                                                                                                                             |
| 删除的方法     | —                                                   | `_reset_session_state()`, `_extract_reasoning_from_chunk()`, `_on_model_call_end()`, `_post_hoc_check()`                                               |
| 保留的检测逻辑 | 内部重复 + 跨调用 + reasoning + post-hoc + 状态重置 | **仅内部重复**                                                                                                                                         |
| import 数      | 15                                                  | 10                                                                                                                                                     |
| 测试结果       | —                                                   | `test_repetition_guard_wrapper.py`: 49 passed; `test_output_repetition_guard.py`: 99 passed; `test_completion_drain.py`: 3 passed (**151/151 PASSED**) |
