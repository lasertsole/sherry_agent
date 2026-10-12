# 计划：todo_reminder — 合并到 TodoContinuationEnforcer

- 建档日期：2026-10-09
- 修订日期：2026-10-12（从独立 reminder_catalog 源改为合并到中间件）
- 状态：**计划（未开工）**
- 父计划：[计划_system-reminder目录化.md](./计划_system-reminder目录化.md)
- 涉及文件：`agent/middlewares/todo_continuation/core.py`、`agent/tools/todolist/stagnation_tracker.py`

## 是什么

`todo_reminder` 是一个"模型长时间没调 `todowrite` 工具时提醒它追踪进度"的机制。借鉴 ZCode 的 `getTodoReminderTurnCounts`。

**修订决定：不新建 reminder_catalog 独立注入源，直接合并到 `TodoContinuationEnforcer`。** enforcer 已经在每轮 turn 结束时读 todos、判断 incomplete、注入续跑指令——它是天然监督 todo 管理的中间件，加一个"距上次 todowrite 几轮"的检查是小改动，不需要另起 catalog 基建。

## 为什么合并而不是独立

1. **enforcer 已有全部需要的状态**——todos、session_id、stagnation tracker 都在手里，加一个 todowrite-usage 计数器即可。
2. **自然解决任务结束后的提醒缺口**——enforcer 在 `if not incomplete: return None` 处已经挡掉了全完成的情况，todo_reminder 不需要再自己过滤 incomplete。
3. **避免为单一功能建 ~560 行基建**——reminder_catalog + DynamicReminderMiddleware + 配置文件 + 测试，如果只有 todo_reminder 一个新注入源，不值得。其他迁移项（taskflow / boulder / knowledge block 从 SystemMessage 挪到 HumanMessage）用的是现有 builder 换注入位置，不需要 catalog。
4. **如果后续 catalog 基建最终落地**，enforcer 里的 todo_reminder 逻辑可以再迁过去——不阻塞当前实现。

## 阈值冲突分析

enforcer 现有 stagnation 检测是 **3 次 snapshot 不变**就进停滞 → 恢复模式。todo_reminder 原设计是 **10 轮**没调 todowrite 才提醒。直接合并不改阈值，todo_reminder 永远不会触发——第 3 轮 snapshot 不变时 stagnation 就接管了。

**但有一个场景让两者不冲突**：模型在推进 TaskFlow 步骤（`taskflow_resume` / `taskflow_run_task` 改了 step 状态 → plan snapshot 变了 → streak 清零）但没调 `todowrite` 更新 todo 行。这时：
- snapshot 在变 → stagnation 不触发 → enforcer 在正常续跑模式
- 但模型已经 N 轮没碰 todowrite → todo_reminder 条件成立

这才是 todo_reminder 真正有价值的场景：**模型在干活但忘了更新 todo 列表**。

snapshot 不会在模型不调 todowrite 的情况下变化——除非 TaskFlow 步骤状态变了（plan_rows 包含 flow step 行）。所以 todo_reminder 的检查只在"模型在推进 TaskFlow 但没更新 todo"这个窗口内有意义。

## 实现方案

### 1. stagnation_tracker 增加 todowrite-usage 计数

```python
# agent/tools/todolist/stagnation_tracker.py

# 新增：距上次 todowrite 的轮次计数（per-session）
_turns_since_todowrite: dict[str, int] = {}

def record_turn(session_id: str) -> None:
    """每轮 aafter_agent 调用一次：递增 todowrite-usage 计数。"""
    _turns_since_todowrite[session_id] = _turns_since_todowrite.get(session_id, 0) + 1

def record_todowrite(session_id: str) -> None:
    """模型调了 todowrite 时重置计数。"""
    _turns_since_todowrite[session_id] = 0

def turns_since_todowrite(session_id: str) -> int:
    return _turns_since_todowrite.get(session_id, 0)

def reset(session_id: str) -> None:
    """clear all tracker state for a session（现有函数，加一行）。"""
    _stagnation_count.pop(session_id, None)
    _last_snapshot.pop(session_id, None)
    _last_inject_time.pop(session_id, None)
    _last_failure_time.pop(session_id, None)
    _recovery_attempts.pop(session_id, None)
    _turns_since_todowrite.pop(session_id, None)   # ← 新增
```

### 2. todowrite 工具调用时重置计数

`todowrite` 工具的执行路径（`TodoService.update_todos` 返回后）调 `st.record_todowrite(session_id)`。最简做法是在 `TodoContinuationEnforcer` 的注入逻辑里不处理——由 `ToolGuardrails` 或 `MessagePersistence` 的 `wrap_tool_call` 钩子在 todowrite 执行后调一行。

