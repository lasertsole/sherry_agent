# Sherry 前缀缓存破坏点评判报告

> 生成日期：2026-09-16
> 评判范围：`agent/middlewares/` 全量中间件 + `agent/wrapper/` 包装器

---

## 什么是前缀缓存

LLM 提供商（OpenAI、Anthropic、Zhipu 等）对**相同前缀**的请求复用已计算的 KV（key-value）缓存，跳过重复计算。核心要求：**从第一条消息起逐条完全相同**，任何一条消息内容变化，从该点起的缓存全部失效。

---

## 评判标准

| 级别         | 含义                                       |
| ------------ | ------------------------------------------ |
| **不可接受** | 每次模型调用都无条件破坏，即使没有实际需要 |
| **可优化**   | 条件触发但可避免，或频率过高               |
| **可接受**   | 仅在压缩/恢复时触发，本应打破前缀          |

---

## 排查结果总览

| #   | 中间件                      | 钩子                     | 频率                         | 破坏方式                                            | 评判         |
| --- | --------------------------- | ------------------------ | ---------------------------- | --------------------------------------------------- | ------------ |
| 1   | ToolCallNormalize           | `before_model`           | **每次调用**                 | 全量重建 + 可能改写中间消息                         | **不可接受** |
| 2   | ContextEngineHook           | `wrap_model_call`        | **每次调用**                 | 每次覆盖 system_message + 改写 AIMessage tool_calls | **不可接受** |
| 3   | MultimodalProcessor         | `before_model`           | 有媒体时                     | 剥离历史消息中的 image_url 块                       | **可优化**   |
| 4   | Summarization               | `wrap_model_call`        | **每次调用**（即使跳过压缩） | cooldown 路径可能覆盖 system_message                | **可优化**   |
| 5   | Summarization（压缩触发时） | T1-T5                    | 仅压缩时                     | compact/truncate/aggressive_truncate 重写消息       | **可接受**   |
| 6   | SubagentCompletionDrain     | `before_model`           | 有子代理完成时               | 仅追加消息到末尾                                    | **可接受**   |
| 7   | TaskIntent                  | `before_model`           | 有任务意图时                 | 仅追加引导消息到末尾                                | **可接受**   |
| 8   | OutputRepetitionGuard       | `wrap_model_call` (post) | 检测到重复时                 | 仅改写输出不改输入消息                              | **可接受**   |
| 9   | MaxTokensBoost              | `wrap_model_call`        | 截断恢复时                   | 仅改 max_tokens 参数不改消息                        | **可接受**   |
| 10  | LLMRetry                    | `wrap_model_call`        | LLM 失败时                   | 仅改参数/切换模型不改消息                           | **可接受**   |

---

## 逐项详细评判

### 1. ToolCallNormalize — 不可接受

**文件**：`agent/middlewares/tool_call_normalize.py`

**问题**：

```python
def _before_model_impl(self, state):
    normalize_messages = sanitize_tool_use_result_pairing(state["messages"])
    normalize_messages = [m for m in normalize_messages if not isinstance(m, RemoveMessage)]
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *normalize_messages]}
```

- **每次 `before_model` 都执行 `RemoveMessage(REMOVE_ALL_MESSAGES)` + 全量重建**，即使 `sanitize_tool_use_result_pairing` 返回的列表没有任何变化（`changed=False` 时返回原列表，但仍然被 RemoveMessage + rebuild 包裹）
- 这告诉 LangGraph 的 messages reducer：清空全部消息 → 重新写入。即使内容相同，消息对象身份可能改变，API 层面前缀缓存从第一条消息就失效

**`sanitize_tool_use_result_pairing` 可改写中间消息**（`pub/func/transcript_repair.py`）：

- 第216行：`msg.model_copy(update={"invalid_tool_calls": []})` — 清除 AIMessage 的 invalid_tool_calls
- 丢弃空 content 的 ToolMessage（第199行）
- 为缺失的 tool_call_id 补造 dummy ToolMessage（第278-283行）
- 移除孤儿 ToolMessage（第302行）

