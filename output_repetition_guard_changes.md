# output_repetition_guard.py 变更记录

文件路径: `agent/middlewares/output_repetition_guard.py`

共 **6 处变更**，覆盖 4 项增强：

- **#1** 短句跨调用检测（content < 20 字符不再被跳过）
- **#2** 归一化 hash（空白/标点差异不再导致漏检）
- **#3** 双 hash 头尾检测（仅尾部 500 字符 hash 不再遗漏头部重复）
- **#4** 工具调用文本跨调用检测（tool_calls 不再完全跳过检测）

---

## 变更 1: 新增 `import unicodedata`

**位置**: 文件顶部 import 区（第 28 行）

### 改之前

```python
from __future__ import annotations
import hashlib
import re
from typing import Any, Callable, Awaitable
```

### 改之后

```python
from __future__ import annotations
import hashlib
import re
import unicodedata                          # <-- 新增
from typing import Any, Callable, Awaitable
```

### 原因

`_normalize_for_hash()` 方法需要 `unicodedata.normalize("NFKC", ...)` 进行全角→半角归一化（#2）。

---

## 变更 2: 新增 `_MIN_CROSSCALL_LENGTH` 常量

**位置**: 常量定义区（第 88-95 行）

### 改之前

```python
# Minimum content/reasoning length before **internal** repetition detection
# runs, preventing false positives on short responses.
_MIN_CONTENT_LENGTH = 20
# Default minimum consecutive identical non-whitespace characters (e.g. 8x
# ``啊``) required to flag a character run as "repetitive".
_CHAR_RUN_MIN = 8
```

### 改之后

```python
# Minimum content/reasoning length before **internal** repetition detection
# runs, preventing false positives on short responses.
_MIN_CONTENT_LENGTH = 20
# Minimum content length for **cross-call** repetition detection.  Much lower
# than ``_MIN_CONTENT_LENGTH`` because even a single short sentence repeated
# across consecutive model calls is a valid death-loop signal.  Only
# non-empty content (>= 1 char) is required.
_MIN_CROSSCALL_LENGTH = 1
# Default minimum consecutive identical non-whitespace characters (e.g. 8x
# ``啊``) required to flag a character run as "repetitive".
_CHAR_RUN_MIN = 8
```

### 原因（#1）

原来跨调用检测复用 `_MIN_CONTENT_LENGTH = 20`，导致短于 20 字符的重复输出（如 `"好的"`、`"hi"`）完全跳过检测。新增 `_MIN_CROSSCALL_LENGTH = 1` 使任意非空内容都会被纳入跨调用 hash 历史。

---

## 变更 3: 新增 `_normalize_for_hash()` 静态方法

**位置**: `OutputRepetitionGuard` 类内部，`_content_hash` 之前（第 173-192 行）

### 改之前

无此方法。`_content_hash()` 直接对原始内容做 MD5。

### 改之后

```python
@staticmethod
def _normalize_for_hash(content: str) -> str:
    """Normalize content for robust cross-call hash comparison.

    Applies three transformations so that near-identical outputs
    (e.g. ``"好的，我来处理。"``, ``"好的 我来处理"``, ``"好的，我来处理"``)
    all produce the same hash:

    1. **NFKC normalization** — full-width → half-width (``Ａ`` → ``A``)
    2. **Whitespace removal** — all spaces, tabs, newlines stripped
    3. **Punctuation removal** — all non-word characters removed

    The resulting string contains only letters (incl. CJK), digits and
    underscores, making the hash insensitive to formatting noise that
    shouldn't prevent repetition detection.
    """
    content = unicodedata.normalize("NFKC", content)
    content = re.sub(r"\s+", "", content)
    content = re.sub(r"[^\w]", "", content)
    return content
```

### 原因（#2）

原来 hash 对原始内容直接计算，`"好的，我来处理。"` 和 `"好的我来处理"` 会产生不同 hash，导致近义重复漏检。归一化后只保留 word 字符（字母、CJK、数字、下划线），消除空白和标点干扰。

---

## 变更 4: 重写 `_content_hash()` 方法

**位置**: `OutputRepetitionGuard` 类内部（第 194-224 行）

### 改之前

