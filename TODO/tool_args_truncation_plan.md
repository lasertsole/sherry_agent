# 执行方案：Tool Call Args 中间省略截断

## 问题

`AIMessage.tool_calls[].args` 在整个压缩 pipeline 中从未被截断。

`estimate_msg_tokens` (pub_func/message/estimate_msg_tokens.py:18-19) 将 args 计入 token：

```python
total += len(str(tc.get("args", "")))
```

但所有截断模块只遍历 `ToolMessage`，不处理 `AIMessage.tool_calls`：

| 模块                                           | 处理对象            | 截断方式                         |
| ---------------------------------------------- | ------------------- | -------------------------------- |
| `target_truncation.py`                         | ToolMessage content | head+tail                        |
| `tool_result_ttl.py`                           | ToolMessage content | head+tail (in-place)             |
| `tool_output_prune.py`                         | ToolMessage content | 清空为 marker                    |
| `tool_output_dedup.py`                         | ToolMessage content | 清空为 placeholder               |
| `_aggressive_truncate` (summarization.py:1508) | ToolMessage content | head-only                        |
| `_preemptive_truncate` (summarization.py:1138) | ToolMessage content | head+tail (**死代码，无调用点**) |

超长工具参数（如 `write_file` 的大段 content、`bash` 的长脚本）原样保留在消息列表中，持续占用 token 压力。

## 实际压缩路径（3条）

| 路径      | 入口                              | 调用链                                           |
| --------- | --------------------------------- | ------------------------------------------------ |
| 非LLM策略 | `_run_non_llm_strategies` (L1472) | dedup → prune → target_truncate                  |
| 预算截断  | `_run_budget_truncation` (L649)   | `truncate_to_budget` (tool_result_ttl.py)        |
| 激进截断  | `_aggressive_truncate` (L1508)    | head-only，上限 `AGGRESSIVE_TRUNCATE_CHARS=1000` |

## 设计决策

- **args 替换格式**：`{"_truncated_args": "head...[args truncated, omitted N chars]...tail"}`
  - 保持 dict 类型（LangChain `ToolCall.args` 类型为 dict）
  - provider adapter 可正常 JSON 序列化
  - 模型可明确识别参数被截断
- **返回模式**：`(new_list, freed_tokens)`，与 `target_truncation.py` / `dedup_tool_outputs` 一致
  - 用 `model_copy(update={"tool_calls": new_tcs})` 创建新 AIMessage，不修改原始消息
- **复用现有常量**：`CONTENT_HEAD_RATIO(0.3)` / `CONTENT_TAIL_RATIO(0.3)` / `TRUNCATABLE_RECENT_SKIP(6)` / `PROTECTED_TOOLS`

## 文件变更清单

| #   | 文件                                        | 变更类型 | 说明                                                |
| --- | ------------------------------------------- | -------- | --------------------------------------------------- |
| 1   | `config/num.py`                             | 新增常量 | `MIN_ARGS_CHARS_TO_TRUNCATE`, `MAX_TOOL_ARGS_CHARS` |
| 2   | `pub_func/message/tool_args_truncate.py`    | **新建** | `truncate_tool_args()` 函数                         |
| 3   | `agent/middlewares/summarization.py`        | 修改     | import + 3处接入 + 6处调用点更新                    |
| 4   | `tests/unit/test_pub_func_message_tools.py` | 修改     | 新增 `TestToolArgsTruncation` 测试类                |
| 5   | `agent/middlewares/summarization.py` (可选) | 修改     | `_serialize_for_summary` args head-only → head+tail |

---

## Step 1: `config/num.py` — 新增常量

位置：在 `MAX_TOOL_OUTPUT_CHARS = 2_000` 之后添加：

```python
MIN_ARGS_CHARS_TO_TRUNCATE = 500
MAX_TOOL_ARGS_CHARS = 2_000
```

与 tool output 截断常量初始值一致，但独立配置以便后续调参。

---

## Step 2: `pub_func/message/tool_args_truncate.py` — 新建模块