**影响**：即使对话完全正常、没有孤儿消息，这个中间件也会每次全量重建消息列表，导致 **100% 的模型调用失去前缀缓存**。

**建议**：

- `sanitize_tool_use_result_pairing` 返回 `changed=False` 时，`_before_model_impl` 应直接返回 `None`（无操作），不发 RemoveMessage
- 只有检测到实际需要修复时才执行重建

```python
# 建议修复
def _before_model_impl(self, state):
    original = state["messages"]
    normalized = sanitize_tool_use_result_pairing(original)
    normalized = [m for m in normalized if not isinstance(m, RemoveMessage)]
    # 只有内容实际变化时才重建
    if len(normalized) == len(original) and all(
        a is b or a == b for a, b in zip(normalized, original)
    ):
        return None  # 无变化，不触发重建
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *normalized]}
```

---

### 2. ContextEngineHook — 不可接受

**文件**：`agent/middlewares/context_engine/core.py`

**问题 A：每次覆盖 system_message**

```python
def _wrap_model_call_impl(self, request):
    return request.override(
        system_message=SystemMessage(
            content=self._get_and_reload_system_prompt(session_id)
        )
    )
```

- `wrap_model_call` **每次调用**都执行 `request.override(system_message=...)`
- 如果 system prompt 内容在两轮之间有任何变化（如 TODO 状态更新、子代理状态变化），前缀从 system message 起就断裂
- 即使 system prompt 内容完全相同，`request.override()` 可能创建新的 SystemMessage 对象，取决于 LangChain 的缓存键策略

**问题 B：改写中间消息的 tool_calls**

```python
# _rebuild_orphan_tool_calls (line 97)
msg = msg.model_copy(update={"tool_calls": merged})
```

- 在消息列表中间修改 AIMessage 的 tool_calls 字段
- 从该消息起，后续所有消息的前缀缓存失效

**影响**：system_message 覆盖影响 100% 的调用；orphan 修复只在有孤儿时触发但会改写中间消息。

**建议**：

- system_message 注入应检查内容是否变化，相同则不 override
- `_rebuild_orphan_tool_calls` 应只在实际检测到孤儿时执行，且在日志中标记

---

### 3. MultimodalProcessor — 可优化

**文件**：`agent/middlewares/media_pipeline.py`

**问题**：

```python
# 修改最后一条 HumanMessage (line 79)
last_mes.content = [text_dict]

# 剥离历史消息中的 image_url 块 (line 83-89)
for mes in state_mes_list[:-1]:
    if isinstance(mes, HumanMessage):
        mes_content = getattr(mes, "content", None)
        if isinstance(mes_content, list):
            text_only = self._strip_image_url_from_content(mes_content)
            mes.content = text_only if text_only else mes_content
```

- `state_mes_list[:-1]` 遍历**所有历史 HumanMessage**，剥离 image_url 块
- 这直接修改了消息列表中间的消息内容
- 从第一条被修改的历史消息起，前缀缓存失效

**影响**：只在有媒体（图片/视频/音频）的会话中触发，但一旦触发就会对整个历史生效。

**建议**：

- 历史消息的 image_url 剥离应在**首次处理时就完成**（写入 checkpointer 时），而不是每次 `before_model` 都重新扫
- 或在 checkpointer 恢复时一次性剥离，后续调用不再重复

---

### 4. Summarization（非压缩路径）— 可优化

**文件**：`agent/middlewares/summarization.py`

**问题**：

`wrap_model_call` / `awrap_model_call` **每次调用都触发**，即使最终跳过压缩：

