# 摘要消息过滤 (Summary Message Filtering) 实现计划

## 1. 背景与问题

### 1.1 现状：链式摘要冗余

Sherry 的 `_create_summary` / `_acreate_summary`（`core.py:1552-1602`）在执行链式摘要时，
旧摘要内容**同时出现在两个位置**：

```
_extract_previous_summary(messages)     → 旧摘要文本 → <prior-summary> 标签   (第一份)
_serialize_for_summary(messages)        → 序列化所有消息(含旧摘要 AIMessage) → <conversation> 标签 (第二份)
_build_summary_prompt(serialized, previous_summary)
```

最终 prompt 结构：

```
<conversation>
  ...新对话...
  [Assistant]: <summary>旧摘要全文...</summary>    ← 冗余！旧摘要残留在对话中
</conversation>

<prior-summary>
  旧摘要全文                                          ← 同样的内容又出现一次
</prior-summary>
```

这导致：

1. **token 浪费** — 旧摘要可能占数千 token，重复出现翻倍开销
2. **摘要器混淆** — LLM 在 `<conversation>` 和 `<prior-summary>` 中看到同样的摘要文本，
   可能原样回吐而非真正合并更新

### 1.2 `<prior-summary>` 的作用与来源

`<prior-summary>` 是链式摘要的正确机制，**不应移除**。它的设计直接来自 opencode-dev
的 `buildPrompt`（`packages/core/src/session/compaction.ts:160-174`）：

```typescript
// opencode-dev 的 buildPrompt — 与 Sherry 的 _build_summary_prompt 结构几乎完全一致
export const buildPrompt = (input: {
  previousSummary?: string;
  context: string[];
}) => {
  const conversation = `Here is the conversation so far:\n\n<conversation>\n${input.context.join("\n\n")}\n</conversation>`;
  if (!input.previousSummary)
    return [
      conversation,
      "Create a new anchored summary...",
      SUMMARY_TEMPLATE,
    ].join("\n\n");
  return [
    conversation,
    `Here is the summary of the conversation before the <conversation> above:\n\n<prior-summary>\n${input.previousSummary}\n</prior-summary>`,
    SUMMARY_UPDATE_INSTRUCTIONS,
    SUMMARY_TEMPLATE,
  ].join("\n\n");
};
```

Sherry 的对应实现（`core.py:1507-1519`）：

```python
conversation = f"Here is the conversation so far:\n\n<conversation>\n{messages_text}\n</conversation>"
parts = [conversation]
if previous_summary:
    parts.append(f"...<prior-summary>\n{previous_summary}\n</prior-summary>")
    parts.append(_SUMMARY_PROMPT_UPDATE)
else:
    parts.append(_SUMMARY_PROMPT_FIRST)
```

两者的 prompt 模板完全对齐（`<conversation>` + `<prior-summary>` + update instructions + template）。

**关键差异**在于 `<conversation>` 的内容来源：

| 项目                | `<conversation>` 内容                                 | `<prior-summary>` 内容 | 冗余？ |
| ------------------- | ----------------------------------------------------- | ---------------------- | ------ |
| **opencode-dev**    | 过滤后的对话（`hidden` Set 移除旧摘要）               | 旧摘要文本             | 无     |
| **deepagents**      | 过滤后的对话（`_filter_summary_messages` 移除旧摘要） | 旧摘要文本             | 无     |
| **Sherry (现状)**   | **全部**消息（含旧摘要 AIMessage）                    | 旧摘要文本             | **有** |
| **Sherry (修复后)** | 过滤后的对话（`_filter_summary_messages` 移除旧摘要） | 旧摘要文本             | 无     |

### 1.3 对齐目标

修复后 Sherry 与 opencode-dev (+omo 插件) 的对齐情况：

| 能力                        | opencode-dev 核心           | opencode-dev + omo                        | Sherry (修复后)                |
| --------------------------- | --------------------------- | ----------------------------------------- | ------------------------------ |
| `<prior-summary>` 注入      | ✅ `buildPrompt`            | ✅ 不变                                   | ✅ `_build_summary_prompt`     |
| `<conversation>` 过滤旧摘要 | ✅ `hidden` Set             | ✅ 不变                                   | ✅ `_filter_summary_messages`  |
| 摘要消息标记                | ✅ compaction part type     | ✅ 不变                                   | ✅ `lc_source` (Human+AI)      |
| 旧摘要文本提取              | ✅ `completedCompactions()` | ✅ 不变                                   | ✅ `_extract_previous_summary` |
| 插件上下文注入              | —                           | ✅ `compacting.context`                   | N/A (无插件系统)               |
| autocontinue                | —                           | ✅ `experimental.compaction.autocontinue` | N/A                            |

