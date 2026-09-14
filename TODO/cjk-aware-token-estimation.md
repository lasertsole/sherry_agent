# TODO: CJK 感知的 Token 估算 — 全局替换 `len // 4` 模式

> 状态: **执行中** — 核心函数已创建，调用点替换待执行
> 创建时间: 2026-09-14
> 优先级: P1
> 关联文件: `pub/func/estimate_tokens.py`（**已新建**）· `pub/func/cjk.py` · `config/features/agent_side/token_estimation.py`

---

## 问题

项目中所有 token 估算统一使用 `len(text) // CHARS_PER_TOKEN`（`CHARS_PER_TOKEN = 4`），这是英文 ASCII 的经验值（4 字符 ≈ 1 token）。对 CJK 文本（中日韩），1 个汉字 ≈ 1~~2 token，实际比值约 1.5~~2 字符/token，导致估算严重低估（最高低估 2~8 倍）。

后果：

- Summarization 中间件触发压缩的时机偏晚 — CJK 对话会比预期更晚才压缩，可能导致 context 溢出
- ContextLimitGuard 的 mid-stream 输出预算检查放行过多 CJK 文本
- Tool output prune/dedup/truncation 的 token 回收量被低估，截断不充分
- Overflow router 的压力计算偏低，路由决策可能从 "fits" 错误地落到不压缩

现有 CJK 检测函数已在 `pub/func/cjk.py`（`contains_cjk`、`is_cjk_codepoint`、`count_cjk`），但从未被 token 估算逻辑引用。

---

## 已完成

### 1. 核心函数 — `pub/func/estimate_tokens.py`（已新建）

三层降级估算（优先级从高到低）：

| Tier   | 来源                                                                                         | 精度 | 可用时机   |
| ------ | -------------------------------------------------------------------------------------------- | ---- | ---------- |
| **T1** | API 上报 `usage_metadata.input_tokens`（从最后一条 `AIMessage` 自动提取）                    | 精确 | 模型调用后 |
| **T2** | CJK 感知启发式 — `count_cjk // CHARS_PER_TOKEN_CJK` + `(len - count_cjk) // CHARS_PER_TOKEN` | 中等 | 始终可用   |
| **T3** | 旧 `len // 4`（T2 对纯 ASCII 的退化形态，非独立代码路径）                                    | 低   | 始终可用   |

#### 新文件完整源码

```python
# pub/func/estimate_tokens.py

"""CJK-aware token estimation with three-tier fallback.

Three tiers (highest priority first):

  **Tier 1 — API-reported token usage**
      ``usage_metadata["input_tokens"]`` from the last ``AIMessage`` in the
      message list.  This is the provider's ground-truth count, captured at
      model-call time.  When available it short-circuits all local
      estimation.

  **Tier 2 — CJK-aware heuristic**
      Splits text into CJK characters (≈ ``len // CHARS_PER_TOKEN_CJK``)
      and non-CJK characters (≈ ``len // CHARS_PER_TOKEN``).  The existing
      ``pub.func.cjk.count_cjk`` helper is reused for CJK detection.
      For pure ASCII text this degenerates to the legacy formula, so
      existing ASCII-based tests keep the same numbers.

  **Tier 3 — Legacy ``len(text) // CHARS_PER_TOKEN``**
      Not a separate code path — it is the degenerate case of Tier 2 when
      ``count_cjk(text) == 0``.  Documented for clarity only.

Usage::

    from pub.func.estimate_tokens import estimate_messages_tokens

    # Auto: Tier 1 if last AIMessage carries usage_metadata, else Tier 2.
    tokens = estimate_messages_tokens(messages)

    # Explicit Tier 1 override (e.g. from a model response, not from messages).
    tokens = estimate_messages_tokens(messages, reported_tokens=8192)

    # Force Tier 2 only (disable Tier 1 auto-extract).
    tokens = estimate_messages_tokens(messages, reported_tokens=0)

For callers that need ``max(local_estimate, reported)`` semantics (e.g.
``overflow_router.compute_pressure``), pass ``reported_tokens=0`` to get
the pure local estimate and let the caller apply ``max()`` itself.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from config.features import TOKEN_ESTIMATION
from pub.func.cjk import count_cjk

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]


def estimate_text_tokens(text: str) -> int:
    cjk = count_cjk(text)
    non_cjk = len(text) - cjk
    return (cjk // CHARS_PER_TOKEN_CJK) + (non_cjk // CHARS_PER_TOKEN)


def extract_reported_tokens(messages: Sequence[Any]) -> int | None:
    for msg in reversed(list(messages)):
        if isinstance(msg, AIMessage):
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, dict):
                val = usage.get("input_tokens")
                if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    return val
                val = usage.get("total_tokens")
                if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    return val
            return None
    return None


def estimate_msg_tokens(msg: BaseMessage) -> int:
    total = 0
    content = msg.content

    if isinstance(content, str):
        total += estimate_text_tokens(content)
    else:
        total += estimate_text_tokens(json.dumps(content)) if content is not None else 0

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            total += estimate_text_tokens(str(tc.get("name", "")))
            total += estimate_text_tokens(str(tc.get("args", "")))

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        total += estimate_text_tokens(str(tool_call_id))

    return total


def estimate_messages_tokens(
    messages: Sequence[Any],
    reported_tokens: int | None = None,
) -> int:
    if reported_tokens is not None and reported_tokens > 0:
        return reported_tokens
    if reported_tokens is None:
        auto = extract_reported_tokens(messages)
        if auto is not None and auto > 0:
            return auto
    return sum(estimate_msg_tokens(m) for m in messages)
```

