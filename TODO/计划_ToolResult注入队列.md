# 计划：ToolResult 注入队列（可扩展的工具结果增强机制）

- 建档日期：2026-11-02
- 状态：**待评审**
- 来源：ToolGuardrails WARN 硬编码追加是唯一能往 ToolMessage 里注入提示的机制；UserHooksMiddleware 解析了 `additionalContext` 但没接线；需要一个像 reminder catalog 那样的注册式队列方便扩展
- 安全等级：**中**——新增 `wrap_tool_call` 中间件，改变工具结果的最终内容

---

## 0. 现状

往 ToolMessage 追加额外信息的机制目前散落在三个地方，互不统一：

| 中间件 | 怎么做的 | 问题 |
|---|---|---|
| `ToolGuardrails` WARN | `content=f"{result_content}\n\n{warning}"` 硬编码追加（`tool_guardrails/core.py:525-530`） | 只此一处真正追加；逻辑与 guardrail 状态紧耦合，不可复用 |
| `DelegationPressureGate` | 不碰 ToolMessage，走 `abefore_model` 注入 HumanMessage（`core.py:267-288`） | 下一轮模型调用才看到提示，不是跟工具结果同轮到达 |
| `UserHooksMiddleware` | `_dispatch`（`core.py:263-284`）解析了 hook 输出的 `additionalContext`（:194-199），存到 `HookOutcome.additional_context`（:83），但 `awrap_tool_call`（:324）丢弃了 `_dispatch` 的返回值，:325 原样返回 `result` | 半成品——解析了没接线，Step 1 只读设计的直接后果 |

**没有统一的"往工具结果里追加提示"机制**。每次要加一个新的提示来源，都得自己写一个 `wrap_tool_call` 中间件或硬编码到 ToolGuardrails 里。

---

## 1. 设计

### 1.1 注入队列中间件

新建 `ToolResultEnrichmentMiddleware`，注册为 `wrap_tool_call` 链的**最外层**（在 ToolGuardrails 之前）。它让 handler 正常执行，拿到最终结果后，跑注入队列，把所有 injector 返回的文本追加到 ToolMessage.content 末尾。

### 1.2 注入器注册表

模仿 reminder catalog 的 `ReminderSource`，但作用域是每次工具调用：

```python
@dataclass(frozen=True)
class ToolResultInjector:
    injector_id: str
    builder: Callable[[str, dict, ToolMessage, str], str | None]
    # (tool_name, tool_args, result, session_id) → text or None
    priority: int = 50
    # 低优先级先追加（在文本上方），高优先级后追加（在文本底部，最靠近模型注意力）
    tools_filter: frozenset[str] | None = None
    # None = 所有工具；限定则只对指定工具生效
    only_if_changed: bool = False
    # 同一 injector 对同一工具连续返回相同文本则跳过
```

### 1.3 中间件行为——JSON 感知的双模式注入

```python
async def awrap_tool_call(self, request, handler):
    result = await handler(request)
    if not isinstance(result, ToolMessage):
        return result

    # error 结果默认不追加（失败的工具调用不需要提示）
    if str(getattr(result, "status", "")).lower() == "error":
        return result

    injections = resolve_injections(request, result)
    if not injections:
        return result

    suffix = "\n\n".join(injections)
    content = result.content if isinstance(result.content, str) else str(result.content)

    # JSON 感知：如果 content 是合法 JSON（read_file 等），把 hint 作为
    # 一个新字段注入到 JSON 内部，而不是追加文本——保持 json.loads 对
    # 下游 eviction / truncation 管线合法。
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            payload["advisory"] = suffix
            new_content = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            new_content = f"{content}\n\n{suffix}"
    except (json.JSONDecodeError, TypeError):
        # 纯文本工具（terminal / search_files / ...）→ 末尾追加
        new_content = f"{content}\n\n{suffix}"

    return ToolMessage(
        content=new_content,
        tool_call_id=result.tool_call_id,
        name=result.name,
        status=result.status,
    )
```

