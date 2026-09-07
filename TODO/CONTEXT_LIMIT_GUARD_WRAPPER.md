# 流式上下文上限防御：ContextLimitGuardWrapper

> 日期: 2026-09-07
> 状态: 已实施
> 前置: STREAM_REPETITION_OPTIMIZATION.md (已完成), Summarization 中间件 T1-T5 (已完成)
> 关联: TOKEN_LIMIT_CONTINUATION_PLAN.md (Draft — 输出侧 max_tokens 续写, 与本文互补)

---

## 1. 背景

### 1.1 问题

Summarization 中间件的五个触发点（T1–T5）覆盖了模型调用的前/后/错误恢复，但**没有一个能在流式输出过程中介入**——即 agent loop 中两次 model call 之间的 stream 空隙。

```
astream(stream_mode=["messages", "updates"]) 单次 agent loop:

  before_agent(T1) → wrap_model_call(T2) → [handler 调模型, 流式chunk吐出] → T3 post-response → tool执行 → wrap_model_call(T2) → ...
                                            ↑                                          ↑
                                     流式 chunk 在这里吐出                      下一次调用的 input 在这里
                                            │                                          │
                            ╔═══════════════╧══════════════╗              ╔═════════╧══════════╗
                            ║  中间件完全看不到这些 chunk   ║              ║  T2 用 len//4 粗估  ║
                            ║  模型的 output 正在膨胀        ║              ║  + anti-thrash gate  ║
                            ╚═══════════════════════════════╝              ╚════════════════════╝
```

### 1.2 中间件三重盲区

| 盲区                                 | 根因                                                                                                                | 后果                       |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| **T3 检测到溢出但无法压缩**          | `_post_response_check` 修改 `request.messages` 后直接 `return response`（原始响应），修改被丢弃。只做了日志记录。   | 真实 token 溢出信号被浪费  |
| **T2 应该压缩但被 anti-thrash 挡住** | `cooldown_active`（`COMPACTION_COOLDOWN_ROUNDS=3`）和 `attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN`（3）会跳过压缩。 | 下一次调用带着超限 context |
| **T2 估算太粗**                      | `estimate_msg_tokens` = `len(content) // 4`（`CHARS_PER_TOKEN=4`），实际 token 数可能差 2-3 倍。                    | T2 认为"没超"→ 实际已超    |

**最终结果**：T3 说"要溢出了"→ T2 说"cooldown 中，跳过"→ 下一次调用带着超限 context → provider 报错 → 昂贵的 T4/T5 恢复（用户可见的失败 + 重试）。

### 1.3 为什么 wrapper 能做到

`RepetitionGuardWrapper` 的模式证明了：在 graph 外部包一层 `astream` 拦截器，可以逐 chunk 监控流式输出。同样模式可用于上下文上限检测——在 model-call 边界（`"updates"` chunk）从 `usage_metadata` 提取真实 `input_tokens`，直接写 `state_register_mem` 绕过 T2 的 anti-thrash gate。

---

## 2. 方案概览

| 防御层                             | 机制                                                                                      | 中间件为什么做不到                                                                                                                                 |
| ---------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **model-call 边界 force-compress** | 从 `usage_metadata.input_tokens` 拿真实 token，超 80% 窗口就设 `_FORCE_RECOVERY_KEY=True` | T3 也检测到但它的 `request` 修改被丢弃（直接 `return response`）；T2 能压缩但被 anti-thrash gate 挡住。wrapper 在 stream 层写 state，绕过所有 gate |
| **mid-stream output 截断**         | 累积输出文本估算 token，超窗口 20% 就停止 forward text chunk                              | 中间件完全看不到 mid-stream chunk，只在调用前/后介入                                                                                               |

### 决策摘要

