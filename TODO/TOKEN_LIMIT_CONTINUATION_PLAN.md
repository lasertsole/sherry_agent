# Token Limit Detection & Continuation — Implementation Plan

> Status: **Completed** (119/119 tests pass, 0 regressions)
> Date: 2026-09-07
> Owner: Agent
> Predecessor: STREAM_REPETITION_OPTIMIZATION.md (completed, 140/140 tests pass)

---

## 1. Background

### 1.1 问题

当 LLM 输出达到 `max_tokens` 上限时，`finish_reason`（OpenAI）或 `stop_reason`（Anthropic）返回 `"length"` / `"max_tokens"`，响应被截断。当前 sherry_agent 代码：

- **从不读取** `finish_reason` — `stream_dispatch.py:305-317` 只提取 `model_name` 和 `usage_metadata`
- **DB 始终写 None** — `context_engine/store/core.py:133,188,212` 硬编码 `"finish_reason": None`
- **无续写/重试机制** — 截断后直接结束，用户收到不完整回答

### 1.2 跨项目调研结论

| 项目                | 检测                                                  | 文本续写                              | 工具调用截断                                           | 评价     |
| ------------------- | ----------------------------------------------------- | ------------------------------------- | ------------------------------------------------------ | -------- |
| **hermes-agent**    | `finish_reason=="length"` 全 provider                 | 续写 HumanMessage + re-call，4 次重试 | `max_tokens *= 2^retry`，cap 32768，不 append 截断响应 | 最完整   |
| **oh-my-openagent** | `isContinuableStopReason(stop_reason="length")`       | goal continuation queue               | 无                                                     | 中等     |
| **opencode-dev**    | 定义了 FinishReason schema + 5 个 `mapFinishReason()` | **检测到但不处理**，session 直接 idle | 无                                                     | 反面教材 |
| **sherry_agent**    | **无**                                                | **无**                                | **无**                                                 | 需实现   |

### 1.3 架构约束

1. **langchain `create_agent` 行为**：AIMessage 无 tool_calls → agent loop 结束。middleware 的 `wrap_model_call` 只能替换输出，不能触发新的 model 调用（调 handler = 递归）。因此**续写必须在 `StreamTurn` 服务层**。

2. **agent 每 turn 重建**：`built_agent(force_rebuild=True)` 每次调用都重建 graph + 新 httpx client。SQLite checkpointer 独立于 graph 持久化状态，重建安全。

3. **`_GenerateTurn` 不持有 agent 引用**：`_get_generator()` 内部建 agent 并返回 stream/invoke result，turn 对象拿不到 agent。`_ResumeTurn` 相反——`_prepare()` 中 `self._agent = await built_agent(force_rebuild=True)`。

4. **checkpointer 续写原理**：调用 `agent.astream()` 传入新 HumanMessage 时，checkpointer 加载已有状态（含截断的 AIMessage）→ append 新 HumanMessage → model 看到 [截断回答 + 续写指令] → 从中断处继续。

5. **provider key 差异**：
   - OpenAI: `response_metadata["finish_reason"] == "length"`
   - Anthropic: `response_metadata["stop_reason"] == "max_tokens"`

6. **当前 max_tokens 未显式设置**：`main_llm.py` 的 `model_config` 用 `profile.max_input_tokens`（上下文窗口），无输出 `max_tokens`。模型使用 provider 默认输出限制。

7. **`request.model_settings` 是 `ModelRequest` 的 dict**（非 `model_kwargs`），通过 `model.bind(**request.model_settings)` 传递给模型。middleware 注入 max_tokens 需写入 `request.model_settings`。

8. **流式 token 通过 callback 实时外泄**：langgraph `stream_mode=["messages"]` 通过 `on_llm_new_token` callback 实时转发 token 到客户端。第一次 model call 的 token 已发送给客户端，无法回退。但 re-call 时可 strip `request.config["callbacks"]` 为 None，使重试调用不产生流式输出，避免重复/garbled 输出。callback 在 `finally` 中恢复，不影响后续 model call。

---

## 2. 方案概览

| Phase | 目标                         | 改造层                           | 复杂度 | 文件数        | 状态 |
| ----- | ---------------------------- | -------------------------------- | ------ | ------------- | ---- |
| 1     | 检测 finish_reason 并透传    | StreamTurn + store/core.py       | 低     | 4             | ✅   |
| 2     | 文本续写（无 tool calls）    | StreamTurn.run() + _GenerateTurn | 高     | 2             | ✅   |
| 3     | 工具调用截断 max_tokens 倍增 | 新 middleware + core.py          | 中     | 3 新建 + 2 改 | ✅   |

