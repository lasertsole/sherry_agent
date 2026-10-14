# 计划：memory review 防爆窗口（参考 hermes-agent）

- 建档日期：2026-10-14
- 状态：方案设计，未开工
- 参考：hermes-agent `background_review.py` + `context_compressor.py`
- 前置：`计划_修复plan_extraction重复抽取.md` 已将 plan extraction 独立到 plan 完成路径。本计划只管 memory review。

---

## 0. 现状问题

memory review 在压缩路径触发，收到 `original_messages`（**完整未压缩对话**）：

```python
# nudges.py:465-466
sanitized = sanitize_tool_use_result_pairing(list(messages))  # messages = original_messages（完整对话）
await _nudge_memory(session_id, system_prompt, sanitized)
```

压缩触发时对话接近 `window × 0.8 ≈ 105K` tokens。memory review agent（main 模型）拿：
- system_prompt(~4K) + 完整对话(~105K) + memory review prompt(~2K) = **~111K**

131K 窗口下差 20K 就满。如果对话更大、system_prompt 更大（skills index 大）、或 memory review prompt 更长，直接爆。

---

## 1. hermes-agent 的做法

hermes-agent 有三种策略，按情况选：

| 策略 | 条件 | 传什么 | 成本 |
|---|---|---|---|
| **cache-warm 同模型** | 默认（不路由到便宜模型） | 完整快照，但 pin 住父 agent 的 system_prompt / session_id / tools → 命中同一前缀缓存 → 完整重放 = cache reads | 低（缓存命中） |
| **digest 异模型** | 路由到便宜模型（缓存冷） | `_digest_history()`：保留最后 24 条原文，更早的折叠成摘要（USER ≤300 字符/条、ASSISTANT ≤200 字符/条） | 低（折叠后远小于完整对话） |
| **压缩 summarizer** | 压缩时 | 只传被丢弃的中间段，硬截断（每条 ≤6000 字符、工具参数 ≤1500 字符、reasoning 剥离） | 低（只传丢弃段 + aux 模型） |

hermes 的 memory review 不在压缩路径——它在每 N 轮（默认 10）turn 结束后 daemon thread 跑。压缩路径只调 aux summarizer。

---

## 2. 设计

### 2.1 核心策略：memory review 改用 aux 模型 + digest

memory review 的职责是"从即将丢失的消息中提取值得长期记忆的事实"。它不需要完整对话的逐字细节——需要的是"发生了什么"的摘要级信息。用 aux 模型 + digest 折叠历史：

| 维度 | 改前 | 改后 |
|---|---|---|
| 模型 | `build_main_llm()`（main 模型） | `build_auxiliary_llm(temperature=0)`（aux 模型） |
| 输入 | 完整对话 `original_messages`(~105K) | digest 折叠后的消息(~15-25K) |
| agent 工厂 | 共享 `_create_nudge_agent` | 专用 `_create_memory_review_agent`（aux 模型） |

### 2.2 digest 函数

**新文件**：`agent/middlewares/summarization/digest.py`