```python
@staticmethod
def _content_hash(content: str) -> str:
    """Hash content for cross-call comparison."""
    return hashlib.md5(content.encode()).hexdigest()
```

### 改之后

```python
@staticmethod
def _content_hash(content: str) -> str:
    """Hash content for cross-call comparison.

    Returns a dual ``"head_hash|tail_hash"`` string:

    * For short content (≤ ``_TAIL_CHARS`` after normalization): both
      parts are the MD5 of the full normalized content.
    * For long content: head = MD5 of the first ``_TAIL_CHARS`` chars of
      the normalized content, tail = MD5 of the last ``_TAIL_CHARS``.

    This dual-hash approach catches repetition at **either** end of the
    output:

    * Same output → both match.
    * Same prefix, different suffix → head matches.
    * Different prefix, same suffix → tail matches.

    The caller should split on ``"|"`` and compare either part.
    """
    normalized = OutputRepetitionGuard._normalize_for_hash(content)
    if not normalized:
        return "|"

    if len(normalized) <= _TAIL_CHARS:
        h = hashlib.md5(normalized.encode()).hexdigest()
        return f"{h}|{h}"

    head = normalized[:_TAIL_CHARS]
    tail = normalized[-_TAIL_CHARS:]
    return f"{hashlib.md5(head.encode()).hexdigest()}|{hashlib.md5(tail.encode()).hexdigest()}"
```

### 原因（#2 + #3）

两个问题同时解决：

1. **#2 归一化**: 调用 `_normalize_for_hash()` 后再计算 hash，使近义内容产生相同 hash。
2. **#3 双 hash**: 原来只对完整内容做单个 hash。对于超长输出（>500 字符），如果只有头部相同而尾部不同（或反之），单个 hash 无法匹配。改为返回 `"head_hash|tail_hash"` 双 hash 格式，head = 前 500 字符 hash，tail = 后 500 字符 hash，调用方可以分别比较。

---

## 变更 5: 修改 `_check_text_repetition()` 方法

**位置**: `OutputRepetitionGuard` 类内部（第 377-499 行）

### 改之前

```python
def _check_text_repetition(
    self,
    session_id: str,
    text: str,
    content_prefix: str,
    history_key: str,
    internal_warned_key: str,
    label: str,
) -> AIMessage | None:
    """Shared cross-call + internal repetition check for any text stream.
    ...
    """
    ch = self._content_hash(text)

    # Rolling history of content hashes; ``consecutive`` is the length of
    # the run of hashes matching the current one at the tail of the list.
    history: list[str] = state_register_mem.get_state(session_id, history_key, [])

    consecutive = 0
    for h in reversed(history):
        if ch == h:                      # <-- 简单相等比较
            consecutive += 1
        else:
            break

    # Append the current hash and cap the window.
    history.append(ch)
    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
    state_register_mem.set_state(session_id, history_key, history)

    # This occurrence itself counts toward the run.
    total_identical = consecutive + 1

    # ---- 1. Hard halt ----------------------------------------------
    ...

    # ---- 2. Soft warning --------------------------------------------
    ...

    # ---- 3. Internal repetition (at most once per session/label) ----
    if self._detect_internal_repetition(text):    # <-- 无条件运行内部检测
        ...
```

### 改之后

```python
def _check_text_repetition(
    self,
    session_id: str,
    text: str,
    content_prefix: str,
    history_key: str,
    internal_warned_key: str,
    label: str,
    check_internal: bool = True,              # <-- 新增参数
) -> AIMessage | None:
    """Shared cross-call + internal repetition check for any text stream.
    ...
    Parameters
    ----------
    check_internal : bool
        Whether to run the internal-repetition sub-detectors on ``text``.
        Set to ``False`` when the model is making tool calls (text-output
        guard only) to avoid false positives on tool-call accompanying
        text.  Cross-call detection always runs regardless.
    ...
    """
    ch = self._content_hash(text)
    # Dual hash: "head_hash|tail_hash".  Split into parts for comparison.
    ch_head, _, ch_tail = ch.partition("|")     # <-- 拆分双 hash

    # Rolling history of content hashes; ``consecutive`` is the length of
    # the run of hashes matching the current one at the tail of the list.
    # A previous entry matches if EITHER its head or tail hash equals the
    # current head or tail hash respectively -- this catches repetition
    # at either end of long outputs.
    history: list[str] = state_register_mem.get_state(session_id, history_key, [])

    consecutive = 0
    for h in reversed(history):
        h_head, _, h_tail = h.partition("|")
        if ch_head == h_head or ch_tail == h_tail:   # <-- 双 hash 比较
            consecutive += 1
        else:
            break

    # Append the current hash and cap the window.
    history.append(ch)
    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
    state_register_mem.set_state(session_id, history_key, history)

    # This occurrence itself counts toward the run.
    total_identical = consecutive + 1

    # ---- 1. Hard halt ----------------------------------------------
    ...

    # ---- 2. Soft warning --------------------------------------------
    ...

    # ---- 3. Internal repetition (at most once per session/label) ----
    if check_internal and self._detect_internal_repetition(text):    # <-- 增加 check_internal 门控
        ...
```