### 决策摘要

| 问题                                       | 决策                                                         | 理由                                                                                                                                   |
| ------------------------------------------ | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Phase 3 用 middleware 还是 agent rebuild？ | **Middleware（统一 re-call 策略 + callback stripping）**     | 非流式和流式都用 in-handler re-call（+0 budget）；流式 re-call 时 strip callbacks 避免重复输出                                         |
| base max_tokens 从哪读？                   | **新 env var `MAIN_LLM_OUTPUT_MAX_TOKEN`（默认 8192）**      | 不与 `MAIN_LLM_MAX_TOKEN`（上下文窗口）混淆                                                                                            |
| 续写在哪做？                               | **`StreamTurn.run()` 外层 while 循环**                       | middleware 无法触发新 model 调用                                                                                                       |
| `_get_generator` 怎么处理？                | **内联到 `_GenerateTurn._create_source()`**                  | `_GenerateTurn` 需持有 agent 引用，与 `_ResumeTurn` 对齐                                                                               |
| 续写 HumanMessage 内容？                   | hermes-agent 原文                                            | 已验证有效                                                                                                                             |
| 最大重试次数？                             | 文本续写 4 次，工具调用 3 次                                 | 与 hermes-agent 一致                                                                                                                   |
| 流式 re-call 如何避免重复输出？            | **strip `request.config["callbacks"]` → None，finally 恢复** | 第一次 call 的 token 已外泄（客户端可见），re-call 时无 callback 不产生流式输出。agent loop 只看到 middleware 返回的最终成功 AIMessage |

---

## 3. Phase 1: Detection ✅

### 3.1 目标

从 stream 和 invoke 路径提取 `finish_reason`，存入 `StreamTurn.meta_finish_reason`，在 meta chunk 中透传给客户端，并在 DB 中持久化。

### 3.2 文件变更

#### `server/service/stream_dispatch.py`

**`StreamTurn.__init__`** — 新增两个属性：

```python
self.meta_finish_reason: str | None = None
self._has_tool_calls: bool = False
```

**`StreamTurn.run()` metadata 捕获块** — 在 `_usage` 提取之后添加：

```python
_finish = _resp_meta.get("finish_reason") or _resp_meta.get("stop_reason")
if _finish:
    self.meta_finish_reason = _finish
```

**tool_calls 检测块** — 在 `if len(tool_calls) > 0` 条件成立时：

```python
if len(tool_calls) > 0:
    self._has_tool_calls = True
```

#### `server/service/messages.py`

**`_GenerateTurn._invoke_frames()`** — 在现有 metadata 提取块中补充 finish_reason + tool_calls 检测。

**`_GenerateTurn._final_frames()`** — meta chunk 增加 `finish_reason` 字段：

```python
return [{
    "type": "meta",
    "content": "",
    "model_name": self.meta_model_name or "",
    "input_tokens": self.meta_input_tokens or 0,
    "output_tokens": self.meta_output_tokens or 0,
    "finish_reason": self.meta_finish_reason or "",
}]
```

#### `context_engine/store/core.py`

3 处 `"finish_reason": None` → 从 `response_metadata` 实际提取：

```python
_finish_reason = response_metadata.get("finish_reason") or response_metadata.get("stop_reason")
```

#### `server/service/stream_driver.py`

done frame 增加 `finish_reason` 字段。

### 3.3 验证结果

- ✅ 正常完成时 meta chunk 的 `finish_reason` 为 `"stop"` 或 `""`
- ✅ 截断时为 `"length"`（OpenAI）或 `"max_tokens"`（Anthropic）
- ✅ DB 中 `finish_reason` 列不再始终为 None
- ✅ 客户端 done frame 透传 `finish_reason`

---

## 4. Phase 2: Text Continuation ✅

### 4.1 目标

当模型输出被 `max_tokens` 截断且响应**不含 tool calls**时，注入续写 HumanMessage 并 re-stream，最多 4 次。

### 4.2 核心流程

```
┌─────────────────────────────────────────────────┐
│  StreamTurn.run()                                │
│                                                  │
│  while True:                                     │
│    _create_source()  ────► agent.astream/ainvoke │
│    [process stream/invoke, yield frames]         │
│    if _should_text_continue():                   │
│      _prepare_continuation()  ──► set flag       │
│      _continuation_retries += 1                  │
│      continue  ──────────────────────────┐       │
│    break  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ │       │
│  yield _final_frames()                    │       │
│  [cleanup]                                ▼       │
│                                      next iteration│
│  (with continuation HumanMessage)                │
└─────────────────────────────────────────────────┘
```

### 4.3 文件变更

