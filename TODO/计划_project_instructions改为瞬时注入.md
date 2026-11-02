# 计划：project_instructions 改为瞬时注入（不常驻消息列表）

- 建档日期：2026-11-02
- 状态：**待评审**
- 来源：用户反馈——project_instructions（≤ 8 k）注入后常驻消息列表，后续每轮都在上下文里，浪费空间
- 安全等级：**低**——仅改提醒投递行为，不涉及系统提示词缓存、不新增状态键

---

## 0. 问题

`project_instructions`（项目的 `AGENTS.md`，回退 `CLAUDE.md`）作为提醒目录的一个来源注册（`agent/middlewares/reminder_catalog/sources.py:112-121`，优先级 62，预算 8 000，`only_if_changed=True`）。

当前行为：

1. 内容变化的那一轮（目录切换 / 首次 / 压缩后）→ `resolve_injections` 解析成功 → 注入一条 `<system-reminder>` HumanMessage
2. 后续每一轮内容没变 → `only_if_changed` 跳过 → 不产生新注入
3. **但** `DynamicReminderMiddleware` 每轮做 whole-list rewrite（`dynamic.py:129-136`）：`RemoveMessage(REMOVE_ALL_MESSAGES)` + `*messages[:-1]` + `*reminders` + `messages[-1]`。旧提醒消息在 `messages[:-1]` 里被**原样重发**，所以那条 8 k HumanMessage 一直留在对话历史里，直到压缩把它摘要掉
4. 压缩后 `reset_session` 清空 throttle state（`core.py:186-193`）→ 下一轮 `only_if_changed` 无历史 hash → 重新注入

问题在第 3 步：project_instructions 是**半稳定**内容（只在切目录时变），注入一次就够了，后续轮次模型已经读过。让它常驻消息列表每轮占 8 k，压缩时还要被摘要器处理，是纯浪费。

**期望行为**：只在内容变化的那一轮出现，下一轮就从消息列表清除；压缩后的第一轮重新注入。

---

## 1. 设计

### 1.1 transient 标志

给 `ReminderSource` dataclass 加一个 `transient: bool = False` 字段。`transient=True` 的来源只在**被解析注入的那一轮**出现在消息列表里；下一轮的 whole-list rewrite 会把它从 `messages[:-1]` 中滤除。

只有 `project_instructions` 设 `transient=True`。其余 7 个来源保持 `transient=False`（它们体积小——最大 2 000 字符——且每轮都需要模型看到当前值，常驻是正确行为）。

### 1.2 行为对比

| 场景 | 现在 | 改后 |
|---|---|---|
| 切目录那一轮 | 注入 8 k HumanMessage | 同左（不变） |
| 后续不切目录的每一轮 | 旧 8 k 常驻消息列表 | **旧 8 k 被滤除，消息列表里没有** |
| 压缩后第一轮 | 重新注入 | 同左（不变，`reset_session` 清 hash → 重新解析） |
| 首轮 | 注入 | 同左 |

### 1.3 为什么不动系统提示词

之前的方案（移入系统提示词尾部）需要新增 mtime 状态键、在 `WorkspaceNoticeMiddleware` 的 early return 前插缓存失效逻辑、处理 Anthropic BP1 断点变化——复杂度高。本方案只改 `DynamicReminderMiddleware` 的消息重写逻辑，不碰系统提示词、不碰缓存、不新增状态键。

---

## 2. 实施步骤（含完整 diff）

### Step 1：`core.py` — ReminderSource 加 transient 字段 + 查询函数

**文件**：`agent/middlewares/reminder_catalog/core.py`

**1a.** `ReminderSource` dataclass（:62-86）在 `tags` 之后加一个字段：

