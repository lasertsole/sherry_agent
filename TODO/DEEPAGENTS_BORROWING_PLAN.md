# DeepAgents 值得借鉴的防护能力 — 具体实现方案

> 基于 `PROTECTION_COMPARISON.md` 对比报告，筛选 DeepAgents 中 Sherry 可落地的防护能力，给出具体实现方案。
> 优先级：P0(安全关键，建议立即实施) → P1(增强体验) → P2(长期优化)
>
> **P0 处置记录（2026-09-17）**：P0-1 已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow`/`_raise_if_symlink_loop`，read/write/patch 全走，另见 `docs/sandbox/README*`）；P0-3 **已废弃**（base64+eval 无安全增益，且会削弱现有危险命令/敏感文件防线；terminal 本就以 shell 语义执行）；P0-4 已落地（配置键 `file_tools_search_max_matches`/`file_tools_search_time_budget_s`/`file_tools_search_prune_dirs` + `search_scan.py` 的 `bounded_walk`，提交 `6073f7c`/`c77846e`/`4db42e0`）。三个小节已从本页移除。

---

## 目录

1. [P0-2：工具结果消息驱逐 (卸载到文件 + head/tail预览)](#p0-2工具结果消息驱逐)
2. [P1-1：参数截断 (TruncateArgsSettings)](#p1-1参数截断)
3. [P1-2：溢出尾部裁剪 (快速恢复路径)](#p1-2溢出尾部裁剪)
4. [P1-3：模型感知摘要默认值](#p1-3模型感知摘要默认值)
5. [P1-4：增量检查点优化 (DeltaChannel)](#p1-4增量检查点优化)
6. [P1-5：消息增量缩减器 (去重+墓碑)](#p1-5消息增量缩减器)
7. [P1-6：中间件脚手架保护](#p1-6中间件脚手架保护)
8. [P1-7：多模态内容清理](#p1-7多模态内容清理)
9. [P1-8：威胁模型文档](#p1-8威胁模型文档)
10. [P2-1：ripgrep 双重超时看门狗](#p2-1ripgrep-双重超时看门狗)
11. [P2-2：持久化工具审批策略 (字节修订 CAS)](#p2-2持久化工具审批策略)
12. [P2-3：伪文件系统修剪](#p2-3伪文件系统修剪)
13. [P2-4：read_file 结果切片](#p2-4read_file-结果切片)
14. [实施优先级与依赖关系总览](#实施优先级与依赖关系总览)

---

## P0-2：工具结果消息驱逐

### 问题

Sherry 当前对超大工具结果仅做截断（默认500行/2000行上限），被截断的内容永久丢失。DeepAgents 将大型工具结果卸载到文件系统，替换为 head+tail 预览，模型可按需重新读取完整内容。

### DeepAgents 做法

- `_offload_tool_message_content()` 将大型工具结果写入文件
- `_create_content_preview()` 生成 head+tail 预览（各5行）
- 工具消息内容替换为预览 + 文件路径引用
- read_file 结果切片而非卸载（文件已在后端）

### 具体实现方案

#### 文件清单

| 文件                                             | 修改类型 | 说明                             |
| ------------------------------------------------ | -------- | -------------------------------- |
| `agent/middlewares/message_eviction.py`          | 新建     | 驱逐逻辑（函数级，非独立中间件） |
| `agent/middlewares/summarization/core.py`             | 修改     | 在压缩流程内调用驱逐             |
| `config/features/agent_side/message_eviction.py` | 新建     | 驱逐配置 TypedDict               |
| `config/features/agent_side/__init__.py`         | 修改     | 导出新配置                       |
| `config/features/__init__.py`                    | 修改     | 导出新配置                       |

#### 配置设计

**`config/features/agent_side/message_eviction.py`**:

```python
from typing import TypedDict


class MessageEviction(TypedDict):
    """工具结果消息驱逐配置。"""

    # 超过此字符数的工具结果将被驱逐到文件系统
    tool_result_evict_threshold: int

    # 预览头尾行数
    preview_head_lines: int
    preview_tail_lines: int

    # 驱逐文件存储目录（相对于 SESSIONS_DIR/{session_id}/，session 删除时自动清理）
    eviction_subdir: str

    # 驱逐文件最大保留数（LRU清理）
    max_evicted_files: int

    # 是否启用内联媒体驱逐
    evict_inline_media: bool