**双模式自动检测**，不需要工具声明任何 metadata：

| content 类型 | 注入方式 | 例子 |
|---|---|---|
| 合法 JSON dict | 加 `"advisory"` 字段，重新 `json.dumps` | `read_file`: `{"content": "...", "truncated": false}` → `{"content": "...", "truncated": false, "advisory": "[Hint: ...]"}` |
| 纯文本 | 末尾追加 `\n\n{suffix}` | `terminal`: `"Exit code: 0\noutput..."` → `"Exit code: 0\noutput...\n\n[Hint: ...]"` |

**为什么 JSON 字段注入安全**：下游对 read_file content 做 `json.loads` 的三处（`eviction.py:423`、`target_truncation.py:101,138`）只读 `content` 和 `truncated` 这两个 key，新增的 `"advisory"` 字段被忽略。模型本来就要解析这个 JSON 才能用 read_file 结果——多一个字段是自然的，注意力仍在 `content` 上，`advisory` 是附带看到的。

**不需要 `enrich=False` 排除集了**：JSON 工具自动走字段注入，纯文本工具自动走末尾追加，中间件用 `try json.loads` 自动检测，工具不需要声明任何 metadata。

### 1.4 为什么放最外层（ToolGuardrails 之前）

`wrap_tool_call` 链的执行顺序：先注册 = 最外层。当前链：

```
ToolGuardrails(外) → ToolResourceLock → UserHooks → ContextEviction →
ToolCallNormalize → PathGuard → HeartbeatStaleness → HITL →
MessagePersistence → DelegationPressureGate(内) → handler
```

Enrichment 注册在 ToolGuardrails 之前（更外层）：

```
Enrichment(最外) → ToolGuardrails → ... → handler
```

结果从内向外冒泡：handler → DelegationPressureGate 计数 → MessagePersistence 持久化原始结果 → ... → ToolGuardrails WARN/BLOCK → **Enrichment 追加注入器输出**。

这样 Enrichment 看到的是 ToolGuardrails 处理后的最终结果：
- WARN 后的结果 → Enrichment 再追加（guardrail 警告 + injector 提示共存）
- BLOCK/HALT 后的 error ToolMessage → Enrichment 跳过（`status=="error"`）
- 正常结果 → Enrichment 追加

**MessagePersistence 在 Enrichment 内侧**：原始结果先持久化到 MesMemory，然后 Enrichment 的追加版本替换 state 中的副本。与 ToolGuardrails WARN 当前的行为一致——MesMemory 存原始，state 存增强。

### 1.5 注入器 vs system-reminder 队列

| | reminder catalog | tool result 队列 |
|---|---|---|
| 触发时机 | 每轮首个模型调用（`before_model`） | 每次工具调用后（`wrap_tool_call`） |
| 注入载体 | HumanMessage | ToolMessage.content（JSON 工具加字段，纯文本工具末尾追加） |
| 优先级排序 | budget 分配（丢弃低优先级） | 追加顺序（低优先级先，高优先级后） |
| 两者关系 | 正交，可共存 | 正交，可共存 |

---

## 2. 实施步骤（含完整 diff）

### Step 1：新建注入队列模块

**文件**：`agent/middlewares/tool_result_enrichment/__init__.py` + `core.py` + `sources.py`

**`core.py`**——注册表 + 解析器：