| 问题                    | 决策                                                          | 理由                                                                                                   |
| ----------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| 包裹顺序？              | **RepetitionGuard(内) → ContextLimitGuard(外)**               | 外层看到 RepetitionGuard 过滤后的 chunk；`usage_metadata` 在 final chunk 上且 content 通常为空，能透传 |
| 复用哪个 state key？    | **`_FORCE_RECOVERY_KEY`**（`"summarization_force_recovery"`） | 已有机制：`_should_skip_compression` 读此 flag 返回 False + `not forced` 短路 anti-thrash gate         |
| output 截断比例？       | **20%**（`_DEFAULT_OUTPUT_CUT_RATIO = 0.20`）                 | 保守值：65536 窗口 → 13107 token 预算。截断只影响客户端视图，graph 仍累积完整消息                      |
| 预测性 force-compress？ | **`input + output >= threshold` 也 arm**                      | 即使当前 input 没超限，output 会成为下轮 input 的一部分，超限就提前 arm                                |
| 子代理是否包裹？        | **否**                                                        | 子代理用 `ainvoke` 不走 `astream`，wrapper 的 `ainvoke` 直接 delegate 无作用                           |

---

## 3. 改动文件

| 文件                                   | 改动类型                                                   |
| -------------------------------------- | ---------------------------------------------------------- |
| `agent/context_limit_guard_wrapper.py` | **新建** — `ContextLimitGuardWrapper` 类（489 行）         |
| `agent/__init__.py`                    | 新增 `ContextLimitGuardWrapper` 到 `__all__` + lazy import |
| `agent/core.py`                        | 新增 import + 在 `built_agent()` 中包裹 `_agent`           |

---

## 4. 设计详解

### 4.1 包裹结构

```
CompiledStateGraph (graph + 10 middleware)
  │
  │  astream(stream_mode=["messages", "updates"])
  ▼
RepetitionGuardWrapper           ← 内层：流式重复检测
  │  过滤重复文本 chunk
  │  透传 usage_metadata（final chunk content 为空，不被 call_cut 截断）
  ▼
ContextLimitGuardWrapper         ← 外层：流式上下文上限检测
  │  1. 捕获 usage_metadata.input_tokens
  │  2. model-call 边界设 _FORCE_RECOVERY_KEY
  │  3. mid-stream output 预算截断
  ▼
Client (stream_dispatch.py)
```

### 4.2 核心数据流

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
  ├─ tool executes → context 继续膨胀（tool result 加入）
  │
  └─ Model call N+1 starts → T2 (wrap_model_call)
      ├─ forced = get(_FORCE_RECOVERY_KEY) = True
      ├─ _should_skip_compression: clears flag, returns False
      ├─ `not forced` = False → 跳过 cooldown/attempt-cap gate
      ├─ _decide_overflow_route → ROUTE_COMPACT_ONLY
      ├─ _execute_compact → 压缩历史消息
      └─ handler 调用 with 压缩后的 context → 不溢出 ✓
```

### 4.3 防御一：model-call 边界 force-compress

**文件**: `agent/context_limit_guard_wrapper.py` — `_check_and_force_compress()`

在 `astream` 的 `"updates"` 模式和非 model 节点边界调用。从 `usage_metadata` 提取真实 token 数后做两层判断：

| 判断         | 条件                                        | 动作                          |
| ------------ | ------------------------------------------- | ----------------------------- |
| **当前溢出** | `input_tokens >= threshold`                 | 设 `_FORCE_RECOVERY_KEY=True` |
| **预测溢出** | `input_tokens + output_tokens >= threshold` | 设 `_FORCE_RECOVERY_KEY=True` |

其中 `threshold = context_window * COMPRESSION_TRIGGER_RATIO (0.80)`。

**T2 侧的消费路径**（`summarization.py:1908-1988`）：

```python
# wrap_model_call 入口
forced = state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False)  # ← wrapper 设的
cooldown_active = self._tick_cooldown(session_id)

if self._should_skip_compression(session_id):  # ← flag=True 时 clears + returns False
    ...skip path...
    return response

