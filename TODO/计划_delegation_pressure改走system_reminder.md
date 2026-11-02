# 计划：DelegationPressureGate 改走 system-reminder 格式（最高优先级）

- 建档日期：2026-11-02
- 状态：**待评审**
- 来源：用户要求——delegation_pressure 的指令应走 `<system-reminder>` 格式，优先级最高
- 安全等级：**中**——改变注入消息的 metadata contract 和内容格式，影响 `is_system_reminder` 判定和摘要器序列化

---

## 0. 现状

`DelegationPressureGate`（`agent/middlewares/delegation_pressure/core.py`）有自己的 `abefore_model` 注入路径，注入的是**普通 HumanMessage**：

```python
# core.py:104-112
def _directive(self, content: str) -> HumanMessage:
    return HumanMessage(
        content=content,                                    # 以 "[SYSTEM DIRECTIVE: ...]" 文本开头
        metadata={
            "origin": "delegation_pressure",
            "internal": True,
            "provenance": "delegation_pressure",            # ← 不是 "reminder_catalog"
        },
    )
```

内容以 `[SYSTEM DIRECTIVE: DELEGATION PRESSURE]` / `[SYSTEM DIRECTIVE: DELEGATION PRESSURE — READ/WRITE DISABLED]` 文本前缀开头。

**两个问题**：

1. `is_system_reminder(msg)` 对它返回 `False`（provenance 不匹配）→ 摘要器 `_serialize_for_summary` 把它当成普通 `HumanMessage` 标为 `[User]: ...`——委派指令看起来像用户说的话
2. 不走 `<system-reminder>` 标签，与提醒目录的其他 8 个来源格式不一致

**为什么不能注册为提醒目录来源**：提醒目录的 `DynamicReminderMiddleware.abefore_model` 只在**每轮首个模型调用**触发（要求最后一条消息是真实用户消息）。委派压力在 **turn 内部**触发——8 次 read 后 warn、16 次 read 后 block，这些发生在工具调用之后的**后续模型调用**上。目录的 first-model-call-only 门控会阻止 mid-turn 注入。所以必须保留 gate 自己的 `abefore_model` 注入路径。

---

## 1. 设计

### 1.1 改用 system-reminder 格式

`_directive()` 改用 `wrap_reminder` + `system_reminder_metadata`，使注入的消息成为合法的 `<system-reminder>`：

```python
def _directive(self, content: str) -> HumanMessage:
    return HumanMessage(
        content=wrap_reminder("delegation_pressure", content),
        metadata=system_reminder_metadata("delegation_pressure"),
    )
```

消息内容从 `[SYSTEM DIRECTIVE: ...]\n\n...` 变为 `<system-reminder source="delegation_pressure">\n...\n</system-reminder>`。metadata 的 provenance 从 `"delegation_pressure"` 变为 `"reminder_catalog"`。

### 1.2 最高优先级——如何实现

提醒目录的优先级是 budget 分配顺序（`resolve_injections` 按优先级降序保留）。但 gate 不走目录解析，所以"优先级"不能通过目录的 priority 字段实现。

**实际效果上已经是最高优先级**：gate 的 `abefore_model` 在 mid-turn 模型调用上触发，此时目录是 no-op（最后一条消息是 ToolMessage，不是用户消息）→ gate 的指令是那条模型调用上唯一的 `<system-reminder>` → 不存在竞争。

但为了**显式声明**优先级并在文档中可查，gate 的 `abefore_model` 注入返回 `{"messages": [directive]}`（append 到消息列表末尾），使指令成为模型调用前**最后一条消息**——紧贴生成点，位置上就是最高优先级。

### 1.3 保留的：注入路径 + 工具拒绝

| 组件 | 改不改 | 原因 |
|---|---|---|
| `_directive()` | **改** — 用 `wrap_reminder` + `system_reminder_metadata` | 核心改动 |
| `_warn_message()` / `_block_message()` | **改** — 去掉 `[SYSTEM DIRECTIVE: ...]` 前缀 | `<system-reminder>` 标签替代文本前缀 |
| `abefore_model` / `before_model` | **不改** | 保留 gate 自己的 mid-turn 注入路径 |
| `_refusal()` ToolMessage | **不改** | 是工具结果（ToolMessage），不是提醒（HumanMessage） |
| `_pre_tool` / `_post_tool` / `wrap_tool_call` | **不改** | 计数和 block 逻辑不变 |

---

## 2. 实施步骤（含完整 diff）

### Step 1：导入 system-reminder 工具

**文件**：`agent/middlewares/delegation_pressure/core.py`

```diff
 from langchain.agents.middleware import AgentMiddleware, AgentState
 from langchain_core.messages import HumanMessage, ToolMessage
 from langgraph.prebuilt.tool_node import ToolCallRequest
 from loguru import logger

+from pub.func.message.system_reminder import (
+    system_reminder_metadata,
+    wrap_reminder,
+)
+
 from config.features import DELEGATION_PRESSURE
```

### Step 2：改 `_directive()`

**文件**：`agent/middlewares/delegation_pressure/core.py:104-112`