```python
"""ToolResult enrichment: a registry of injectors that append hints to ToolMessage content.

Modelled on the reminder catalog (``agent/middlewares/reminder_catalog``), but
the trigger is each tool call's return instead of the turn's first model call.
An injector receives ``(tool_name, tool_args, result, session_id)`` and returns
optional text; the middleware appends all non-None outputs to the result's
content, sorted by priority (low first, so high-priority hints land at the
bottom — closest to the model's attention).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage
from loguru import logger

__all__ = [
    "ToolResultInjector",
    "register",
    "registered_injectors",
    "resolve_injections",
    "clear_session",
]


@dataclass(frozen=True)
class ToolResultInjector:
    """One enrichment source's declaration.

    :param injector_id: Stable identifier (used for logging + change-detection).
    :param builder: ``(tool_name, tool_args, result, session_id) -> str | None``;
        ``None``/blank means "nothing to append for this call".
    :param priority: Lower appended first (top of the suffix block), higher
        appended last (bottom — closest to the model's attention). 100 = highest.
    :param tools_filter: ``None`` = all tools; a set limits the injector to
        those tool names only.
    :param only_if_changed: Skip when the text is identical to the last text
        this injector produced for the same tool in this session.
    """

    injector_id: str
    builder: Callable[[str, dict[str, Any], ToolMessage, str], str | None]
    priority: int = 50
    tools_filter: frozenset[str] | None = None
    only_if_changed: bool = False


_INJECTORS: dict[str, ToolResultInjector] = {}

#: ``{session_id: {injector_id: {tool_name: {"hash": str}}}}`` — change-detection
#: memory for ``only_if_changed`` injectors.
_CHANGE_STATE: dict[str, dict[str, dict[str, str]]] = {}


def register(injector: ToolResultInjector) -> None:
    """Add an injector to the registry (re-registering an id replaces it)."""
    _INJECTORS[injector.injector_id] = injector


def registered_injectors() -> dict[str, ToolResultInjector]:
    """The registered injectors (a copy — callers must not mutate)."""
    return dict(_INJECTORS)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve_injections(
    tool_name: str,
    tool_args: dict[str, Any],
    result: ToolMessage,
    session_id: str,
) -> list[str]:
    """Resolve every injector for one tool call, in append order (low priority first).

    :returns: Non-blank texts to append, ordered so the highest priority is last.
    """
    session_state = _CHANGE_STATE.get(session_id, {})
    candidates: list[tuple[ToolResultInjector, str]] = []

    for injector in _INJECTORS.values():
        if injector.tools_filter is not None and tool_name not in injector.tools_filter:
            continue
        try:
            text = injector.builder(tool_name, tool_args, result, session_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "tool_result_enrichment: builder failed for {}", injector.injector_id
            )
            continue
        if not text or not text.strip():
            continue
        if injector.only_if_changed:
            key_state = session_state.get(injector.injector_id, {})
            current_hash = _content_hash(f"{tool_name}:{text}")
            if current_hash == key_state.get(tool_name):
                continue
            key_state[tool_name] = current_hash
            _CHANGE_STATE.setdefault(session_id, {})[injector.injector_id] = key_state
        candidates.append((injector, text))

    # Low priority first (appended at top), high priority last (bottom).
    candidates.sort(key=lambda item: item[0].priority)
    return [text for _, text in candidates]


def clear_session(session_id: str) -> None:
    """Drop a session's change-detection state (session teardown, tests)."""
    _CHANGE_STATE.pop(session_id, None)
```

**`sources.py`**——默认注入器注册：

```python
"""Default tool-result injectors."""

from __future__ import annotations

from .core import register, ToolResultInjector
from .injectors import (
    build_delegation_hint,
    build_hook_context,
)

__all__ = ["register_default_injectors"]


def register_default_injectors() -> None:
    """Register the default injectors (idempotent — re-register replaces)."""
    register(ToolResultInjector(
        injector_id="delegation_hint",
        builder=build_delegation_hint,
        priority=80,             # high — closest to the model's attention
        tools_filter=None,       # ALL tools get the hint once threshold is reached
        only_if_changed=False,   # every tool result gets the hint, even if text is identical
    ))
    register(ToolResultInjector(
        injector_id="hook_context",
        builder=build_hook_context,
        priority=60,
        tools_filter=None,
        only_if_changed=True,
    ))
```

### Step 2：默认注入器实现

**文件**：`agent/middlewares/tool_result_enrichment/injectors.py`