### 原因（#3 + #4）

两个变更点：

1. **#3 双 hash 比较**: 从 `ch == h`（单个 hash 相等）改为 `ch_head == h_head or ch_tail == h_tail`（head 或 tail 任一匹配即视为重复）。配合变更 4 的双 hash 格式，覆盖头部相同尾部不同、尾部相同头部不同两种场景。

2. **#4 `check_internal` 参数**: 新增 `check_internal: bool = True` 参数，允许调用方在 tool_calls 场景下设为 `False` 跳过内部检测，避免对工具调用附带文本的误报。跨调用检测始终运行。

---

## 变更 6: 修改 `_wrap_model_call_post()` 方法

**位置**: `OutputRepetitionGuard` 类内部（第 501-583 行）

### 改之前

```python
def _wrap_model_call_post(
    self,
    request: ModelRequest[ContextT],
    result: Any,
) -> AIMessage | None:
    """Post-hoc inspection of a single model call's result.
    ...
    """
    session_id = self._get_session_id(request.state)

    ai_msg = self._extract_ai_message(result)
    if ai_msg is None:
        return None

    # Skip if model is making tool calls -- that's ToolGuardrails' job
    tool_calls = getattr(ai_msg, "tool_calls", None)
    if tool_calls:
        return None                          # <-- tool_calls 时完全跳过

    content = str(ai_msg.content or "").strip()
    reasoning = self._extract_reasoning(ai_msg)

    # Fall back to inline <think>…</think> style reasoning
    if not reasoning:
        reasoning = self._extract_inline_reasoning(content)
        if reasoning:
            content = self._strip_inline_reasoning(content)

    # If already halted this turn, keep returning the halt message
    halted: bool = state_register_mem.get_state(session_id, _HALTED_KEY, False)
    if halted:
        return AIMessage(...)

    if content:
        r = self._check_text_repetition(
            session_id,
            content,
            content,
            _HISTORY_KEY,
            _INTERNAL_WARNED_KEY,
            "output",
            # <-- 无 check_internal 参数，内部检测无条件运行
        )
        if r is not None:
            return r

    if reasoning:
        r = self._check_text_repetition(
            session_id,
            reasoning,
            content,
            _REASONING_HISTORY_KEY,
            _REASONING_WARNED_KEY,
            "reasoning",
            # <-- 无 check_internal 参数
        )
        if r is not None:
            return r

    return None
```

### 改之后