```python
# 跳过压缩的路径 (line 2131-2136)
if self._should_skip_compression(session_id):
    # ... 仍然调用 _execute_with_recovery
    response = self._execute_with_recovery(request, handler, session_id)
    return response

# cooldown 路径 (line 2139-2159)
if not forced and (cooldown_active or attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
    # ... 仍然可能 override system_message (line 2153)
    if self._need_update_system_prompt:
        rebuilt = state_register_mem.get_state(session_id, "system_prompt", "")
        if rebuilt:
            request = request.override(system_message=SystemMessage(content=rebuilt))
```

- cooldown 路径里有条件地 `request.override(system_message=...)`，会再次注入系统提示
- 与 ContextEngineHook 的 system_message 注入重复，且可能覆盖不同的内容

**影响**：cooldown 路径只在压缩刚发生后才有意义（`_compaction_just_happened` 为 True），频率不高但仍是额外的前缀断裂点。

**建议**：

- `_need_update_system_prompt` 路径应与 ContextEngineHook 统一，避免两处分别 override system_message
- skip 路径直接透传 request，不做任何 override

---

### 5. Summarization（压缩触发时）— 可接受

**文件**：`agent/middlewares/summarization.py`

- `_dispatch_overflow_route` → `_run_budget_truncation` / `_execute_compact`
- `messages[i] = m.model_copy(update={"content": new_content})`（第1726行）
- `RemoveMessage(REMOVE_ALL_MESSAGES)` + 摘要消息对替换（第2045行）
- `preemptive_truncate` / `aggressive_truncate` 修改 ToolMessage content 和 AIMessage tool_calls args

**评判**：**可接受**。压缩的本质就是重写消息，前缀缓存本应在此断裂。这不是 bug，是设计如此。

---

### 6-10. 其他中间件 — 可接受

| 中间件                  | 原因                                                                                                  |
| ----------------------- | ----------------------------------------------------------------------------------------------------- |
| SubagentCompletionDrain | `before_model` 仅**追加**消息到末尾（`return {"messages": items}`），不改中间消息，追加不影响已有前缀 |
| TaskIntent              | `before_model` 仅**追加**引导消息到末尾                                                               |
| OutputRepetitionGuard   | `wrap_model_call` post 阶段改写的是**输出**，不改输入消息列表                                         |
| MaxTokensBoost          | 仅修改 `max_tokens` 参数和 `callbacks`，不改消息内容                                                  |
| LLMRetry                | 仅修改模型/退避参数，不改消息内容                                                                     |

---

## 影响量化

假设一个典型会话有 20 轮对话，每轮 1 次模型调用（实际更多）：

| 场景                       | 前缀缓存命中率（理想） | 实际（当前） | 损失                                                    |
| -------------------------- | ---------------------- | ------------ | ------------------------------------------------------- |
| 正常对话（无媒体、无压缩） | ~95%                   | **~0%**      | ToolCallNormalize 每次全量重建                          |
| 有媒体的对话               | ~90%                   | **~0%**      | ToolCallNormalize + MultimodalProcessor 双重破坏        |
| 压缩后下一轮               | ~50%（压缩后前缀短）   | **~0%**      | 压缩断裂是正常的，但 ToolCallNormalize 连短前缀也保不住 |

**核心损失**：ToolCallNormalize 让前缀缓存在 **100% 的调用中失效**，这是最大的可优化点。

---

## 已实施修复

### P0-1：ToolCallNormalize 无变化时跳过重建 — 已完成

**文件**：`agent/middlewares/tool_call_normalize.py`

**改动前**：

```python
def _before_model_impl(self, state):
    normalize_messages = sanitize_tool_use_result_pairing(state["messages"])
    normalize_messages = [m for m in normalize_messages if not isinstance(m, RemoveMessage)]
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *normalize_messages]}
```

每次 `before_model` 都无条件执行 `RemoveMessage(REMOVE_ALL_MESSAGES)` + 全量重建，即使消息列表没有任何变化。

**改动后**：

