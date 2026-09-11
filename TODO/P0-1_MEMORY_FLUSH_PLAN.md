# P0-1 预压缩 Memory Flush — 实施计划

> 日期: 2026-09-11
> 来源: SESSION_MEMORY_BORROWING_PLAN.md §P0-1 (行 26-265)
> 决策: 新建 memory_flush.py + hook 到 _aapply_compression + 便宜模型提取 + 写入 MEMORY.md
> 预估工时: 1-2 天

---

## 1. 问题

Summarization 中间件在压缩时直接调用主 LLM 生成摘要。被压缩丢弃的消息中的关键信息（用户意图、关键决策、文件变更）永久丢失。压缩后仅依赖摘要质量，如果摘要退化则无法恢复。

**当前状态：**

- `Summarization` 中间件（`agent/middlewares/summarization.py:539`）有 `_apply_compression()` (sync) 和 `_aapply_compression()` (async)
- `_determine_cutoff()`（行 1276）确定保留/丢弃边界
- `_create_summary()` (sync, 行 1361) / `_acreate_summary()` (async, 行 1386) 调用主 LLM 生成摘要
- `MemoryStore`（`agent/tools/memory.py:104`）有 `add()` 方法但无 `append_entries()` 批量追加方法
- 压缩后 `_apply_compression` 在行 1651-1656 调用 `build_system_prompt(session_id=session_id)` 重建系统 prompt
- 无 `memory_flush.py` 中间件
- `config/num.py` 无 Memory Flush 配置

---

## 2. 设计决策

### 2.1 在压缩前用便宜模型提取事实

在 `_aapply_compression()` 和 `_apply_compression()` 中，**在 LLM 摘要之前**，用便宜模型扫描即将被丢弃的消息，提取持久有用的事实写入 `MEMORY.md`（或 LT-1 的 `facts/` 层）。

### 2.2 关键决策

| 决策点   | 选择                                              | 理由                                                          |
| -------- | ------------------------------------------------- | ------------------------------------------------------------- |
| 提取模型 | 独立便宜模型（可配置）                            | 不占用主模型配额；配置项 `MEMORY_FLUSH_MODEL`                 |
| 触发条件 | 被丢弃消息 ≥ 阈值（token 或字符）                 | 少量消息不值得 flush（浪费 API 调用）                         |
| 写入目标 | `MemoryStore.add()` 逐条写入                      | 复用现有去重 + 字符限制 + 文件锁；未来可指向 LT-1 `facts/` 层 |
| 超时处理 | 30s 超时跳过，不阻塞压缩                          | 压缩是关键路径，flush 是可选增强                              |
| 失败处理 | try/except 静默降级                               | flush 失败不能阻塞压缩                                        |
| 注入点   | `_aapply_compression` + `_apply_compression`      | 两个压缩路径都需要 hook                                       |
| 依赖注入 | `Summarization.__init__` 新增 `memory_store` 参数 | 中间件需要访问 MemoryStore 实例                               |

---

## 3. 涉及文件

| 文件                                           | 操作                           | 预估行数 |
| ---------------------------------------------- | ------------------------------ | -------- |
| `config/num.py`                                | 修改：新增配置项               | ~8       |
| `agent/middlewares/memory_flush.py`            | **新建**                       | ~140     |
| `agent/tools/memory.py`                        | 修改：新增 `append_entries()`  | ~30      |
| `agent/middlewares/summarization.py`           | 修改：**init** + 压缩入口 hook | ~25      |
| `tests/agent/middlewares/test_memory_flush.py` | **新建**                       | ~120     |

---

## 4. 详细设计

### 4.1 config/num.py — 新增配置项