```python
def _wrap_model_call_post(
    self,
    request: ModelRequest[ContextT],
    result: Any,
) -> AIMessage | None:
    """Post-hoc inspection of a single model call's result.
    ...
    """
    session_id = self._get_session_id(request.state)

    ai_msg = self._extract_ai_message(result)
    if ai_msg is None:
        return None

    # #4: Don't skip entirely when tool_calls are present.  Cross-call
    # repetition detection still runs (the model may loop on the same
    # text alongside tool calls).  Only internal-repetition detection
    # is skipped for tool-call messages to avoid false positives.
    has_tool_calls = bool(getattr(ai_msg, "tool_calls", None))   # <-- 不再 return None

    content = str(ai_msg.content or "").strip()
    reasoning = self._extract_reasoning(ai_msg)

    # Fall back to inline <think>…</think> style reasoning
    if not reasoning:
        reasoning = self._extract_inline_reasoning(content)
        if reasoning:
            content = self._strip_inline_reasoning(content)

    # If already halted this turn, keep returning the halt message
    halted: bool = state_register_mem.get_state(session_id, _HALTED_KEY, False)
    if halted:
        return AIMessage(...)

    # #1: Cross-call detection uses _MIN_CROSSCALL_LENGTH (1) so even
    # short repeated outputs are caught.  Internal detection uses the
    # higher _MIN_CONTENT_LENGTH threshold and is skipped when the
    # model is making tool calls.
    if len(content) >= _MIN_CROSSCALL_LENGTH:                    # <-- 用 _MIN_CROSSCALL_LENGTH 替代隐式检查
        r = self._check_text_repetition(
            session_id,
            content,
            content,
            _HISTORY_KEY,
            _INTERNAL_WARNED_KEY,
            "output",
            check_internal=(                                          # <-- 新增 check_internal 参数
                len(content) >= _MIN_CONTENT_LENGTH and not has_tool_calls
            ),
        )
        if r is not None:
            return r

    if len(reasoning) >= _MIN_CROSSCALL_LENGTH:                 # <-- reasoning 也用 _MIN_CROSSCALL_LENGTH
        r = self._check_text_repetition(
            session_id,
            reasoning,
            content,
            _REASONING_HISTORY_KEY,
            _REASONING_WARNED_KEY,
            "reasoning",
            check_internal=(                                          # <-- 新增 check_internal 参数
                len(reasoning) >= _MIN_CONTENT_LENGTH and not has_tool_calls
            ),
        )
        if r is not None:
            return r

    return None
```

### 原因（#1 + #4）

三个变更点：

1. **#4 tool_calls 不再完全跳过**: 删除 `if tool_calls: return None`，改为记录 `has_tool_calls` 标志。跨调用检测始终运行（模型可能在工具调用旁边重复相同文本），仅内部检测在 `has_tool_calls=True` 时跳过（通过 `check_internal=False`）。

2. **#1 短句跨调用检测**: 长度判断从隐式的 `if content:`（非空即检查）改为 `if len(content) >= _MIN_CROSSCALL_LENGTH:`（≥1 字符）。配合变更 2 的新常量，短于 20 字符的内容也会被纳入跨调用 hash 历史。内部检测仍保留 `_MIN_CONTENT_LENGTH = 20` 的门槛。

3. **`check_internal` 参数传递**: 调用 `_check_text_repetition()` 时传入 `check_internal=(len(content) >= _MIN_CONTENT_LENGTH and not has_tool_calls)`，精确控制内部检测的运行条件。

---

## 测试覆盖

| 增强编号        | 中间件测试类                   | 测试数  |
| --------------- | ------------------------------ | ------- |
| #1 短句跨调用   | `TestShortCrossCallRepetition` | 3       |
| #2 归一化 hash  | `TestNormalizedHashCrossCall`  | 3       |
| #3 双 hash 头尾 | `TestDualHashCrossCall`        | 3       |
| #4 工具调用文本 | `TestToolCallCrossCall`        | 3       |
| 原有功能回归    | 其余测试类                     | 90      |
| **合计**        |                                | **102** |

全部 99 项中间件测试 + 49 项 wrapper 测试 + 3 项 completion_drain 测试 = **151/151 PASSED**

---

## 变更 7: 新增内部消息跳过逻辑

**位置**: `_wrap_model_call_post()` 方法（第 520-527 行）

### 改之前

`_wrap_model_call_post()` 无内部消息跳过逻辑，所有 `AIMessage` 都会进入检测流程。

### 改之后

```python
# Skip internal completion-drain messages (subagent completion
# notifications injected by SubagentCompletionDrainMiddleware).
_meta = getattr(ai_msg, "metadata", None)
if isinstance(_meta, dict):
    if _meta.get("internal") is True:
        return None
    if _meta.get("provenance") == "subagent_completion":
        return None
```

### 原因

slim wrapper 删除了 `_post_hoc_check()` 后，内部消息（subagent completion drain 注入的 `AIMessage`）的跳过逻辑需要移至 middleware。否则这些内部消息会触发误报——它们的内容是固定的通知文本，在多次 subagent 完成时会看起来像"重复输出"。