```diff
 @dataclass(frozen=True)
 class ReminderSource:
     """One injection point's declaration.

     :param source_id: Stable identifier (also the message's ``origin`` and the
         ``source=`` attribute in the tag).
     :param channel: Delivery channel (only ``CURRENT_TURN`` is consumed today).
     :param builder: ``(session_id, state) -> text | None``; ``None``/blank means
         "nothing to say this turn".
     :param budget_chars: Per-source cap; longer text is clipped with an ellipsis.
     :param priority: Higher wins under the total budget; 100 = must keep.
     :param cooldown_turns: Inject at most once per N turns (0 = no cooldown).
     :param only_if_changed: Skip when the rendered text is identical to the last
         text this session injected for the source.
+    :param transient: When True, the reminder is stripped from the message list
+        on every turn it is not re-resolved, so it only occupies context on the
+        turn its content actually changed (and the first turn after a compaction).
+        Use for large, semi-stable payloads (project_instructions) that the model
+        reads once and does not need repeated every turn.
     """

     source_id: str
     channel: ReminderChannel
     builder: Callable[[str, dict[str, Any]], str | None]
     budget_chars: int = 2000
     priority: int = 50
     cooldown_turns: int = REMINDER_CATALOG["default_cooldown_turns"]
     only_if_changed: bool = False
     is_meta: bool = True
     tags: tuple[str, ...] = field(default_factory=tuple)
+    transient: bool = False
```

**1b.** 在 `registered_sources()` 函数（:111-113）之后新增一个查询函数：

```python
def transient_source_ids() -> frozenset[str]:
    """Source ids marked ``transient`` — their reminders are stripped from the
    message list on turns they are not re-injected.

    Reads the live registry so a test that swaps ``_SOURCES`` sees its own
    sources (a module-level constant computed at import would be stale).
    """
    return frozenset(src.source_id for src in _SOURCES.values() if src.transient)
```

**1c.** 更新 `__all__`（:35-42）加入 `transient_source_ids`：

```diff
 __all__ = [
     "ReminderChannel",
     "ReminderSource",
     "clear_session",
     "registered_sources",
     "register",
+    "transient_source_ids",
     "resolve_injections",
 ]
```

### Step 2：`sources.py` — project_instructions 设 transient=True

**文件**：`agent/middlewares/reminder_catalog/sources.py`

`project_instructions` 注册块（:112-121）加一行：

```diff
     register(
         ReminderSource(
             source_id="project_instructions",
             channel=ReminderChannel.CURRENT_TURN,
             builder=_as_builder(_build_project_instructions_block),
             budget_chars=8000,
             priority=62,
             only_if_changed=True,
+            # 8 k of semi-stable AGENTS.md content: inject once on the turn it
+            # changed (directory switch / first turn / post-compaction), then
+            # strip it from the message list on subsequent turns so it does not
+            # ride every turn's context window.  The model reads it once.
+            transient=True,
         )
     )
```

### Step 3：`dynamic.py` — rewrite 前滤除旧 transient 提醒

**文件**：`agent/middlewares/reminder_catalog/dynamic.py`

**3a.** 导入 `transient_source_ids`（:31）：

```diff
-from .core import clear_session, resolve_injections
+from .core import clear_session, resolve_injections, transient_source_ids
```

**3b.** 在 `_already_injected_this_turn` 函数之后（:63 之后）新增辅助函数：

```python
def _is_transient_reminder(msg: Any, transient_ids: frozenset[str]) -> bool:
    """True when *msg* is a reminder for a transient source.

    A transient source's reminder must not persist in the message list beyond
    the turn it was injected on.  The caller strips these from ``messages[:-1]``
    during the whole-list rewrite so they only appear on injection turns.
    """
    if not transient_ids or not is_system_reminder(msg):
        return False
    meta = getattr(msg, "metadata", None) or {}
    return meta.get("origin") in transient_ids
```

**3c.** 改写 `abefore_model` 的 line 100-136。当前代码：

```python
            injections = resolve_injections(session_id, state, current_turn=turn)
            if not injections:
                return None

            reminders = [
                HumanMessage(
                    content=wrap_reminder(source_id, content),
                    metadata=system_reminder_metadata(source_id),
                )
                for source_id, content in reversed(list(injections.items()))
            ]
            logger.debug(
                "DynamicReminderMiddleware: injecting {} reminder(s) for session {} (turn {}): {}",
                len(reminders),
                session_id,
                turn,
                ", ".join(injections),
            )
            return {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *messages[:-1],
                    *reminders,
                    messages[-1],
                ]
            }
```