MESSAGE_EVICTION: MessageEviction = {
    "tool_result_evict_threshold": 20_000,  # ~5000 tokens
    "preview_head_lines": 5,
    "preview_tail_lines": 5,
    "eviction_subdir": "evicted",  # → SESSIONS_DIR/{session_id}/evicted/
    "max_evicted_files": 100,
    "evict_inline_media": True,
}
```

#### 实现代码

**`agent/middlewares/message_eviction.py`**:

```python
"""工具结果消息驱逐逻辑。

将超大型工具结果卸载到文件系统，
替换为 head+tail 预览 + 文件路径引用。
模型可按需重新读取完整内容。

仅在压缩流程内调用，不作为独立中间件。
原因：驱逐改写消息内容会破坏前缀缓存，
只在压缩时（前缀本来就会被重写）一起做，避免额外缓存失效。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from langchain_core.messages import BaseMessage, ToolMessage

from config import SESSIONS_DIR
from config.features.agent_side.message_eviction import MESSAGE_EVICTION

_PREVIEW_TEMPLATE = """\
[evicted to: {path}]
--- head ({head_n} lines) ---
{head}
{midline_marker}
--- tail ({tail_n} lines) ---
{tail}
[full content: {total_chars} chars, evicted at {ts}]
"""


def evict_large_tool_results(
    messages: list[BaseMessage],
    session_id: str,
) -> list[BaseMessage] | None:
    """驱逐超大工具结果到文件系统，返回新消息列表。

    仅在压缩流程（T1-T5）内调用。被驱逐的工具结果写入
    SESSIONS_DIR/{session_id}/evicted/ 下，session 删除时由
    clear_session() 的 shutil.rmtree 自动清理。

    Args:
        messages: 当前消息列表
        session_id: 会话ID，用于隔离驱逐文件

    Returns:
        驱逐后的新消息列表；如无驱逐则返回 None。
    """
    threshold = MESSAGE_EVICTION["tool_result_evict_threshold"]
    modified = False
    new_messages = []

    eviction_dir = _get_eviction_dir(session_id)

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            new_messages.append(msg)
            continue

        content = msg.content
        if not isinstance(content, str) or len(content) <= threshold:
            new_messages.append(msg)
            continue

        # 驱逐到文件
        evicted_path = _write_eviction(msg, eviction_dir)
        preview = _build_preview(content, evicted_path)
        new_msg = ToolMessage(
            content=preview,
            tool_call_id=msg.tool_call_id,
            name=msg.name,
        )
        new_messages.append(new_msg)
        modified = True

    return new_messages if modified else None


def _get_eviction_dir(session_id: str) -> Path:
    """返回 session 的驱逐目录，按需创建。"""
    d = Path(SESSIONS_DIR) / session_id / MESSAGE_EVICTION["eviction_subdir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_eviction(msg: ToolMessage, eviction_dir: Path) -> Path:
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    key = f"{msg.tool_call_id}_{hashlib.md5(content.encode()).hexdigest()[:8]}"
    path = eviction_dir / f"{key}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def _build_preview(content: str, path: Path) -> str:
    lines = content.splitlines()
    head_n = MESSAGE_EVICTION["preview_head_lines"]
    tail_n = MESSAGE_EVICTION["preview_tail_lines"]
    head = "\n".join(lines[:head_n])
    tail = "\n".join(lines[-tail_n:])
    midline = "..." if len(lines) > head_n + tail_n else ""
    return _PREVIEW_TEMPLATE.format(
        path=str(path),
        head_n=min(head_n, len(lines)),
        tail_n=min(tail_n, len(lines)),
        head=head,
        midline_marker=midline,
        tail=tail,
        total_chars=len(content),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
```

#### 集成方式（嵌入 Summarization 压缩流程）

在 `agent/middlewares/summarization/core.py` 的压缩入口（T1-T5 路由决策后、调 LLM 摘要前）插入驱逐步骤：

```python
# summarization/core.py 压缩流程内，摘要前先驱逐:

def _apply_compression(self, state, session_id: str):
    from agent.middlewares.message_eviction import evict_large_tool_results

    # Step 1: 驱逐超大工具结果到文件（减少压缩输入体积）
    messages = state.get("messages", [])
    evicted = evict_large_tool_results(messages, session_id)
    if evicted is not None:
        state["messages"] = evicted
        messages = evicted

    # Step 2: 原有压缩逻辑（调 LLM 摘要）
    # ... 现有 T1-T5 路由 + 4路由溢出处理 ...
```

**为什么不做独立中间件**：驱逐改写消息列表中的 ToolMessage 内容，会从该点起破坏前缀缓存。如果作为 `before_model` 钩子每次调用前都驱逐，会在**没有压缩压力时**白白打破缓存。只在压缩流程内调用，前缀本来就要被摘要重写，驱逐不额外增加缓存失效。

#### Session 删除时自动清理

驱逐文件存储在 `SESSIONS_DIR/{session_id}/evicted/` 下。`server/DAO/messages.py::clear_session()` 第47行已执行 `shutil.rmtree(SESSIONS_DIR/{session_id}/)`，**驱逐文件随之自动清理，无需修改 `clear_session`**。

```
clear_session() 清理链：
  (0) 保存 session 终态
  (1) 删除 mes_memory 消息行
  (2) 删除 checkpointer 记录
  (3) shutil.rmtree(SESSIONS_DIR/{session_id}/)  ← 驱逐文件在此自动清除
  (4) 清理内存状态 + state_register_db
```

#### 与 P1-2 溢出尾部裁剪的协同

```
压缩触发 (T1-T5):
  → Step 1: 消息驱逐（P0-2）— 大工具结果写文件，替换为预览
  → Step 2: 压缩（调 LLM 摘要）— 压缩后体积进一步降低
  → 若仍溢出:
    → P1-2 溢出尾部裁剪 — 快速移除尾部 ToolMessage 预览，秒级重试
    → 裁剪不够 → 完整压缩再试
```

---

## P1-1：参数截断

### 问题

Sherry 在压缩上下文时截断的是完整的消息，但旧工具调用中的大型参数（如传入的大段代码或文件内容）仍占用空间。

### DeepAgents 做法

`TruncateArgsSettings` 在摘要前截断旧消息中的大型工具调用参数。

### 具体实现方案

#### 文件清单

| 文件                                            | 修改类型 | 说明           |
| ----------------------------------------------- | -------- | -------------- |
| `agent/middlewares/summarization/summarization_components.py` | 修改     | 添加参数截断器 |
| `config/features/agent_side/summarization.py`   | 修改     | 添加截断配置   |

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

配合 `_offload_tool_message_content()`（P0-2 消息驱逐），被裁剪的工具结果已先写入文件，裁剪的只是 head+tail 预览，完整内容不丢失。

### 具体实现方案

#### 文件清单

| 文件                                          | 修改类型 | 说明                                |
| --------------------------------------------- | -------- | ----------------------------------- |
| `agent/middlewares/summarization/core.py`          | 修改     | 在 T4/T5 恢复路径前插入快速裁剪路径 |
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
  工具结果 > 20K chars → P0-2 消息驱逐(写文件+预览) → 消息列表保持小体积

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

| 类别 | 示例             | 存储位置             |
| ---- | ---------------- | -------------------- |
| 敏感 | API keys, tokens | env vars (scrub_env) |
| 私密 | 对话历史         | SQLite (WAL)         |
| 内部 | 工具结果         | 消息列表 + 驱逐文件  |

## 威胁分析

| 威胁         | 现有防护       | 差距             |
| ------------ | -------------- | ---------------- |
| 路径遍历     | 三道结构门禁 + O_NOFOLLOW | 已落地（`path_utils.py`） |
| 符号链接攻击 | `O_NOFOLLOW` + 循环检测 | 已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow` / `_raise_if_symlink_loop`，read/write/patch 全走） |
| Shell注入    | 正则黑名单     | 已评估并否决（base64+`eval` 对以 shell 语义执行的 `terminal` 无增益，且置于 `_check_dangerous`/`_check_sensitive_file_access` 之前会让明文绕过防线；真实读屏障是 OS 沙箱读遮蔽。见头部处置记录） |
| 工具结果OOM  | 截断           | **需实施 P0-2**  |
| ...          | ...            | ...              |
```

---

## P2-1：ripgrep 双重超时看门狗

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

read_file 工具的结果被截断后，完整内容丢失。DeepAgents 对 read_file 特殊处理：因为文件已在后端，只切片不卸载。

### DeepAgents 做法

`_slice_read_file_tm()` 将 read_file 的 ToolMessage 切片为 head+tail，不写入文件（因为文件本身就在后端）。

### 具体实现方案

在 P0-2 的 `MessageEvictionMiddleware` 中，对 `name == "read_file"` 的 ToolMessage 走切片路径而非卸载路径。

---

## 实施优先级与依赖关系总览

```
Phase 1 (P0, 安全关键):
  P0-2 工具结果消息驱逐 ──────→ 独立实施

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
