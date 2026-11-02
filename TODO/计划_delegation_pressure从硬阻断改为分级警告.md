# 计划：DelegationPressureGate 从硬阻断改为分级警告

- 建档日期：2026-11-02
- 状态：**待评审**
- 来源：DeepSWE 本地测试失败分析——硬阻断在子代理完成消息不可见时形成"读不到结果 → 再 spawn"的死锁放大回路
- 安全等级：**中**——移除工具阻断机制，改变 gate 的核心行为契约

---

## 0. 问题

`DelegationPressureGate` 在 16 次 read 后**硬阻断**所有 read/write 工具（返回 `ToolMessage(status="error")`），逼主 agent 只能 `sessions_spawn`。DeepSWE 测试暴露了死锁链：

1. 15 次 read → 所有 read/write 被断
2. 主 agent 按指令 spawn 子代理做验证/修复
3. 子代理完成消息没进入主 transcript（P0 已修）
4. 主 agent 读不到结果 → `sessions_messages` 也返回空 → 只能再 spawn
5. 每次 spawn 3-5 分钟 → 4-6 轮耗尽 1200 秒

即使 completion delivery 修好了，硬阻断仍然阻止主 agent 做关键的后续工作：读子代理改过的文件、跑最终测试、做最后几个 F2P 的小修复。一个 10 秒的 `patch_file` 被迫变成一个 3 分钟的子代理任务。

**根因**：硬阻断把"建议委派"变成了"剥夺工具使用权"。warn（7 次）已经足够引导；14 次时应该升级语气，但不应该切断工具。

---

## 1. 设计

### 1.1 分级警告替代硬阻断

| 阈值 | 现在 | 改后 |
|---|---|---|
| 7 次 read (`warn_after`) | 注入 warn 指令（一次性） | 不变 |
| 14 次 read (`block_after`) | **硬阻断**所有 read/write，返回 error ToolMessage | **注入升级 warn**（更强语气，一次性），工具**照常执行** |
| 成功 spawn | 解除阻断 + 清零 | 清零（不再需要"解除"） |
| `after_agent` | 重置 | 不变 |

### 1.2 移除的机制

| 组件 | 处置 |
|---|---|
| `_refusal()` ToolMessage | **删除**——不再拒绝任何工具调用 |
| `_pre_tool` 的 block 检查 | **删除**——只保留 read 计数 |
| `_post_tool` 的 block 设置 | **删除**——只保留 spawn reset |
| `_PressureState.blocked` | **删除** |
| `_PressureState.block_injected` | **删除** |
| `DELEGATION_PRESSURE["blocked_tools"]` | **删除**——不再有阻断集合 |
| `self._blocked_tools` | **删除** |

### 1.3 新增的机制

| 组件 | 说明 |
|---|---|
| `_PressureState.escalated` | 新字段——14-read 升级 warn 是否已注入（一次性 guard） |
| `_escalated_message()` | 新方法——升级 warn 的内容，语气更强，但不声称工具被断 |

### 1.4 保留的机制

| 组件 | 说明 |
|---|---|
| read 计数 (`_pressure_tools`) | 不变——仍累计 read/exploration 调用 |
| warn 指令（7 次） | 不变——一次性 warn，`warned` guard |
| 成功 spawn reset | 不变——accepted/pending spawn 清零计数 |
| `excluded_tools`（browser 系列） | 不变——不计入、不阻断（现在也不阻断任何东西，但仍不计入） |
| `after_agent` 重置 | 不变 |

---

## 2. 实施步骤（含完整 diff）

### Step 1：config — 移除 blocked_tools

**文件**：`config/features/agent_side/delegation_pressure.py`

```diff
 class DelegationPressureConfig(TypedDict):
     """Turn-scoped read pressure and the tools it blocks."""

     enabled: bool
     warn_after: int
     block_after: int
     pressure_tools: tuple[str, ...]
-    blocked_tools: tuple[str, ...]
     excluded_tools: tuple[str, ...]


 #: A main agent that has read/explored 15 times in one turn is done reading:
-#: every further read or write errors and points at sessions_spawn. The
-#: default pressure set is exploration; ``blocked_tools`` adds the write tools.
+#: it receives an escalated directive to delegate. Tools remain available —
+#: the gate advises, never blocks.
 DELEGATION_PRESSURE: DelegationPressureConfig = {
     "enabled": True,
     "warn_after": 7,
     "block_after": 14,
     "pressure_tools": (
         "read_file",
         "search_files",
         "message_search",
         "web_search",
         "web_fetch",
         "skill_view",
         "knowledge",
         "terminal",
         "terminal_bg",
         "python_repl",
     ),
-    "blocked_tools": (
-        "read_file",
-        "search_files",
-        "message_search",
-        "web_search",
-        "web_fetch",
-        "skill_view",
-        "knowledge",
-        "terminal",
-        "terminal_bg",
-        "python_repl",
-        "patch_file",
-        "write_file",
-    ),
     "excluded_tools": (
         "browser",
         "browser_evaluate",
         "browser_navigate",
         "browser_scroll",
         "browser_screenshot",
         "browser_press",
         "browser_type",
         "browser_click",
         "browser_snapshot",
     ),
 }
```