### 函数签名

```python
def truncate_tool_args(
    messages: list[BaseMessage],
    max_args_chars: int = MAX_TOOL_ARGS_CHARS,
    min_args_chars: int = MIN_ARGS_CHARS_TO_TRUNCATE,
    protected_tools: set[str] | None = None,
    skip_recent: int = TRUNCATABLE_RECENT_SKIP,
    head_ratio: float = CONTENT_HEAD_RATIO,
    tail_ratio: float = CONTENT_TAIL_RATIO,
) -> tuple[list[BaseMessage], int]:
```

### 核心逻辑

1. `keep_until = len(messages) - skip_recent` — 跳过最近消息（与 `find_truncatable_tool_results` 一致）
2. 遍历 `keep_until` 之前的每个 `AIMessage`（有 `tool_calls` 的）
3. 对每个 tool_call：
   - `name in protected_tools` → 跳过
   - `json.dumps(args, ensure_ascii=False)` 序列化，检查长度
   - 超过 `min_args_chars` → head+tail 截断：
     - `head = args_str[:int(max_args_chars * head_ratio)]`
     - `tail = args_str[-int(max_args_chars * tail_ratio):]`
     - `omitted = len(args_str) - len(head) - len(tail)`
     - 截断值 = `f"{head}...[args truncated, omitted {omitted} chars]...{tail}"`
   - 替换 args 为 `{"_truncated_args": 截断值}`
4. 用 `msg.model_copy(update={"tool_calls": new_tcs})` 创建新 AIMessage
5. 返回 `(new_messages, freed_tokens)`
   - `freed_tokens = (原args_str长度 - 新args_str长度) // 4`

### 代码风格参照

- `target_truncation.py`：返回 `(new_list, freed_tokens)` 模式
- `dedup_tool_outputs.py`：protected_tools 参数模式
- `overflow_router.py` 的 `find_truncatable_tool_results`：skip_recent 逻辑

### 完整伪代码

```python
import json
from langchain_core.messages import BaseMessage, AIMessage
from config.num import (
    MAX_TOOL_ARGS_CHARS,
    MIN_ARGS_CHARS_TO_TRUNCATE,
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    TRUNCATABLE_RECENT_SKIP,
)

_ARGS_OMISSION_TEMPLATE = "...[args truncated, omitted {omitted} chars]..."


def _truncate_args_str(
    args_str: str,
    max_chars: int,
    head_ratio: float,
    tail_ratio: float,
) -> str:
    head = args_str[: int(max_chars * head_ratio)]
    tail = args_str[-int(max_chars * tail_ratio):]
    omitted = len(args_str) - len(head) - len(tail)
    return f"{head}{_ARGS_OMISSION_TEMPLATE.format(omitted=omitted)}{tail}"


def truncate_tool_args(
    messages: list[BaseMessage],
    max_args_chars: int = MAX_TOOL_ARGS_CHARS,
    min_args_chars: int = MIN_ARGS_CHARS_TO_TRUNCATE,
    protected_tools: set[str] | None = None,
    skip_recent: int = TRUNCATABLE_RECENT_SKIP,
    head_ratio: float = CONTENT_HEAD_RATIO,
    tail_ratio: float = CONTENT_TAIL_RATIO,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()
    keep_until = max(len(messages) - skip_recent, 0)

    result = list(messages)
    freed_total = 0

    for i in range(keep_until):
        msg = result[i]
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue

        modified = False
        new_tcs = []
        for tc in tool_calls:
            name = tc.get("name", "")
            if name in protected:
                new_tcs.append(tc)
                continue

            args = tc.get("args", {})
            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_str = str(args)

            if len(args_str) <= min_args_chars:
                new_tcs.append(tc)
                continue

            truncated = _truncate_args_str(
                args_str, max_args_chars, head_ratio, tail_ratio
            )
            new_args = {"_truncated_args": truncated}
            new_tc = {**tc, "args": new_args}
            new_tcs.append(new_tc)

            new_args_str = json.dumps(new_args, ensure_ascii=False)
            freed_total += max(
                (len(args_str) - len(new_args_str)) // 4, 0
            )
            modified = True

        if modified:
            result[i] = msg.model_copy(update={"tool_calls": new_tcs})

    return result, freed_total
```