#### `server/service/stream_dispatch.py`

**新增虚方法**：

```python
def _should_text_continue(self) -> bool:
    """Whether to inject a continuation prompt and re-stream after truncation."""
    return False

def _prepare_continuation(self) -> None:
    """Prepare state for a continuation re-restream (e.g., set a flag)."""
    pass
```

**`run()` 重构为外层 while 循环**：

- `_create_source()` 在 while 循环内调用，每次迭代创建新 source
- 前一个 source 在创建新 source 前关闭（`await source.aclose()`）
- `_final_frames()` 和 `_log_completed` 移到 while 循环外——只在所有续写完成后执行一次
- exception handling 不变
- 新增 `is_stream_turn` flag（在 `_create_source()` 后设置，`_cleanup()` 清除）供 Phase 3 middleware 使用

#### `server/service/messages.py`

**常量**：

```python
_CONTINUATION_PROMPT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)
_MAX_CONTINUATION_RETRIES = 4
```

**`_GenerateTurn.__init__`** — 新增字段：

- `self._agent: Any = None` — 持有 agent 引用
- `self._continuation_retries: int = 0` — 续写计数
- `self._is_continuation: bool = False` — 续写 flag

**`_GenerateTurn._prepare()`** — 构建 agent（对齐 `_ResumeTurn`）：

```python
async def _prepare(self) -> None:
    reset_idle_for_seconds()
    self._agent = await built_agent(force_rebuild=True)
```

**`_GenerateTurn._create_source()`** — 内联 `_get_generator` 逻辑 + 续写路径：

- `self._is_continuation == True` → input 只含 `HumanMessage(content=_CONTINUATION_PROMPT)`
- `self._is_continuation == False` → 原始 `content_list` 路径
- 使用 `self._agent.astream()` / `self._agent.ainvoke()` 而非全局 `_get_generator()`

**`_GenerateTurn._should_text_continue()`** — 覆写：

```python
def _should_text_continue(self) -> bool:
    return (
        self.meta_finish_reason in ("length", "max_tokens")
        and not self._has_tool_calls
        and self._continuation_retries < _MAX_CONTINUATION_RETRIES
    )
```

**`_GenerateTurn._prepare_continuation()`** — 覆写：

- 设置 `self._is_continuation = True`
- 递增 `self._continuation_retries`
- 重置 `meta_finish_reason` 和 `_has_tool_calls`

**`_GenerateTurn._cleanup()`** — 清理 agent 引用：`self._agent = None`

### 4.4 续写时的 meta chunk 行为

- `_final_frames()` 只在 while 循环外调用一次——客户端只收到一个 meta chunk
- 该 meta chunk 携带的是**最后一次** model 调用的 finish_reason
- `output_tokens` 是最后一次调用的 token 数（非累计）

### 4.5 `_ResumeTurn` 影响

`_ResumeTurn` 不覆写 `_should_text_continue()`（继承基类的 `return False`），不受 Phase 2 影响。

### 4.6 `_get_generator` 保留

`_get_generator()` 函数仍存在于 `messages.py` 中（不删除），但不再被 `_GenerateTurn._create_source` 调用。如有其他代码路径使用仍可调用。

---

## 5. Phase 3: Tool-Call Truncation max_tokens Boost ✅

### 5.1 目标

当 `finish_reason=length` 且响应**含 tool calls**（工具调用 JSON 被截断）时，倍增 `max_tokens` 并在 middleware 内 re-call handler 重试。

### 5.2 机制（统一 re-call + callback stripping）

**流式和非流式统一策略**：都在 `awrap_model_call` 内 re-call handler。区别是流式 re-call 前 strip `request.config["callbacks"]` 为 None，避免重复流式输出。

```
┌─ 统一 re-call 流程（流式 + 非流式） ───────────────────────────┐
│ MaxTokensBoost.awrap_model_call(request, handler)              │
│                                                                │
│   result = await handler(request)        ← 第一次 call         │
│   if not truncation: return result       ← 成功，直接返回       │
│                                                                │
│   if is_stream:                          ← 流式：strip callbacks│
│     saved = request.config["callbacks"]                        │
│     request.config["callbacks"] = None                          │
│                                                                │
│   try:                                                         │
│     for attempt in range(1, 4):                                │
│       boosted = base * 2^attempt, capped at 32768             │
│       _inject_boost(request, boosted)                          │
│       result = await handler(request)   ← re-call，无流式输出  │
│       if not truncation: break           ← 成功                 │
│     else:                               ← 耗尽                  │
│       log warning                                               │
│   finally:                                                     │
│     if is_stream: request.config["callbacks"] = saved  ← 恢复  │
│                                                                │
│   return result                         ← 最终结果（成功/截断）│
│                                                                │
│ IterationBudget: +1 total (第一次 call)                        │
│ Re-calls: +0 (在 awrap_model_call 内，不触发外层)              │
└────────────────────────────────────────────────────────────────┘
```