### Step 2：core.py — 状态 + 消息 + 逻辑

**文件**：`agent/middlewares/delegation_pressure/core.py`

**2a.** `_PressureState`（:44-49）——移除 blocked/block_injected，加 escalated：

```diff
 @dataclass
 class _PressureState:
     read_calls: int = 0
     warned: bool = False
-    blocked: bool = False
-    block_injected: bool = False
+    escalated: bool = False
```

**2b.** `__init__`（:70-74）——移除 `_blocked_tools`：

```diff
     def __init__(self) -> None:
         super().__init__()
         self._pressure_tools = frozenset(DELEGATION_PRESSURE["pressure_tools"])
-        self._blocked_tools = frozenset(DELEGATION_PRESSURE["blocked_tools"])
         self._excluded_tools = frozenset(DELEGATION_PRESSURE["excluded_tools"])
```

**2c.** `_warn_message`（:114-123）——去掉"will be disabled"威胁：

```diff
     def _warn_message(self, calls: int) -> str:
         return (
-            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE]\n\n"
             f"You have made {calls} read/exploration calls this turn.\n"
-            f"At {DELEGATION_PRESSURE['block_after']} read calls, read and write tools "
-            "will be disabled for the rest of the turn.\n"
-            "Delegate the remaining work now: call `sessions_spawn` with\n"
+            "Consider delegating the remaining work: call `sessions_spawn` with\n"
             "functional_role='researcher' for read-only research, or 'executor' for\n"
             "implementation/test work."
         )
```

**2d.** 替换 `_block_message` + `_refusal` 为 `_escalated_message`（:125-149）：

```diff
-    def _block_message(self) -> str:
-        return (
-            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE — READ/WRITE DISABLED]\n\n"
-            f"{DELEGATION_PRESSURE['block_after']} read calls were reached; every "
-            "read/write tool is paused for the rest of this turn.\n"
-            "Use `sessions_spawn` now: functional_role='researcher' for read-only\n"
-            "research, 'executor' for implementation/test work. Then use\n"
-            "`sessions_messages` / `subagents_list` / `sessions_yield` to collect the\n"
-            "result. Browser debugging tools remain available."
-        )
-
-    def _refusal(self, request: ToolCallRequest, tool_name: str) -> ToolMessage:
-        return ToolMessage(
-            content=(
-                "Delegation pressure: read/write tools are disabled for this turn "
-                f"after {DELEGATION_PRESSURE['block_after']} read calls. "
-                "Use `sessions_spawn` (researcher for read-only research, executor for "
-                "implementation) and collect its result with `sessions_messages` / "
-                "`subagents_list` / `sessions_yield`. "
-                f"`{tool_name}` remains unavailable until the next turn."
-            ),
-            tool_call_id=str(request.tool_call.get("id", "")),
-            name=tool_name,
-            status="error",
-        )
+    def _escalated_message(self, calls: int) -> str:
+        return (
+            "[SYSTEM DIRECTIVE: DELEGATION PRESSURE — ESCALATED]\n\n"
+            f"You have made {calls} read/exploration calls this turn — this is well "
+            "beyond what a main agent should spend on exploration before delegating.\n"
+            "Delegate NOW: call `sessions_spawn` with "
+            "functional_role='researcher' for read-only research, or 'executor' for\n"
+            "implementation/test work. Then use `sessions_messages` / "
+            "`subagents_list` / `sessions_yield` to collect the result.\n"
+            "Tools remain available, but continuing to read instead of delegating "
+            "will waste the turn's time budget."
+        )
```

**2e.** `_pre_tool`（:182-197）——移除 block 检查，只保留计数：

```diff
     def _pre_tool(self, request: ToolCallRequest) -> ToolMessage | None:
         session_id = str((request.state or {}).get("session_id") or "")
         if not _active(session_id):
             return None
         name = str(request.tool_call.get("name") or "")
         if self._excluded(name):
             return None
         state = self._get(session_id)
-        if state.blocked:
-            if name in self._blocked_tools:
-                return self._refusal(request, name)
-            return None
         if name in self._pressure_tools:
             state.read_calls += 1
             self._save(session_id, state)
-        return None
+        return None  # never refuse — the gate advises, never blocks
```