**2a. DelegationPressureGate 提示注入器**——把 gate 的 warn/escalated 提示追加到 read 工具结果：

```python
"""Default injector builders."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage


def build_delegation_hint(
    tool_name: str,
    tool_args: dict[str, Any],
    result: ToolMessage,
    session_id: str,
) -> str | None:
    """Append a delegation-pressure hint to EVERY tool result once the threshold is reached.

    Reads the gate's pressure state (the same ``_STATE_KEY`` the middleware
    uses).  At ``warn_after`` reads, every subsequent tool result gets a warning
    hint; at ``block_after`` reads, every tool result gets an escalated (stronger)
    hint.  Applies to ALL tools (not just read tools) — the agent should see the
    pressure nudge regardless of which tool it called.
    """
    from config.features import DELEGATION_PRESSURE
    from runtime import state_register_mem

    if not DELEGATION_PRESSURE["enabled"]:
        return None

    state_key = "delegation_pressure_state"
    state = state_register_mem.get_state(session_id, state_key, None)
    if state is None:
        return None

    read_calls = getattr(state, "read_calls", 0)
    escalated = getattr(state, "escalated", False)
    warn_after = DELEGATION_PRESSURE["warn_after"]
    block_after = DELEGATION_PRESSURE["block_after"]

    if read_calls >= block_after:
        return (
            "[Advisory: You have made {} read/exploration calls this turn — "
            "this is well beyond what a main agent should spend on exploration. "
            "Delegate NOW via `sessions_spawn` (researcher for read-only, "
            "executor for implementation). Then use `sessions_messages` / "
            "`subagents_list` / `sessions_yield` to collect the result.]".format(read_calls)
        )
    if read_calls >= warn_after:
        return (
            "[Advisory: {} read/exploration calls this turn. Consider delegating "
            "via `sessions_spawn` (researcher for read-only, executor for "
            "implementation).]".format(read_calls)
        )
    return None
```

**2b. UserHooksMiddleware `additional_context` 接线**——把 hook 的 `additionalContext` 通过注入队列追加到工具结果：

```python
# Per-session stash: hook outcomes waiting to be consumed by the injector.
# UserHooksMiddleware._dispatch writes here; the injector reads and clears.
from collections import defaultdict

_pending_hook_context: dict[str, list[str]] = defaultdict(list)


def stash_hook_context(session_id: str, text: str) -> None:
    """Called by UserHooksMiddleware after a PostToolUse hook returns additional_context."""
    _pending_hook_context[session_id].append(text)


def build_hook_context(
    tool_name: str,
    tool_args: dict[str, Any],
    result: ToolMessage,
    session_id: str,
) -> str | None:
    """Drain pending ``additionalContext`` from PostToolUse hooks into the tool result.

    UserHooksMiddleware runs INSIDE the enrichment middleware (it is registered
    later = inner), so by the time the injector runs, ``_dispatch`` has already
    collected the hook outcomes.  The stashed texts are drained in order and
    appended as a single block.
    """
    texts = _pending_hook_context.pop(session_id, None)
    if not texts:
        return None
    return "\n".join(f"[Hook context]: {t}" for t in texts)
```

### Step 3：中间件

**文件**：`agent/middlewares/tool_result_enrichment/middleware.py`