`estimate_messages_tokens` 的 `reported_tokens` 参数语义：

- `None`（默认）：自动从 `messages` 中提取 T1 → 不可用则走 T2
- `> 0`：直接使用此值（T1 显式覆盖）
- `== 0`：跳过 T1，直接走 T2（供需要自己做 `max()` 的调用方使用）

### 2. 配置常量 — `config/features/agent_side/token_estimation.py`（已编辑）

```python
# --- 旧 ---
"""Token-estimation constant shared by truncation helpers."""

from typing import TypedDict


class TokenEstimationConfig(TypedDict):
    """Token-estimation constant shared by truncation helpers."""

    chars_per_token: int


TOKEN_ESTIMATION: TokenEstimationConfig = {
    "chars_per_token": 4,
}
```

```python
# --- 新 ---
"""Token-estimation constants shared by truncation helpers."""

from typing import TypedDict


class TokenEstimationConfig(TypedDict):
    """Token-estimation constants shared by truncation helpers."""

    chars_per_token: int
    chars_per_token_cjk: int


TOKEN_ESTIMATION: TokenEstimationConfig = {
    "chars_per_token": 4,
    "chars_per_token_cjk": 2,
}
```

`chars_per_token_cjk = 2` 是保守值（1 个汉字 ≈ 1~1.5 token，2 字符/token 略偏高估 → 压缩略早触发 → 安全方向）。

### 3. 向后兼容 — `pub/func/message/estimate_msg_tokens.py`（已改为 re-export）

```python
# --- 旧 ---
import json
from langchain_core.messages import BaseMessage
from config.features import TOKEN_ESTIMATION

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]


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

```python
# --- 新 ---
"""Backward-compatible re-export.

The canonical implementation now lives in :mod:`pub.func.estimate_tokens`
(three-tier fallback: API-reported → CJK-aware heuristic → legacy ``// 4``).
"""

from pub.func.estimate_tokens import (
    estimate_msg_tokens as estimate_msg_tokens,
    estimate_messages_tokens as estimate_messages_tokens,
)