**2f.** `_post_tool`（:199-227）——移除 block 设置，只保留 spawn reset：

```diff
     def _post_tool(self, request: ToolCallRequest, result: Any) -> None:
         session_id = str((request.state or {}).get("session_id") or "")
         if not _active(session_id):
             return
         name = str(request.tool_call.get("name") or "")
         state = self._get(session_id)
         if name == "sessions_spawn" and self._spawn_accepted(result):
-            # A successful hand-off is the gate's purpose. Release the block
-            # and restart the count so the main agent can inspect/verify; a
-            # later 8/15-read pressure cycle will arm again. A failed spawn is
-            # not a hand-off and deliberately falls through without a reset.
+            # A successful hand-off is the gate's purpose. Restart the count
+            # so the main agent can inspect/verify; a later 7/14-read pressure
+            # cycle will arm again. A failed spawn is not a hand-off and
+            # deliberately falls through without a reset.
             self._save(session_id, _PressureState())
             logger.info(
                 "DelegationPressureGate: accepted spawn reset the pressure count session={}",
                 session_id,
             )
             return
-        if self._excluded(name) or name not in self._pressure_tools:
-            return
-        if state.blocked:
-            return
-        if state.read_calls >= DELEGATION_PRESSURE["block_after"]:
-            state.blocked = True
-            self._save(session_id, state)
-            logger.warning(
-                "DelegationPressureGate: read/write blocked session={} reads={}",
-                session_id,
-                state.read_calls,
-            )
```

**2g.** `abefore_model`（:267-288）——block 注入改为 escalated warn 注入：

```diff
     async def abefore_model(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
-        """Inject the one-time warning or the blocked directive."""
+        """Inject the one-time warning or the escalated directive."""
         try:
             session_id = self._session_id(state)
             if not _active(session_id):
                 return None
             gate = self._get(session_id)
-            if gate.blocked and not gate.block_injected:
-                gate.block_injected = True
-                self._save(session_id, gate)
-                return {"messages": [self._directive(self._block_message())]}
+            # Escalated warning at block_after reads: stronger language, still
+            # no tool block.  Checked before the first warning so the higher
+            # threshold wins when both are reached on the same call.
+            if (
+                gate.read_calls >= DELEGATION_PRESSURE["block_after"]
+                and not gate.escalated
+            ):
+                gate.escalated = True
+                self._save(session_id, gate)
+                return {"messages": [self._directive(self._escalated_message(gate.read_calls))]}
             if (
-                not gate.blocked
-                and gate.read_calls >= DELEGATION_PRESSURE["warn_after"]
+                not gate.escalated
+                and gate.read_calls >= DELEGATION_PRESSURE["warn_after"]
                 and not gate.warned
             ):
                 gate.warned = True
                 self._save(session_id, gate)
                 return {"messages": [self._directive(self._warn_message(gate.read_calls))]}
         except Exception:  # noqa: BLE001
             logger.exception("DelegationPressureGate: before_model failed; continuing")
         return None
```

**2h.** 模块 docstring（:1-15）——更新描述：

```diff
 """Main-agent delegation-pressure gate (7-read warn, 14-read escalated warn).

-The counter is cumulative for ONE turn. At ``warn_after`` the main agent
-receives one directive to delegate; at ``block_after`` every read/write tool
-call is refused for the rest of the turn with an error pointing at
-``sessions_spawn``. A successful spawn (accepted or pending) releases the block
-and resets the counter so the main agent can inspect/verify and a later 8/15
-cycle re-arms; a failed spawn does not reset. Writes alone do not reset the
-counter. ``after_agent`` also resets it at the turn boundary.
+The counter is cumulative for ONE turn. At ``warn_after`` the main agent
+receives one directive to delegate; at ``block_after`` it receives an escalated
+directive with stronger language. Tools remain available at both levels — the
+gate advises, never blocks. A successful spawn (accepted or pending) resets the
+counter so the main agent can inspect/verify and a later 8/15 cycle re-arms;
+a failed spawn does not reset. Writes alone do not reset the counter.
+``after_agent`` also resets it at the turn boundary.
 """
```

### Step 3：测试

**文件**：`tests/agent/middlewares/test_delegation_pressure.py`

**3a.** `test_defaults_are_eight_and_sixteen`（:79-86）——改名 + 移除 `blocked_tools` 断言：