# anti-thrash gate
attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
if not forced and (cooldown_active or attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
    # ← forced=True 时 `not forced` = False → 短路 → 不跳过
    ...skip path...
    return response

# T2 dispatch — 压缩执行
route = self._decide_overflow_route(messages, session_id)
if route is not None and route != ROUTE_FITS:
    request = self._dispatch_overflow_route(request, route, session_id, trigger="T2")
```

### 4.4 防御二：mid-stream output 预算截断

**文件**: `agent/context_limit_guard_wrapper.py` — `astream()` 内 model-node 分支

在 model-node 的 `"messages"` chunk 累积文本，每 `check_interval`（默认 20）个 chunk 估算一次 token 数：

```python
call_output_text += content
token_counter += 1
if token_counter >= self._check_interval:
    token_counter = 0
    call_output_tokens = len(call_output_text) // CHARS_PER_TOKEN
    if call_output_tokens > self._output_token_budget:
        output_cut = True  # 后续 text chunk 不再 forward
```

| 参数                  | 默认值                  | 含义                                   |
| --------------------- | ----------------------- | -------------------------------------- |
| `output_cut_ratio`    | `0.20`                  | 占窗口的比例。65536 → 13107 token 预算 |
| `check_interval`      | `20`                    | 每 20 个 content chunk 检查一次        |
| `output_token_budget` | `context_window * 0.20` | 超过此值停止 forward text              |

**不漏检保证** — 三处 flush（与 `RepetitionGuardWrapper` 一致）：

| 触发点         | 位置                     | 说明                   |
| -------------- | ------------------------ | ---------------------- |
| `updates` 边界 | `mode == "updates"` 分支 | 模型调用结束时重置     |
| 非 model 节点  | `node != "model"` 分支   | 摘要等节点出现时重置   |
| 流结束         | `async for` 循环之后     | 最后一次模型调用的残留 |

**截断行为**：

- `output_cut=True` 后，后续有 `content` 且无 `tool_calls` 的 chunk 不 yield（客户端看不到）
- graph 仍累积完整 AIMessage（截断只影响客户端视图）
- tool-call chunk 不受影响（功能不受损）
- T2 在下次调用前会压缩掉超长输出

### 4.5 `usage_metadata` 提取

```python
usage = getattr(msg_chunk, "usage_metadata", None)
if isinstance(usage, dict):
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if inp is not None:
        last_input_tokens = int(inp)
    if out is not None:
        last_output_tokens = int(out)
```

- `usage_metadata` 由 LangChain 在流的 **final chunk** 上携带
- 所有 lookup 有 try/except 保护，missing field 不会 break stream
- 如果 RepetitionGuard 的 `call_cut` 截断了含 content 的 final chunk，wrapper 可能拿不到 `usage_metadata`。但 final chunk 通常 content 为空（只有 metadata），所以一般能透传

### 4.6 非流式路径（ainvoke）

```python
async def ainvoke(self, *args, **kwargs) -> Any:
    return await self._inner.ainvoke(*args, **kwargs)
```

`ainvoke` 是单次模型调用，不是 agent loop——context 不会在调用间膨胀。T1/T2/T3 中间件已覆盖。wrapper 直接 delegate。

---

## 5. 集成

### 5.1 `agent/core.py`

```python
from .stream_repetition_guard_wrapper import RepetitionGuardWrapper
from .context_limit_guard_wrapper import ContextLimitGuardWrapper

# ... create_agent(...) ...

_agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
_agent = ContextLimitGuardWrapper(_agent, context_window=main_llm_max_tokens)
```

### 5.2 `agent/__init__.py`

```python
__all__ = [
    ...,
    "RepetitionGuardWrapper",
    "ContextLimitGuardWrapper",
]


def __getattr__(name: str):
    ...
    if name == "ContextLimitGuardWrapper":
        from .context_limit_guard_wrapper import ContextLimitGuardWrapper

        return ContextLimitGuardWrapper
    ...
```

---

## 6. 与现有机制的关系

### 6.1 与 Summarization T1-T5 的协作

```
           ┌─────────────────────────────────────────────────────────┐
           │                 agent loop (astream)                     │
           │                                                         │
  T1 ──────┤ before_agent: 估算 token, 预压缩                        │
           │     ↓                                                   │
  T2 ──────┤ wrap_model_call: 估算 token, 检查 anti-thrash gate       │
           │     ↓                                                   │
           │     handler() → 模型流式输出                             │
           │     ↓ ↓ ↓ chunk stream ↓ ↓ ↓                            │
           │     ┌───────────────────────────────────┐               │
           │     │ RepetitionGuardWrapper            │               │
           │     │   重复检测 + 截断                  │               │
           │     │   透传 usage_metadata             │               │
           │     └───────────┬───────────────────────┘               │
           │                 │                                       │
  NEW ────┤     ┌───────────▼───────────────────────┐               │
           │     │ ContextLimitGuardWrapper           │               │
           │     │   捕获 usage_metadata              │               │
           │     │   output 预算截断                   │               │
           │     │   model-call 边界设 force flag     │               │
           │     └───────────┬───────────────────────┘               │
           │                 │                                       │
  T3 ──────┤ post-response: 真实 token 检测（日志, 修改被丢弃）        │
           │     ↓                                                   │
           │     tool 执行                                           │
           │     ↓                                                   │
  T2 ──────┤ wrap_model_call: 读取 force flag → 绕过 gate → 压缩     │
           │     ↓                                                   │
           │     ... (loop)                                          │
  T4/T5 ──┤ error recovery: provider 报错时强制压缩 + 重试           │
           └─────────────────────────────────────────────────────────┘
```

**分工**：

| 层               | 职责                                      | 数据来源                                      |
| ---------------- | ----------------------------------------- | --------------------------------------------- |
| T1               | 回合开始前预检                            | `estimate_msg_tokens`（粗估）                 |
| T2               | 每次调用前检查 + 压缩                     | `estimate_msg_tokens` + `_FORCE_RECOVERY_KEY` |
| **NEW: wrapper** | **stream 层真实 token 检测 + force flag** | **`usage_metadata.input_tokens`（真实）**     |
| T3               | 响应后真实 token 检测（仅日志）           | `extract_reported_input_tokens`               |
| T4/T5            | provider 报错恢复                         | `classify_provider_error`                     |

### 6.2 与 TOKEN_LIMIT_CONTINUATION_PLAN.md 的关系

| 维度     | 本方案 (ContextLimitGuard)    | Token Limit Plan                  |
| -------- | ----------------------------- | --------------------------------- |
| 防御对象 | **INPUT 侧** — 上下文窗口溢出 | **OUTPUT 侧** — `max_tokens` 截断 |
| 检测信号 | `usage_metadata.input_tokens` | `finish_reason == "length"`       |
| 动作     | 设 force flag → T2 压缩       | 注入续写 HumanMessage → re-stream |
| 层       | wrapper（stream 层）          | StreamTurn（服务层）              |

两者互补：本方案防 input 溢出，Token Limit Plan 防 output 截断。

---

## 7. 已知局限

| 局限                                         | 影响                                                                     | 缓解                                                              |
| -------------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------- |
| `usage_metadata` 可能被 RepetitionGuard 截断 | 如果 final chunk 有 content 且 `call_cut=True`，wrapper 拿不到真实 token | final chunk 通常 content 为空；fallback 到 T2 的粗估 + T4/T5 兜底 |
| output 截断只影响客户端视图                  | graph 仍累积完整 AIMessage，T2 需在下次调用前压缩                        | 这是设计意图——截断的是"会被压缩掉的冗余输出"，不是功能性截断      |
| 子代理不包裹                                 | 子代理用 `ainvoke` 不走 `astream`                                        | `ainvoke` 是单次调用，T1/T2/T3 已覆盖                             |
| `estimate_msg_tokens` 精度                   | output 预算用 `len//4` 估算                                              | 保守值（20%），实际 token 数通常高于估算，不会误截                |
| `usage_metadata` 不存在的 provider           | 无法做真实 token 检测                                                    | fallback 到 T2 的粗估 + T4/T5 兜底                                |

---

## 8. 配置参数

| 参数               | 位置     | 默认值                | 说明                                 |
| ------------------ | -------- | --------------------- | ------------------------------------ |
| `context_window`   | 构造函数 | `main_llm_max_tokens` | 模型上下文窗口大小                   |
| `output_cut_ratio` | 构造函数 | `0.20`                | output 预算占窗口比例；设 0 禁用截断 |
| `check_interval`   | 构造函数 | `20`                  | output 预算检查间隔（chunk 数）      |

环境变量（间接）：

| 变量                        | 来源                                    | 用途                             |
| --------------------------- | --------------------------------------- | -------------------------------- |
| `MAIN_LLM_MAX_TOKEN`        | `models/LLMs/main_llm.py` → `core.py:8` | 传入 wrapper 的 `context_window` |
| `COMPRESSION_TRIGGER_RATIO` | `config/num.py` = `0.80`                | force-compress 触发阈值比例      |
| `CHARS_PER_TOKEN`           | `config/num.py` = `4`                   | output token 估算除数            |