替换为：

```python
            transient_ids = transient_source_ids()
            injections = resolve_injections(session_id, state, current_turn=turn)

            reminders = [
                HumanMessage(
                    content=wrap_reminder(source_id, content),
                    metadata=system_reminder_metadata(source_id),
                )
                for source_id, content in reversed(list(injections.items()))
            ]

            # Strip old transient reminders from the message tail so they do not
            # persist beyond the turn they were injected on.  Without this, an
            # only_if_changed source (project_instructions: 8 k of AGENTS.md)
            # would ride every turn's context via the messages[:-1] re-emit.
            tail = messages[:-1]
            stripped = False
            if transient_ids:
                filtered = [
                    msg for msg in tail if not _is_transient_reminder(msg, transient_ids)
                ]
                stripped = len(filtered) < len(tail)
                tail = filtered

            # The original early return was `if not injections: return None`.
            # That skips the rewrite when nothing was resolved — but if old
            # transient reminders are sitting in the tail, they MUST be cleaned
            # up even with an empty injection set, or they escape another turn.
            if not injections and not stripped:
                return None

            logger.debug(
                "DynamicReminderMiddleware: injecting {} reminder(s) for session {} (turn {}): {}",
                len(reminders),
                session_id,
                turn,
                ", ".join(injections),
            )
            return {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *tail,
                    *reminders,
                    messages[-1],
                ]
            }
```

**为什么 `stripped` 布尔而不是比较长度**：当 `transient_ids` 为空（没有 transient 来源注册）时，filter 是 no-op，`stripped` 为 `False`，条件退化为 `if not injections: return None`——与原始行为字节一致。当有 transient 来源但 tail 里没有它们的旧提醒时，`stripped` 也为 `False`，同样退化为原始行为。只有 tail 里**确实**有旧 transient 提醒被滤掉时，`stripped` 才为 `True`，即使 `injections` 为空也执行 rewrite。

### Step 4：测试

**文件**：`tests/agent/middlewares/test_reminder_catalog.py`

**4a.** 测试 fixture（:36-71）加一个 transient 源 `delta`：

```diff
     sources = {
         "alpha": ReminderSource(
             source_id="alpha",
             channel=ReminderChannel.CURRENT_TURN,
             builder=lambda sid, state: state.get("alpha_text"),
             priority=90,
             budget_chars=100,
         ),
         "beta": ReminderSource(
             source_id="beta",
             channel=ReminderChannel.CURRENT_TURN,
             builder=lambda sid, state: state.get("beta_text"),
             priority=10,
             budget_chars=100,
             only_if_changed=True,
         ),
         "gamma": ReminderSource(
             source_id="gamma",
             channel=ReminderChannel.CURRENT_TURN,
             builder=lambda sid, state: state.get("gamma_text"),
             priority=50,
             budget_chars=100,
             cooldown_turns=5,
         ),
+        "delta": ReminderSource(
+            source_id="delta",
+            channel=ReminderChannel.CURRENT_TURN,
+            builder=lambda sid, state: state.get("delta_text"),
+            priority=40,
+            budget_chars=100,
+            only_if_changed=True,
+            transient=True,
+        ),
     }
```

**4b.** `TestRegistration` 类（:157-187）新增 transient 断言：

```python
    def test_project_instructions_is_transient(self, _isolated_catalog):
        source = _isolated_catalog["real"]["project_instructions"]

        assert source.transient is True, (
            "8 k of AGENTS.md must not persist in the message list beyond the"
            " turn it was injected on"
        )

    def test_only_project_instructions_is_transient(self, _isolated_catalog):
        transient = {
            sid for sid, src in _isolated_catalog["real"].items() if src.transient
        }

        assert transient == {"project_instructions"}, (
            "no other source should be transient without an explicit reason"
        )
```

