# Summarization 重构变更记录 — Part 1：配置与消息处理工具

> 本文档记录 config 和 pub_func/message 模块的变更（第 1-8 节）。
> 中间件重写与集成部分见 [Part 2](SUMMARIZATION_CHANGES_PART2.md)。

---

## 目录

1. [config/num.py](#1-confignumpy)
2. [pub_func/message/estimate_msg_tokens.py](#2-pub_funcmessageestimate_msg_tokenspy)
3. [pub_func/message/turn_utils.py（新建）](#3-pub_funcmessageturn_utilspy新建)
4. [pub_func/message/tool_output_dedup.py（新建）](#4-pub_funcmessagetool_output_deduppy新建)
5. [pub_func/message/tool_output_prune.py（新建）](#5-pub_funcmessagetool_output_prunepy新建)
6. [pub_func/message/target_truncation.py（新建）](#6-pub_funcmessagetarget_truncationpy新建)
7. [pub_func/message/**init**.py](#7-pub_funcmessage__init__py)
8. [tests/unit/test_message_utils.py](#8-testsunittest_message_utilspy)

---

## 1. config/num.py

### 旧代码

```python
# Compression and RAG thresholds
ARCHIVE_THRESHOLD = 8_000
MEMORY_THRESHOLD = 10_000
COMPRESS_RATIO = 0.5  # 压缩比例，值越大，旧消息数组（要被压缩的部分）就越大。

# === Trigger Thresholds ===
# Preemptive truncation: at this pressure, truncate large tool outputs (no LLM call)
PREEMPTIVE_TRUNCATE_RATIO = 0.70
# Full compression: at this pressure, trigger LLM summarization
COMPRESSION_TRIGGER_RATIO = 0.80
# Reserve tokens: buffer between trigger and context window limit
COMPRESSION_RESERVE_TOKENS = 16_000
```

### 新代码

```python
# Compression and RAG thresholds
ARCHIVE_THRESHOLD = 8_000
MEMORY_THRESHOLD = 10_000
COMPRESS_RATIO = 0.5

# === Trigger Thresholds ===
PREEMPTIVE_TRUNCATE_RATIO = 0.70
COMPRESSION_TRIGGER_RATIO = 0.80
COMPRESSION_RESERVE_TOKENS = 16_000

# === Budget-based Tail ===
MIN_PRESERVE_TOKENS = 2_000
MAX_PRESERVE_TOKENS = 15_000
PRESERVE_RATIO = 0.25

# === Multi-strategy Pipeline ===
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MIN_REDUCTION_TOKENS = 5_000
TARGET_TRUNCATE_RATIO = 0.5
MIN_OUTPUT_CHARS_TO_TRUNCATE = 500
MAX_TOOL_OUTPUT_CHARS = 2_000
AGGRESSIVE_TRUNCATE_CHARS = 1_000

# === LLM Summary Improvement ===
SUMMARY_TRIM_TOKENS = 12_000
SUMMARY_TOTAL_MAX_CHARS = 16_000
CONTENT_HEAD_RATIO = 0.3
CONTENT_TAIL_RATIO = 0.3

# === Degradation Monitoring ===
DEGRADATION_MONITOR_COUNT = 5
DEGRADATION_NO_TEXT_THRESHOLD = 3
MAX_RECOVERY_ATTEMPTS = 2

# === Anti-thrashing (progressive escalation) ===
MAX_TOTAL_COMPRESSION_ATTEMPTS = 5
INEFFECTIVE_THRESHOLD = 2
MIN_EFFECTIVENESS_PCT = 0.05

# === Protected Tools ===
PROTECTED_TOOLS = frozenset({"memory", "skill_view", "skill_list"})

# === Last Turn Detection ===
LAST_TURN_RATIO_THRESHOLD = 0.5

# === FIFO Section Limits ===
COMPLETED_MAX_ITEMS = 5
KEY_DECISIONS_MAX_ITEMS = 5
CRITICAL_CONTEXT_MAX_ITEMS = 3

# === File Operations Ratchet ===
FILE_OPS_LIST_MAX_CHARS = 900
FILE_OPS_SECTION_MAX_CHARS = 2_000

# === Latest User Request ===
LATEST_USER_REQUEST_MAX_CHARS = 800

# === Auto-continue ===
AUTO_CONTINUE_PROMPT = (
    "Continue if you have next steps, or stop and ask for clarification "
    "if you are unsure how to proceed."
)

# === Token estimation ===
CHARS_PER_TOKEN = 4
```

### 变更说明

- 保留了原有的 `ARCHIVE_THRESHOLD`、`MEMORY_THRESHOLD`、`COMPRESS_RATIO` 和三个触发阈值常量
- 新增 25 个常量，分属 10 个功能组，为 Phase 2 全新中间件提供配置

---

## 2. pub_func/message/estimate_msg_tokens.py

### 旧代码

```python
import json
from langchain_core.messages import BaseMessage


def estimate_msg_tokens(msg: BaseMessage) -> int:
    content = msg.content

    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content) if content is not None else ""

    return len(text)
```

### 新代码

```python
import json
from langchain_core.messages import BaseMessage
from config.num import CHARS_PER_TOKEN


def estimate_msg_tokens(msg: BaseMessage) -> int:
    total = 0
    content = msg.content

    if isinstance(content, str):
        total += len(content)
    else:
        total += len(json.dumps(content)) if content is not None else 0

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            total += len(str(tc.get("name", "")))
            total += len(str(tc.get("args", "")))

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        total += len(str(tool_call_id))

    return total // CHARS_PER_TOKEN


def estimate_messages_tokens(messages) -> int:
    return sum(estimate_msg_tokens(m) for m in messages)
```

### 变更说明

- **旧函数返回字符数**（`len(text)`），函数名虽然叫 `estimate_msg_tokens` 但实际返回的是 chars
- **新函数返回 token 估算值**（`total // 4`），真正匹配函数名语义
- 新增 `tool_calls`（工具调用名称 + 参数）和 `tool_call_id` 的字符计入
- 新增 `estimate_messages_tokens` 批量估算函数
- 从 `config.num` 导入 `CHARS_PER_TOKEN` 常量

---

## 3. pub_func/message/turn_utils.py（新建）

```python
from __future__ import annotations
from dataclasses import dataclass
from langchain_core.messages import BaseMessage, HumanMessage


@dataclass
class Turn:
    start_idx: int
    end_idx: int
    messages: list[BaseMessage]


def split_into_turns(messages: list[BaseMessage]) -> list[Turn]:
    if not messages:
        return []
    turns: list[Turn] = []
    turn_start = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) and i > 0:
            turns.append(Turn(turn_start, i, messages[turn_start:i]))
            turn_start = i
    turns.append(Turn(turn_start, len(messages), messages[turn_start:]))
    return turns


def split_turn(
    turn: Turn,
    budget_tokens: int,
    estimator,
) -> int | None:
    if budget_tokens <= 0:
        return None
    if turn.end_idx - turn.start_idx <= 1:
        return None
    for start in range(turn.start_idx + 1, turn.end_idx):
        remaining = turn.messages[start - turn.start_idx:]
        size = estimator(remaining)
        if size <= budget_tokens:
            return start
    return None
```

### 说明

- `split_into_turns`：按 HumanMessage 边界切分消息列表为多个 Turn
- `split_turn`：在单个 Turn 内部找到分割点，使剩余消息适配 token 预算
- 灵感来源：opencode-dev 的 `splitTurn` 机制

---

## 4. pub_func/message/tool_output_dedup.py（新建）

```python
import json
import hashlib
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage

DEFAULT_PROTECTED_TOOLS: set[str] = set()


def _tool_signature(tool_call: dict) -> str:
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    try:
        sorted_args = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        sorted_args = str(args)
    return f"{name}::{hashlib.md5(sorted_args.encode()).hexdigest()}"


def dedup_tool_outputs(
    messages: list[BaseMessage],
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or DEFAULT_PROTECTED_TOOLS

    sig_to_tc_ids: dict[str, list[str]] = {}
    tc_id_to_sig: dict[str, str] = {}

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                name = tc.get("name", "")
                if name in protected:
                    continue
                sig = _tool_signature(tc)
                if tc_id:
                    tc_id_to_sig[tc_id] = sig
                    sig_to_tc_ids.setdefault(sig, []).append(tc_id)

    sigs_with_dupes = {
        sig for sig, ids in sig_to_tc_ids.items() if len(ids) > 1
    }
    if not sigs_with_dupes:
        return messages, 0

    keep_tc_ids: set[str] = set()
    for sig in sigs_with_dupes:
        keep_tc_ids.add(sig_to_tc_ids[sig][-1])

    to_replace: dict[int, str] = {}
    tokens_reduced = 0

    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        sig = tc_id_to_sig.get(tc_id)
        if sig is None or sig not in sigs_with_dupes:
            continue
        if tc_id in keep_tc_ids:
            continue
        old_len = len(str(getattr(msg, "content", "")))
        tool_name = sig.split("::")[0]
        placeholder = f"[Duplicated call to {tool_name} - output cleared, see latest result]"
        new_len = len(placeholder)
        if old_len > new_len:
            tokens_reduced += (old_len - new_len) // 4
        to_replace[i] = placeholder

    if not to_replace:
        return messages, 0

    result = list(messages)
    for idx, placeholder in to_replace.items():
        result[idx] = result[idx].model_copy(update={"content": placeholder})

    return result, tokens_reduced
```

### 说明

- 对相同工具名 + 相同参数的重复调用，只保留最后一次的 ToolMessage 输出
- 早期的重复输出替换为占位文本 `[Duplicated call to {tool} - output cleared, see latest result]`
- 受保护工具（如 memory）跳过去重
- 返回 `(新消息列表, 减少的 token 数)`

---

## 5. pub_func/message/tool_output_prune.py（新建）

```python
from langchain_core.messages import (
    BaseMessage,
    ToolMessage,
    HumanMessage,
    AIMessage,
)

_PRUNE_MARKER = "[Old tool result content cleared]"
_SUMMARY_LC_SOURCE = "summarization"


def _is_summary_message(msg: BaseMessage) -> bool:
    return getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE


def _find_tool_name(
    messages: list[BaseMessage], target_idx: int, tc_id: str
) -> str:
    if not tc_id:
        return ""
    for i in range(target_idx - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("id") == tc_id:
                    return tc.get("name", "")
    return ""


def prune_tool_outputs(
    messages: list[BaseMessage],
    protect_tokens: int = 40_000,
    min_reduction_tokens: int = 5_000,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()
    if estimator is None:
        def estimator(msgs):
            return sum(len(str(getattr(m, "content", ""))) // 4 for m in msgs)

    total_tool_tokens = 0
    pruned_tokens = 0
    to_prune: list[int] = []

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if _is_summary_message(msg):
            break
        if not isinstance(msg, ToolMessage):
            continue

        tc_id = getattr(msg, "tool_call_id", "")
        tool_name = _find_tool_name(messages, i, tc_id)
        if tool_name in protected:
            continue
        if getattr(msg, "status", "") == "compacted":
            continue

        content_len = len(str(getattr(msg, "content", "")))
        token_est = content_len // 4
        total_tool_tokens += token_est

        if total_tool_tokens <= protect_tokens:
            continue

        to_prune.append(i)
        pruned_tokens += token_est

    if pruned_tokens < min_reduction_tokens or not to_prune:
        return messages, 0

    result = list(messages)
    for idx in to_prune:
        result[idx] = result[idx].model_copy(update={"content": _PRUNE_MARKER})

    return result, pruned_tokens
```

### 说明

- 从末尾向前遍历，保护最近的 `protect_tokens`（默认 40000）个 token 的工具输出
- 超出保护窗口的旧 ToolMessage 替换为 `[Old tool result content cleared]`
- 遇到摘要消息（`lc_source="summarization"`）时停止向前遍历
- 如果总减少量 < `min_reduction_tokens`（默认 5000），则不执行（不值得）
- 灵感来源：opencode-dev 的 prune 机制

---

## 6. pub_func/message/target_truncation.py（新建）

```python
from langchain_core.messages import BaseMessage, ToolMessage, AIMessage

_OMISSION_TEMPLATE = "...[truncated {omitted} chars]..."


def _truncate_content(
    content: str,
    max_chars: int,
    head_ratio: float = 0.3,
    tail_ratio: float = 0.3,
) -> str:
    if len(content) <= max_chars:
        return content
    head = content[: int(max_chars * head_ratio)]
    tail = content[-int(max_chars * tail_ratio):]
    omitted = len(content) - len(head) - len(tail)
    return f"{head}{_OMISSION_TEMPLATE.format(omitted=omitted)}{tail}"


def _find_tool_name(
    messages: list[BaseMessage], target_idx: int, tc_id: str
) -> str:
    if not tc_id:
        return ""
    for i in range(target_idx - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("id") == tc_id:
                    return tc.get("name", "")
    return ""


def target_truncate_tool_outputs(
    messages: list[BaseMessage],
    target_reduction_tokens: int,
    min_output_chars: int = 500,
    max_output_chars: int = 2000,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()

    candidates: list[tuple[int, int]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        content = str(getattr(msg, "content", ""))
        if len(content) < min_output_chars:
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        tool_name = _find_tool_name(messages, i, tc_id)
        if tool_name in protected:
            continue
        candidates.append((i, len(content)))

    candidates.sort(key=lambda x: x[1], reverse=True)

    if not candidates:
        return messages, 0

    result = list(messages)
    total_reduced = 0

    for idx, old_len in candidates:
        if total_reduced >= target_reduction_tokens:
            break
        msg = result[idx]
        content = str(getattr(msg, "content", ""))
        truncated = _truncate_content(content, max_output_chars)
        new_len = len(truncated)
        reduced_tokens = (old_len - new_len) // 4
        total_reduced += max(reduced_tokens, 0)
        result[idx] = msg.model_copy(update={"content": truncated})

    return result, total_reduced
```

### 说明

- 按内容长度降序排列所有 ToolMessage，从最大的开始截断
- 每个截断为 head（30%）+ tail（30%），中间替换为 `[truncated N chars]`
- 达到目标减少量 `target_reduction_tokens` 时停止
- 小于 `min_output_chars`（默认 500）的输出不值得截断，跳过
- 灵感来源：oh-my-openagent 的 target-token-truncation

---

## 7. pub_func/message/**init**.py

### 旧代码

```python
from .estimate_msg_tokens import estimate_msg_tokens
from .extract_final_answer import extract_final_answer
from .slice_last_turn import slice_last_turn, slice_last_n_turn

__all__ = ["slice_last_turn", "slice_last_n_turn", "estimate_msg_tokens", "extract_final_answer"]
```

### 新代码

```python
from .estimate_msg_tokens import estimate_msg_tokens, estimate_messages_tokens
from .extract_final_answer import extract_final_answer
from .slice_last_turn import slice_last_turn, slice_last_n_turn
from .turn_utils import split_into_turns, split_turn, Turn
from .tool_output_dedup import dedup_tool_outputs
from .tool_output_prune import prune_tool_outputs
from .target_truncation import target_truncate_tool_outputs

__all__ = [
    "slice_last_turn",
    "slice_last_n_turn",
    "estimate_msg_tokens",
    "estimate_messages_tokens",
    "extract_final_answer",
    "split_into_turns",
    "split_turn",
    "Turn",
    "dedup_tool_outputs",
    "prune_tool_outputs",
    "target_truncate_tool_outputs",
]
```

### 变更说明

- 新增导出 `estimate_messages_tokens`、`split_into_turns`、`split_turn`、`Turn`、`dedup_tool_outputs`、`prune_tool_outputs`、`target_truncate_tool_outputs`

---

## 8. tests/unit/test_message_utils.py

### 旧代码（TestEstimateMsgTokens 类）

```python
class TestEstimateMsgTokens:
    def test_string_content(self):
        msg = SimpleNamespace(content="hello world")
        assert estimate_msg_tokens(msg) == len("hello world")

    def test_structured_content_uses_json(self):
        content = [{"type": "text", "text": "hi"}]
        msg = SimpleNamespace(content=content)
        assert estimate_msg_tokens(msg) == len('[{"type": "text", "text": "hi"}]')

    def test_empty_string(self):
        msg = SimpleNamespace(content="")
        assert estimate_msg_tokens(msg) == 0

    def test_none_content(self):
        msg = SimpleNamespace(content=None)
        assert estimate_msg_tokens(msg) == 0

    def test_real_langchain_message(self):
        msg = AIMessage(content="reasoned")
        assert estimate_msg_tokens(msg) == len("reasoned")
```

### 新代码（TestEstimateMsgTokens 类）

```python
class TestEstimateMsgTokens:
    def test_string_content(self):
        msg = SimpleNamespace(content="hello world")
        assert estimate_msg_tokens(msg) == len("hello world") // 4

    def test_structured_content_uses_json(self):
        content = [{"type": "text", "text": "hi"}]
        msg = SimpleNamespace(content=content)
        assert estimate_msg_tokens(msg) == len('[{"type": "text", "text": "hi"}]') // 4

    def test_empty_string(self):
        msg = SimpleNamespace(content="")
        assert estimate_msg_tokens(msg) == 0

    def test_none_content(self):
        msg = SimpleNamespace(content=None)
        assert estimate_msg_tokens(msg) == 0

    def test_real_langchain_message(self):
        msg = AIMessage(content="reasoned")
        assert estimate_msg_tokens(msg) == len("reasoned") // 4
```

### 变更说明

- 所有断言从 `len(text)` 改为 `len(text) // 4`，因为 `estimate_msg_tokens` 现在返回 token 估算值而非字符数
- `test_empty_string` 和 `test_none_content` 的断言保持 `== 0`（0 // 4 仍为 0）

---