**为什么流式 re-call 也 +0 IterationBudget**：middleware 链中 `handler` 是内层 middleware/model call，不是 agent loop。re-call handler 时 IterationBudget 的 `awrap_model_call` 已在外层执行过，不会重复触发。agent loop 只看到 middleware 返回的最终 `AIMessage`，计 1 次迭代。

**截断响应不进 checkpointer**：agent loop 只看到 middleware 返回的最终（成功版）`AIMessage`，截断的中间结果在 middleware 内被丢弃，不写入 checkpointer state。与 hermes-agent "不 append 截断响应"行为一致。

**callback stripping 的效果**：

- 第一次 call：使用原始 callbacks，token 实时流式转发到客户端（正常流式体验）
- re-call（如果第一次截断）：callbacks = None，不产生 `on_llm_new_token`，客户端看不到重复输出
- 恢复 callbacks：在 `finally` 中恢复，不影响后续 model call

**客户端体验**：

- 第一次 call 的截断输出会发送到客户端（部分文本 + 截断的 tool_call JSON）
- re-call 不产生流式输出（静默重试）
- agent loop 处理 middleware 返回的成功 AIMessage（完整 tool_calls → 正常执行）
- 客户端看到的是：部分截断输出 → tool 执行结果 → 后续 model call 的正常流式输出

### 5.3 文件变更

#### 新建 `agent/middlewares/max_tokens_boost.py`

```python
_BASE_MAX_TOKENS = int(os.getenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "8192"))
_MAX_CAP = 32_768
_MAX_RETRIES = 3
_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})
_STREAM_FLAG = "is_stream_turn"
```

**`MaxTokensBoostMiddleware` 核心逻辑**：

- `_extract_ai_message(result)` — 处理三种返回类型：`AIMessage` / `ModelResponse` / `ExtendedModelResponse`
- `_detect_tool_call_truncation(result)` — `finish_reason in _TRUNCATION_REASONS and has tool_calls`
- `_inject_boost(request, max_tokens)` — 写入 `request.model_settings["max_tokens"]`
- `_strip_callbacks(request)` — 保存并清除 `request.config["callbacks"]`，返回原始值
- `_restore_callbacks(request, original)` — 恢复原始 callbacks
- `wrap_model_call(request, handler)` — sync 透传
- `awrap_model_call(request, handler)` — 统一 re-call 策略：
  - 第一次 handler call（原始 max_tokens，原始 callbacks）
  - 如果截断 + tool_calls → 进入 re-call 循环
  - 流式模式：strip callbacks 避免重复输出
  - progressive boost `base * 2^attempt`，cap 32768，max 3 retries
  - 成功则 break，耗尽则返回最后截断 result
  - `finally` 中恢复 callbacks（即使异常也恢复）

#### `agent/middlewares/__init__.py`

导出 `MaxTokensBoostMiddleware`。

#### `agent/core.py`

middleware 列表添加 `MaxTokensBoostMiddleware()`，位置在 `OutputRepetitionGuard()` 之后、`HeartbeatStaleness()` 之前：

```python
middleware=[
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ToolGuardrails(),
    ToolCallNormalize(),
    SubagentCompletionDrainMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),  # 新增
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(...),
],
```

#### `server/service/stream_dispatch.py`

- `run()` 中 `_create_source()` 后设置 `is_stream_turn` flag 到 `state_register_mem`
- `_cleanup()` 清除 `is_stream_turn`（不再清除 `_max_tokens_boost_level`，该 key 已废弃）

### 5.4 环境变量

```env
MAIN_LLM_OUTPUT_MAX_TOKEN=8192
```

- `MAIN_LLM_MAX_TOKEN` → `profile.max_input_tokens`（context window, e.g. 65536）
- `MAIN_LLM_OUTPUT_MAX_TOKEN` → output token limit（e.g. 8192），Phase 3 boost 的 base

### 5.5 IterationBudget 消耗对比

| 方案                                      | 每次 tool-call 截断重试消耗                            | 3 次重试总消耗 | 与 hermes-agent 对齐？ |
| ----------------------------------------- | ------------------------------------------------------ | -------------- | ---------------------- |
| 原方案（agent loop 自然重试）             | +2（model call + tool 失败 → error → model retry）     | +6             | ❌                     |
| **统一 re-call + callback stripping**     | **+0**（在 middleware 内完成，IterationBudget 不重计） | **+0**         | ✅                     |
| hermes-agent（run_conversation 内层循环） | +0（同一 attempt 内 re-issue API call）                | +0             | —                      |