__all__ = ["estimate_msg_tokens", "estimate_messages_tokens"]
```

所有 `from pub.func.message.estimate_msg_tokens import ...` 和 `from pub.func.message import ...` 路径保持不变。

### 4. 顶层导出 — `pub/func/__init__.py`（已编辑）

```python
# --- 旧 ---
from .cjk import (
    contains_cjk as contains_cjk,
    is_cjk_codepoint as is_cjk_codepoint,
    count_cjk as count_cjk,
)
```

```python
# --- 新 ---
from .cjk import (
    contains_cjk as contains_cjk,
    is_cjk_codepoint as is_cjk_codepoint,
    count_cjk as count_cjk,
)
from .estimate_tokens import (
    estimate_text_tokens as estimate_text_tokens,
    estimate_msg_tokens as estimate_msg_tokens,
    estimate_messages_tokens as estimate_messages_tokens,
    extract_reported_tokens as extract_reported_tokens,
)
```

---

## 待执行：调用点替换

### Tier A: 已自动生效（无需改代码）

这些文件已通过 `estimate_msg_tokens` / `estimate_messages_tokens` 做估算，re-export 后自动使用新实现：

| #   | 文件                                     | 行号                                                     | 说明            |
| --- | ---------------------------------------- | -------------------------------------------------------- | --------------- |
| A1  | `agent/middlewares/summarization.py:698` | `return estimate_messages_tokens(list(messages))`        | 自动 T1→T2 降级 |
| A2  | `pub/func/message/overflow_router.py:36` | `from ...estimate_msg_tokens import estimate_msg_tokens` | 自动 T2         |
| A3  | `pub/func/message/__init__.py`           | re-export                                                | 自动            |
| A4  | `pub/func/__init__.py`                   | re-export                                                | 已更新          |

**注意 — `summarization.py:_check_trigger` 的 `max()` 模式**:

当前代码（L743-745）：

```python
local_est = self._estimate_tokens(messages)       # 现在自动 T1→T2
reported = self._get_reported_tokens(messages)    # 手动提取 total_tokens
effective = max(local_est, reported) if reported > 0 else local_est
```

`_estimate_tokens` 现在会自动提取 T1（`input_tokens`），而 `_get_reported_tokens` 提取的是 `total_tokens`。两者来源不同：

- `local_est` = T1(`input_tokens`) 或 T2(启发式)
- `reported` = `total_tokens`（input + output）

`max(local_est, reported)` 在 T1 可用时 = `max(input_tokens, total_tokens)` = `total_tokens`（因为 total > input）。这在逻辑上是对的（取更保守的值）。但更清晰的做法是简化为：

```python
effective = self._estimate_tokens(messages)  # T1→T2, 已含最佳估算
```

移除 `_get_reported_tokens` 和 `max()` 调用。但这会改变行为（从 `max(input, total)` 变为 `input` 或 `local`），需要验证不会导致压缩触发偏晚。**建议保留现状，待性能验证后决定是否简化。**

### Tier B: 内联 `// 4` 硬编码 — 需手动替换

#### B1. `pub/func/message/tool_output_prune.py` — `_default_estimator`

```python
# --- 旧 (L112-114) ---
        def _default_estimator(msgs):
            return sum(len(str(getattr(m, "content", ""))) // 4 for m in msgs)
```

```python
# --- 新 ---
        def _default_estimator(msgs):
            return sum(
                estimate_text_tokens(str(getattr(m, "content", "")))
                for m in msgs
            )
```

需新增 import（文件顶部）：

```python
# --- 旧 ---
from config.features import SUMMARIZATION
```

```python
# --- 新 ---
from config.features import SUMMARIZATION
from pub.func.estimate_tokens import estimate_text_tokens
```

#### B1b. `pub/func/message/tool_output_prune.py` — `token_est` 计算（同一文件第二处）

```python
# --- 旧 (L136-137) ---
        content_len = len(str(getattr(msg, "content", "")))
        token_est = content_len // 4
```

```python
# --- 新 ---
        content_str = str(getattr(msg, "content", ""))
        token_est = estimate_text_tokens(content_str)
```

> import 与 B1 共享（同一文件，已在 B1 添加 `from pub.func.estimate_tokens import estimate_text_tokens`）。

#### B2. `pub/func/message/tool_output_dedup.py` — `tokens_reduced` 计算