---

## Step 3: `agent/middlewares/summarization.py` — 接入三条路径

### 3a. Import（文件顶部，约 L28-32 区域）

```python
from pub_func.message.tool_args_truncate import truncate_tool_args
```

在 `config.num` 的 import 块中追加：

```python
MIN_ARGS_CHARS_TO_TRUNCATE,
MAX_TOOL_ARGS_CHARS,
```

### 3b. `_run_non_llm_strategies` (L1472-1506)

在 `target_truncate_tool_outputs` 调用之后（L1501 之后）添加第4步：

```python
current, reduced = truncate_tool_args(
    current,
    max_args_chars=MAX_TOOL_ARGS_CHARS,
    min_args_chars=MIN_ARGS_CHARS_TO_TRUNCATE,
    protected_tools=set(PROTECTED_TOOLS),
)
total_reduced += reduced
if reduced > 0:
    logger.debug("Tool args truncation reduced ~{} tokens, session={}", reduced, session_id)
```

### 3c. `_run_budget_truncation` (L649-661)

**修改返回值**从 `int` 改为 `tuple[list[BaseMessage], int]`：

```python
def _run_budget_truncation(
    self, messages: list[BaseMessage], usable: int
) -> tuple[list[BaseMessage], int]:
    # Step 1: truncate tool call args (returns new list via model_copy)
    messages, args_freed = truncate_tool_args(
        list(messages),
        max_args_chars=MAX_TOOL_ARGS_CHARS,
        min_args_chars=MIN_ARGS_CHARS_TO_TRUNCATE,
        protected_tools=set(PROTECTED_TOOLS),
    )
    # Step 2: truncate tool results (mutates ToolMessages in place)
    candidates = find_truncatable_tool_results(list(messages))
    result_freed = truncate_to_budget(
        list(messages), candidates, int(usable * TRUNCATE_BUDGET_RATIO)
    )
    return messages, args_freed + result_freed
```

**6处调用点更新**：

#### 调用点 1: `_dispatch_overflow_route` (L755-762)

Before:

```python
self._run_budget_truncation(
    cast("list[BaseMessage]", list(messages)), usable
)
request = request.override(
    messages=cast("list[AnyMessage]", list(messages))
)
```

After:

```python
final_msgs, _ = self._run_budget_truncation(
    cast("list[BaseMessage]", list(messages)), usable
)
request = request.override(
    messages=cast("list[AnyMessage]", final_msgs)
)
```

#### 调用点 2: `_adispatch_overflow_route` (L790-797)

同调用点 1 的模式，sync → async 对应。

#### 调用点 3: `_execute_compact` (L698-705)

Before:

```python
final_messages = list(request.messages)
self._run_budget_truncation(
    cast("list[BaseMessage]", final_messages), usable
)
request = request.override(
    messages=cast("list[AnyMessage]", final_messages)
)
```

After:

```python
final_messages = list(request.messages)
final_messages, _ = self._run_budget_truncation(
    cast("list[BaseMessage]", final_messages), usable
)
request = request.override(
    messages=cast("list[AnyMessage]", final_messages)
)
```

#### 调用点 4: `_aexecute_compact` (L727-734)

同调用点 3 的模式，sync → async 对应。

#### 调用点 5: `_forced_recovery_request` (L996-998)

Before:

```python
final_messages = list(request.messages)
self._run_budget_truncation(cast("list[BaseMessage]", final_messages), usable)
request = request.override(messages=cast("list[AnyMessage]", final_messages))
```

After:

```python
final_messages = list(request.messages)
final_messages, _ = self._run_budget_truncation(cast("list[BaseMessage]", final_messages), usable)
request = request.override(messages=cast("list[AnyMessage]", final_messages))
```

#### 调用点 6: `_aforced_recovery_request` (L1023-1025)