```python
# === Memory Flush (P0-1) ===
import os

MEMORY_FLUSH_ENABLED = True
MEMORY_FLUSH_MODEL = os.getenv("MEMORY_FLUSH_MODEL", "")  # 空=用默认便宜模型
MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS = 8_000   # 被丢弃消息超过此 token 数才触发 flush
MEMORY_FLUSH_FORCE_FLUSH_CHARS = 50_000      # 被丢弃消息超过此字符数强制 flush
MEMORY_FLUSH_OUTPUT_MAX_TOKENS = 2_048        # flush 输出 token 上限
MEMORY_FLUSH_TIMEOUT_SECONDS = 30             # flush 超时（超时则跳过，不阻塞压缩）
```

### 4.2 agent/tools/memory.py — 新增 append_entries()

```python
# MemoryStore 类新增方法

def append_entries(self, new_entries: str) -> dict[str, Any]:
    """
    追加多条条目到 MEMORY.md（Memory Flush 用）。
    条目以 § 分隔，自动去重，超限时 pop 最旧。

    与 add() 的区别：
    - add() 一次一条，拒绝超限
    - append_entries() 一次多条，pop 最旧以腾空间
    """
    new_entries = new_entries.strip()
    if not new_entries:
        return {"success": True, "message": "No entries to add."}

    # 解析条目（§ 分隔）
    candidates = [e.strip() for e in new_entries.split(ENTRY_DELIMITER) if e.strip()]
    if not candidates:
        return {"success": True, "message": "No entries to add."}

    with self._file_lock(self._path_for("memory")):
        self._reload_target("memory")
        entries = self.memory_entries
        limit = self.memory_char_limit

        # 去重：跳过已存在的条目
        existing_set = set(entries)
        new_items = [e for e in candidates if e not in existing_set]
        if not new_items:
            return {"success": True, "message": "All entries already exist (no duplicates added)."}

        # 合并
        all_entries = entries + new_items
        combined = ENTRY_DELIMITER.join(all_entries)

        # 容量截断：pop 最旧
        while len(combined) > limit and len(all_entries) > 1:
            all_entries.pop(0)
            combined = ENTRY_DELIMITER.join(all_entries)

        self.memory_entries = all_entries
        self.save_to_disk("memory")

    return {
        "success": True,
        "message": f"Added {len(new_items)} entries (deduplicated {len(candidates) - len(new_items)}).",
        "entry_count": len(all_entries),
        "usage": f"{len(combined)}/{limit} chars",
    }
```

### 4.3 agent/middlewares/memory_flush.py — 新建