### 5.6 callback stripping 如何封死流式重复输出

| 问题                        | 解决方案                                                                                                             |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| 第一次 call 的 token 已外泄 | 无法回退——客户端已收到部分截断输出。但 agent loop 只使用 middleware 返回的最终成功 AIMessage，不影响 tool 执行正确性 |
| re-call 产生重复流式输出    | re-call 前 `request.config["callbacks"] = None`，`on_llm_new_token` 不触发，无重复输出                               |
| 异常时 callbacks 未恢复     | `try/finally` 保证恢复——即使 re-call 抛异常，`finally` 中恢复原始 callbacks                                          |
| 后续 model call 受影响      | 不受影响——callbacks 在 `finally` 中恢复，且每个 model call 有独立的 `ModelRequest`                                   |

---

## 6. 三 Phase 协同流程

```
用户消息
    │
    ▼
StreamTurn.run()  ─── while True ──────────────────────────┐
    │                                                      │
    ├─ _create_source() → agent.astream/ainvoke             │
    │   (set is_stream_turn flag for Phase 3)               │
    │                                                      │
    ├─ [stream chunks processed, text/tool frames yielded] │
    │                                                      │
    │   ┌── middleware chain (per model call) ─────────┐  │
    │   │ IterationBudget.awrap_model_call  (+1 计数)   │  │
    │   │  └→ MaxTokensBoost.awrap_model_call           │  │
    │   │       result = handler(request)  ← 1st call   │  │
    │   │       if truncation + tool_calls:             │  │
    │   │         strip callbacks (if streaming)        │  │
    │   │         for attempt in 1..3:                  │  │
    │   │           _inject_boost(base * 2^attempt)      │  │
    │   │           handler(request) → no streaming      │  │
    │   │           if not truncation: break             │  │
    │   │         restore callbacks (finally)            │  │
    │   │       return final result                      │  │
    │   │ OutputRepetitionGuard                          │  │
    │   │   (post-call repetition detection)             │  │
    │   └──────────────────────────────────────────────┘  │
    │                                                      │
    │   [agent loop: model → tools → model → ...]          │
    │                                                      │
    ├─ stream ends, check _should_text_continue()           │
    │   ├─ finish_reason=length AND no tool_calls           │
    │   │   AND retries < 4 → _prepare_continuation()       │
    │   │                       continue ──────────────────┤
    │   └─ else → break ─────────────────────────────────  │
    │                                          │           │
    ├─ yield _final_frames()  (meta chunk)    │           │
    └─ _cleanup()  (close source, clear flags) │           │
                                               └───────────┘
```

**Phase 2 和 Phase 3 互斥**：

- `finish_reason=length` + **无** tool_calls → Phase 2（文本续写，StreamTurn 外层循环）
- `finish_reason=length` + **有** tool_calls → Phase 3（middleware boost + callback stripping）
- 不会同时触发

**Phase 3 在 Phase 2 的 while 循环内**：如果 Phase 3 的 boost 让 model 在重试中完成了 tool call，然后 model 产出文本又被截断（`finish_reason=length` + 无 tool_calls），Phase 2 的 `_should_text_continue()` 会检测到并注入续写。两个 Phase 协同工作。

---

## 7. 完整文件变更清单

### 新建文件

| 文件                                          | 内容                                                          |
| --------------------------------------------- | ------------------------------------------------------------- |
| `agent/middlewares/max_tokens_boost.py`       | MaxTokensBoostMiddleware（统一 re-call + callback stripping） |
| `tests/unit/test_token_limit_continuation.py` | 37 个单元测试（Phase 1+2+3 + 集成）                           |

### 修改文件