```python
"""``wrap_tool_call``: inject injector outputs into the tool result (JSON-aware)."""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

from .core import resolve_injections

__all__ = ["ToolResultEnrichmentMiddleware"]


class ToolResultEnrichmentMiddleware(AgentMiddleware):
    """Outermost ``wrap_tool_call``: inject the injector queue's output into the result.

    Registered BEFORE ``ToolGuardrails`` (outermost), so it sees the final
    result after every other middleware has processed it.  Error results
    (``status="error"``) are passed through untouched — a blocked/halted tool
    call does not receive hints.

    **JSON-aware injection**: if the result content is valid JSON (e.g.
    ``read_file`` returns ``{"content": ..., "truncated": bool, ...}``), the
    hint is injected as a new ``"advisory"`` field inside the JSON object,
    keeping ``json.loads`` valid for the downstream eviction / truncation
    pipeline.  If the content is plain text, the hint is appended to the end.
    The mode is auto-detected via ``try json.loads`` — no tool-level declaration
    needed.
    """

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result
        if str(getattr(result, "status", "") or "").lower() == "error":
            return result

        call = request.tool_call or {}
        tool_name = str(call.get("name") or getattr(request.tool, "name", "") or "")
        tool_args = call.get("args") or {}
        session_id = str((request.state or {}).get("session_id") or "")
        if not session_id:
            return result

        try:
            injections = resolve_injections(tool_name, tool_args, result, session_id)
        except Exception:  # noqa: BLE001
            logger.exception("ToolResultEnrichmentMiddleware: resolve failed; returning raw result")
            return result

        if not injections:
            return result

        suffix = "\n\n".join(injections)
        content = result.content if isinstance(result.content, str) else str(result.content)

        # JSON-aware injection: if content is a JSON object, add the hint as a
        # field so json.loads stays valid for eviction / truncation (which only
        # read "content" and "truncated" keys).  Plain text → append.
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                payload["advisory"] = suffix
                new_content = json.dumps(payload, ensure_ascii=False, default=str)
            else:
                new_content = f"{content}\n\n{suffix}"
        except (json.JSONDecodeError, TypeError):
            new_content = f"{content}\n\n{suffix}"

        return ToolMessage(
            content=new_content,
            tool_call_id=result.tool_call_id,
            name=result.name,
            status=result.status,
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.awrap_tool_call(request, handler))
        # Running loop — async-only path; return raw result.
        result = handler(request)
        return result
```

### Step 4：注册到中间件链

**文件**：`agent/core.py`

在 `ToolGuardrails()` **之前**注册（:336）：

```diff
+        # ToolResult enrichment (outermost wrap_tool_call): appends injector
+        # outputs (delegation hints, hook context) to the tool result after
+        # every other middleware has processed it.  Registered BEFORE
+        # ToolGuardrails so it sees the final result (WARN-appended or blocked).
+        ToolResultEnrichmentMiddleware(),
         ToolGuardrails(),
```

导入：
```diff
+from .middlewares.tool_result_enrichment import ToolResultEnrichmentMiddleware
```

### Step 5：UserHooksMiddleware 接线

**文件**：`agent/middlewares/user_hooks/core.py`

`awrap_tool_call`（:288-325）——`_dispatch` 的返回值不再丢弃，把 `additional_context` stash 给注入器：

```diff
-        await self._dispatch(event, payload, session_id, tool_name)
+        outcomes = await self._dispatch(event, payload, session_id, tool_name)
+        # Wire additional_context through the enrichment queue instead of
+        # discarding it.  The injector (registered in the enrichment middleware
+        # OUTSIDE this one) drains the stash and appends it to the result.
+        from agent.middlewares.tool_result_enrichment.injectors import stash_hook_context
+        for _runner, outcome in outcomes:
+            if outcome.additional_context:
+                stash_hook_context(session_id, outcome.additional_context)
         return result
```

**更新 docstring**（:5-7）——Step 1 的只读限制不再适用于 `additional_context`：

```diff
 * This is **Step 1** of the plan: the events are dispatched read-only — a hook can
-* audit, notify and meter, and that is all. It cannot block a tool, rewrite its
-* arguments or inject context into the conversation.
+* audit, notify and meter, and that is all. It cannot block a tool or rewrite
+* its arguments. ``additionalContext`` from a ``PostToolUse`` hook IS wired
+* through the tool-result enrichment queue — the hook's text is appended to the
+* tool result the model sees, but the hook cannot block, replace, or rewrite it.
```

**更新 HookOutcome docstring**（:74-76）：