或者更简单：`aafter_agent` 里扫描当前轮的消息，如果出现了 `todowrite` 的 ToolMessage（工具返回），就调 `record_todowrite`。不依赖额外的中间件改动。

```python
# TodoContinuationEnforcer.aafter_agent 里，读 todos 之前：

# 每轮递增 todowrite-usage 计数
st.record_turn(session_id)

# 如果本轮调了 todowrite，重置计数
messages = state.get("messages", [])
if _turn_used_todowrite(messages):
    st.record_todowrite(session_id)
```

### 3. 续跑指令里追加 todowrite 提醒

在 enforcer 的正常续跑注入点（非停滞、非冷却、有 incomplete），检查 todowrite-usage：

```python
# agent/middlewares/todo_continuation/core.py

_TODO_WRITE_NUDGE_TURNS = 10  # 配置化：TODOLIST_INFRA["todo_write_nudge_turns"]

_TODO_WRITE_NUDGE = """

[REMINDER: TODO TRACKING]
You haven't used the todowrite tool in {n} turns. If task statuses have changed, 
use todowrite to keep your todo list in sync. Current todo list may be stale.
This is a gentle reminder — ignore if not applicable.
"""

# 在 aafter_agent 的正常续跑分支：
if st.is_in_cooldown(session_id):
    return None

n = st.turns_since_todowrite(session_id)
prompt = _CONTINUATION_PROMPT.format(
    todo_status=_build_status_block(todos), flow_status=flow_status
)
if n >= _TODO_WRITE_NUDGE_TURNS:
    prompt += _TODO_WRITE_NUDGE.format(n=n)

await self._inject(session_id, prompt)
```

**注意**：nudge 是**追加**到续跑指令后面，不是替代。模型同时收到"继续干活"和"别忘了更新 todo"两条信息。

### 4. 停滞/恢复模式下不追加 nudge

stagnation 触发时（`check_stagnation` 返回 True），走的是 `_RECOVERY_PROMPT`，里面已经有"Run todoread / mark completed via todowrite"的指引——不需要再追加 nudge。recovery prompt 比 nudge 更强，追加会冗余。

## 改名

`TodoContinuationEnforcer` → `PlanContinuationEnforcer`（或 `PlanSupervisorMiddleware`）。

现在就已经名不副实——模块 docstring 自己说的：

> "The module/class keep their `todo_continuation` names for compatibility; the todo list is one of the two plan surfaces, not the gate's whole scope."

它管的是"plan = todos + TaskFlow flows"两条线，名字只提了 todo。

### 兼容性

`_ORIGIN = "todo_continuation"` 标签写进了 message metadata 的 `origin` 字段，消息存储里已有历史行。改名时 **保留 `_ORIGIN` 值不动**（只改类名），避免历史行的 origin 读取断裂。或者同时改 origin 值 + 加一个向后兼容的读取层，但保留不动是最省事的。

`agent/middlewares/agent_switch.py::middleware_enabled` 通过 `type(self).__name__` 匹配中间件名——改名后预设配置里写的旧名会失配。需要：
- `agent_switch.py` 加一个别名映射（旧名 → 新名），或
- 预设配置的 `middlewares_disabled` 列表更新为新名（已有 session 的配置需迁移）

## 不做的事

- **不建 reminder_catalog 基建**——todo_reminder 是唯一的新注入源，不值得为此建 catalog + DynamicReminderMiddleware。其他迁移项（block 从 SystemMessage 挪到 HumanMessage）可以独立做，不需要 catalog。
- **不改 stagnation 检测的 3 轮阈值**——todo_reminder 的 10 轮检查与 stagnation 的 3 轮检查是正交的：stagnation 看 snapshot 变没变，todo_reminder 看 todowrite 调没调。模型推 TaskFlow 但不碰 todo → snapshot 变 → 不停滞 → todo_reminder 有窗口触发。
- **不独立注入 todo_reminder**——nudge 追加到续跑指令后面，不单独注入一条 HumanMessage，不单独触发 auto-turn。

## 验收

- 模型频繁调 todowrite → `turns_since_todowrite` 被重置 → 不追加 nudge
- 模型推进 TaskFlow 但 10 轮没调 todowrite → 续跑指令后追加 nudge
- 模型既没推进 TaskFlow 也没调 todowrite → snapshot 不变 → 3 轮后 stagnation 接管 → 走 recovery prompt（不追加 nudge，recovery 已含 todowrite 指引）
- 任务全部完成 → `if not incomplete: return None` → 不注入、不追加 nudge（缺口自然修复）
- 停滞/恢复模式下不追加 nudge（recovery prompt 已含更强指引）
- 类名改为 `PlanContinuationEnforcer`，`_ORIGIN` 值保留 `"todo_continuation"` 不动
- 预设配置的中中间件名兼容（别名映射或配置迁移）