同调用点 5 的模式，sync → async 对应。

### 3d. `_aggressive_truncate` (L1508-1519)

在现有 `ToolMessage` 分支后增加 `AIMessage` 分支：

```python
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
        elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            new_tcs = []
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                if name in PROTECTED_TOOLS:
                    new_tcs.append(tc)
                    continue
                args = tc.get("args", {})
                try:
                    args_str = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_str = str(args)
                if len(args_str) > AGGRESSIVE_TRUNCATE_CHARS:
                    truncated = args_str[:AGGRESSIVE_TRUNCATE_CHARS] + (
                        f"...[args aggressively truncated, "
                        f"{len(args_str) - AGGRESSIVE_TRUNCATE_CHARS} chars omitted]"
                    )
                    new_tcs.append({**tc, "args": {"_truncated_args": truncated}})
                else:
                    new_tcs.append(tc)
            msg = msg.model_copy(update={"tool_calls": new_tcs})
        result.append(msg)
    return result
```

> 注意：激进路径不检查 `skip_recent`（与现有 ToolMessage 逻辑一致），不检查 `min_args_chars`（只要超过 `AGGRESSIVE_TRUNCATE_CHARS` 就截断），使用 head-only（不保留 tail）。

---

## Step 4: `tests/unit/test_pub_func_message_tools.py` — 新增测试

参照 `TestTargetTruncation` 模式，新增 `TestToolArgsTruncation` 测试类：

```python
from pub_func.message.tool_args_truncate import truncate_tool_args


class TestToolArgsTruncation:

    def test_large_args_head_tail_format(self):
        # args JSON > 500 chars → head 600 + tail 600 + omission marker
        ...

    def test_small_args_skipped(self):
        # args JSON <= 500 chars → unchanged
        ...

    def test_protected_tools_skipped(self):
        # tool name in protected_tools → args unchanged
        ...

    def test_skip_recent_messages(self):
        # AIMessage in the last 6 messages → args unchanged
        ...

    def test_empty_messages_list(self):
        assert truncate_tool_args([]) == ([], 0)

    def test_multiple_tool_calls_in_one_message(self):
        # one large + one small in same AIMessage → only large truncated
        ...

    def test_no_tool_calls_aimessage(self):
        # AIMessage with content but no tool_calls → unchanged
        ...
```

---

## Step 5（可选）: `_serialize_for_summary` (L267)

当前 head-only：

```python
args = str(tc.get("args", ""))[:500]
```

改为 head+tail 中间省略：

```python
args_str = str(tc.get("args", ""))
if len(args_str) > 500:
    head = args_str[:300]
    tail = args_str[-150:]
    omitted = len(args_str) - len(head) - len(tail)
    args = f"{head}...[args truncated, omitted {omitted} chars]...{tail}"
else:
    args = args_str
```

使摘要 LLM 也保留参数尾部上下文。

---

## 执行顺序

1. `config/num.py` — 新增常量
2. `pub_func/message/tool_args_truncate.py` — 新建模块
3. `tests/unit/test_pub_func_message_tools.py` — 新增测试（先写测试验证模块）
4. `agent/middlewares/summarization.py` — import + 3处接入 + 6处调用点
5. `agent/middlewares/summarization.py` — `_serialize_for_summary` 改进（可选）
6. 运行测试验证

## 风险评估

- **provider 兼容性**：args 替换为 `{"_truncated_args": "..."}` 仍为 dict，provider adapter JSON 序列化无风险
- **pairing 安全**：截断 args 不改变 tool_call_id，不删除消息，不影响 AIMessage↔ToolMessage 配对
- **token 估算一致性**：`estimate_msg_tokens` 用 `str(tc.get("args", ""))` 计算，截断后 `str({"_truncated_args": "..."})` 长度大幅减少，估算自动反映截断效果
- **`_run_budget_truncation` 签名变更**：从 `int` → `tuple[list, int]`，6处调用点需全部更新，遗漏会导致 `request.override` 使用未截断 args 的旧列表