omo 的插件功能（上下文注入、autocontinue）是 omo 特有架构，不适用于 Sherry。
核心摘要过滤逻辑修复后**完全对齐**。

---

## 2. 实现方案

### 2.1 改动清单

| #   | 文件                                      | 位置                                     | 改动                                     |
| --- | ----------------------------------------- | ---------------------------------------- | ---------------------------------------- |
| 1   | `agent/middlewares/summarization/core.py` | `:1625-1631` `_build_new_messages`       | 给 HumanMessage 加 `lc_source` 标记      |
| 2   | `agent/middlewares/summarization/core.py` | 新增模块级函数                           | `_filter_summary_messages()`             |
| 3   | `agent/middlewares/summarization/core.py` | `:1552-1575` `_create_summary`           | 提取后过滤，序列化+fallback 用过滤后消息 |
| 4   | `agent/middlewares/summarization/core.py` | `:1577-1602` `_acreate_summary`          | 同上 (async 镜像)                        |
| 5   | `agent/middlewares/summarization/core.py` | `:1843` `_apply_compression_under_lock`  | `skip_llm` 路径也过滤                    |
| 6   | `agent/middlewares/summarization/core.py` | `:1946` `_aapply_compression_under_lock` | 同上 (async 镜像)                        |
| 7   | `tests/agent/middlewares/`                | 新文件                                   | 单元测试 + 集成测试                      |

### 2.2 详细改动

#### 改动 1：`_build_new_messages` — 标记 HumanMessage

**文件**: `agent/middlewares/summarization/core.py:1608-1631`

```python
# Before:
return [
    HumanMessage(content="What did we do so far?"),
    AIMessage(
        content=full_content,
        additional_kwargs={"lc_source": _SUMMARY_LC_SOURCE},
    ),
]

# After:
return [
    HumanMessage(
        content="What did we do so far?",
        additional_kwargs={"lc_source": _SUMMARY_LC_SOURCE},
    ),
    AIMessage(
        content=full_content,
        additional_kwargs={"lc_source": _SUMMARY_LC_SOURCE},
    ),
]
```

**原因**: 使过滤逻辑统一——只需检查 `additional_kwargs["lc_source"]` 即可识别摘要对的两条消息。

#### 改动 2：新增 `_filter_summary_messages` 模块级函数

**文件**: `agent/middlewares/summarization/core.py`，放在 `_serialize_for_summary` 附近

```python
def _filter_summary_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Filter out previous summary messages to avoid chain summary redundancy.

    Removes messages tagged with ``lc_source='summarization'`` — the
    HumanMessage ('What did we do so far?') + AIMessage (summary content)
    pair produced by ``_build_new_messages``.  The previous summary text
    is already extracted via ``_extract_previous_summary`` and injected
    into ``<prior-summary>`` separately; keeping it in the serialized
    ``<conversation>`` causes token waste and summarizer confusion.

    Mirrors opencode-dev's ``hidden`` Set filtering and deepagents'
    ``_filter_summary_messages``.
    """
    return [
        m for m in messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") != _SUMMARY_LC_SOURCE
    ]
```

#### 改动 3：`_create_summary` — 提取后过滤

**文件**: `agent/middlewares/summarization/core.py:1552-1575`

```python
# Before:
def _create_summary(self, messages_to_summarize, session_id=""):
    if not messages_to_summarize:
        return "No previous conversation history."
    previous_summary = self._extract_previous_summary(messages_to_summarize)
    serialized = _serialize_for_summary(messages_to_summarize)
    if not serialized.strip():
        return "No previous conversation history."
    prompt = self._build_summary_prompt(serialized, previous_summary, session_id=session_id)
    try:
        response = self._model.invoke(prompt, ...)
        summary = response.text.strip()
        if not summary or len(summary) < 50:
            return _build_static_fallback_summary(messages_to_summarize)
        return summary
    except Exception as e:
        return _build_static_fallback_summary(messages_to_summarize)

# After:
def _create_summary(self, messages_to_summarize, session_id=""):
    if not messages_to_summarize:
        return "No previous conversation history."
    previous_summary = self._extract_previous_summary(messages_to_summarize)
    filtered = _filter_summary_messages(messages_to_summarize)          # NEW
    serialized = _serialize_for_summary(filtered)                        # filtered
    if not serialized.strip():
        return "No previous conversation history."
    prompt = self._build_summary_prompt(serialized, previous_summary, session_id=session_id)
    try:
        response = self._model.invoke(prompt, ...)
        summary = response.text.strip()
        if not summary or len(summary) < 50:
            return _build_static_fallback_summary(filtered)             # filtered
        return summary
    except Exception as e:
        return _build_static_fallback_summary(filtered)                  # filtered
```

