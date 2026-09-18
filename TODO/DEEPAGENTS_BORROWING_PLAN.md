# DeepAgents 值得借鉴的防护能力 — 具体实现方案

> 基于 `PROTECTION_COMPARISON.md` 对比报告，筛选 DeepAgents 中 Sherry 可落地的防护能力，给出具体实现方案。
> 优先级：P0(安全关键，建议立即实施) → P1(增强体验) → P2(长期优化)
>
> **P0 处置记录（2026-09-17）**：P0-1 已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow`/`_raise_if_symlink_loop`，read/write/patch 全走，另见 `docs/sandbox/README*`）；P0-3 **已废弃**（base64+eval 无安全增益，且会削弱现有危险命令/敏感文件防线；terminal 本就以 shell 语义执行）；P0-4 已落地（配置键 `file_tools_search_max_matches`/`file_tools_search_time_budget_s`/`file_tools_search_prune_dirs` + `search_scan.py` 的 `bounded_walk`，提交 `6073f7c`/`c77846e`/`4db42e0`）。
>
> **P0-2 处置记录（2026-09-17）**：经对比 opencode-dev、oh-my-openagent、openclaw、hermes-agent 四个项目，全部都在工具执行时将大结果驱逐到文件。方案采用 DeepAgents 的 `wrap_tool_call` 拦截策略——大内容从不进入 state，在工具返回后立即写文件并替换为 head+tail 预览。
>
> **P2-4 处置记录（2026-09-17）**：依赖 P0-2。在 `wrap_tool_call` 中对 `name == "read_file"` 的结果走切片路径（不写文件，文件已在磁盘），与 `target_truncation.py` 的压缩时切片互补。
>
> **P0-2 落地记录（2026-09-18）**：**已落地** —— `agent/middlewares/tool_result_eviction/`（`wrap_tool_call` / `awrap_tool_call` 拦截）+ `pub/func/message/eviction.py`（`evict_tool_result` 等纯函数）+ `config/features/agent_side/tool_result_eviction.py`（TypedDict + 实例）。参数：阈值 `evict_threshold_chars=20_000`、预览 head+tail 各 5 行、`eviction_subdir="evicted"`（`SESSIONS_DIR/{session_id}/evicted/`）、`excluded_tools` 8 项（`read_file` / `write_file` / `patch_file` / `search_files` / `list_files` / `memory` / `skill_view` / `skill_list`）。提交 `4a260c7`（实现）/ `565fc13`（测试）/ `466c7d3`（中间件文档）/ `06f8d77`（导出清单）。
>
> **P2-4 落地记录（2026-09-18）**：**已落地** —— `read_file` 在 `wrap_tool_call` 中走**切片**路径（`model_copy` 换内容，不写新文件——文件已在磁盘），与压缩期 `target_truncation.py` 的可找回切片**互补**；同批提交 `4a260c7` / `565fc13` / `466c7d3` / `06f8d77`。
>
> **P1-2 落地记录（2026-09-18）**：**已落地** —— `pub/func/message/overflow_clip.py`（`clip_overflow_tail`）+ `agent/middlewares/summarization/core.py` 在 **route 与 forced recovery 前置**快速尾部裁剪；`SUMMARIZATION` 新增 `overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep` 三键（46→49）。**修正了原方案的孤儿 ToolMessage 做法**：不再删除尾部批次、不再注入 `_overflow_clip` 消息，改为经 `ToolMessage.model_copy` 保住消息身份（`id` / `tool_call_id` / `name` / `additional_kwargs`）的**内容 stub**，从而保留 P0-2 驱逐指针与 P2-4 切片提示，维持配对净化与持久水位。够用即短路（不调 LLM、不进任何 route，仅接受单独达标者）；不够则整份丢弃，原列表照走既有压缩路径。提交 `aa327fa`（中间件集成）/ `dcf8ece`（纯函数）/ `f83fe20`（配置）。
>
> **P1-1 落地记录（2026-09-18）：能力已存在，原方案不采用。** 工具调用参数截断早已落地于 `pub/func/message/tool_args_truncate.py::truncate_tool_args`（head+tail + `...[args truncated, omitted N chars]...`，与 `target_truncation.py` 风格一致）；配置键 `SUMMARIZATION["max_tool_args_chars"] / ["min_args_chars_to_truncate"]`（后者 500）；接入 `agent/middlewares/summarization/core.py` 两处（预算截断 `:848`、非 LLM 策略链 `:1778`），另有 `summarization/summarization_components.py` 的 aggressive 兜底；"多少轮之后才截断"由 `find_truncatable_tool_results` 的 `skip_recent`（`TRUNCATABLE_RECENT_SKIP`）与 `protected_tools` 精确表达，测试见 `tests/pub/func/message/test_pub_func_message_tools.py`。计划书的 `ToolArgsTruncator` 类未采用，且其样例代码有两处缺陷：①`str(v)[:max] + marker` 会把 `args` 的值降级为字符串，破坏 LangChain `ToolCall.args` 为 dict 的契约（部分 provider 适配器会失败）——现状写入 `{"_truncated_args": "head…omitted…tail"}`，保持 dict 且可 JSON 序列化；②重建 `AIMessage(content=…, tool_calls=…)` 会丢 `id`/`metadata`/`additional_kwargs`（会连带破坏持久水位与 tool-call 配对）——现状用 `msg.model_copy(update={"tool_calls": …})` 保留消息身份。
>
> **未执行项清单（2026-09-18）**：以下条目**仍由本文件跟踪、尚未执行**（本文件曾于 `257879c` 被误删，已由 `a750fdf` 恢复并**保持活跃**，不再退休）：
>
> - P1-1 参数截断 —— **已落地**（能力已存在，见上方 P1-1 落地记录），无需另做
> - P1-9 人类消息驱逐（超大 HumanMessage 的上下文治理）—— **未执行**，详细实现规划见 `## P1-9`（2026-09-18 新增，来源：对比报告第三批复核）
> - P1-3 模型感知摘要默认值（`compute_summarization_defaults`）
> - P1-4 增量检查点优化（`DeltaChannel`）
> - P1-5 消息增量缩减器（去重 + 墓碑）
> - P1-6 中间件脚手架保护（`_REQUIRED_MIDDLEWARE`）
> - P1-7 多模态内容清理（`_scrub_unsupported_multimodal_content`）
> - P1-8 威胁模型文档（`docs/THREAT_MODEL.md`）
> - P2-1 ripgrep 双重超时看门狗（`_reap_ripgrep`）
> - P2-2 持久化工具审批策略（字节修订 CAS）
> - P2-3 伪文件系统修剪 —— **已由 P0-4 覆盖**（配置键 `file_tools_search_prune_dirs` + `search_scan.py::bounded_walk`），无需另做