**4c.** `TestMiddleware` 类末尾新增 transient 行为测试：

```python
    @pytest.mark.asyncio
    async def test_a_transient_reminder_is_stripped_on_the_next_turn(self):
        """A transient source's reminder must not persist beyond its injection turn."""
        mw = DynamicReminderMiddleware()

        # Turn 1: delta content is new → injected.
        state1 = self._state(HumanMessage(content="first"), delta_text="D1")
        out1 = await mw.abefore_model(state1)
        assert out1 is not None
        turn1_msgs = out1["messages"]
        # The delta reminder sits before the user message.
        delta_reminder = next(
            m for m in turn1_msgs if isinstance(m, HumanMessage) and m.metadata.get("origin") == "delta"
        )
        assert delta_reminder.content.startswith('<system-reminder source="delta">')

        # Turn 2: delta content unchanged → only_if_changed skips → no new delta.
        # But the old delta reminder from turn 1 is in the tail.  The middleware
        # must rewrite to strip it, even though injections is non-empty for other
        # sources (or empty if no other source fires).
        state2 = self._state(
            HumanMessage(content="first"),
            AIMessage(content="answer"),
            HumanMessage(content="second"),
            delta_text="D1",  # unchanged
        )
        out2 = await mw.abefore_model(state2)
        assert out2 is not None, "must still rewrite to strip the old transient reminder"
        turn2_msgs = out2["messages"]
        # No delta reminder in the rewritten list.
        assert not any(
            isinstance(m, HumanMessage) and m.metadata.get("origin") == "delta"
            for m in turn2_msgs
        ), "the old transient reminder was stripped"

    @pytest.mark.asyncio
    async def test_empty_injections_still_rewrite_to_strip_transient_reminders(self):
        """The early return must not skip cleanup when old transient reminders exist."""
        mw = DynamicReminderMiddleware()

        # Seed a transient reminder into the message list as if it was injected
        # last turn, then start a new turn where NO source resolves (empty state).
        old_delta = HumanMessage(
            content=wrap_reminder("delta", "stale content"),
            metadata=system_reminder_metadata("delta"),
        )
        state = self._state(
            old_delta,
            HumanMessage(content="next turn"),
        )
        # No alpha_text / beta_text / gamma_text / delta_text → injections empty.
        out = await mw.abefore_model(state)

        assert out is not None, (
            "even with empty injections the rewrite must run to strip the old"
            " transient reminder"
        )
        msgs = out["messages"]
        assert not any(
            isinstance(m, HumanMessage) and m.metadata.get("origin") == "delta"
            for m in msgs
        ), "the stale transient reminder was stripped"

    @pytest.mark.asyncio
    async def test_empty_injections_with_no_transient_reminder_is_a_noop(self):
        """The original early-return behavior is preserved when there is nothing to clean."""
        mw = DynamicReminderMiddleware()

        # No transient reminder in the tail, no source text → must return None.
        state = self._state(HumanMessage(content="hi"))
        assert await mw.abefore_model(state) is None

    @pytest.mark.asyncio
    async def test_a_transient_reminder_is_replaced_when_content_changes(self):
        """When the transient source's content changes, the old one is stripped and the new one injected."""
        mw = DynamicReminderMiddleware()

        # Turn 1: inject D1.
        state1 = self._state(HumanMessage(content="first"), delta_text="D1")
        out1 = await mw.abefore_model(state1)
        assert out1 is not None

        # Turn 2: content changed to D2 → new injection, old D1 stripped.
        state2 = self._state(
            HumanMessage(content="first"),
            AIMessage(content="answer"),
            HumanMessage(content="second"),
            delta_text="D2",
        )
        out2 = await mw.abefore_model(state2)
        assert out2 is not None
        delta_msgs = [
            m for m in out2["messages"]
            if isinstance(m, HumanMessage) and m.metadata.get("origin") == "delta"
        ]
        assert len(delta_msgs) == 1, "exactly one delta reminder (the new one)"
        assert "D2" in delta_msgs[0].content
```