```diff
-    Step 1 reads ``status`` (and logs the rest): ``decision`` / ``updated_input``
-    are the Step-2/3 fields, parsed so the contract is visible and refuses to
-    grow silently.
+    Step 1 reads ``status`` and ``additional_context`` (the latter is wired
+    through the enrichment queue): ``decision`` / ``updated_input`` are the
+    Step-2/3 fields, parsed so the contract is visible and refuses to grow
+    silently.
```

### Step 6：DelegationPressureGate 移除 abefore_model

**文件**：`agent/middlewares/delegation_pressure/core.py`

委派压力提示**只走工具结果注入器**——每个工具结果在阈值达到后自动带上 warning / escalated 文本。`abefore_model` 的 HumanMessage 注入**全部移除**：不需要每轮注入独立消息，工具结果里的 hint 已经覆盖每个工具调用。

```diff
-    async def abefore_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
-        """Inject the one-time warning or the escalated directive."""
-        try:
-            session_id = self._session_id(state)
-            if not _active(session_id):
-                return None
-            gate = self._get(session_id)
-            if (
-                gate.read_calls >= DELEGATION_PRESSURE["block_after"]
-                and not gate.escalated
-            ):
-                gate.escalated = True
-                self._save(session_id, gate)
-                return {"messages": [self._directive(self._escalated_message(gate.read_calls))]}
-            if (
-                not gate.escalated
-                and gate.read_calls >= DELEGATION_PRESSURE["warn_after"]
-                and not gate.warned
-            ):
-                gate.warned = True
-                self._save(session_id, gate)
-                return {"messages": [self._directive(self._warn_message(gate.read_calls))]}
-        except Exception:  # noqa: BLE001
-            logger.exception("DelegationPressureGate: before_model failed; continuing")
-        return None
-
-    def before_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
-        """Sync best-effort path; the streaming main agent uses ``abefore_model``."""
-        try:
-            asyncio.get_running_loop()
-        except RuntimeError:
-            return asyncio.run(self.abefore_model(state, runtime))
-        return None
```

`_PressureState` 移除 `warned` 和 `escalated`——注入器直接比较 `read_calls >= warn_after` / `read_calls >= block_after`，不需要状态标志：

```diff
 @dataclass
 class _PressureState:
     read_calls: int = 0
-    warned: bool = False
-    escalated: bool = False
```

`_warn_message` / `_escalated_message` / `_directive` 方法**删除**——内容逻辑移到 `build_delegation_hint` 注入器里。

### Step 7：测试

| 测试文件 | 测试 |
|---|---|
| `tests/agent/middlewares/tool_result_enrichment/test_core.py`（新建） | 注册/解析/优先级排序/`only_if_changed` 去重/`tools_filter` 过滤/error 结果跳过 |
| `tests/agent/middlewares/tool_result_enrichment/test_middleware.py`（新建） | 中间件行为：handler 正常执行→追加；error 结果不追加；非 ToolMessage 不处理；空注入列表 no-op |
| `tests/agent/middlewares/tool_result_enrichment/test_injectors.py`（新建） | `delegation_hint`：未到阈值返回 None；到 warn_after 返回提示；到 block_after 返回 escalated 提示；非 read 工具返回 None。`hook_context`：stash 后 drain；无 stash 返回 None |
| `tests/agent/middlewares/test_user_hooks.py`（更新） | `additional_context` 从 hook 到工具结果的完整路径：hook 返回 `additionalContext` → `_dispatch` 收集 → `stash_hook_context` → 注入器 drain → ToolMessage.content 含 `[Hook context]` |
| `tests/agent/middlewares/test_delegation_pressure.py`（更新） | `abefore_model` 移除；每个工具结果（含 write 工具、read_file）在阈值后带 hint |

### Step 8：文档

| 文档 | 改动 |
|---|---|
| `wiki/context-governance/system-reminder/README*.md` | 新增一段说明 tool result 注入队列与 system-reminder 队列的区别和关系 |
| `agent/middlewares/tool_result_enrichment/core.py` docstring | 已含完整说明 |
| `agent/middlewares/user_hooks/core.py` docstring | 更新 Step 1 描述：`additional_context` 已接线 |