```diff
-    def _directive(self, content: str) -> HumanMessage:
-        return HumanMessage(
-            content=content,
-            metadata={
-                "origin": "delegation_pressure",
-                "internal": True,
-                "provenance": "delegation_pressure",
-            },
-        )
+    def _directive(self, content: str) -> HumanMessage:
+        return HumanMessage(
+            content=wrap_reminder("delegation_pressure", content),
+            metadata=system_reminder_metadata("delegation_pressure"),
+        )
```

metadata 从 `{"origin": "delegation_pressure", "internal": True, "provenance": "delegation_pressure"}` 变为 `{"origin": "delegation_pressure", "internal": True, "provenance": "reminder_catalog"}`。`is_system_reminder(msg)` 现在对它返回 `True`。

### Step 3：去掉 `[SYSTEM DIRECTIVE: ...]` 前缀

**文件**：`agent/middlewares/delegation_pressure/core.py:114-134`

```diff
     def _warn_message(self, calls: int) -> str:
         return (
-            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE]\n\n"
             f"You have made {calls} read/exploration calls this turn.\n"
             f"At {DELEGATION_PRESSURE['block_after']} read calls, read and write tools "
             "will be disabled for the rest of the turn.\n"
             "Delegate the remaining work now: call `sessions_spawn` with\n"
             "functional_role='researcher' for read-only research, or 'executor' for\n"
             "implementation/test work."
         )

     def _block_message(self) -> str:
         return (
-            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE — READ/WRITE DISABLED]\n\n"
             f"{DELEGATION_PRESSURE['block_after']} read calls were reached; every "
             "read/write tool is paused for the rest of this turn.\n"
             "Use `sessions_spawn` now: functional_role='researcher' for read-only\n"
             "research, 'executor' for implementation/test work. Then use\n"
             "`sessions_messages` / `subagents_list` / `sessions_yield` to collect the\n"
             "result. Browser debugging tools remain available."
         )
```

`wrap_reminder` 会在内容外包 `<system-reminder source="delegation_pressure">\n...\n</system-reminder>`，所以 `[SYSTEM DIRECTIVE: ...]` 前缀是多余的。

### Step 4：更新测试

**文件**：`tests/agent/middlewares/test_delegation_pressure.py`（或对应测试文件）

需要更新的断言：

```python
# 旧：检查 [SYSTEM DIRECTIVE 前缀
assert "[SYSTEM DIRECTIVE: DELEGATION PRESSURE]" in msg.content

# 新：检查 <system-reminder> 标签 + source
assert msg.content.startswith('<system-reminder source="delegation_pressure">')
assert msg.content.endswith("</system-reminder>")

# 旧：检查 provenance
assert msg.metadata["provenance"] == "delegation_pressure"

# 新：检查 reminder_catalog provenance + is_system_reminder
assert msg.metadata["provenance"] == "reminder_catalog"
assert is_system_reminder(msg) is True
```

新增测试：

```python
    async def test_the_directive_is_a_system_reminder(self):
        """The delegation directive must be a <system-reminder>, not a plain HumanMessage."""
        mw = DelegationPressureGate()
        directive = mw._directive("test content")

        assert is_system_reminder(directive) is True
        assert directive.metadata["origin"] == "delegation_pressure"
        assert directive.metadata["provenance"] == "reminder_catalog"
        assert '<system-reminder source="delegation_pressure">' in directive.content
        assert "test content" in directive.content
        assert directive.content.endswith("</system-reminder>")

    async def test_the_warn_directive_omits_the_system_directive_prefix(self):
        mw = DelegationPressureGate()
        content = mw._warn_message(8)
        directive = mw._directive(content)

        # The <system-reminder> tag replaces the old [SYSTEM DIRECTIVE] prefix.
        assert "[SYSTEM DIRECTIVE" not in directive.content
        assert "8 read/exploration calls" in directive.content

    async def test_the_block_directive_omits_the_system_directive_prefix(self):
        mw = DelegationPressureGate()
        content = mw._block_message()
        directive = mw._directive(content)

        assert "[SYSTEM DIRECTIVE" not in directive.content
        assert "read/write tool is paused" in directive.content or "read/write tools are paused" in directive.content
```

### Step 5：文档

**文件**：`wiki/context-governance/system-reminder/README*.md`（四个语言）

在"Registered Sources"段的"Not part of this catalogue"部分，把 `DelegationPressureGate` 单独列出：

> **DelegationPressureGate** (`agent/middlewares/delegation_pressure/core.py`) injects its warn / block directives as `<system-reminder source="delegation_pressure">` messages with the standard reminder metadata contract (`provenance="reminder_catalog"`), but through its **own `abefore_model` path** — not the catalogue's `resolve_injections`. The catalogue only resolves on the turn's first model call (when the last message is a genuine user message); delegation pressure triggers **mid-turn** (after 8 / 16 tool calls, on subsequent model calls), so the catalogue's first-call-only gate would block it. Conceptual priority 100 (highest): on the mid-turn model calls where the gate fires, the catalogue is a no-op, so the delegation directive is the only `<system-reminder>` present — it lands at the tail of the message list (appended by the gate's `{"messages": [directive]}` return), closest to the generation point.