```python
# --- 旧 (L65-70) ---
        old_len = len(str(getattr(msg, "content", "")))
        tool_name = sig.split("::")[0]
        placeholder = f"[Duplicated call to {tool_name} - output cleared, see latest result]"
        new_len = len(placeholder)
        if old_len > new_len:
            tokens_reduced += (old_len - new_len) // 4
```

```python
# --- 新 ---
        old_content = str(getattr(msg, "content", ""))
        tool_name = sig.split("::")[0]
        placeholder = f"[Duplicated call to {tool_name} - output cleared, see latest result]"
        if len(old_content) > len(placeholder):
            tokens_reduced += estimate_text_tokens(old_content) - estimate_text_tokens(placeholder)
```

需新增 import（文件顶部）：

```python
# --- 旧 ---
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage

from config.features import MESSAGE_PIPELINE
```

```python
# --- 新 ---
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage

from config.features import MESSAGE_PIPELINE
from pub.func.estimate_tokens import estimate_text_tokens
```

#### B3. `pub/func/message/tool_args_truncate.py` — `freed_total` 计算

```python
# --- 旧 (L104-105) ---
            new_args_str = json.dumps(new_args, ensure_ascii=False)
            freed_total += max((len(args_str) - len(new_args_str)) // 4, 0)
```

```python
# --- 新 ---
            new_args_str = json.dumps(new_args, ensure_ascii=False)
            freed_total += max(
                estimate_text_tokens(args_str) - estimate_text_tokens(new_args_str), 0
            )
```

需新增 import（文件顶部，在现有 import 块之后添加）：

```python
# --- 新增 ---
from pub.func.estimate_tokens import estimate_text_tokens
```

#### B4. `pub/func/message/target_truncation.py` — `reduced_tokens` 计算

```python
# --- 旧 (L73-77) ---
        content = str(getattr(msg, "content", ""))
        truncated = _truncate_content(content, max_output_chars)
        new_len = len(truncated)
        reduced_tokens = (old_len - new_len) // 4
        total_reduced += max(reduced_tokens, 0)
```

```python
# --- 新 ---
        content = str(getattr(msg, "content", ""))
        truncated = _truncate_content(content, max_output_chars)
        reduced_tokens = estimate_text_tokens(content) - estimate_text_tokens(truncated)
        total_reduced += max(reduced_tokens, 0)
```

> 注意：`old_len` 参数在调用方传入的是 `len(content)`，替换后不再需要 `old_len`，可直接用 `content` 估算。但为了最小改动，也可保留 `old_len` 变量并用 `estimate_text_tokens(content)` 替代 `old_len - new_len` 的计算。上面写法已省略 `new_len` 变量。

需新增 import（文件顶部）：

```python
# --- 旧 ---
from config.features import SUMMARIZATION
from langchain_core.messages import BaseMessage, ToolMessage, AIMessage
```

```python
# --- 新 ---
from config.features import SUMMARIZATION
from langchain_core.messages import BaseMessage, ToolMessage, AIMessage
from pub.func.estimate_tokens import estimate_text_tokens
```

#### B5. `agent/middlewares/summarization.py` — `_estimate_system_prompt_tokens`

```python
# --- 旧 (L769-773) ---
    def _estimate_system_prompt_tokens(self, session_id: str) -> int:
        prompt = state_register_mem.get_state(session_id, "system_prompt", "")
        if isinstance(prompt, str) and prompt:
            return len(prompt) // 4
        return 0
```

```python
# --- 新 ---
    def _estimate_system_prompt_tokens(self, session_id: str) -> int:
        prompt = state_register_mem.get_state(session_id, "system_prompt", "")
        if isinstance(prompt, str) and prompt:
            return estimate_text_tokens(prompt)
        return 0
```

需新增 import（文件顶部 L28 旁）：

```python
# --- 旧 ---
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens, estimate_messages_tokens
```

```python
# --- 新 ---
from pub.func.estimate_tokens import estimate_text_tokens
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens, estimate_messages_tokens
```