```diff
 def test_defaults_are_seven_and_fourteen():
     assert DELEGATION_PRESSURE["warn_after"] == 7
     assert DELEGATION_PRESSURE["block_after"] == 14
     assert "read_file" in DELEGATION_PRESSURE["pressure_tools"]
-    assert "patch_file" in DELEGATION_PRESSURE["blocked_tools"]
     assert "browser" in DELEGATION_PRESSURE["excluded_tools"]
     assert "browser" not in DELEGATION_PRESSURE["pressure_tools"]
-    assert "browser" not in DELEGATION_PRESSURE["blocked_tools"]
```

**3b.** 重写 `test_warn_eight_then_block_reads_and_writes_until_after_agent`（:89-151）——改为 `test_warn_then_escalated_warn_never_blocks_tools`：

```python
@pytest.mark.asyncio
async def test_warn_then_escalated_warn_never_blocks_tools(
    gate: DelegationPressureGate,
):
    state_obj = {"session_id": _SESSION}

    # Warning at 2 (test override); the call executes.
    await _run(gate, "read_file")
    await _run(gate, "read_file")
    warn = await gate.abefore_model(state_obj)
    assert warn is not None
    assert "DELEGATION PRESSURE" in warn["messages"][0].content
    assert _state(gate).warned is True

    # Reads beyond warn_after still execute — no block.
    await _run(gate, "read_file")
    fourth, calls = await _run(gate, "read_file")
    assert fourth.status != "error"
    assert calls == ["read_file"]

    # Escalated warning at 4 (test override block_after=4).
    escalated = await gate.abefore_model(state_obj)
    assert escalated is not None
    assert "ESCALATED" in escalated["messages"][0].content
    assert _state(gate).escalated is True

    # Every read/write call STILL executes after the escalated warn.
    for name in ("read_file", "search_files", "terminal", "patch_file", "write_file"):
        result, calls = await _run(gate, name)
        assert result.status != "error", f"{name} must not be blocked"
        assert calls == [name], f"{name} must execute"

    # Browser debugging tools are neither counted nor blocked.
    browser, calls = await _run(gate, "browser")
    assert browser.status != "error"
    assert calls == ["browser"]

    # A successful spawn resets the count.
    spawn, calls = await _run(
        gate,
        "sessions_spawn",
        content="Subagent spawned: status=accepted, run_id=run-1",
    )
    assert spawn.status != "error"
    assert calls == ["sessions_spawn"]
    assert _state(gate).read_calls == 0
    assert _state(gate).escalated is False

    # The gate re-arms after another block_after reads.
    for _ in range(4):
        await _run(gate, "read_file")
    assert _state(gate).escalated is True

    # after_agent still resets at the turn boundary.
    gate._after_agent_impl(state_obj)
    assert _state(gate).read_calls == 0
    assert _state(gate).escalated is False
    next_turn, calls = await _run(gate, "read_file")
    assert next_turn.status != "error"
    assert calls == ["read_file"]
```

**3c.** 重写 `test_failed_spawn_does_not_unlock_or_reset`（:154-174）——改为验证 spawn 失败后计数不清零，但工具仍可用：

```python
@pytest.mark.asyncio
async def test_failed_spawn_does_not_reset_but_tools_remain_available(
    gate: DelegationPressureGate,
):
    for _ in range(4):
        await _run(gate, "read_file")
    assert _state(gate).read_calls == 4

    failed, calls = await _run(
        gate,
        "sessions_spawn",
        content="Error: spawn rejected",
        status="error",
    )
    assert calls == ["sessions_spawn"]
    assert _state(gate).read_calls == 4

    # Tools still work after a failed spawn — no block.
    still_works, calls = await _run(gate, "read_file")
    assert still_works.status != "error"
    assert calls == ["read_file"]
```

**3d.** 重写 `test_write_before_block_is_allowed_and_does_not_reset_reads`（:203-217）——改为验证 write 工具始终可用：

```python
@pytest.mark.asyncio
async def test_write_tools_are_always_available_and_do_not_reset_reads(
    gate: DelegationPressureGate,
):
    await _run(gate, "read_file")
    write, calls = await _run(gate, "patch_file")
    assert write.status != "error"
    assert calls == ["patch_file"]
    assert _state(gate).read_calls == 1

    # Even after block_after reads, write tools still work.
    for _ in range(3):
        await _run(gate, "read_file")
    write_after, calls = await _run(gate, "patch_file")
    assert write_after.status != "error", "write tools must not be blocked"
    assert calls == ["patch_file"]
```

**3e.** `test_successful_spawn_before_block_resets_and_rearms_warning`（:177-200）和 `test_inactive_session_is_a_noop`（:220-235）——基本不变，只需把 `"DELEGATION PRESSURE"` 断言保留（warn 内容仍含此词）。