### Step 5：文档

**文件**：`wiki/context-governance/system-reminder/README*.md`（四个语言）

注册来源表加 `Transient` 列，`project_instructions` 标 `Yes`，其余标 `No`。在表后补一段说明：

> **Transient sources** (`transient=True`) only occupy the message list on the turn they are re-resolved — the next turn's whole-list rewrite strips them from `messages[:-1]` so they do not ride every turn's context window. `project_instructions` (≤ 8 000 chars of semi-stable `AGENTS.md`) is the first transient source: the model reads it once on the directory-switch / first / post-compaction turn, and it is absent from subsequent turns until the content changes again or a compaction resets the throttle state.

---

## 3. 边界与取舍

### 3.1 模型丢失上下文？

后续轮次消息列表里没有 project_instructions 了，模型是否"忘了"项目指令？

**不会**：模型在注入轮已经完整读过 AGENTS.md，后续轮次它基于已读内容推理。这与人类读完一份规范后开始工作——不需要每说一句话都把规范摆在面前。project_instructions 是**参考材料**（规则、约定、目录结构），不是**实时状态**（todo、TaskFlow 看板）——后者每轮都会变，必须常驻；前者不会。

### 3.2 长对话中模型"遗忘"

如果对话很长（几十轮），模型可能在注意力衰减时不再严格遵循 project_instructions。但：
- 压缩后 `reset_session` 清 hash → 重新注入，所以压缩是一个自然刷新点
- 如果需要更频繁的刷新，可以给 project_instructions 加一个 `cooldown_turns`（比如 20 轮重新注入一次），但这与 `transient` 正交，可以后续单独加

### 3.3 只影响 project_instructions

`transient` 是通用机制（`ReminderSource` 上的字段），但当前只有 `project_instructions` 设 `True`。其余 7 个来源体积小（最大 2 000 字符）且是实时状态，保持常驻。如果未来有其他大体积半稳定来源（比如一个项目级的 `CONVENTIONS.md`），可以直接设 `transient=True` 复用同一机制。

### 3.4 与 whole-list rewrite 的交互

`DynamicReminderMiddleware` 每轮首个模型调用做一次 rewrite。改动后：
- 有新注入或有旧 transient 待清理 → 执行 rewrite
- 都没有 → `return None`（no-op，与当前行为一致）

不影响 `_already_injected_this_turn`（line 90）的 resumed-turn 去重——一个 resumed turn 保持已有提醒块不变。

### 3.5 子代理

子代理不注册 `DynamicReminderMiddleware`，不受影响。

---

## 4. 验收清单

- [ ] `project_instructions` 注入后，下一轮（内容不变）消息列表里没有它
- [ ] `project_instructions` 注入后，下一轮（内容变化）新注入替换旧版本
- [ ] `injections` 为空但旧 transient 提醒存在时，仍执行 rewrite 清理
- [ ] `injections` 为空且无 transient 提醒时，`return None`（no-op）
- [ ] 压缩后第一轮重新注入 project_instructions
- [ ] 其余 7 个来源不受影响（仍常驻消息列表）
- [ ] `uv run --no-sync pytest tests/agent/middlewares/test_reminder_catalog.py -q` PASS
- [ ] `uv run --no-sync python scripts/check_docs_parity.py` PASS
- [ ] `uv run --no-sync python scripts/check_doc_links.py` PASS

---

## 5. 涉及文件一览

| 文件 | 改动类型 |
|---|---|
| `agent/middlewares/reminder_catalog/core.py` | `ReminderSource` 加 `transient` 字段 |
| `agent/middlewares/reminder_catalog/sources.py` | `project_instructions` 设 `transient=True` |
| `agent/middlewares/reminder_catalog/dynamic.py` | rewrite 前滤除旧 transient 提醒；early return 前检查清理需求 |
| `tests/agent/middlewares/test_reminder_catalog.py` | 新增 transient 断言 + 滤除行为测试 |
| `wiki/context-governance/system-reminder/README*.md` | 来源表加 transient 列 |