```python
"""
预压缩 Memory Flush：在 LLM 摘要压缩前，用便宜模型从即将被丢弃的消息中
提取关键事实写入 MEMORY.md，确保压缩后仍可通过记忆文件恢复重要上下文。

参考: openclaw compaction.memoryFlush 机制
"""

import logging
from typing import TYPE_CHECKING

from config.num import (
    MEMORY_FLUSH_ENABLED,
    MEMORY_FLUSH_MODEL,
    MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS,
    MEMORY_FLUSH_FORCE_FLUSH_CHARS,
    MEMORY_FLUSH_OUTPUT_MAX_TOKENS,
    MEMORY_FLUSH_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

_FLUSH_PROMPT = """\
You are a memory extraction assistant. Below is conversation history that is \
about to be compacted (discarded). Extract **persistent facts** that would be \
useful in future sessions.

Extraction rules:
1. Only extract cross-session facts: user preferences, project conventions, \
key decisions, environment facts, tool lessons learned.
2. Do NOT extract temporary task progress (that is the summary's job).
3. Each fact on one line, prefixed with a category.
4. If nothing worth extracting, output a single line: "(none)".

Format (use § to separate entries):
§ Environment: <fact>
§ Project: <fact>
§ Decision: <fact>
§ User: <fact>
§ Tool: <fact>

Conversation to be discarded:
{discarded_text}
"""


def should_flush(discarded_messages: list, estimated_tokens: int) -> bool:
    """判断是否需要执行 Memory Flush"""
    if not MEMORY_FLUSH_ENABLED:
        return False
    total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
    if total_chars >= MEMORY_FLUSH_FORCE_FLUSH_CHARS:
        return True
    if estimated_tokens >= MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS:
        return True
    return False


async def run_memory_flush(
    discarded_messages: list,
    estimated_tokens: int,
    memory_store,    # agent.tools.memory.MemoryStore
    llm_factory,     # callable that returns an LLM instance
) -> bool:
    """
    执行 Memory Flush，将提取的事实写入 MEMORY.md。
    返回 True 表示成功，False 表示跳过或失败。
    """
    if not should_flush(discarded_messages, estimated_tokens):
        return False

    discarded_text = "\n\n".join(
        f"[{_msg_type(m)}] {_msg_to_text(m)}"
        for m in discarded_messages
    )

    if not discarded_text.strip():
        return False

    prompt = _FLUSH_PROMPT.format(discarded_text=discarded_text)

    try:
        llm = llm_factory(
            model=MEMORY_FLUSH_MODEL or None,
            max_tokens=MEMORY_FLUSH_OUTPUT_MAX_TOKENS,
            timeout=MEMORY_FLUSH_TIMEOUT_SECONDS,
        )
        response = await llm.ainvoke(prompt)
        extracted = _extract_text(response)

        if not extracted or extracted.strip() == "(none)":
            logger.info("Memory Flush: no facts to extract")
            return False

        # 写入 MEMORY.md（append_entries 自动去重 + 容量截断）
        result = memory_store.append_entries(extracted)
        logger.info(
            "Memory Flush: wrote %d chars to MEMORY.md (%s)",
            len(extracted),
            result.get("message", ""),
        )
        return True

    except Exception as e:
        logger.warning("Memory Flush failed (non-blocking): %s", e)
        return False


def run_memory_flush_sync(
    discarded_messages: list,
    estimated_tokens: int,
    memory_store,
    llm_factory,
) -> bool:
    """同步版 Memory Flush（用于 _apply_compression 同步路径）"""
    if not should_flush(discarded_messages, estimated_tokens):
        return False

    discarded_text = "\n\n".join(
        f"[{_msg_type(m)}] {_msg_to_text(m)}"
        for m in discarded_messages
    )

    if not discarded_text.strip():
        return False

    prompt = _FLUSH_PROMPT.format(discarded_text=discarded_text)

    try:
        llm = llm_factory(
            model=MEMORY_FLUSH_MODEL or None,
            max_tokens=MEMORY_FLUSH_OUTPUT_MAX_TOKENS,
            timeout=MEMORY_FLUSH_TIMEOUT_SECONDS,
        )
        response = llm.invoke(prompt)
        extracted = _extract_text(response)

        if not extracted or extracted.strip() == "(none)":
            logger.info("Memory Flush: no facts to extract")
            return False

        result = memory_store.append_entries(extracted)
        logger.info(
            "Memory Flush: wrote %d chars to MEMORY.md (%s)",
            len(extracted),
            result.get("message", ""),
        )
        return True

    except Exception as e:
        logger.warning("Memory Flush failed (non-blocking): %s", e)
        return False


def _msg_to_text(msg) -> str:
    """将消息对象转为文本"""
    if isinstance(msg, str):
        return msg
    if hasattr(msg, "content"):
        if isinstance(msg.content, str):
            return msg.content
        if isinstance(msg.content, list):
            return " ".join(
                block.get("text", "")
                for block in msg.content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return str(msg)


def _msg_type(msg) -> str:
    """获取消息类型标签"""
    return getattr(msg, "type", "") or type(msg).__name__.lower()


def _extract_text(response) -> str:
    """从 LLM 响应中提取文本"""
    if hasattr(response, "content"):
        return response.content.strip() if isinstance(response.content, str) else str(response.content).strip()
    if hasattr(response, "text"):
        return response.text.strip()
    return str(response).strip()
```

### 4.4 agent/middlewares/summarization.py — 修改