| 文件                                        | Phase | 变更                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `server/service/stream_dispatch.py`         | 1,2,3 | `__init__` 加 `meta_finish_reason`/`_has_tool_calls`；metadata 块加 finish_reason 提取 + tool_calls 标记；`run()` 加外层 while 循环 + 续写检查 + source close；新增 `_should_text_continue`/`_prepare_continuation` 虚方法；`_create_source()` 后设置 `is_stream_turn` flag；`_cleanup()` 清除 `is_stream_turn`                                                    |
| `server/service/messages.py`                | 1,2   | `_GenerateTurn.__init__` 加 `_agent`/`_continuation_retries`/`_is_continuation`；`_prepare` 加 agent 构建；`_create_source` 内联 + 续写路径；`_invoke_frames` 加 finish_reason + tool_calls 检测；`_final_frames` 加 finish_reason 字段；新增 `_should_text_continue`/`_prepare_continuation` 覆写；`_cleanup` 加 agent 引用清理；新增 `_CONTINUATION_PROMPT` 常量 |
| `server/service/stream_driver.py`           | 1     | done frame 加 `finish_reason` 字段                                                                                                                                                                                                                                                                                                                                 |
| `context_engine/store/core.py`              | 1     | 3 处 `"finish_reason": None` → 从 `response_metadata` 实际提取                                                                                                                                                                                                                                                                                                     |
| `agent/core.py`                             | 3     | middleware 列表添加 `MaxTokensBoostMiddleware()`（在 OutputRepetitionGuard 之后）                                                                                                                                                                                                                                                                                  |
| `agent/middlewares/__init__.py`             | 3     | 导出 `MaxTokensBoostMiddleware`                                                                                                                                                                                                                                                                                                                                    |
| `tests/unit/server/test_stream_dispatch.py` | -     | 加 `built_agent` mock + `finish_reason` 断言；重构 `test_invoke_source_emits_text_then_meta`                                                                                                                                                                                                                                                                       |
| `tests/unit/server/test_stream_driver.py`   | -     | done frame 断言加 `finish_reason`                                                                                                                                                                                                                                                                                                                                  |

### 不变文件

| 文件                                       | 原因                                                                              |
| ------------------------------------------ | --------------------------------------------------------------------------------- |
| `models/LLMs/main_llm.py`                  | 不需要改——max_tokens 注入由 middleware 在 request 层完成，不涉及 model 构造       |
| `models/LLMs/reasoning_normalizer.py`      | 不需要改——NormalizingChatModel 透传 kwargs，middleware 注入的 max_tokens 自动穿透 |
| `_ResumeTurn`（messages.py）               | 不覆写 `_should_text_continue`，继承基类 `return False`                           |
| `agent/stream_repetition_guard_wrapper.py` | 已完成优化，不涉及 token limit                                                    |
| `_get_generator`（messages.py）            | 保留不删除，不再被 `_GenerateTurn._create_source` 调用                            |

---

## 8. 测试结果

### 实际测试文件

统一放在 `tests/unit/test_token_limit_continuation.py`，共 37 个测试，分 4 个测试类：

### 8.1 Phase 1: TestFinishReasonDetection (8 tests) ✅

| 测试名                                         | 描述                                                                           |
| ---------------------------------------------- | ------------------------------------------------------------------------------ |
| `test_stream_finish_reason_length`             | stream final chunk `finish_reason="length"` → `meta_finish_reason == "length"` |
| `test_stream_finish_reason_stop`               | `finish_reason="stop"` → `meta_finish_reason == "stop"`                        |
| `test_stream_stop_reason_max_tokens_anthropic` | `stop_reason="max_tokens"` → 正确提取                                          |
| `test_invoke_finish_reason_extracted`          | invoke result `finish_reason="length"` → `_invoke_frames` 提取                 |
| `test_meta_chunk_includes_finish_reason`       | `_final_frames()` meta chunk 含 `"finish_reason"` key                          |
| `test_meta_chunk_finish_reason_stop_on_normal` | 正常完成时 `finish_reason == "stop"`                                           |
| `test_has_tool_calls_set_on_tool_call_chunk`   | stream 含 tool_call chunk → `_has_tool_calls == True`                          |
| `test_has_tool_calls_false_on_text_only`       | 纯文本 stream → `_has_tool_calls == False`                                     |

### 8.2 Phase 2: TestTextContinuation (8 tests) ✅

| 测试名                                               | 描述                                                                           |
| ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| `test_should_continue_on_length_no_tool_calls`       | `finish_reason="length"` + 无 tool_calls → `_should_text_continue()` 返回 True |
| `test_should_continue_on_max_tokens_no_tool_calls`   | `finish_reason="max_tokens"` + 无 tool_calls → True                            |
| `test_should_not_continue_on_stop`                   | `finish_reason="stop"` → False                                                 |
| `test_should_not_continue_with_tool_calls`           | `finish_reason="length"` + 有 tool_calls → False（走 Phase 3）                 |
| `test_should_not_continue_after_max_retries`         | retries >= 4 → False                                                           |
| `test_prepare_continuation_sets_flag_and_increments` | 调用后 `_is_continuation == True` + `_continuation_retries += 1`               |
| `test_create_source_uses_continuation_prompt`        | `_is_continuation == True` 时 input 含 `_CONTINUATION_PROMPT`                  |
| `test_continuation_retries_reset_per_turn`           | 新 turn 时 `_continuation_retries` 重置为 0                                    |