---

## 目录

1. [P0-2：工具结果消息驱逐 (wrap_tool_call 主动驱逐 + head/tail预览)](#p0-2工具结果消息驱逐)
2. [P1-1：参数截断 (TruncateArgsSettings)](#p1-1参数截断)
3. [P1-2：溢出尾部裁剪 (快速恢复路径)](#p1-2溢出尾部裁剪)
4. [P1-3：模型感知摘要默认值](#p1-3模型感知摘要默认值)
5. [P1-4：增量检查点优化 (DeltaChannel)](#p1-4增量检查点优化)
6. [P1-5：消息增量缩减器 (去重+墓碑)](#p1-5消息增量缩减器)
7. [P1-6：中间件脚手架保护](#p1-6中间件脚手架保护)
8. [P1-7：多模态内容清理](#p1-7多模态内容清理)
9. [P1-8：威胁模型文档](#p1-8威胁模型文档)
10. [P1-9：人类消息驱逐（超大 HumanMessage 的上下文治理）](#p1-9人类消息驱逐超大-humanmessage-的上下文治理)
11. [P2-1：ripgrep 双重超时看门狗](#p2-1ripgrep-双重超时看门狗)
12. [P2-2：持久化工具审批策略 (字节修订 CAS)](#p2-2持久化工具审批策略-字节修订-cas)
13. [P2-3：伪文件系统修剪](#p2-3伪文件系统修剪)
14. [P2-4：read_file 结果切片](#p2-4read_file-结果切片)
15. [实施优先级与依赖关系总览](#实施优先级与依赖关系总览)

---

## P0-2：工具结果消息驱逐

### 问题

Sherry 的工具结果**完整进入 state**，超大输出（如 terminal 50K 字符、search 全量匹配）占据大量上下文。现有截断全部发生在**压缩流程内**（summarization 的 `target_truncate_tool_outputs` / `truncate_to_budget`），是被动截断——大内容已经污染了 state 和前缀缓存，截断后内容永久丢失。

横向对比 opencode-dev、oh-my-openagent、openclaw、hermes-agent 四个项目，**全部都在工具执行时（非压缩时）将大结果驱逐到文件**，只放预览到上下文。

### DeepAgents 做法

`FilesystemMiddleware.wrap_tool_call`（`filesystem.py:3605-3654`）在工具返回后、消息进入 state **之前**拦截：

1. 工具在 `TOOLS_EXCLUDED_FROM_EVICTION` 列表中 → 不驱逐（结果本就在后端文件系统）
2. `content_str = _extract_text_from_message(message)` — 提取文本
3. `len(content_str) <= threshold` → 不驱逐
4. 否则 → `_offload_tool_message_content()`：写文件 + 替换为 head+tail 预览 + 文件路径引用

**关键设计**：大内容**从不进入 state**，进入 state 的从一开始就是预览。因此不破坏前缀缓存、不增加 checkpointer 体积、不触发后续截断。

非文本块（图片/音频）通过 `_build_evicted_content` 保留，只替换文本部分。

### 具体实现方案

#### 文件清单

| 文件                                                 | 修改类型 | 说明                                                                        |
| ---------------------------------------------------- | -------- | --------------------------------------------------------------------------- |
| `agent/middlewares/tool_result_eviction/__init__.py` | 新建     | 导出中间件                                                                  |
| `agent/middlewares/tool_result_eviction/core.py`     | 新建     | `wrap_tool_call` / `awrap_tool_call` 实现                                   |
| `pub/func/message/eviction.py`                       | 新建     | 纯函数：`evict_tool_result` / `load_evicted` / `build_preview`              |
| `config/features/agent_side/tool_result_eviction.py` | 新建     | 配置 TypedDict + 实例                                                       |
| `config/features/agent_side/__init__.py`             | 修改     | 导出新配置                                                                  |
| `config/features/__init__.py`                        | 修改     | 导出新配置                                                                  |
| `agent/core.py`                                      | 修改     | 中间件列表中插入 `ToolResultEvictionMiddleware`（在 `ToolGuardrails` 之后） |
| `agent/middlewares/__init__.py`                      | 修改     | 导出新中间件                                                                |

#### 配置设计

**`config/features/agent_side/tool_result_eviction.py`**:

```python
from typing import TypedDict


class ToolResultEvictionConfig(TypedDict):
    """工具结果消息驱逐配置。"""

    enabled: bool
    # 超过此字符数的工具结果将被驱逐到文件系统（~5000 tokens）
    evict_threshold_chars: int
    # 预览头尾行数
    preview_head_lines: int
    preview_tail_lines: int
    # 驱逐文件存储子目录（相对于 SESSIONS_DIR/{session_id}/）
    eviction_subdir: str
    # 不驱逐的工具（结果本就在后端文件系统）
    excluded_tools: frozenset[str]


TOOL_RESULT_EVICTION: ToolResultEvictionConfig = {
    "enabled": True,
    "evict_threshold_chars": 20_000,
    "preview_head_lines": 5,
    "preview_tail_lines": 5,
    "eviction_subdir": "evicted",
    "excluded_tools": frozenset({
        "read_file",       # 文件已在磁盘，用 offset/limit 恢复
        "write_file",
        "patch_file",
        "search_files",
        "list_files",
        "memory",
        "skill_view",
        "skill_list",
    }),
}
```

#### 核心纯函数

**`pub/func/message/eviction.py`**:

```python
"""工具结果消息驱逐纯函数。

将超大工具结果卸载到文件系统，替换为 head+tail 预览 + 文件路径引用。
模型可按需用 read_file(offset, limit) 重新读取完整内容。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from langchain_core.messages import ToolMessage

from config import SESSIONS_DIR
from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION

_PREVIEW_TEMPLATE = """\
[evicted to: {path}]
--- head ({head_n} lines) ---
{head}
{midline}
--- tail ({tail_n} lines) ---
{tail}
[full content: {total_chars} chars, evicted at {ts}]

Use read_file(file_path='{path}', offset=0, limit=100) to read the full content in chunks.]"""


def get_eviction_dir(session_id: str) -> Path:
    """返回 session 的驱逐目录，按需创建。"""
    d = Path(SESSIONS_DIR) / session_id / TOOL_RESULT_EVICTION["eviction_subdir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def evict_tool_result(msg: ToolMessage, session_id: str) -> ToolMessage | None:
    """驱逐超大工具结果到文件系统，返回替换后的预览消息。

    返回 None 表示未达阈值或被排除，调用方应保留原消息。
    """
    threshold = TOOL_RESULT_EVICTION["evict_threshold_chars"]
    excluded = TOOL_RESULT_EVICTION["excluded_tools"]

    if (getattr(msg, "name", "") or "") in excluded:
        return None

    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    if len(content) <= threshold:
        return None

    eviction_dir = get_eviction_dir(session_id)
    tc_id = getattr(msg, "tool_call_id", "") or "unknown"
    key = f"{tc_id}_{hashlib.md5(content.encode()).hexdigest()[:8]}"
    file_path = eviction_dir / f"{key}.txt"
    file_path.write_text(content, encoding="utf-8")

    preview = _build_preview(content, file_path)
    return ToolMessage(
        content=preview,
        tool_call_id=msg.tool_call_id,
        name=msg.name,
        id=msg.id,
        status=getattr(msg, "status", "success"),
    )


def _build_preview(content: str, file_path: Path) -> str:
    lines = content.splitlines()
    head_n = TOOL_RESULT_EVICTION["preview_head_lines"]
    tail_n = TOOL_RESULT_EVICTION["preview_tail_lines"]
    head = "\n".join(lines[:head_n])
    tail = "\n".join(lines[-tail_n:])
    midline = "..." if len(lines) > head_n + tail_n else ""
    return _PREVIEW_TEMPLATE.format(
        path=str(file_path), head_n=min(head_n, len(lines)),
        tail_n=min(tail_n, len(lines)), head=head, midline=midline,
        tail=tail, total_chars=len(content),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
```

#### 中间件实现

**`agent/middlewares/tool_result_eviction/core.py`**:

```python
"""工具结果驱逐中间件。

在 wrap_tool_call 中拦截超大工具结果，写入文件系统后替换为 head+tail 预览。
大内容从不进入 state，因此不破坏前缀缓存。

中间件链位置：在 ToolGuardrails 之后、ToolCallNormalize 之前。
- ToolGuardrails 负责安全预检，结果可能被阻止
- 本中间件只处理"安全且过大"的结果
"""

from __future__ import annotations

from typing import override

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from loguru import logger

from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION
from pub.func.message.eviction import evict_tool_result


class ToolResultEvictionMiddleware(AgentMiddleware):
    """驱逐超大工具结果到文件系统。"""

    def __init__(self) -> None:
        self._enabled = TOOL_RESULT_EVICTION["enabled"]

    @override
    def wrap_tool_call(self, request: ToolCallRequest, handler) -> ToolMessage:
        result = handler(request)
        return self._maybe_evict(result, request)

    @override
    async def awrap_tool_call(self, request: ToolCallRequest, handler) -> ToolMessage:
        result = await handler(request)
        return self._maybe_evict(result, request)

    def _maybe_evict(self, result: ToolMessage, request: ToolCallRequest) -> ToolMessage:
        if not self._enabled:
            return result
        session_id = self._get_session_id(request)
        if not session_id:
            return result
        evicted = evict_tool_result(result, session_id)
        if evicted is not None:
            logger.debug(
                "Evicted tool result ({} chars → file), tool={}, session={}",
                len(str(result.content)), getattr(result, "name", "?"), session_id,
            )
            return evicted
        return result

    @staticmethod
    def _get_session_id(request: ToolCallRequest) -> str:
        ctx = getattr(request, "context", None) or {}
        return ctx.get("session_id", "")
```

> **注意**：`ToolCallRequest` 的 context 字段名需根据 LangChain 1.3.9 实际 API 确认。备选：通过 `request.runtime.state` 获取 StateSchema 中的 `session_id`。

#### 中间件注册

在 `agent/core.py` 的中间件列表中，将 `ToolResultEvictionMiddleware` 插入到 `ToolGuardrails` **之后**：

```python
middlewares = [
    TodoContinuationEnforcer(),           # FIRST
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(),
    ToolGuardrails(),
    ToolResultEvictionMiddleware(),       # ← 新增
    ToolCallNormalize(),
    # ...
    Summarization(),                     # LAST
]
```

#### Session 删除时自动清理

驱逐文件存储在 `SESSIONS_DIR/{session_id}/evicted/` 下。`clear_session()`（`server/DAO/messages.py:44-47`）的 `shutil.rmtree` 会自动清理整个 session 目录，**无需修改**。

#### 缓存安全

大内容**从不进入 state**，进入 state 的从一开始就是预览。因此：

- 不破坏前缀缓存（大内容从未在 prefix 中）
- 不增加 checkpointer 体积（预览远小于完整内容）
- 不触发后续截断（预览已在阈值内）

#### 与压缩流程的协同

```
工具返回 → wrap_tool_call 驱逐(写文件+预览) → state 中只有预览
  → 上下文压力降低 → 压缩触发频率下降
  → 即使触发压缩，截断的是预览(小体积)而非原始内容
```

#### 与 P1-2 溢出尾部裁剪的协同

```
正常流程:
  工具结果 > 20K chars → P0-2 驱逐(写文件+预览) → state 中保持小体积

溢出恢复:
  ContextOverflowError
    → P1-2 尾部裁剪(秒级，裁的是预览不是原始内容)
    → 裁剪不够 → 降级到 T4/T5 完整压缩(调 LLM)
```

---

## P1-1：参数截断

### 问题

Sherry 在压缩上下文时截断的是完整的消息，但旧工具调用中的大型参数（如传入的大段代码或文件内容）仍占用空间。

### DeepAgents 做法

`TruncateArgsSettings` 在摘要前截断旧消息中的大型工具调用参数。

### 具体实现方案

#### 文件清单

| 文件                                                          | 修改类型 | 说明           |
| ------------------------------------------------------------- | -------- | -------------- |
| `agent/middlewares/summarization/summarization_components.py` | 修改     | 添加参数截断器 |
| `config/features/agent_side/summarization.py`                 | 修改     | 添加截断配置   |

#### 配置新增

在 `SUMMARIZATION` TypedDict 中添加：

```python
truncate_args_enabled: bool          # 是否启用参数截断
truncate_args_max_chars: int         # 参数最大字符数，默认 2000
truncate_args_after_turns: int       # 多少轮后开始截断旧参数，默认 5
truncate_args_marker: str            # 截断标记
```

#### 实现代码

在 `summarization_components.py` 中新增：

```python
class ToolArgsTruncator:
    """截断旧消息中的大型工具调用参数。"""

    def __init__(self) -> None:
        self._max_chars = SUMMARIZATION["truncate_args_max_chars"]
        self._after_turns = SUMMARIZATION["truncate_args_after_turns"]
        self._marker = SUMMARIZATION["truncate_args_marker"]
        self._enabled = SUMMARIZATION["truncate_args_enabled"]

    def truncate(self, messages: list, current_turn: int) -> list:
        if not self._enabled:
            return messages
        if current_turn < self._after_turns:
            return messages

        result = []
        for msg in messages:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                new_calls = []
                for tc in msg.tool_calls:
                    args = tc.get("args", {})
                    truncated_args = self._truncate_args(args)
                    new_calls.append({**tc, "args": truncated_args})
                result.append(AIMessage(content=msg.content, tool_calls=new_calls))
            else:
                result.append(msg)
        return result

    def _truncate_args(self, args: dict) -> dict:
        result = {}
        for k, v in args.items():
            s = str(v)
            if len(s) > self._max_chars:
                result[k] = s[: self._max_chars] + self._marker
            else:
                result[k] = v
        return result
```

---

## P1-2：溢出尾部裁剪

### 问题

Sherry 每次遇到 ContextOverflowError 都走完整压缩管线（T4/T5 → 调 LLM 摘要 → 截断 → 重试），耗时且费 token。实际上很多溢出只需移除尾部几条大型 ToolMessage 即可恢复，无需调用 LLM。

### DeepAgents 做法

`_clip_overflow_tail()` 在捕获 `ContextOverflowError` 后，直接从消息列表尾部移除连续的 ToolMessage 批次，直到请求降到窗口以内，立即重试。不调 LLM，秒级恢复。只有裁剪不够时才降级到完整摘要。

### 具体实现方案

#### 文件清单

| 文件                                          | 修改类型 | 说明                                |
| --------------------------------------------- | -------- | ----------------------------------- |
| `agent/middlewares/summarization/core.py`     | 修改     | 在 T4/T5 恢复路径前插入快速裁剪路径 |
| `agent/middlewares/overflow_clip.py`          | 新建     | 尾部裁剪实现                        |
| `config/features/agent_side/summarization.py` | 修改     | 添加裁剪配置                        |

#### 配置新增

在 `SUMMARIZATION` TypedDict 中添加：

```python
overflow_clip_enabled: bool          # 是否启用快速裁剪路径，默认 True
overflow_clip_max_remove: int        # 单次最多移除尾部消息数，默认 10
overflow_clip_min_keep: int          # 至少保留的消息数，默认 5
```

#### 实现代码

**`agent/middlewares/overflow_clip.py`**:

```python
"""溢出尾部裁剪快速恢复。

在 ContextOverflowError 后，直接移除尾部连续的 ToolMessage 批次，
秒级重试，避免走完整 LLM 摘要管线。
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, ToolMessage

from config.features.agent_side.summarization import SUMMARIZATION


def clip_overflow_tail(
    messages: list[BaseMessage],
    overflow_chars: int,
) -> list[BaseMessage] | None:
    """从尾部移除连续 ToolMessage 批次，降低上下文体积。

    Args:
        messages: 当前消息列表
        overflow_chars: 需要移除的大致字符数

    Returns:
        裁剪后的消息列表；如果无法裁剪（保留数不足）则返回 None，
        调用方应降级到完整摘要路径。
    """
    if not SUMMARIZATION["overflow_clip_enabled"]:
        return None

    max_remove = SUMMARIZATION["overflow_clip_max_remove"]
    min_keep = SUMMARIZATION["overflow_clip_min_keep"]

    if len(messages) <= min_keep:
        return None

    # 从尾部开始收集连续的 ToolMessage
    remove_indices: list[int] = []
    removed_chars = 0
    i = len(messages) - 1
    while i >= 0 and len(remove_indices) < max_remove and removed_chars < overflow_chars:
        msg = messages[i]
        if isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            remove_indices.append(i)
            removed_chars += len(content)
            i -= 1
        else:
            break  # 遇到非 ToolMessage，停止

    if not remove_indices:
        return None

    # 检查保留数
    remaining = len(messages) - len(remove_indices)
    if remaining < min_keep:
        return None

    remove_set = set(remove_indices)
    result = [msg for idx, msg in enumerate(messages) if idx not in remove_set]

    # 注入裁剪通知
    clip_notice = (
        f"[overflow-clip] removed {len(remove_indices)} trailing ToolMessage(s) "
        f"({removed_chars} chars) to recover from ContextOverflowError. "
        f"Full content available via eviction files if P0-2 is enabled."
    )
    result.append(ToolMessage(
        content=clip_notice,
        tool_call_id="_overflow_clip",
    ))
    return result
```

#### 集成方式

在 `agent/middlewares/summarization/core.py` 的 T4/T5 溢出恢复路径中，**先尝试快速裁剪**，裁剪不够才降级到完整压缩：

```python
# summarization/core.py 中 T4/T5 恢复逻辑修改:

def _handle_overflow(self, state, error_chars):
    """溢出恢复：先快速裁剪，再降级到完整压缩。"""
    from agent.middlewares.overflow_clip import clip_overflow_tail

    # 快速路径：尾部裁剪
    clipped = clip_overflow_tail(state["messages"], error_chars)
    if clipped is not None:
        return {"messages": clipped}

    # 慢速路径：完整 LLM 压缩（原有逻辑）
    return self._apply_compression(state)
```

#### 与 P0-2 的协同

```
正常流程:
  工具结果 > 20K chars → P0-2 驱逐(写文件+预览) → state 中保持小体积

溢出恢复:
  ContextOverflowError
    → P1-2 尾部裁剪(秒级，裁的是预览不是原始内容)
    → 裁剪不够 → 降级到 T4/T5 完整压缩(调 LLM)
```

---

## P1-3：模型感知摘要默认值

### 问题

Sherry 的摘要触发阈值需要手动配置，不同模型（128K vs 32K）需要不同阈值，容易配错。

### DeepAgents 做法

`compute_summarization_defaults()` 从模型 profile 的 `max_input_tokens` 自动计算触发(85%)和保留(10%)阈值。

### 具体实现方案

#### 文件清单

| 文件                                          | 修改类型 | 说明             |
| --------------------------------------------- | -------- | ---------------- |
| `config/features/agent_side/summarization.py` | 修改     | 添加自动计算函数 |

#### 实现代码

```python
def compute_summarization_defaults(max_input_tokens: int) -> dict:
    """根据模型 max_input_tokens 自动计算摘要阈值。

    Args:
        max_input_tokens: 模型的最大输入 token 数

    Returns:
        包含 trigger_threshold, keep_threshold, max_output_budget 的字典
    """
    trigger = int(max_input_tokens * 0.85)
    keep = int(max_input_tokens * 0.10)
    # 预留输出 token + 5% 余量
    output_budget = int(max_input_tokens * 0.15)
    return {
        "trigger_threshold": trigger,
        "keep_threshold": keep,
        "max_output_budget": output_budget,
    }
```

在 `agent/core.py` 或 `server/__main__.py` 启动时，从 `MAIN_LLM` 的 model profile 获取 `max_input_tokens`，调用此函数设置 `SUMMARIZATION` 的默认值。

---

## P1-4：增量检查点优化

### 问题

Sherry 使用 LangGraph 的默认 MessagesState，长对话中检查点增长为 O(N²)（每轮保存全部消息的快照），导致 SQLite 膨胀和恢复变慢。

### DeepAgents 做法

`DeltaChannel(snapshot_frequency=50)` 每50条消息才做一次全量快照，中间只存增量，将增长降为 O(N)。

### 具体实现方案

#### 文件清单

| 文件                                    | 修改类型 | 说明              |
| --------------------------------------- | -------- | ----------------- |
| `agent/core.py`                         | 修改     | 替换状态 schema   |
| `agent/middlewares/checkpoint_delta.py` | 新建     | DeltaChannel 实现 |

#### 注意事项

- 此方案需要深入 LangGraph 的 reducer 机制
- 需确保 Sherry 的 `CompactionLock` 和 checkpointer 兼容增量格式
- 建议先做 P0 项，此为长期优化
- 可直接参考 DeepAgents 的 `_messages_reducer.py` 实现

---

## P1-5：消息增量缩减器

### 问题

Sherry 的消息列表可能因子代理完成消息重复注入或压缩后残留而产生重复消息。

### DeepAgents 做法

`_messages_delta_reducer()` 支持 ID 去重、`RemoveMessage` 墓碑和全量重置。

### 具体实现方案

#### 文件清单

| 文件            | 修改类型 | 说明                    |
| --------------- | -------- | ----------------------- |
| `agent/core.py` | 修改     | 自定义 messages reducer |

#### 实现代码

```python
from langchain_core.messages import RemoveMessage

def messages_delta_reducer(existing: list, update: list) -> list:
    """支持增量的消息缩减器。

    - RemoveMessage: 从列表中删除对应ID
    - 普通消息: 按ID去重后追加
    - 特殊指令: 全量替换
    """
    if not update:
        return existing

    # 检查是否为全量替换指令
    if len(update) == 1 and isinstance(update[0], dict) and update[0].get("__reset__"):
        return update[0]["messages"]

    # 构建现有消息ID集合
    existing_ids = {getattr(m, "id", None) for m in existing if hasattr(m, "id")}

    # 处理 RemoveMessage
    to_remove: set[str] = set()
    to_add: list = []
    for msg in update:
        if isinstance(msg, RemoveMessage):
            to_remove.add(msg.id)
        else:
            mid = getattr(msg, "id", None)
            if mid and mid in existing_ids:
                continue  # 去重
            if mid:
                existing_ids.add(mid)
            to_add.append(msg)

    # 过滤已移除的消息
    filtered = [m for m in existing if getattr(m, "id", None) not in to_remove]
    filtered.extend(to_add)
    return filtered
```

---

## P1-6：中间件脚手架保护

### 问题

Sherry 的中间件链没有"必需不可排除"的机制，配置错误可能导致安全中间件被跳过。

### DeepAgents 做法

`_REQUIRED_MIDDLEWARE` 列表标记不可排除的中间件，`_validate_excluded_middleware_config()` 在启动时验证。

### 具体实现方案

#### 文件清单

| 文件                            | 修改类型 | 说明               |
| ------------------------------- | -------- | ------------------ |
| `agent/middlewares/__init__.py` | 修改     | 添加必需中间件标记 |

#### 实现代码

```python
# agent/middlewares/__init__.py

_REQUIRED_MIDDLEWARE = {
    "ToolGuardrails",
    "HITLCore",
    "Summarization",
    "IterationBudget",
}

def validate_required_middleware(active_middleware: list[str]) -> None:
    """启动时验证所有必需中间件已加载。"""
    missing = _REQUIRED_MIDDLEWARE - set(active_middleware)
    if missing:
        raise RuntimeError(
            f"Required middleware missing: {missing}. "
            f"Cannot start agent without safety scaffolding."
        )
```

在 `agent/core.py` 的 `built_agent()` 中调用 `validate_required_middleware()`。

---

## P1-7：多模态内容清理

### 问题

当模型不支持某些内容类型（如视频帧），Sherry 没有清理机制，可能导致 API 调用失败。

### DeepAgents 做法

`_scrub_unsupported_multimodal_content()` 替换模型不支持的内容块为文本占位符。

### 具体实现方案

#### 文件清单

| 文件                                    | 修改类型 | 说明           |
| --------------------------------------- | -------- | -------------- |
| `agent/middlewares/multimodal_scrub.py` | 新建     | 多模态内容清理 |

#### 实现代码

```python
from langchain_core.messages import HumanMessage

def scrub_unsupported_content(
    messages: list,
    supported_types: set[str],
) -> list:
    """替换模型不支持的多模态内容块为文本占位符。"""
    result = []
    for msg in messages:
        if not isinstance(msg, HumanMessage):
            result.append(msg)
            continue
        if not isinstance(msg.content, list):
            result.append(msg)
            continue
        new_content = []
        for block in msg.content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type and block_type in supported_types:
                new_content.append(block)
            else:
                new_content.append({
                    "type": "text",
                    "text": f"[unsupported content type: {block_type}]",
                })
        result.append(HumanMessage(content=new_content))
    return result
```

---

## P1-8：威胁模型文档

### 问题

Sherry 缺少威胁模型文档，安全评审缺少系统性参考。

### DeepAgents 做法

`THREAT_MODEL.md` 文档化信任边界、数据分类和威胁分析。

### 具体实现方案

#### 文件清单

| 文件                   | 修改类型 | 说明         |
| ---------------------- | -------- | ------------ |
| `docs/THREAT_MODEL.md` | 新建     | 威胁模型文档 |

#### 文档结构

```markdown
# Sherry Agent 威胁模型

## 信任边界

1. 用户 ↔ WS 网关
2. 主代理 ↔ 子代理
3. 工具 ↔ 外部系统（终端/文件/网络）
4. LLM 提供商 ↔ Agent
5. MCP 服务器 ↔ Agent

## 数据分类

| 类别 | 示例             | 存储位置                                        |
| ---- | ---------------- | ----------------------------------------------- |
| 敏感 | API keys, tokens | env vars (scrub_env)                            |
| 私密 | 对话历史         | SQLite (WAL)                                    |
| 内部 | 工具结果         | 消息列表 + 驱逐文件（`sessions/{id}/evicted/`） |

## 威胁分析

| 威胁         | 现有防护                  | 差距                                                                                                                                                                                             |
| ------------ | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 路径遍历     | 三道结构门禁 + O_NOFOLLOW | 已落地（`path_utils.py`）                                                                                                                                                                        |
| 符号链接攻击 | `O_NOFOLLOW` + 循环检测   | 已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow` / `_raise_if_symlink_loop`，read/write/patch 全走）                                                                            |
| Shell注入    | 正则黑名单                | 已评估并否决（base64+`eval` 对以 shell 语义执行的 `terminal` 无增益，且置于 `_check_dangerous`/`_check_sensitive_file_access` 之前会让明文绕过防线；真实读屏障是 OS 沙箱读遮蔽。见头部处置记录） |
| 工具结果OOM  | 截断 (head+tail)          | 压缩时截断已落地（`target_truncation.py`等）；**工具执行时驱逐待实施 P0-2**（`wrap_tool_call` 主动写文件+预览，见头部处置记录）                                                                  |
| ...          | ...                       | ...                                                                                                                                                                                              |
```

---

## P1-9：人类消息驱逐（超大 HumanMessage 的上下文治理）

> **状态（2026-09-18）：未执行，本节为详细实现规划。** 来源：对比报告第三批复核（`TODO/PROTECTION_COMPARISON.md` §2.4「人类消息驱逐」行，Sherry ❌）。**借用计划此前未覆盖此项**，本节补全。

### 问题

用户单条消息可能携带超大纯文本（粘贴的日志/文档/代码/长对话导出）。`MultimodalProcessor` 只治理**媒体附件**，超长纯文本会**永久占据 state 与每次模型调用**：压缩触发前它一直全量在上下文里；即使触发压缩，`_determine_cutoff` 的保留策略也倾向于保留最近的用户消息。DeepAgents 的 `FilesystemMiddleware` 对此有专门机制（对比报告判 Sherry ❌、DeepAgents ✅ `human_message_token_limit_before_evict`）。

### DeepAgents 做法（本地检出核实，`libs/deepagents/deepagents/middleware/filesystem.py`）

| 环节 | 机制 | 位置 |
|---|---|---|
| 配置 | `human_message_token_limit_before_evict = 50000`（token），经 `NUM_CHARS_PER_TOKEN` 折算字符阈值 | `:1755`、`:3373` |
| 触发 | **仅检查最后一条消息**：必须是 `HumanMessage`、未带 `lc_evicted_to`、文本超阈值；`None` 则整条路径关闭 | `_check_eviction_needed` `:3364-3385` |
| 落盘 | 全文写入 **backend 文件系统**（与 `write_file` 同一后端） | `_evict_and_truncate_messages` `:3432` |
| state 打标 | `model_copy` **保 id**、写 `additional_kwargs["lc_evicted_to"]=file_path`，经 `Command(update={"messages":[tagged]})` **按 id 原地替换**；**依赖 `ensure_message_ids` + `DeltaChannel` reducer 的 id 去重**，刻意不用 `REMOVE_ALL_MESSAGES` 哨兵（会连带清掉同轮模型写入的 AIMessage） | `_apply_eviction_and_truncate` `:3394-3425` |
| 模型视图 | 每次模型调用前，凡带标记的 HumanMessage 换成 `TOO_LARGE_HUMAN_MSG`（文件路径 + `_create_content_preview` head/tail 预览），`model_copy` **保 id** | `_build_truncated_human_message` `:1657` |
| 多模态 | 只把**文本块**换成通知文本，媒体块原样保留 | `_build_evicted_human_content` `:1632` |

**关键取舍**：**全文仍留在 state/checkpoint**，只截"模型视图"——与它自己的工具结果驱逐（"大内容从不进 state"）是**两套相反策略**。

### 具体实现方案（Sherry 口径）

#### 设计决策（为何不照抄，也不复用工具侧三态）

1. **采用"state 全文 + 模型视图截断"（DeepAgents 取舍），而非 Sherry 工具侧的"state=预览"三态**。理由：
   - 人类消息的持久化发生在**首个 `after_model` 边界**（`MessagePersistenceMiddleware`）。若在 state 里换成预览，MesMemory 只能拿到预览 → **有损归档**（`message_search` 搜不到原文）；保持 state 全文 ⇒ MesMemory 全文 ✓、压缩管线与 `_determine_cutoff` 看到全文 ✓、`read_file` 可取回 ✓。
   - **不需要 `DeltaChannel`（即不依赖 P1-4）**：打标用「同 `id` 的 `model_copy` 经 `add_messages` reducer 原地更新」——LangGraph 标准 reducer 本就按 id 去重，无需 `REMOVE_ALL_MESSAGES` 哨兵（哨兵会连带清掉同轮的 AIMessage，且会重写全列表、破坏前缀缓存）。
2. **与工具驱逐共用同一套目录与原语**：`SESSIONS_DIR/<session_id>/evicted/`、`eviction.py` 的 `get_eviction_dir` / `_extract_text` / `_build_evicted_content`（多模态保留）/ `build_preview` / `load_evicted`；模型经 `read_file(file_path=…)` 取回。
3. **自愈（优于 DeepAgents）**：state 保留全文 ⇒ 若重启后发现"有标记、文件丢失"，`wrap_model_call` 时从 state 内容**重写文件**。DeepAgents 做不到（它的全文不在 state）。

#### 命名（二选一，建议 A）

- **A（推荐）**：`tool_result_eviction/` **改名 `context_eviction/`**（类名 `ContextEvictionMiddleware`），同一中间件持两组钩子（`wrap_tool_call` 工具结果 + `before_model` 打标 / `wrap_model_call` 视图截断）——与 DeepAgents 的 `FilesystemMiddleware` 同构，命名准确。改动面：注册、门面、测试、README×4。
- **B**：保留现名，另建 `human_message_eviction/` 包。改动面更小，但"体积治理"职责拆在两处。
（下文按 A 书写；选 B 仅替换文件名。）

#### 文件清单

| 文件 | 修改类型 | 说明 |
| --- | --- | --- |
| `agent/middlewares/context_eviction/core.py` | 修改（原 tool_result_eviction） | 新增 `before_model`/`abefore_model`（打标+落盘）与 `wrap_model_call`/`awrap_model_call`（视图截断） |
| `pub/func/message/eviction.py` | 修改 | 新增 `evict_human_message(msg, session_id)` / `build_human_preview(...)` / `human_eviction_notice(...)`（复用 `_extract_text`/`_build_evicted_content`/`build_preview`） |
| `config/features/agent_side/tool_result_eviction.py` | 修改 | `ToolResultEvictionConfig` 增加 `human_evict_enabled` / `human_evict_threshold_chars` / `human_preview_head_lines` / `human_preview_tail_lines` |
| `agent/core.py`、`agent/middlewares/__init__.py` | 修改 | 改名后的注册与门面导出 |
| `tests/agent/middlewares/context_eviction/` | 修改+新建 | 既有工具用例迁移 + 人类消息新用例 |
| `docs/middlewares`（`agent/middlewares/README{,.zh,.ja,.ko}.md`）、`docs/session_memory/README{4}`、`TODO/PROTECTION_COMPARISON.md` | 修改 | 机制、路径、行为与矩阵行更新 |

#### 配置新增（`ToolResultEvictionConfig`）

```python
human_evict_enabled: bool = True
# 字符阈值；对齐 DeepAgents 默认 50_000 tokens ≈ 200_000 chars（NUM_CHARS_PER_TOKEN=4）
human_evict_threshold_chars: int = 200_000
human_preview_head_lines: int = 5
human_preview_tail_lines: int = 5
```

> 为什么用字符而不用 token：与工具侧 `evict_threshold_chars` 同一口径、避免在热路径再做一次 token 估算；文档注明与 DeepAgents 默认值的换算关系。

#### 实现要点

1. **打标 + 落盘（`before_model` / `abefore_model`，钩子顺序待实测见下）**
   - 门控：`enabled` 且列表非空且**最后一条**是 `HumanMessage`、`additional_kwargs` 无 `lc_evicted_to`、`len(_extract_text(content)) > human_evict_threshold_chars`
   - 动作：`get_eviction_dir(session_id)` 建目录 → 全文写 `human-<msg_id 或 ts>.md` → 返回**部分状态更新** `{"messages": [msg.model_copy(update={"additional_kwargs": {**msg.additional_kwargs, "lc_evicted_to": str(path)}})]}`（**内容不变、id 不变** → reducer 按 id 原地更新）
   - `session_id` 走 `require_session_id`；`is_safe_session_segment` 校验（非法→跳过不写盘）
2. **模型视图截断（`wrap_model_call` / `awrap_model_call`）**
   - 遍历 `request.messages`：凡 `HumanMessage` 且带 `lc_evicted_to` → `model_copy(update={"content": _build_evicted_content(原文, build_preview(...))})`（**预览模板含原路径 + `read_file` 续读提示**，媒体块保留）
   - `request.override(messages=processed)` —— 与 Summarization 既有 idiom 一致
   - **自愈**：文件缺失时从 state 里的原文重写（见设计决策 3）
   - ⚠ **实现前必须验证**：LangChain 1.3.9 的 `wrap_model_call` 能否返回 `Command(update=...)`（DeepAgents 靠它打标）；**若不支持，打标改在 `before_model` 状态更新里做**（如上），`wrap_model_call` 只做视图截断 —— 两个钩子分工与本节一致，无阻塞风险
3. **钩子顺序（实现前实测）**：`before_model` 的执行顺序（列表序 or 逆序）需按 `factory.py` 现行装配确认；目标语义是**打标/落盘必须发生在 `MultimodalProcessor` 对最后一条 HumanMessage 处理之后**（媒体提示已并入文本块），且**早于**任何 `wrap_model_call`。若顺序不符，把"打标+落盘"并入本中间件的 `wrap_model_call` 首段（写文件 + 视图截断一次完成），状态打标经 `Command(update=...)` 或挪到 `after_model`（消息仍是"最后一条"的轮次内）。
4. **幂等与防御**：已有 `lc_evicted_to` → 跳过；`enabled=False` → 全跳过；非文本为主的消息（媒体块占主导）不驱逐；`min_keep` 不适用（人类消息不参与 tool 配对，`_reconcile_denials`/`sanitize_tool_use_result_pairing` 与 HumanMessage 无交互）

#### 与既有机制的交互（全部须有测试锁定）

| 机制 | 影响 | 结论 |
| --- | --- | --- |
| `MessagePersistenceMiddleware` | 打标**不改 content** ⇒ MesMemory 仍落**全文**；`_is_persistable` 只滤 `lc_source=="summarization"`，不滤 `lc_evicted_to` | 全文归档 ✓（测试锁定） |
| 持久化水位 `persisted_message_ids` | 消息 id 不变 ⇒ 水位不受影响 | 不重复落库 ✓ |
| 压缩/`_determine_cutoff`/P1-2 尾部裁剪 | 看到的是 state 全文；若裁剪到被驱逐的 HumanMessage，**stub 化不销毁标记与指针**（同 P1-2 对 P0-2 指针的既有约定） | 可裁、可恢复 ✓ |
| P0-2 工具驱逐 / P2-4 read_file 切片 | 不消费 `lc_evicted_to`；目录共用 | 互不干扰 ✓ |
| 摘要过滤 `_filter_summary_messages` | 只滤 `lc_source=="summarization"` | 不受影响 ✓ |
| `message_search` | MesMemory 有全文 | 可检索 ✓ |
| HITL | HumanMessage 不参与工具配对 | 无交互 ✓ |
| 前缀缓存 | state content 未变（只加 kwargs）；视图截断只影响本次请求 ⇒ 与 Summarization 的 override 同性质 | 可接受，文档注明 |

#### 测试（新建 + 迁移）

- 阈值边界（=阈值不驱逐 / +1 驱逐）、`human_evict_enabled=False`、**非最后一条的 HumanMessage 不触发**、已带标记不重复驱逐
- 打标状态更新：**content 不变、id 不变、kwargs 增加标记**；经 reducer 原地更新（无 `REMOVE_ALL_MESSAGES`）
- 视图截断：预览含路径 + `read_file` 提示；**媒体块保留**；`override` 后模型看到的是预览
- **三态断言（人类版）**：state=全文+标记 / MesMemory=全文 / 磁盘文件 = 原文（`load_evicted` 逐字节一致）
- 自愈：删文件后 `wrap_model_call` 重写
- 幂等（二次不驱逐/不重写——除非文件缺失）
- 非法 session_id 不写盘；`async` 双路径（`abefore_model`/`awrap_model_call`）
- 既有 `tool_result_eviction` 用例在改名后全部迁移并保持绿

#### 执行顺序

1. `eviction.py` 三个纯函数 + 单测
2. 配置四键 + 契约测试（`tests/config`）
3. 中间件改名 + 新钩子（先写"钩子顺序实测"的验证脚本/测试）
4. 迁移既有工具用例 + 新增人类消息用例
5. 门禁：`pytest tests/agent/middlewares tests/pub/func/message tests/config -q` → split runner → basedpyright/ruff/lint-imports
6. 文档：`agent/middlewares/README{4}`、`docs/session_memory/README{4}`、对比报告矩阵行（❌→✅）

---



### 问题

Sherry 的文件搜索如果使用 ripgrep 后端，缺少进程级超时看门狗。

### DeepAgents 做法

`threading.Timer` 看门狗 → SIGTERM → 5s等待 → SIGKILL → 放弃。

### 具体实现方案

参考 DeepAgents 的 `_reap_ripgrep()` 实现，在文件搜索工具中添加双重超时清理。

---

## P2-2：持久化工具审批策略

### 问题

Sherry 的 HITL 审批是会话级的，重启后丢失。无操作员场景缺少自动拒绝。

### DeepAgents 做法

`ToolApprovalStore` JSON 文件持久化 + 字节修订 CAS + 操作员范围 ContextVar + 无操作员自动拒绝。

### 具体实现方案

在中期迭代中实现 `ToolApprovalStore`，将审批策略持久化到 `workspace/.approvals.json`，使用乐观锁 CAS 更新。

---

## P2-3：伪文件系统修剪

### 问题

文件搜索工具在 Linux 上可能遍历 `/proc`、`/sys`、`/dev` 等伪文件系统，导致卡死。

### DeepAgents 做法

`PRUNE_AT_ROOT = ('proc', 'sys', 'dev')` 跳过这些目录。

### 具体实现方案

已实现：配置键 `file_tools_search_prune_dirs`（默认 `["proc","sys","dev"]`）+ `agent/tools/file_tools/search_scan.py` 的 `bounded_walk()` 在扫描时剪枝（`search_files.py` 两模式共用）。

---

## P2-4：read_file 结果切片

### 问题

read_file 工具的结果被驱逐时，不应写文件——文件本身已在后端磁盘上。DeepAgents 对 read_file 特殊处理：只切片不卸载。

### DeepAgents 做法

`_slice_read_file_tm()`（`_overflow_clip.py:76-93`）将 read_file 的 ToolMessage 切片为 ~4k head 字符 + 路径指针，不写入文件。模型可用 `read_file(file_path=..., offset=N, limit=K)` 恢复。

### 具体实现方案

在 P0-2 的 `ToolResultEvictionMiddleware._maybe_evict()` 中，对 `name == "read_file"` 的 ToolMessage 走切片路径而非卸载路径：

```python
# tool_result_eviction/core.py _maybe_evict 修改:

def _maybe_evict(self, result: ToolMessage, request: ToolCallRequest) -> ToolMessage:
    if not self._enabled:
        return result
    session_id = self._get_session_id(request)
    if not session_id:
        return result

    # read_file 特殊处理：文件已在磁盘，切片不写文件
    if getattr(result, "name", "") == "read_file":
        return _slice_read_file_result(result)

    evicted = evict_tool_result(result, session_id)
    return evicted if evicted is not None else result
```

`_slice_read_file_result` 纯函数（`pub/func/message/eviction.py`）：

```python
_READ_FILE_SLICE_CHARS = 4_000

def slice_read_file_result(msg: ToolMessage) -> ToolMessage:
    """切片 read_file 结果，不写文件（文件已在磁盘）。"""
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    if len(content) <= _READ_FILE_SLICE_CHARS:
        return msg
    notice = (
        f"\n\n[Output was truncated due to eviction threshold. "
        f"Use read_file with offset and limit to retrieve specific portions.]"
    )
    return msg.model_copy(update={"content": content[:_READ_FILE_SLICE_CHARS] + notice})
```

> `target_truncation.py` 的 `_truncate_read_file_content` 覆盖压缩时的 read_file 切片，本方案覆盖工具执行时的切片，两者互补。

---

## 实施优先级与依赖关系总览

```
Phase 1 (P0, 安全关键):
  P0-2 工具结果消息驱逐 ──────→ 独立实施（wrap_tool_call 主动驱逐）

Phase 2 (P1, 增强体验):
  P1-1 参数截断 ─────────────→ 依赖现有Summarization
  P1-2 溢出尾部裁剪 ─────────→ 依赖P0-2(协同)，独立可降级到完整压缩
  P1-3 模型感知摘要默认值 ────→ 独立实施
  P1-6 中间件脚手架保护 ──────→ 独立实施
  P1-7 多模态内容清理 ────────→ 独立实施
  P1-8 威胁模型文档 ─────────→ 独立实施

Phase 3 (P1, 长期优化):
  P1-4 增量检查点优化 ────────→ 深入LangGraph reducer，高复杂度
  P1-5 消息增量缩减器 ────────→ 需修改状态schema

Phase 4 (P2, 补充完善):
  P2-1 ripgrep双重超时 ───────→ 依赖文件搜索后端
  P2-2 持久化审批策略 ────────→ 依赖现有HITL
  P2-3 伪文件系统修剪 ────────→ 已实现（`file_tools_search_prune_dirs` + `search_scan.py::bounded_walk`）
  P2-4 read_file切片 ─────────→ 依赖P0-2
```

| Phase             | 工作量估算 | 风险              |
| ----------------- | ---------- | ----------------- |
| Phase 1 (P0)      | ~3-5天     | 低，独立模块      |
| Phase 2 (P1前6项) | ~4-6天     | 低-中，需集成测试 |
| Phase 3 (P1后2项) | ~5-8天     | 高，核心架构变更  |
| Phase 4 (P2)      | ~2-3天     | 低，增量改进      |