### Step 4：其他引用

**文件**：`tests/config/test_features_agent_side.py:149`

```diff
-    (DELEGATION_PRESSURE, "block_after", 16),
+    (DELEGATION_PRESSURE, "block_after", 14),
```
`block_after` 仍在 config 中，默认值从 16 改为 14，语义从"阻断阈值"变为"升级 warn 阈值"。

**文件**：`tests/agent/core/test_middleware_order.py`——不需要改（gate 仍注册在中间件链中）。

---

## 3. 边界与取舍

### 3.1 会不会退回 patch 循环

报告提到"首版软门正是因此出现 patch 循环"——模型不停 patch 不委派。但：
- 首版软门没有 warn，只有建议，且没有 `OutputRepetitionGuard` 和 `PlanContinuationEnforcer` 的停滞检测
- 现在这两个中间件已经覆盖了 patch 循环的检测：`OutputRepetitionGuard` 检测重复输出，`PlanContinuationEnforcer` 检测 todo 停滞
- 7 次 warn + 14 次 escalated warn 仍然在引导模型委派，只是不强制
- 模型在看到 escalated warn 后仍选择自己做，通常是因为剩下的工作不值得 spawn（一个 10 秒的小修复 vs 一个 3 分钟的子代理）
- 成功 spawn（accepted/pending）后 `_post_tool` 重置 `read_calls` 为 0，计数器重新开始——主 agent 可以检视/验证子代理结果，直到再积累 7 次 read 才重新触发 warn

### 3.2 与 system-reminder 迁移计划的关系

本计划保留 `[SYSTEM DIRECTIVE: ...]` 文本前缀。`计划_delegation_pressure改走system_reminder.md` 可以与本计划叠加实施——先改格式（`wrap_reminder`），再改行为（block→warn），或反过来，互不冲突。

### 3.3 `DelegateOnlyGate` 不受影响

`DelegateOnlyGate`（`SHERRY_EVAL_DELEGATE_ONLY=1`）是 eval 专用的硬阻断门，有自己的 `_refusal`。本计划只改 `DelegationPressureGate`，不动 `DelegateOnlyGate`。

### 3.4 `blocked_tools` 移除的影响

`blocked_tools` 只被 `DelegationPressureGate.__init__` 和 `_pre_tool` 引用，没有外部消费者。移除后 TypedDict 少一个字段，`test_features_agent_side.py` 的 config 校验测试自动适配（它检查的是 key-value 对，不检查 key 存在性）。

### 3.5 升级 warn 的语气

`_escalated_message` 用"Delegate NOW"、"well beyond"、"will waste the turn's time budget"等更强语气，但不声称工具被断、不威胁切断。同时给出 `sessions_messages` / `subagents_list` / `sessions_yield` 的结果收集路径——这是 DeepSWE 报告 P1 TODO 要求的："在门注入的 directive 中说明结果会作为 completion 消息返回，并给出 sessions_messages 兜底"。

---

## 4. 验收清单

- [ ] 7 次 read → 注入 warn 指令（一次性），工具照常执行
- [ ] 14 次 read → 注入 escalated warn 指令（一次性，更强语气），工具**照常执行**
- [ ] read/write 工具在任何 read 次数下都不被阻断（`_pre_tool` 永远返回 `None`）
- [ ] 成功 spawn → 清零计数 + 清零 warned/escalated
- [ ] 失败 spawn → 计数不清零，工具仍可用
- [ ] `after_agent` → 重置 `_PressureState`
- [ ] `blocked_tools` 从 config 和 TypedDict 中移除
- [ ] `_refusal()` 方法删除
- [ ] `_PressureState` 无 `blocked` / `block_injected` 字段，有 `escalated` 字段
- [ ] `uv run --no-sync pytest tests/agent/middlewares/test_delegation_pressure.py -q` PASS
- [ ] `uv run --no-sync pytest tests/config/test_features_agent_side.py -q` PASS

---

## 5. 涉及文件一览

| 文件 | 改动类型 |
|---|---|
| `config/features/agent_side/delegation_pressure.py` | 移除 `blocked_tools` + 更新 docstring |
| `agent/middlewares/delegation_pressure/core.py` | `_PressureState` 改字段 + 删 `_refusal`/`_block_message` + 加 `_escalated_message` + `_pre_tool`/`_post_tool`/`abefore_model` 简化 + docstring |
| `tests/agent/middlewares/test_delegation_pressure.py` | 重写 block 相关测试为 warn-only 行为 |
| `tests/config/test_features_agent_side.py` | 自动适配（`blocked_tools` key 移除） |