```python
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage

#: 保留最近 N 条消息原文（不折叠）
DIGEST_TAIL = 24

#: 折叠时每条消息的字符上限
DIGEST_USER_MAX = 300
DIGEST_ASSISTANT_MAX = 200
DIGEST_TOOL_MAX = 150


def digest_history(messages: list[BaseMessage], tail: int = DIGEST_TAIL) -> list[BaseMessage]:
    """折叠历史消息，避免爆上下文窗口。

    对标 hermes-agent background_review.py::_digest_history()。

    策略：
    - 保留最后 ``tail`` 条消息原文（不折叠）
    - 更早的消息折叠成一条 HumanMessage 摘要（每条按角色截断）
    - 如果消息总数 <= tail，原样返回（不需要折叠）

    返回的消息列表可直接传给 nudge agent。
    """
    if len(messages) <= tail:
        return list(messages)

    old_messages = messages[:-tail]
    recent_messages = messages[-tail:]

    digest_lines: list[str] = []
    for msg in old_messages:
        if isinstance(msg, HumanMessage):
            content = _text_of(msg)
            role = "USER"
            limit = DIGEST_USER_MAX
        elif isinstance(msg, AIMessage):
            content = _text_of(msg)
            role = "ASSISTANT"
            limit = DIGEST_ASSISTANT_MAX
        elif isinstance(msg, ToolMessage):
            content = _text_of(msg)
            role = "TOOL"
            limit = DIGEST_TOOL_MAX
        else:
            continue  # 跳过 SystemMessage 等

        content = content.strip()
        if not content:
            continue
        if len(content) > limit:
            content = content[:limit] + "..."
        digest_lines.append(f"{role}: {content}")

    if not digest_lines:
        return recent_messages

    digest_text = (
        "[Earlier conversation digest — older turns summarized to save context. "
        f"{len(old_messages)} messages condensed.]\n\n"
        + "\n".join(digest_lines)
    )

    return [HumanMessage(content=digest_text)] + recent_messages


def _text_of(msg: BaseMessage) -> str:
    """提取消息的文本内容（处理 str 和 list[dict] 两种 content 格式）。"""
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content)
```

### 2.3 专用 agent 工厂（aux 模型）

**文件**：`agent/middlewares/summarization/nudges.py`

```python
async def _create_memory_review_agent(system_prompt: str):
    """memory review 专用 agent——aux 模型 + nudge 工具门控。

    不用 build_main_llm：memory review 的输入是 digest 折叠后的摘要级信息，
    任务是"提取值得长期记忆的事实"，aux 模型够用。
    """
    from agent.middlewares import ToolCallNormalize, ToolGuardrails
    from models import build_auxiliary_llm
    from agent import get_agent_tools

    aux_llm = build_auxiliary_llm(temperature=0)
    agent_tools = get_agent_tools()
    return create_agent(
        model=aux_llm,
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            _NudgeLimitTool(),
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=list(agent_tools),
    )
```

### 2.4 `_nudge_memory` 改用 digest + aux 模型

**文件**：`agent/middlewares/summarization/nudges.py`

```python
async def _nudge_memory(session_id: str, system_prompt: str, messages: list[BaseMessage]) -> None:
    state_register_mem.set_state(session_id, _NUDGE_MEMORY_LOCK_KEY, True)
    try:
        # digest 折叠历史（对标 hermes-agent 的 _digest_history）
        from agent.middlewares.summarization.digest import digest_history
        digested = digest_history(messages)
        sanitized = sanitize_tool_use_result_pairing(digested)

        async with lane_slot(LaneType.NUDGE):
            _agent = await _create_memory_review_agent(system_prompt)  # ← aux 模型
            res = await _agent.ainvoke(
                input={
                    "session_id": session_id,
                    "messages": [
                        *sanitized,
                        HumanMessage(content=_render_prompt_facts(_MEMORY_REVIEW_PROMPT)),
                    ],
                }
            )
        logger.debug("nudge memory res is {}", res["messages"][-1])
    finally:
        state_register_mem.set_state(session_id, _NUDGE_MEMORY_LOCK_KEY, False)
```

### 2.5 `_run_compression_nudges` 传参改为被丢弃的消息段

**文件**：`agent/middlewares/summarization/nudges.py`

```python
# 改前（plan extraction 已移除，只剩 memory review）：
async def _run_compression_nudges(session_id, messages):  # messages = original_messages（完整对话）
    system_prompt = _get_and_reload_system_prompt(session_id)
    sanitized = sanitize_tool_use_result_pairing(list(messages))
    await _nudge_memory(session_id, system_prompt, sanitized)

# 改后：传 messages_to_summarize（被丢弃的部分），不是 original_messages
async def _run_memory_review(session_id, messages_to_summarize):
    system_prompt = _get_and_reload_system_prompt(session_id)
    await _nudge_memory(session_id, system_prompt, list(messages_to_summarize))
```