#### **init** 新增 memory_store 参数

```python
def __init__(
    self,
    model,
    trigger: list | None = None,
    keep: tuple = ("messages", 10),
    main_llm_context_window: int | None = None,
    need_update_system_prompt: bool = False,
    memory_store=None,          # ← 新增
    llm_factory=None,           # ← 新增: callable(model, max_tokens, timeout) -> LLM
    **kwargs,
):
    self._model = model
    self._trigger = trigger or [("tokens", 80_000)]
    self._keep = keep
    self._main_llm_context_window = main_llm_context_window
    self._need_update_system_prompt = need_update_system_prompt
    self._compress_last_turn: bool = False
    self._compaction_just_happened: bool = False
    self._memory_store = memory_store       # ← 新增
    self._llm_factory = llm_factory         # ← 新增
    self._effectiveness_tracker = CompressionEffectivenessTracker(self._estimate_tokens)
```

#### _aapply_compression() 插入 flush 调用

在 `_aapply_compression()` 中，`_determine_cutoff` 之后、`_create_summary` 之前：

```python
async def _aapply_compression(self, request, session_id):
    original_messages = ...
    recovery_ctx = ...

    current_messages, non_llm_reduced = self._run_non_llm_strategies(...)

    if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
        cutoff = self._determine_cutoff(current_messages)
        if cutoff > 0:
            messages_to_summarize = current_messages[:cutoff]
            preserved = current_messages[cutoff:]

            # === 新增：预压缩 Memory Flush (P0-1) ===
            if self._memory_store and self._llm_factory:
                from agent.middlewares.memory_flush import run_memory_flush

                est_tokens = self._estimate_tokens(messages_to_summarize)
                await run_memory_flush(
                    discarded_messages=messages_to_summarize,
                    estimated_tokens=est_tokens,
                    memory_store=self._memory_store,
                    llm_factory=self._llm_factory,
                )

            # LLM 摘要（现有逻辑）
            summary_text = await self._acreate_summary(messages_to_summarize)
            ...
```

在 `_apply_compression()` (sync) 中同理插入 `run_memory_flush_sync`。

---

## 5. 实施顺序

```
Step 1: config/num.py — 新增 Memory Flush 配置项
Step 2: agent/tools/memory.py — 新增 append_entries() 方法
Step 3: agent/middlewares/memory_flush.py — 新建模块
Step 4: agent/middlewares/summarization.py — __init__ 新增 memory_store + llm_factory
Step 5: agent/middlewares/summarization.py — _aapply_compression() 插入 flush 调用
Step 6: agent/middlewares/summarization.py — _apply_compression() 插入 sync flush 调用
Step 7: tests/agent/middlewares/test_memory_flush.py — 编写测试
Step 8: 运行测试 + lint + typecheck
Step 9: 端到端验证：触发压缩 → 检查 MEMORY.md 是否被 flush 更新
```

---

## 6. 测试计划

### 6.1 单元测试 (tests/agent/middlewares/test_memory_flush.py)

| 测试                                      | 说明                                                     |
| ----------------------------------------- | -------------------------------------------------------- |
| `test_should_flush_below_threshold`       | 丢弃消息 < 8000 tokens 且 < 50000 chars → False          |
| `test_should_flush_above_token_threshold` | 丢弃消息 ≥ 8000 tokens → True                            |
| `test_should_flush_above_char_threshold`  | 丢弃消息 ≥ 50000 chars → True                            |
| `test_should_flush_disabled`              | `MEMORY_FLUSH_ENABLED=False` → False                     |
| `test_append_entries_dedup`               | 已存在的条目 → 跳过，不重复                              |
| `test_append_entries_truncation`          | 超限时 pop 最旧                                          |
| `test_append_entries_empty`               | 空输入 → success, no-op                                  |
| `test_run_memory_flush_extracts_facts`    | mock LLM 返回 facts → memory_store.append_entries 被调用 |
| `test_run_memory_flush_no_facts`          | mock LLM 返回 "(none)" → 不写入                          |
| `test_run_memory_flush_timeout`           | mock LLM 超时 → 返回 False，不抛异常                     |
| `test_run_memory_flush_llm_error`         | mock LLM 异常 → 返回 False，不抛异常                     |
| `test_compression_with_flush`             | 集成：触发压缩 → flush 被调用 → MEMORY.md 被更新         |
| `test_compression_without_flush_config`   | 无 memory_store → 跳过 flush，压缩正常                   |