---

## 3. 边界与取舍

### 3.1 ToolGuardrails WARN 不迁移

ToolGuardrails WARN 的追加逻辑（`content=f"{result_content}\n\n{warning}"`）与 guardrail 状态（`GuardrailState`、 pathology 检测、计数器）紧耦合。迁移到注入器需要把 guardrail 状态暴露给外部 builder，破坏封装。**保留 ToolGuardrails WARN 原地不动**——Enrichment 中间件在它外侧，看到 WARN 后的结果再追加注入器输出，两者共存：

```
{原始结果}\n\n{guardrail_warning}\n\n{delegation_hint}\n\n{hook_context}
```

### 3.2 error 结果不追加

`status="error"` 的 ToolMessage（ToolGuardrails BLOCK/HALT、HITL 拒绝等）不接收注入器提示。失败的工具调用不需要"考虑委派"或"hook 上下文"——错误信息本身已经足够。

### 3.3 JSON 结构化结果——字段注入而非文本追加

`read_file` 返回 `json.dumps({"content": ..., "truncated": bool, ...})`，被 `eviction.py:423-427`、`target_truncation.py:100-124`、`target_truncation.py:127-141` 三处做 `json.loads` 解析。中间件用 `try json.loads` 自动检测 content 类型：JSON dict → 加 `"advisory"` 字段重新序列化（`json.loads` 保持合法）；纯文本 → 末尾追加。三处解析点只读 `content` 和 `truncated` key，新增字段被忽略——不需要排除集，不需要工具声明 metadata。

### 3.4 hook context 的安全性

`additional_context` 是 hook 命令的 stdout JSON 字段。它追加到工具结果，模型能看到。安全考量：
- hook 命令已经是 operator 配置的（`sherry.jsonc` `hooks` 段，需要 `hooks.enabled=true`）
- `additional_context` 是 `str` 类型校验的（:196-197）
- 追加到工具结果 ≠ 替换工具结果——hook 不能改写工具的真实输出
- 与 Step 2/3 的 `decision`（allow/deny）和 `updated_input`（改写参数）不同——`additional_context` 只是追加文本，不控制执行流

### 3.5 DelegationPressureGate 单通道——只走工具结果注入器

委派压力提示**只通过工具结果注入器**到达模型，不再走 `abefore_model` HumanMessage 注入。阈值达到后，**每个**工具结果（read / write / 所有工具）都带上 hint：

- `read_calls >= warn_after`（7）：每个工具结果追加 `[Advisory: N read calls. Consider delegating...]`
- `read_calls >= block_after`（14）：每个工具结果追加更强语气的 `[Advisory: N read calls. Delegate NOW...]`

注入器 `only_if_changed=False`——每个工具结果都带 hint，即使文本相同（write 工具不增加 read_calls，但 agent 仍然需要看到"该委派了"的提示）。JSON 工具（read_file）通过 `"advisory"` 字段注入，纯文本工具末尾追加。

`abefore_model`、`_directive`、`_warn_message`、`_escalated_message` 全部删除。`_PressureState` 只保留 `read_calls`——注入器直接比较阈值，不需要 `warned` / `escalated` 状态标志。

**spawn 重置时序**：`_post_tool`（DelegationPressureGate 内部，enrichment 中间件内侧）在 `sessions_spawn` 成功时把 `read_calls` 归零。结果冒泡到 enrichment 中间件时，注入器读到 `read_calls=0` → 低于阈值 → 返回 None → spawn 结果不带 hint。后续工具结果也不带 hint，直到再积累 7 次 read。时序正确：重置发生在注入器读取状态之前（`_post_tool` 在 `wrap_tool_call` 内侧，enrichment 在外侧）。

### 3.6 `only_if_changed` 的粒度