### 8.3 Phase 3: TestMaxTokensBoostMiddleware (18 tests) ✅

#### 共享行为（非流式 + 流式）

| 测试名                                           | 描述                                                                  |
| ------------------------------------------------ | --------------------------------------------------------------------- |
| `test_no_retry_on_normal_stop`                   | `finish_reason="stop"` → handler 只 call 1 次                         |
| `test_no_retry_on_text_truncation_no_tool_calls` | `finish_reason="length"` 无 tool_calls → 不 re-call（Phase 2 的场景） |
| `test_boost_capped_at_max_cap`                   | boost 不超过 32768                                                    |
| `test_truncated_result_returned_on_exhaustion`   | 4 次都截断 → 返回最后截断 result（不抛异常）                          |

#### 非流式 re-call

| 测试名                                            | 描述                                                             |
| ------------------------------------------------- | ---------------------------------------------------------------- |
| `test_non_stream_re_call_on_tool_call_truncation` | 非流式 + `finish_reason="length"` + tool_calls → handler re-call |
| `test_non_stream_progressive_boost`               | 非流式连续截断 → max_tokens 依次 8192(原始), 16384, 32768        |
| `test_non_stream_max_3_retries`                   | 非流式每次截断 → handler 最多 call 4 次（1 + 3 retry）           |
| `test_non_stream_success_returns_second_result`   | 第 1 次截断 → re-call → 第 2 次成功 → 返回第 2 次结果            |

#### 流式 re-call + callback stripping

| 测试名                                                  | 描述                                                         |
| ------------------------------------------------------- | ------------------------------------------------------------ |
| `test_stream_re_call_on_tool_call_truncation`           | 流式 + 截断 → handler re-call（与非流式行为一致）            |
| `test_stream_strips_callbacks_during_re_call`           | 第一次 call 保留原始 callbacks，re-call 时 callbacks == None |
| `test_stream_restores_callbacks_after_re_call`          | re-call 完成后 callbacks 恢复为原始值                        |
| `test_stream_restores_callbacks_even_on_exception`      | re-call 抛异常时 `finally` 中 callbacks 仍恢复               |
| `test_stream_callbacks_not_stripped_on_first_call`      | 第一次 call 不 strip callbacks（正常流式体验）               |
| `test_stream_progressive_boost`                         | 流式连续截断 → max_tokens 依次 8192(原始), 16384, 32768      |
| `test_stream_max_3_retries`                             | 流式每次截断 → handler 最多 call 4 次（1 + 3 retry）         |
| `test_stream_success_returns_second_result`             | 流式第 1 次截断 → re-call → 第 2 次成功 → 返回第 2 次结果    |
| `test_stream_no_retry_on_normal_stop`                   | 流式 + stop → 1 call，callbacks 未 strip                     |
| `test_stream_no_retry_on_text_truncation_no_tool_calls` | 流式 + length + 无 tool_calls → 1 call，callbacks 未 strip   |

### 8.4 Phase 2 + Phase 3 集成: TestPhase2Phase3Integration (3 tests) ✅

| 测试名                                              | 描述                                                                    |
| --------------------------------------------------- | ----------------------------------------------------------------------- |
| `test_text_truncation_triggers_phase2_not_phase3`   | `finish_reason="length"` + 无 tool_calls → Phase 2 触发，Phase 3 不触发 |
| `test_tool_call_truncation_does_not_trigger_phase2` | `finish_reason="length"` + 有 tool_calls → Phase 2 不触发               |
| `test_stop_does_not_trigger_either_phase`           | `finish_reason="stop"` → 两个 Phase 都不触发                            |

### 8.5 现有测试更新 ✅

| 测试文件                                    | 变更                                                                                                   |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `tests/unit/server/test_stream_dispatch.py` | `built_agent` mock + `finish_reason` 断言；`test_invoke_source_emits_text_then_meta` 重构用 fake agent |
| `tests/unit/server/test_stream_driver.py`   | done frame 断言加 `finish_reason`                                                                      |

### 8.6 全量回归

```
119 passed, 2 warnings in 5.06s
```

- 37 new tests (`test_token_limit_continuation.py`)
- 82 existing tests (updated for `built_agent` mock + `finish_reason` assertions)
- 0 regressions

---

## 9. 风险与缓解