#### 改动 4：`_acreate_summary` — 同步镜像 (async)

**文件**: `agent/middlewares/summarization/core.py:1577-1602`

与改动 3 完全对称，`_serialize_for_summary` 和 `_build_static_fallback_summary` 均改用 `filtered`。

#### 改动 5：`_apply_compression_under_lock` — skip_llm 路径过滤

**文件**: `agent/middlewares/summarization/core.py:1842-1844`

```python
# Before:
if skip_llm:
    summary_text = _build_static_fallback_summary(messages_to_summarize)
    strategy_used = "fallback"

# After:
if skip_llm:
    filtered = _filter_summary_messages(messages_to_summarize)
    summary_text = _build_static_fallback_summary(filtered)
    strategy_used = "fallback"
```

#### 改动 6：`_aapply_compression_under_lock` — async 镜像

**文件**: `agent/middlewares/summarization/core.py:1945-1947`

与改动 5 完全对称。

### 2.3 不影响的路径

| 路径                                         | 原因                                                                             |
| -------------------------------------------- | -------------------------------------------------------------------------------- |
| `_extract_previous_summary`                  | 在过滤**之前**调用，仍能从原始消息列表中找到旧摘要                               |
| `_inject_recovery_context`                   | 操作 `final_messages`（压缩后），不涉及 `messages_to_summarize`                  |
| `memory_flush`                               | 接收 `messages_to_summarize`（过滤前的原始列表），旧摘要参与事实提取不影响正确性 |
| `_build_new_messages`                        | 构建新摘要消息，与过滤逻辑无交互                                                 |
| `MessageTruncator.truncate_summary_messages` | 操作 `final_messages`，不涉及过滤                                                |
| `_determine_cutoff`                          | 基于全部消息计算截断点，过滤发生在截断之后                                       |

### 2.4 向后兼容

- **旧会话中的摘要 AIMessage**：已有 `lc_source="summarization"` 标记，可被过滤
- **旧会话中的摘要 HumanMessage**：无 `lc_source` 标记，不会被过滤——但内容仅为 "What did we do so far?" (6 词)，影响可忽略
- **新会话**：两者均有标记，完整过滤

---

## 3. 测试计划

### 3.1 单元测试

**文件**: `tests/agent/middlewares/test_summary_message_filtering.py` (新建)

| 测试                                    | 验证点                                                                                                        |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `test_filter_removes_summary_pair`      | 构造含 `[HumanMessage(lc_source), AIMessage(lc_source)]` 的消息列表，断言 `_filter_summary_messages` 移除两者 |
| `test_filter_preserves_normal_messages` | 普通对话消息不受影响                                                                                          |
| `test_filter_empty_list`                | 空列表输入返回空列表                                                                                          |
| `test_filter_multiple_summary_pairs`    | 多轮链式摘要，所有旧摘要对均被移除                                                                            |
| `test_filter_unmarked_old_human`        | 旧会话中无 `lc_source` 的 HumanMessage 不被过滤（向后兼容）                                                   |

### 3.2 集成测试

**文件**: `tests/agent/middlewares/test_compression_comprehensive.py` (追加)

| 测试                                         | 验证点                                                                 |
| -------------------------------------------- | ---------------------------------------------------------------------- |
| `test_chained_summary_no_redundancy`         | 二次压缩时，`_serialize_for_summary` 输出不含 `<summary>` 标签内容     |
| `test_chained_summary_prior_summary_present` | 二次压缩时，prompt 中 `<prior-summary>` 仍包含旧摘要文本               |
| `test_skip_llm_path_filters_summary`         | `skip_llm=True` 路径的 `_build_static_fallback_summary` 不含旧摘要内容 |
| `test_build_new_messages_marks_human`        | `_build_new_messages` 返回的 HumanMessage 有 `lc_source` 标记          |

### 3.3 回归测试

运行现有测试确保不破坏：

```bash
uv run pytest tests/agent/middlewares -q -k "not llm_e2e"
```

---

## 4. 执行顺序

1. 改动 1 — `_build_new_messages` 标记 HumanMessage
2. 改动 2 — 新增 `_filter_summary_messages` 函数
3. 改动 3+4 — `_create_summary` / `_acreate_summary` 插入过滤
4. 改动 5+6 — `_apply/aapply_compression_under_lock` skip_llm 路径
5. 新建单元测试文件
6. 追加集成测试
7. `uv run pytest tests/agent/middlewares -q -k "not llm_e2e"` 全量回归
8. `uv run --with ruff ruff check . && uv run --with ruff ruff format --check .` lint
9. `uv run --no-sync basedpyright agent/middlewares/summarization/` 类型检查