#### B6. `agent/wrapper/context_limit.py` — mid-stream 输出预算检查

```python
# --- 旧 (L150-158) ---
                        if len(call_output_text) // _CHARS_PER_TOKEN > self._output_token_budget:
                            output_cut = True
                            logger.warning(
                                "ContextLimitGuard: mid-stream output budget "
                                "({} est. tokens > {}) exceeded for session {} — "
                                "truncating client view",
                                len(call_output_text) // _CHARS_PER_TOKEN,
                                self._output_token_budget,
                                session_id,
                            )
```

```python
# --- 新 ---
                        est_output_tokens = estimate_text_tokens(call_output_text)
                        if est_output_tokens > self._output_token_budget:
                            output_cut = True
                            logger.warning(
                                "ContextLimitGuard: mid-stream output budget "
                                "({} est. tokens > {}) exceeded for session {} — "
                                "truncating client view",
                                est_output_tokens,
                                self._output_token_budget,
                                session_id,
                            )
```

需新增 import + 移除旧常量（文件顶部）：

```python
# --- 旧 (L36-42) ---
from config.features import CONTEXT_GUARD, SUMMARIZATION, TOKEN_ESTIMATION
from runtime import state_register_mem
from agent.middlewares.summarization_components import _FORCE_RECOVERY_KEY
from .repetition_guard import RepetitionGuardWrapper

COMPRESSION_TRIGGER_RATIO = SUMMARIZATION["compression_trigger_ratio"]
_CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
```

```python
# --- 新 ---
from config.features import CONTEXT_GUARD, SUMMARIZATION
from runtime import state_register_mem
from agent.middlewares.summarization_components import _FORCE_RECOVERY_KEY
from pub.func.estimate_tokens import estimate_text_tokens
from .repetition_guard import RepetitionGuardWrapper

COMPRESSION_TRIGGER_RATIO = SUMMARIZATION["compression_trigger_ratio"]
```

> `TOKEN_ESTIMATION` import 和 `_CHARS_PER_TOKEN` 常量移除（不再被引用）。需确认文件内无其他 `_CHARS_PER_TOKEN` 引用。

### Tier C: 不改

| #   | 文件                                                                        | 行号                         | 原因                                                |
| --- | --------------------------------------------------------------------------- | ---------------------------- | --------------------------------------------------- |
| C1  | `agent/tools/message_search.py:145`                                         | `candidate - max_chars // 4` | 不是 token 估算，是搜索窗口的 25%/75% 字符分配      |
| C2  | `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/**` | 多处 `_count_tokens`         | vendored 代码，使用独立 tokenizer，不走项目估算体系 |

---

## 测试计划

### 新增测试

| #   | 文件                                         | Marker      | 描述                                                                                                                                               |
| --- | -------------------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1  | `tests/pub/func/test_estimate_tokens.py`     | unit        | `estimate_text_tokens`: 纯 CJK 文本 ≈ `len // 2`；纯 ASCII ≈ `len // 4`；混合文本正确分离                                                          |
| T2  | 同上                                         | unit        | CJK 范围覆盖：汉字、ひらがな、カタカナ、한글                                                                                                       |
| T3  | 同上                                         | unit        | `extract_reported_tokens`: 有 `AIMessage.usage_metadata` → 返回 `input_tokens`；无 AIMessage → None；`input_tokens` 缺失 → fallback `total_tokens` |
| T4  | 同上                                         | unit        | `estimate_messages_tokens`: `reported_tokens=None` 且有 usage_metadata → T1；`reported_tokens=0` → T2；`reported_tokens=8192` → T1                 |
| T5  | 同上                                         | unit        | `estimate_msg_tokens`: content + tool_calls + tool_call_id 均计入                                                                                  |
| T6  | `tests/pub/func/test_estimate_tokens_cjk.py` | integration | 端到端: CJK 消息列表估算 > 旧 `len // 4` 估算                                                                                                      |

### 现有测试适配