```python
def _before_model_impl(self, state):
    original = state["messages"]
    normalized = sanitize_tool_use_result_pairing(original)
    normalized = [m for m in normalized if not isinstance(m, RemoveMessage)]

    # 无变化时返回 None，不发 RemoveMessage，保留前缀缓存
    if len(normalized) == len(original) and all(
        a is b for a, b in zip(normalized, original)
    ):
        return None

    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *normalized]}
```

**原理**：`sanitize_tool_use_result_pairing`（`pub/func/transcript_repair.py:308`）在无变化时返回原列表对象（`return cleaned if changed else messages`）。此时 filter 不产生变化（正常状态下无 RemoveMessage 残留），`a is b` 全为 True，直接返回 `None` — LangGraph 不做任何状态写入，前缀缓存完整保留。只有检测到实际需要修复时才发 RemoveMessage + 重建。

**预期效果**：前缀缓存命中率 ~0% → ~90%。

---

### P0-2：ContextEngineHook system_message 内容相同时不 override — 已完成

**文件**：`agent/middlewares/context_engine/core.py`

**改动前**：

```python
def _wrap_model_call_impl(self, request):
    return request.override(
        system_message=SystemMessage(
            content=self._get_and_reload_system_prompt(
                self._get_session_id_or_raise(request.state)
            )
        )
    )
```

每次 `wrap_model_call` 都创建新 `SystemMessage` 并 override，即使内容完全相同。

**改动后**：

```python
def _wrap_model_call_impl(self, request):
    session_id = self._get_session_id_or_raise(request.state)
    prompt_str = self._get_and_reload_system_prompt(session_id)
    existing = getattr(request, "system_message", None)
    if (
        existing is not None
        and isinstance(existing, SystemMessage)
        and existing.content == prompt_str
    ):
        return request  # 内容相同，不 override，保留前缀缓存
    return request.override(system_message=SystemMessage(content=prompt_str))
```

**原理**：先检查 request 上已有的 `system_message`，如果内容相同则原样返回 request，不创建新对象。只有 system prompt 实际变化时（如 TODO 状态更新、子代理状态变化）才 override。

**预期效果**：消除同一轮内多次模型调用之间的重复 system_message 断裂。

---

### 测试验证

全部测试通过，无功能回退：

```
tests/agent/middlewares/                              750 passed, 1 skipped
tests/context_engine/store/test_interrupt_marker_approach.py   11 passed
tests/agent/middlewares/context_engine/test_context_engine_denial_persistence.py   8 passed
tests/agent/tools/subagent/test_carrier_metadata_locking.py     2 passed
tests/pub/func/message/test_tool_result_ttl.py                 28 passed
tests/agent/middlewares/test_context_engine_session_guard.py     2 passed
```

关键测试覆盖：

- `test_fact_a_marker_model_visible_on_next_invoke` — 验证 ToolCallNormalize 返回 None 后 marker 仍在状态中对模型可见 ✅
- `test_fact_b_tool_call_healing_drops_trailing_human_when_no_marker` — 验证有变化时仍执行 RemoveMessage + 重建 ✅
- `test_context_engine_denial_persistence` — 验证 HITL denial 消息经 sanitize 后仍持久化 ✅
- `test_carrier_metadata_locking` — 验证 carrier HumanMessage 经 sanitize 后 metadata 完整 ✅
- `test_tool_result_ttl` — 验证工具结果 TTL 机制不受影响 ✅

---

## 待优化项（未实施）

### P1：MultimodalProcessor 历史消息剥离一次性完成

**问题**：`media_pipeline.py:83-89` 每次 `before_model` 都遍历 `state_mes_list[:-1]` 剥离历史消息中的 image_url 块。

**建议**：在 checkpointer 恢复时一次性剥离，后续调用不再重复。

### P1：Summarization cooldown 路径统一 system_message 注入

**问题**：`summarization.py:2150-2153` cooldown 路径里单独 `request.override(system_message=...)`，与 ContextEngineHook 重复。

**建议**：统一由 ContextEngineHook 负责系统提示注入，Summarization 不再单独 override。