| 风险                                                                      | 缓解                                                                                                                          |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 续写 HumanMessage 被 checkpointer 持久化，出现在历史中                    | 可接受——hermes-agent 同样持久化续写消息。客户端可识别 `[System: ...]` 前缀并隐藏                                              |
| `meta_finish_reason` 是最后一次调用而非首次截断的值                       | 可在 `_prepare_continuation` 中保存前次值。初版接受"最后一次"语义                                                             |
| middleware `request.model_settings` 访问方式                              | 已验证 `ModelRequest` 有 `model_settings: dict[str, Any]`，通过 `model.bind(**request.model_settings)` 传递给模型             |
| `_get_generator` 内联后其他调用方断链                                     | 搜索确认仅 `_GenerateTurn._create_source` 调用。保留 `_get_generator` 函数体避免 breaking change                              |
| Ollama GLM 可能误报 `finish_reason="length"`（hermes-agent 有专门检测）   | 初版不处理；如出现可在 `_detect_tool_call_truncation` 中加 Ollama 检测分支                                                    |
| 续写时 `ai_text` 累积重复                                                 | `_prepare_continuation` 不清空 `ai_text`——续写文本 append 到已有文本之后，行为正确                                            |
| 第一次 call 的截断 token 已发送到客户端                                   | 无法回退——但 agent loop 只使用 middleware 返回的最终成功 AIMessage，tool 执行正确。客户端 UI 可在 tool 结果到达后替换截断输出 |
| callback stripping 异常时未恢复                                           | `try/finally` 保证恢复——即使 re-call 抛异常，`finally` 中恢复原始 callbacks                                                   |
| handler 返回多种类型（AIMessage / ModelResponse / ExtendedModelResponse） | middleware 的 `_extract_ai_message` 方法处理三种类型                                                                          |

---

## 10. 实施记录

```
Phase 1 (Detection) ✅
  └─ 改 stream_dispatch.py + messages.py + store/core.py + stream_driver.py
  └─ 写 Phase 1 测试 (8 tests)
  └─ 全量测试通过
       │
Phase 2 (Text Continuation) ✅
  └─ 重构 _GenerateTurn (持有 agent)
  └─ 重构 StreamTurn.run() (外层 while)
  └─ 写 Phase 2 测试 (8 tests)
  └─ 全量测试通过
       │
Phase 3 v1 (混合策略 middleware) ✅
  └─ 新建 max_tokens_boost.py (非流式 re-call + 流式 state_register_mem)
  └─ 改 core.py middleware 列表
  └─ 改 stream_dispatch.py (is_stream_turn flag + boost level cleanup)
  └─ 写 Phase 3 测试 (12 tests)
  └─ 写集成测试 (3 tests)
  └─ 全量回归 112/112 pass
       │
Phase 3 v2 (统一 re-call + callback stripping) ✅
  └─ 重写 max_tokens_boost.py (统一流式/非流式 re-call)
  └─ 新增 _strip_callbacks / _restore_callbacks
  └─ 移除 _BOOST_KEY / state_register_mem boost level 逻辑
  └─ 改 stream_dispatch.py (移除 _max_tokens_boost_level cleanup)
  └─ 重写 Phase 3 试 (18 tests, 含 5 个 callback stripping 测试)
  └─ 全量回归 119/119 pass
       │
Lint ✅
  └─ pyflakes clean (新文件无 unused import)
```

---

## 11. hermes-agent 参考要点

| 要素                            | hermes-agent 值                                                                                                                                                                      | 本方案采用                                                                                |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| 续写 prompt                     | `"[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]"` | 原文采用                                                                                  |
| 最大续写次数                    | 4                                                                                                                                                                                    | 4                                                                                         |
| max_tokens boost 公式           | `base * 2^retry`                                                                                                                                                                     | 相同                                                                                      |
| max_tokens cap                  | 32768                                                                                                                                                                                | 相同                                                                                      |
| 工具调用截断最大重试            | 4（hermes 用 truncated_tool_call_retries）                                                                                                                                           | 3（本方案更保守）                                                                         |
| 工具调用截断策略                | 不 append 截断响应，re-run same API call                                                                                                                                             | **相同**——middleware 内 re-call handler，截断结果被丢弃，agent loop 只看到最终成功 result |
| IterationBudget 消耗            | 每次 tool-call 截断重试 +0（同一 attempt 内 re-issue）                                                                                                                               | **相同 +0**（流式和非流式统一，callback stripping 避免重复输出）                          |
| 检测 key                        | `finish_reason` (OpenAI) / `stop_reason` (Anthropic)                                                                                                                                 | 相同                                                                                      |
| output_cap error 检测           | `is_output_cap_error(error_msg)`                                                                                                                                                     | 不实现（初版）                                                                            |
| thinking-budget exhaustion 检测 | 有                                                                                                                                                                                   | 不实现（初版）                                                                            |