**文件**：`agent/middlewares/delegation_pressure/core.py` 模块 docstring

更新开头说明注入格式从 `[SYSTEM DIRECTIVE]` 变为 `<system-reminder>`。

---

## 3. 边界与取舍

### 3.1 `_is_injected` 的 `[SYSTEM DIRECTIVE` 回退检查

`dynamic.py:41-47` 的 `_is_injected` 有一个回退检查 `content.startswith("[SYSTEM DIRECTIVE")`，用于识别没有 `internal: True` metadata 的注入消息。delegation 指令有 `internal: True`，所以这个回退从未被触发——去掉前缀不影响 `_is_injected` 对 delegation 指令的判定。

但 **`task_intent/core.py:327`** 和 **`todo_continuation/core.py`** 也检查 `text.startswith("[SYSTEM DIRECTIVE")` 来识别自己注入的指令。delegation 指令去掉前缀后，这些检查不再匹配 delegation 指令。需要验证这是否有副作用：

- `task_intent/core.py:327`：用于跳过指令消息不触发 steering。但 task_intent 同时检查 `metadata.internal`（delegation 指令有 `internal: True`），所以跳过逻辑不受影响。
- `todo_continuation/core.py`：同理——它的指令识别也基于 `metadata.internal`。

**结论**：去掉前缀安全，但需在测试中验证 task_intent / todo_continuation 不因前缀消失而误触发。

### 3.2 摘要器序列化的改善

当前 delegation 指令的 `provenance="delegation_pressure"` → `is_system_reminder()` 返回 `False` → 摘要器标为 `[User]: {content[:2000]}`（错误——委派指令看起来像用户说的话）。

改后 `provenance="reminder_catalog"` → `is_system_reminder()` 返回 `True` → 摘要器标为 `[System notice]: {content[:500]}`（正确）。

这是一个**附带改善**：委派指令不再被误标为用户内容。

### 3.3 compressible 的未来扩展

改后 delegation 指令是合法的 `<system-reminder>`，metadata 带 `"provenance": "reminder_catalog"`。如果未来要让摘要器跳过 delegation 指令（它是 turn-scoped 的一次性指令，压缩后无意义），可以在 `_directive()` 里加 `compressible=True`：

```python
metadata=system_reminder_metadata("delegation_pressure", compressible=True)
```

`_filter_summary_messages` 的 `metadata.get("compressible") is True` 检查不依赖来源是否注册在目录里——任何带这个标记的消息都会被过滤。这是本计划的自然扩展点，不需要现在实施。

### 3.4 其他 `[SYSTEM DIRECTIVE]` 来源

`task_intent`（`[SYSTEM DIRECTIVE: TASK INTENT DETECTED]` 等）和 `todo_continuation`（`[SYSTEM DIRECTIVE: TODO CONTINUATION]` 等）也用 `[SYSTEM DIRECTIVE]` 前缀。本计划只改 `delegation_pressure`。它们的迁移是独立工作，可以后续单独做——但一旦全部迁移完，`_is_injected` 的 `[SYSTEM DIRECTIVE` 回退检查就可以删除。

### 3.5 工具拒绝 ToolMessage 不变

`_refusal()` 返回的是 `ToolMessage(status="error")`，不是 HumanMessage。它是工具调用的结果，不是提醒。保持不变——`<system-reminder>` 格式只适用于注入到转录的 HumanMessage，不适用于 ToolMessage。

---

## 4. 验收清单

- [ ] delegation 指令内容被 `<system-reminder source="delegation_pressure">...</system-reminder>` 包裹
- [ ] delegation 指令 metadata 的 provenance 是 `"reminder_catalog"`（不再是 `"delegation_pressure"`）
- [ ] `is_system_reminder(directive)` 返回 `True`
- [ ] `_warn_message` / `_block_message` 内容不含 `[SYSTEM DIRECTIVE` 前缀
- [ ] `abefore_model` 注入路径不变（mid-turn 触发，不走目录解析）
- [ ] `_refusal()` ToolMessage 不变
- [ ] 摘要器标 delegation 指令为 `[System notice]`（不再误标为 `[User]`）
- [ ] task_intent / todo_continuation 不因前缀消失而误触发
- [ ] `uv run --no-sync pytest tests/agent/middlewares/test_delegation_pressure.py -q` PASS
- [ ] `uv run --no-sync python scripts/check_docs_parity.py` PASS

---

## 5. 涉及文件一览

| 文件 | 改动类型 |
|---|---|
| `agent/middlewares/delegation_pressure/core.py` | 导入 + `_directive` 改用 `wrap_reminder`/`system_reminder_metadata` + 去前缀 + docstring |
| `tests/agent/middlewares/test_delegation_pressure.py` | 断言从 `[SYSTEM DIRECTIVE` 改为 `<system-reminder>` + 新增 is_system_reminder 测试 |
| `wiki/context-governance/system-reminder/README*.md` | "Not part of this catalogue" 段补充 DelegationPressureGate 说明 |