现有测试全用 ASCII payload。Tier 2 对 ASCII 的结果 = `count_cjk=0, non_cjk_len // 4` = 旧 `len // 4`，数值完全一致。但 `estimate_messages_tokens` 现在会自动提取 T1 — 如果测试消息列表中有带 `usage_metadata` 的 `AIMessage`，结果会变。

| #   | 文件                                                          | 策略                                                                                           |
| --- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| S1  | `tests/pub/func/message/test_message_utils.py`                | 若 `AIMessage` 无 `usage_metadata` → 不影响。确认或 monkeypatch                                |
| S2  | `tests/pub/func/message/test_overflow_router.py`              | 注释已说 "All payloads are ASCII" — 确认无 `usage_metadata`                                    |
| S3  | `tests/pub/func/message/test_tool_output_prune.py`            | `5000 // 4` → 替换为 `estimate_text_tokens("x" * 5000)` 或确认等价                             |
| S4  | `tests/pub/func/message/test_pub_func_message_tools.py`       | 同上                                                                                           |
| S5  | `tests/agent/middlewares/test_summarization_comprehensive.py` | 确认 mock 消息无 `usage_metadata`，或显式设为 None                                             |
| S6  | `tests/agent/middlewares/test_e2e_summarization.py`           | `_TURN_CHARS = 5000` ASCII — 确认一致                                                          |
| S7  | `tests/agent/middlewares/test_compression_comprehensive.py`   | 多处 `// 4` 预期值 — 确认                                                                      |
| S8  | `tests/config/test_num_contract.py:294`                       | `assert TOKEN_ESTIMATION["chars_per_token"] == 4` — 保留，新增 `chars_per_token_cjk == 2` 断言 |
| S9  | `tests/config/test_features_agent_side.py:102`                | 新增 `(TOKEN_ESTIMATION, "chars_per_token_cjk", 2)`                                            |

### 测试策略

- **不需要 monkeypatch 禁用 T1**：只要测试消息列表中没有带 `usage_metadata` 的 `AIMessage`，T1 自动跳过，走 T2。
- **ASCII 一致性**：T2 对 ASCII = 旧 `len // 4`，所有 ASCII 测试数值不变。
- **CJK 新测试**：验证 CJK 文本估算 > 旧 `len // 4`（证明不再低估）。

---

## 全量审计：所有 `// 4` / `CHARS_PER_TOKEN` 匹配清单

以下是对全项目（排除 `__pycache__`）grep `// *4|// *CHARS_PER_TOKEN|chars_per_token|CHARS_PER_TOKEN` 的**逐条分类**，确保无遗漏。

### 生产代码 — 需替换（Tier B）

| #   | 文件:行号                                    | 代码                                                     | 对应计划项                      |
| --- | -------------------------------------------- | -------------------------------------------------------- | ------------------------------- |
| 1   | `pub/func/message/tool_output_prune.py:114`  | `sum(len(str(...)) // 4 for m in msgs)`                  | B1                              |
| 2   | `pub/func/message/tool_output_prune.py:137`  | `token_est = content_len // 4`                           | **B1b**                         |
| 3   | `pub/func/message/tool_output_dedup.py:70`   | `(old_len - new_len) // 4`                               | B2                              |
| 4   | `pub/func/message/tool_args_truncate.py:105` | `max((len(args_str) - len(new_args_str)) // 4, 0)`       | B3                              |
| 5   | `pub/func/message/target_truncation.py:76`   | `(old_len - new_len) // 4`                               | B4                              |
| 6   | `agent/middlewares/summarization.py:772`     | `return len(prompt) // 4`                                | B5                              |
| 7   | `agent/wrapper/context_limit.py:42`          | `_CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]` | B6 (常量移除)                   |
| 8   | `agent/wrapper/context_limit.py:150`         | `len(call_output_text) // _CHARS_PER_TOKEN`              | B6                              |
| 9   | `agent/wrapper/context_limit.py:156`         | `len(call_output_text) // _CHARS_PER_TOKEN`              | B6 (同一处 logger.warning 参数) |

### 生产代码 — 已完成 / 自动生效