**文件**：`agent/middlewares/summarization/core.py`

```python
# 改前：
def _fire_compression_nudges(self, session_id, original_messages, messages_to_summarize):
    _schedule_compression_nudges(session_id, original_messages)  # 传完整对话

# 改后：
def _fire_compression_nudges(self, session_id, original_messages, messages_to_summarize):
    _schedule_memory_review(session_id, messages_to_summarize)  # 传被丢弃的部分
    _schedule_compression_todo_update(session_id, messages_to_summarize)
```

### 2.6 `schedule_compression_nudges` → `schedule_memory_review`

**文件**：`agent/middlewares/summarization/nudges.py`

```python
def schedule_memory_review(session_id: str, messages_to_summarize: Sequence[BaseMessage]) -> bool:
    """调度 memory review（压缩时触发）。

    改前传 original_messages（完整对话），改后传 messages_to_summarize（被丢弃的部分）。
    memory review 的职责是抢救即将丢失的消息——被丢弃的才是要 review 的。
    """
    from agent.middlewares.agent_switch import middleware_option

    if not middleware_option(session_id, "Summarization", "nudge"):
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False

    if _nudges_locked(session_id):  # _NUDGE_MEMORY_LOCK_KEY 或 _PLAN_EXTRACTION_LOCK_KEY
        return False

    # need_plan 逻辑已删除（plan extraction 移到 plan 完成路径）

    coro = _run_memory_review(session_id, list(messages_to_summarize))
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        return False
    _COMPRESSION_NUDGE_TASKS.add(task)
    task.add_done_callback(_COMPRESSION_NUDGE_TASKS.discard)
    return True
```

---

## 3. 双重保护

memory review 有两层防爆窗口保护：

| 层 | 机制 | 效果 |
|---|---|---|
| **第一层：传被丢弃的部分** | `messages_to_summarize` 而非 `original_messages` | 输入从 ~105K 降到 ~60K（被丢弃的部分通常比完整对话小） |
| **第二层：digest 折叠** | `digest_history()` 保留最后 24 条原文 + 更早的折叠成摘要 | 输入从 ~60K 降到 ~15-25K（折叠后每条 ≤300 字符） |

两层叠加：~105K → ~15-25K。131K 窗口下不可能爆。

---

## 4. token 成本对比

| 项 | 改前 | 改后 |
|---|---|---|
| 模型 | main 模型（DeepSeek Chat 等） | aux 模型（Qwen 9B Q4 本地 / 便宜远程） |
| 输入 | system_prompt(~4K) + 完整对话(~105K) + prompt(~2K) = **~111K** | system_prompt(~4K) + digest(~20K) + prompt(~2K) = **~26K** |
| 输出 | ~1K | ~1K（相同） |
| 成本/次 | ~$0.016 | ~$0.0003 或免费 |
| 爆窗口风险 | 131K 差 20K | 不可能（26K ≪ 131K） |
| **总成本降幅** | — | **输入降 77%，模型降一级，总成本降 ~98%** |

---

## 5. 关键文件清单

| 文件 | 改动 |
|---|---|
| `agent/middlewares/summarization/digest.py` | **新增**：`digest_history()` + `_text_of()` |
| `agent/middlewares/summarization/nudges.py` | `_nudge_memory` 改用 digest + `_create_memory_review_agent`（aux 模型）；`_run_compression_nudges` → `_run_memory_review`（传 `messages_to_summarize`）；`schedule_compression_nudges` → `schedule_memory_review`（传 `messages_to_summarize`） |
| `agent/middlewares/summarization/core.py` | `_fire_compression_nudges` 改为调 `schedule_memory_review(session_id, messages_to_summarize)` |

### 不改的