注入器的 `only_if_changed` 按 `(injector_id, tool_name)` 去重——同一 injector 对同一工具连续返回相同文本则跳过。`delegation_hint` 的文本包含 `read_calls` 数字，每次 read 后数字递增 → 文本不同 → 每次都会追加（这是期望行为：数字递增提醒模型压力在增加）。如果 `read_calls` 没变（非 read 工具触发），注入器返回 None → 自然跳过。

### 3.7 与 transient / compressible 计划的关系

本计划与 `计划_project_instructions改为瞬时注入.md` 和 `计划_压缩时过滤低价值reminders.md` 正交——那些改 reminder catalog 和摘要器，本计划改 tool result 管线。可以独立实施。

---

## 4. 验收清单

- [ ] `ToolResultEnrichmentMiddleware` 注册为 `wrap_tool_call` 最外层
- [ ] handler 正常执行后，注入器输出追加到 ToolMessage.content
- [ ] error 结果（`status="error"`）不追加
- [ ] JSON 结构化结果（read_file）：hint 作为 `"advisory"` 字段注入到 JSON 内部，`json.loads` 保持合法
- [ ] 纯文本结果（terminal / search_files / ...）：hint 末尾追加 `\n\n{suffix}`
- [ ] 非 ToolMessage 结果不处理
- [ ] 空注入列表时 no-op（原样返回）
- [ ] `delegation_hint` 注入器：read 工具结果末尾追加 read 次数提示
- [ ] `hook_context` 注入器：hook 的 `additionalContext` 出现在工具结果末尾
- [ ] UserHooksMiddleware 的 `_dispatch` 返回值不再丢弃
- [ ] `additional_context` 从 hook stdout → `_dispatch` → `stash_hook_context` → 注入器 drain → ToolMessage.content 完整路径
- [ ] DelegationPressureGate 的 `abefore_model` 移除，提示只走工具结果注入器
- [ ] `read_calls >= 7` 时每个工具结果追加 warning hint
- [ ] `read_calls >= 14` 时每个工具结果追加 escalated hint
- [ ] 所有工具（含 read_file）都收到 hint（JSON 工具走 `"advisory"` 字段，纯文本走末尾追加）
- [ ] `uv run --no-sync pytest tests/agent/middlewares/tool_result_enrichment/ -q` PASS
- [ ] `uv run --no-sync pytest tests/agent/middlewares/test_user_hooks.py -q` PASS
- [ ] `uv run --no-sync pytest tests/agent/middlewares/test_delegation_pressure.py -q` PASS

---

## 5. 涉及文件一览

| 文件 | 改动类型 |
|---|---|
| `agent/middlewares/tool_result_enrichment/__init__.py` | 新建 |
| `agent/middlewares/tool_result_enrichment/core.py` | 新建——注册表 + 解析器 |
| `agent/middlewares/tool_result_enrichment/sources.py` | 新建——默认注入器注册 |
| `agent/middlewares/tool_result_enrichment/injectors.py` | 新建——`build_delegation_hint` + `build_hook_context` + `stash_hook_context` |
| `agent/middlewares/tool_result_enrichment/middleware.py` | 新建——`ToolResultEnrichmentMiddleware` |
| `agent/core.py` | 注册中间件（ToolGuardrails 之前） |
| `agent/middlewares/user_hooks/core.py` | `_dispatch` 返回值接线 + docstring 更新 |
| `agent/middlewares/delegation_pressure/core.py` | 移除 `abefore_model` / `_directive` / `_warn_message` / `_escalated_message`；`_PressureState` 只留 `read_calls` |
| `tests/agent/middlewares/tool_result_enrichment/`（新建） | 注册表 + 中间件 + 注入器测试 |
| `tests/agent/middlewares/test_user_hooks.py` | `additional_context` 完整路径测试 |
| `tests/agent/middlewares/test_delegation_pressure.py` | `abefore_model` 移除；每个工具结果（含 write 工具、read_file）在阈值后带 hint |