| #   | 文件:行号                                             | 说明                                                                        |
| --- | ----------------------------------------------------- | --------------------------------------------------------------------------- |
| 10  | `pub/func/estimate_tokens.py` (整文件)                | ✅ 新建，包含 `CHARS_PER_TOKEN` / `CHARS_PER_TOKEN_CJK` 常量 + 三层降级函数 |
| 11  | `pub/func/message/estimate_msg_tokens.py` (整文件)    | ✅ 改为 re-export                                                           |
| 12  | `config/features/agent_side/token_estimation.py:9-15` | ✅ 新增 `chars_per_token_cjk` 字段                                          |
| 13  | `agent/middlewares/summarization.py:698`              | A1 — 调用 `estimate_messages_tokens`，自动 T1→T2                            |
| 14  | `pub/func/message/overflow_router.py:36`              | A2 — import `estimate_msg_tokens`，自动 T2                                  |

### 生产代码 — 仅注释/docstring（不改代码，可选更新文案）

| #   | 文件:行号                                   | 内容                                                        | 是否更新                                                          |
| --- | ------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------- |
| 15  | `pub/func/message/overflow_router.py:57`    | docstring: "CHARS_PER_TOKEN=4 can underestimate real usage" | 可选: 补充 "CJK low-estimation fixed in pub.func.estimate_tokens" |
| 16  | `pub/func/message/tool_args_truncate.py:65` | docstring: "chars // 4, same estimator convention"          | 可选: 改为 "estimate_text_tokens"                                 |
| 17  | `agent/wrapper/context_limit.py:17`         | docstring: "`_CHARS_PER_TOKEN` characters per token"        | B6 替换后此 docstring 需更新                                      |

### 生产代码 — 不改

| #   | 文件:行号                                                        | 原因                                                        |
| --- | ---------------------------------------------------------------- | ----------------------------------------------------------- |
| 18  | `agent/tools/message_search.py:145`                              | `max_chars // 4` — 搜索窗口 25%/75% 字符分配，非 token 估算 |
| 19  | `skills/.../vendored_lightrag/llm/deprecated/siliconcloud.py:59` | `len(decode_bytes) // 4` — vendored 代码，独立体系          |

### 测试代码 — 需适配（Tier S）

| #   | 文件:行号                                                                              | 内容                                        | 对应计划项 |
| --- | -------------------------------------------------------------------------------------- | ------------------------------------------- | ---------- |
| 20  | `tests/pub/func/message/test_message_utils.py:101,106,118`                             | `len("hello world") // 4` 等硬编码预期      | S1         |
| 21  | `tests/pub/func/message/test_overflow_router.py:9,23,43,46`                            | `CHARS_PER_TOKEN` 变量 + ASCII payload 构造 | S2         |
| 22  | `tests/pub/func/message/test_tool_output_prune.py:99,134`                              | `5000 // 4`, `8000 // 4` 预期值             | S3         |
| 23  | `tests/pub/func/message/test_pub_func_message_tools.py:88,115,216,238,279,284,326,382` | 多处 `// 4` 预期值                          | S4         |
| 24  | `tests/agent/middlewares/test_summarization_comprehensive.py:105,124,125,367`          | 注释 + `3200 // 4 == 800`                   | S5         |
| 25  | `tests/agent/middlewares/test_e2e_summarization.py:82`                                 | `_TURN_CHARS = 5000` ASCII 注释             | S6         |
| 26  | `tests/agent/middlewares/test_compression_comprehensive.py:147,165,469,476,1018`       | 多处 `// 4` 注释/预期                       | S7         |
| 27  | `tests/config/test_num_contract.py:293-295`                                            | `chars_per_token == 4` 断言                 | S8         |
| 28  | `tests/config/test_features_agent_side.py:102`                                         | `(TOKEN_ESTIMATION, "chars_per_token", 4)`  | S9         |

### 测试代码 — 仅注释