| 项 | 原因 |
|---|---|
| `_create_nudge_agent` | 保留——如果其他地方还用 main 模型的 nudge agent（目前没有，但保留以防未来） |
| `_NudgeLimitTool` | 工具门控不变 |
| `_MEMORY_REVIEW_PROMPT` | prompt 不改 |
| memory review 触发时机 | 仍在压缩路径触发（不改为每 N 轮——那是 hermes 的做法，Sherry 的压缩路径触发已经够用） |
| plan extraction | 已在 `计划_修复plan_extraction重复抽取.md` 独立到 plan 完成路径 |

---

## 6. 测试

| 测试 | 场景 | 验证点 |
|---|---|---|
| `test_digest_short_history_unchanged` | 消息数 ≤ 24 | 原样返回，不折叠 |
| `test_digest_folds_old_messages` | 30 条消息 | 前 6 条折叠成摘要 + 后 24 条原文 |
| `test_digest_truncation` | 消息内容 > 300 字符 | 截断 + `...` |
| `test_digest_tool_message` | ToolMessage 在历史里 | 角色标为 `TOOL`，限 150 字符 |
| `test_memory_review_uses_aux_model` | mock `build_auxiliary_llm` | agent 用 aux 模型 |
| `test_memory_review_gets_discarded_messages` | 压缩触发 | memory review 收到 `messages_to_summarize`（被丢弃部分），非 `original_messages` |
| `test_memory_review_gets_digest` | 50 条被丢弃消息 | `_nudge_memory` 内部调 `digest_history`，传给 agent 的是折叠后的 |
| `test_memory_review_context_under_budget` | 大量消息压缩 | digest 后输入 < 30K tokens |

---

## 7. 风险

| 风险 | 对策 |
|---|---|
| digest 折叠丢失 memory-worthy 事实 | 保留最后 24 条原文（最近的对话最有价值）；旧消息折叠但保留摘要（不是完全丢弃） |
| aux 模型记忆提取质量不如 main | memory review 的任务是"提取值得记住的事实"，不是复杂推理；StepJudge/CompletionJudge 已验证 aux 能做同类任务；fail-open（失败不阻断） |
| `messages_to_summarize` 可能仍然很大（一次压缩丢弃很多） | digest 第二层保护：即使丢弃段有 60K，折叠后仍 < 30K |
| `digest_history` 对 multimodal 消息（图片/音频）的处理 | `_text_of` 只提取文本块；图片/音频块跳过（memory review 不需要看图） |
| 向后兼容 | 改前传 `original_messages`、用 main 模型；改后传 `messages_to_summarize`、用 aux + digest。行为变化但方向正确（更省、更安全），无需兼容旧行为 |
| `_create_nudge_agent` 变成无人调用 | 保留以防未来需要 main 模型的 nudge agent；可标 deprecated |

---

## 8. 与 hermes-agent 的对比

| 维度 | hermes-agent | Sherry 改后 |
|---|---|---|
| memory review 触发 | 每 N 轮 turn 后 daemon thread | 压缩时（在压缩路径触发） |
| memory review 模型 | main（cache-warm）或 aux（digest） | aux（digest） |
| memory review 输入 | 完整快照（cache-warm）或 digest | `messages_to_summarize`（被丢弃段）+ digest 折叠 |
| 压缩 summarizer | aux 模型，只传被丢弃的中间段 | Sherry 的压缩本身不变（Summarization middleware 的 `_apply_compression`） |
| 合并 prompt | memory + skill 合并为一次 fork | 不需要合并——plan extraction 已独立 |
| compression lock | session 级 SQLite 锁防并发压缩 | Sherry 有 per-session lock（`_state_lock`），但不是压缩专用。可后续加 |

Sherry 不照搬 hermes 的"每 N 轮触发"——Sherry 的压缩路径触发已够用（压缩时消息要丢失了，正好 review）。Sherry 也不需要 cache-warm 策略——直接用 aux 模型 + digest 更简单（不依赖 provider 的前缀缓存行为）。