### 6.2 运行验证

```bash
python -m pytest tests/agent/middlewares/test_memory_flush.py -v
python -m pytest tests/agent/middlewares/test_e2e_summarization.py -v  # 确保不破坏
ruff check agent/middlewares/memory_flush.py agent/tools/memory.py agent/middlewares/summarization.py
pyright agent/middlewares/memory_flush.py agent/tools/memory.py
```

---

## 7. 数据流

```
对话触发压缩 (T1-T5)
  → _aapply_compression(request, session_id)
  → _run_non_llm_strategies(messages) → 消减 tool outputs
  → _determine_cutoff(messages) → cutoff=20 (前 20 条丢弃, 后 10 条保留)
  → messages_to_summarize = messages[:20]
  → preserved = messages[20:]

  === Memory Flush (新增) ===
  → should_flush(messages_to_summarize, est_tokens=12000)?
    → 12000 > 8000 → True
  → run_memory_flush(messages_to_summarize, 12000, memory_store, llm_factory)
    → 便宜模型扫描被丢弃消息
    → 返回: "§ Environment: Python 3.12 on Windows\n§ Project: uses pytest + ruff"
    → memory_store.append_entries("§ Environment: ...")
    → MEMORY.md 更新（去重 + 容量截断）
    → 返回 True

  === LLM 摘要 (现有) ===
  → _acreate_summary(messages_to_summarize)
    → 主模型生成结构化摘要
  → _build_new_messages(summary)
  → final_messages = [summary_pair, *preserved]
  → build_system_prompt(session_id) → 重建系统 prompt
```

---

## 8. 与其他方案的关系

| 方案                          | 关系                                                                    |
| ----------------------------- | ----------------------------------------------------------------------- |
| **LT-1 分层记忆**             | 协作。flush 写入 MEMORY.md（Layer 1）；未来可改为写入 facts/（Layer 2） |
| **LT-3 摘要退化防护**         | 协作。flush 在压缩前保存事实；LT-3 在压缩时重建基准线。互补。           |
| **LT-7 摘要与 TaskFlow 协调** | 独立。flush 提取事实，LT-7 注入 TaskFlow 状态到摘要 prompt。            |
| **P0-4 工具输出摘要**         | 独立。P0-4 在非 LLM 策略中替换工具输出；P0-1 在 LLM 摘要前提取事实。    |

---

## 9. 风险与缓解

| 风险                                | 缓解                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------- |
| flush 超时阻塞压缩                  | 30s 超时 + try/except；flush 失败不影响压缩流程                           |
| 便宜模型不可用                      | `MEMORY_FLUSH_MODEL` 可配置；失败时静默跳过                               |
| 提取的事实无用或噪声多              | prompt 约束提取规则；"(none)" 兜底；未来可用 LT-1 facts/ 分类存储         |
| append_entries 竞态写入             | 复用 MemoryStore._file_lock() 跨平台文件锁                                |
| MEMORY.md 字符限制挤出旧事实        | pop 最旧策略与 add() 一致；LT-1 分层记忆可缓解（facts/ 层不挤 MEMORY.md） |
| 主模型和 flush 模型调用叠加延迟     | flush 仅在被丢弃消息超阈值时触发；少量丢弃消息直接跳过                    |
| 依赖注入需要调用方传入 memory_store | `__init__` 参数可选；不传则跳过 flush（向后兼容）                         |