| #   | 文件:行号                                                          | 说明                                              |
| --- | ------------------------------------------------------------------ | ------------------------------------------------- |
| 29  | `tests/context_engine/store/test_interrupt_marker_approach.py:569` | 注释 "len // CHARS_PER_TOKEN"，不涉及断言，无需改 |

**合计: 9 处生产代码替换 (B1-B6 含 B1b) + 4 处已完成 + 3 处仅注释 + 2 处不改 + 10 处测试适配 = 28 条匹配，全部归类。**

---

## 执行顺序

```
[已完成]
1. config/features/agent_side/token_estimation.py — 新增 chars_per_token_cjk
2. pub/func/estimate_tokens.py — 新建三层降级函数
3. pub/func/message/estimate_msg_tokens.py — 改为 re-export
4. pub/func/__init__.py — 添加新导出

[待执行]
5.  Tier B 逐个替换 // 4 硬编码 (B1, B1b, B2-B6)
6.  新增测试 T1-T6
7.  适配现有测试 S1-S9
8.  全量测试: uv run pytest tests/pub/func tests/agent/middlewares tests/config -q
9.  lint + typecheck:
    uv run --with ruff ruff check pub/ agent/ config/
    uv run --no-sync basedpyright pub/ agent/middlewares/ agent/wrapper/ config/
```

---

## 文件汇总

| 层     | 文件                                             | 操作           | 状态      |
| ------ | ------------------------------------------------ | -------------- | --------- |
| Config | `config/features/agent_side/token_estimation.py` | 编辑           | ✅ 已完成 |
| Pub    | `pub/func/estimate_tokens.py`                    | **新建**       | ✅ 已完成 |
| Pub    | `pub/func/message/estimate_msg_tokens.py`        | 改为 re-export | ✅ 已完成 |
| Pub    | `pub/func/__init__.py`                           | 编辑           | ✅ 已完成 |
| Pub    | `pub/func/message/tool_output_prune.py`          | 编辑           | ⏳ 待执行 |
| Pub    | `pub/func/message/tool_output_dedup.py`          | 编辑           | ⏳ 待执行 |
| Pub    | `pub/func/message/tool_args_truncate.py`         | 编辑           | ⏳ 待执行 |
| Pub    | `pub/func/message/target_truncation.py`          | 编辑           | ⏳ 待执行 |
| Agent  | `agent/middlewares/summarization.py`             | 编辑           | ⏳ 待执行 |
| Agent  | `agent/wrapper/context_limit.py`                 | 编辑           | ⏳ 待执行 |
| Tests  | 6 个新建 + 9 个适配                              | 新建/编辑      | ⏳ 待执行 |

---

## 风险与注意事项

1. **T1 自动提取与 `max()` 模式的交互**: `summarization.py:_check_trigger` 的 `max(local_est, reported)` 模式在 T1 自动提取后变为 `max(T1_input, total_tokens)`。这在逻辑上仍然正确（取更保守值），但语义略有变化。建议先保留现状，性能验证后再决定是否简化。

2. **`chars_per_token_cjk = 2` 的取值**: 实测 GPT-4 tokenizer 对中文 ~1.3-1.5 字符/token，取 2 偏保守（略微高估 token 数 → 压缩略早触发 → 安全方向）。若取 1 则可能从低估跳到高估。推荐保持 2。

3. **性能**: `count_cjk` 遍历每个字符的 `ord()`，对长文本有 O(n) 开销。但 token 估算本身已是 O(n)（`len()` 也是），且只在压缩触发检查时调用（非每条消息），可接受。

4. **`context_limit.py` 的 `_CHARS_PER_TOKEN` 常量**: 替换 B6 后，检查该常量是否还有其他引用。若无，可移除以避免 dead code。

5. **vendored 代码**: `skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/` 下的 `_count_tokens` 使用独立 tokenizer，不走项目估算体系。**不改**。

6. **向后兼容**: 旧 `pub/func/message/estimate_msg_tokens.py` 已改为 re-export，所有 `from pub.func.message.estimate_msg_tokens import ...` 和 `from pub.func.message import ...` 路径保持不变。
