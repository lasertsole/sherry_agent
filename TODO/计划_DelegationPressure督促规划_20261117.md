# 计划：DelegationPressure 督促使用 TodoList 和 TaskFlow

- 日期：2026-11-17（r2，修复 TaskFlow dispatch 与 sessions_spawn 委派的冲突）
- 状态：**待评审**
- 定位：在现有 delegation_pressure 的"督促委派"之上，加一条"督促先规划"——agent 读了很多但从未创建 todo/TaskFlow 时，提醒先规划再执行。同时修复 TaskFlow dispatch 不被识别为委派的冲突。
- 风险等级：**低-中** — 加触发条件 + 提醒消息 + TaskFlow dispatch 感知
- 依赖：无

---

## 0. 当前 gap + 冲突

### gap：不督促规划

现有 delegation_pressure 在 7 次读操作后督促 `sessions_spawn`（委派），12 次后升级。但它**从不督促先规划**。现有 reminder 机制（todo_block / taskflow_status / _TODO_WRITE_REMINDER）都只在 todo/flow **已存在**时才显示/催更新——没有任何机制在 agent 大量读操作但从未规划时督促"先创建"。

### 冲突：TaskFlow dispatch 不被识别为委派

TaskFlow dispatch（`taskflow_step(action='dispatch')` 或 `taskflow_flow(action='plan')` auto-dispatch）内部调 `spawn_subagent_direct` spawn 子 agent——这是**结构化委派**。但 delegation_pressure 只认 `sessions_spawn` 工具调用作为"已委派"标志（`_post_tool` 检查工具名 → reset counter）。TaskFlow dispatch 的工具名是 `taskflow_step`/`taskflow_flow`，不是 `sessions_spawn`——**counter 不 reset，warn 照常触发，催 agent 用 sessions_spawn**。

结果：agent 被 plan_nudge 催去用 TaskFlow → 创建 flow + dispatch steps → 子 agent 在干活 → 主 agent 继续读 → warn 触发催 sessions_spawn → **agent 困惑：我已经在用 TaskFlow 委派了为什么还催 sessions_spawn？**

---

## 1. 修复后的完整触发阶梯

```
turn 开始 → read_calls=0

5 次读 + 无 todo + 无 flow
  → plan_nudge："先用 todowrite 或 taskflow_flow 规划"
  → agent 创建 flow + dispatch steps
  → has_taskflow=True, has_taskflow_dispatch=True
  → counter reset（TaskFlow dispatch = 委派，和 sessions_spawn 一样 reset）
  → read_calls=0

继续读（审阅结果/理解上下文）→ 7 次读 + has_taskflow_dispatch=True
  → warn（TaskFlow 版）："你在 TaskFlow 委派的同时还在大量读——用 wait_all 等结果或 dispatch 更多 step，别自己读"

继续读 → 12 次读 + has_taskflow_dispatch=True
  → escalated（TaskFlow 版）："停下来！你的 TaskFlow 子 agent 在干活，等结果（wait_all），别自己做"

=== 如果 agent 不用 TaskFlow ===

5 次读 + 无 todo + 无 flow
  → plan_nudge → agent 忽略，继续自己读

7 次读 + 无 taskflow_dispatch
  → warn（现有）："用 sessions_spawn 委派"

12 次读 + 无 taskflow_dispatch
  → escalated（现有）："立即 sessions_spawn"

=== 如果 agent 用了 todowrite 但不用 TaskFlow ===

5 次读 + has_todowrite=True + 无 flow
  → plan_nudge 不触发（已规划）
  → 继续读到 7
  → warn（现有）："用 sessions_spawn 委派"
```

### 三种路径总结

| 路径 | 5 次读 | 7 次读 | 12 次读 |
|------|--------|--------|---------|
| 用 TaskFlow 委派 | plan_nudge → 创建+dispatch → counter reset | warn（TaskFlow 版：dispatch/wait_all 别自己读） | escalated（TaskFlow 版：停下来等结果） |
| 用 sessions_spawn 直接委派 | plan_nudge → 忽略 | warn（现有：sessions_spawn）→ spawn → counter reset | — |
| 不规划不委派（自己干） | plan_nudge → 忽略 | warn（现有：sessions_spawn） | escalated（现有：立即 sessions_spawn） |
| 用了 todowrite 但不委派 | 不触发 plan_nudge | warn（现有：sessions_spawn） | escalated（现有） |

---

## 2. 状态扩展

```python
@dataclass
class _PressureState:
    read_calls: int = 0
    warned: bool = False          # 现有
    escalated: bool = False       # 现有
    plan_nudged: bool = False     # 新增：5 次催规划
    has_todowrite: bool = False   # 新增：本 session 调过 todowrite
    has_taskflow: bool = False    # 新增：本 session 创建过 TaskFlow flow
    has_taskflow_dispatch: bool = False  # 新增：通过 TaskFlow dispatch 过 step（= 结构化委派）
```

**每 turn reset**：`has_todowrite` / `has_taskflow` / `has_taskflow_dispatch` **每 turn reset**（和 read_calls 一样）。不跨 turn 保留——因为跨 turn 保留会导致新任务不催规划（turn 1 的 flow 已完成，turn 2 是新任务但 has_taskflow=True 让 plan_nudge 不触发）。

每 turn 重新追踪：如果这个 turn 里 agent 没调 todowrite 也没调 taskflow，就催规划。agent 如果有 active flow 在跑，收到 plan_nudge 后可以忽略（"advisory"——和现有 gate 一致，不阻止）。

```python
def _after_agent_impl(self, state):
    gate = self._load(session_id)
    gate.read_calls = 0
    gate.warned = False
    gate.escalated = False
    gate.plan_nudged = False
    gate.has_todowrite = False       # 每 turn reset
    gate.has_taskflow = False        # 每 turn reset
    gate.has_taskflow_dispatch = False  # 每 turn reset
    self._save(session_id, gate)
```

**warn/escalated 的问题**：`has_taskflow_dispatch` 也每 turn reset → 如果 turn 2 里 agent 没调 dispatch（只是 wait_all / resume 已有 flow），`has_taskflow_dispatch=False` → warn 用现有版（催 sessions_spawn）而非 TaskFlow 版。

**可接受**：如果一个 turn 里 agent 没在 dispatch 新 step，催它"用 sessions_spawn 或继续 dispatch"都是合理的——它确实没在这个 turn 里委派新工作。如果 agent 在这个 turn 里调了 dispatch（哪怕是给已有 flow 加 step），`has_taskflow_dispatch=True` → warn 用 TaskFlow 版。

---

## 3. 触发逻辑（abefore_model）

检查顺序从低阈值到高：

### 3.1 工具可用性检查

middleware 初始化时缓存主 agent 可用的工具名集合：

```python
class DelegationPressureGate(AgentMiddleware):
    def __init__(self):
        ...
        # 缓存工具可用性（进程级，主 agent 工具不变）
        from agent.tools import build_main_tools
        try:
            main_tools = build_main_tools()
            available_names = {t.name for t in (main_tools if isinstance(main_tools, list) else [main_tools])}
        except Exception:
            available_names = set()
        self._has_todowrite = "todowrite" in available_names
        self._has_taskflow = any(n.startswith("taskflow_") for n in available_names)
```

### 3.2 plan_nudge 条件

```python
async def abefore_model(self, state):
    if not self._active(session_id):
        return
    gate = self._load(session_id)

    # 1. plan_nudge（最低阈值 5）——只在从未规划且至少一个规划工具可用时触发
    has_planning_tool = self._has_todowrite or self._has_taskflow
    if (
        not gate.plan_nudged
        and gate.read_calls >= DELEGATION_PRESSURE["plan_nudge_after"]  # 5
        and not gate.has_todowrite
        and not gate.has_taskflow
        and has_planning_tool          # ← 新增：至少一个规划工具可用才催
    ):
        gate.plan_nudged = True
        self._save(session_id, gate)
        return {"messages": [self._directive(self._plan_message(gate.read_calls))]}

    # 2. warn（阈值 7）——消息根据 has_taskflow_dispatch 区分
    if (
        not gate.escalated
        and not gate.warned
        and gate.read_calls >= DELEGATION_PRESSURE["warn_after"]  # 7
    ):
        gate.warned = True
        self._save(session_id, gate)
        return {"messages": [self._directive(self._warn_message(gate.read_calls, gate.has_taskflow_dispatch))]}

    # 3. escalated（阈值 12）——消息根据 has_taskflow_dispatch 区分
    if (
        not gate.escalated
        and gate.read_calls >= DELEGATION_PRESSURE["block_after"]  # 12
    ):
        gate.escalated = True
        self._save(session_id, gate)
        return {"messages": [self._directive(self._escalated_message(gate.read_calls, gate.has_taskflow_dispatch))]}
```

### 3.3 warn/escalated 的 TaskFlow 版也检查工具可用性

warn/escalated 的 TaskFlow 版消息提到 `taskflow_step(action='wait_all')` 等——如果 taskflow 不可用，不应该用 TaskFlow 版消息。但 `has_taskflow_dispatch=True` 只可能在 taskflow 可用时为 True（agent 调了 taskflow 工具才会设 True），所以这里**隐式安全**——不需要额外检查。

如果 `has_taskflow_dispatch=False`（agent 没用 taskflow 或 taskflow 不可用），warn/escalated 用现有版（催 sessions_spawn）。sessions_spawn 是主 agent 始终可用的工具（在 `_MAIN_TOOLS_BUILDERS` 里）。

---

## 4. TaskFlow dispatch 感知（_post_tool）

```python
async def _post_tool(self, state, response):
    name = ...  # 工具名

    # 追踪 todowrite
    if name == "todowrite":
        gate.has_todowrite = True
        self._save(session_id, gate)

    # 追踪 TaskFlow 创建
    if name in ("taskflow_create", "taskflow_flow"):
        gate.has_taskflow = True
        # taskflow_flow(action='plan') 可能 auto-dispatch ready steps
        if self._taskflow_dispatched(response):
            gate.has_taskflow_dispatch = True
        self._save(session_id, gate)

    # 追踪 TaskFlow dispatch
    if name == "taskflow_step":
        if self._taskflow_dispatched(response):
            gate.has_taskflow_dispatch = True
            self._save(session_id, gate)

    # TaskFlow dispatch → reset counter（和 sessions_spawn 一样）
    if gate.has_taskflow_dispatch and name in ("taskflow_step", "taskflow_flow"):
        if self._taskflow_dispatched(response):
            gate.read_calls = 0
            gate.warned = False
            gate.escalated = False
            # plan_nudged 不 reset（已规划标记保留）
            self._save(session_id, gate)
            return

    # 现有：sessions_spawn accepted → reset
    if self._spawn_accepted(name, response):
        gate.read_calls = 0
        gate.warned = False
        gate.escalated = False
        self._save(session_id, gate)
```

### _taskflow_dispatched 检测

```python
def _taskflow_dispatched(self, response) -> bool:
    """TaskFlow 工具调用是否成功 spawn 了子 agent。"""
    text = str(getattr(response, 'content', ''))
    return any(marker in text for marker in (
        "auto-dispatched",      # taskflow_flow(action='plan') auto-dispatch
        "dispatched",           # taskflow_step(action='dispatch')
        "status=accepted",      # 子 run accepted
        "status=pending",       # 子 run pending
        "subagent spawned",     # spawn 成功（复用 _spawn_accepted 的标记）
    ))
```

---

## 5. 消息

### plan_nudge（新增，阈值 5）

消息根据可用工具动态构建——只提可用的工具：

```python
def _plan_message(self, read_calls: int) -> str:
    tips = []
    if self._has_todowrite:
        tips.append("- For simple tasks: use `todowrite` to track your steps.")
    if self._has_taskflow:
        tips.append(
            "- For complex multi-step tasks with dependencies: use "
            "`taskflow_flow(action=\"plan\")` to create a structured DAG that "
            "auto-creates a todo, dispatches steps to subagents, and tracks completion."
        )
    tips_text = "\n".join(tips)
    return (
        f"<system-reminder source=\"delegation_pressure\">"
        f"PLANNING NUDGE: You have made {read_calls} read/exploration calls "
        f"without creating a task plan. Before diving deeper, consider:\n"
        f"{tips_text}\n"
        f"A plan helps you track progress, delegate effectively, and avoid missing steps. "
        f"This is advisory — proceed without a plan if the task is simple enough."
        f"</system-reminder>"
    )
```

**三种消息变体**：

| 工具可用 | 消息内容 |
|---------|---------|
| todowrite ✅ + taskflow ✅ | 提两个（完整消息） |
| 只 todowrite ✅ | 只提 todowrite |
| 只 taskflow ✅ | 只提 taskflow_flow |
| 都不可用 | plan_nudge 不触发（`has_planning_tool=False`） |

### warn（阈值 7，按 has_taskflow_dispatch 区分）

```python
def _warn_message(self, read_calls: int, has_taskflow_dispatch: bool) -> str:
    if has_taskflow_dispatch:
        # TaskFlow 版：agent 已在通过 TaskFlow 委派，催"别自己读"
        return (
            f"<system-reminder source=\"delegation_pressure\">"
            f"You have made {read_calls} read calls while your TaskFlow subagents are working. "
            f"Instead of reading extensively yourself, consider:\n"
            f"- `taskflow_step(action=\"wait_all\")` to wait for dispatched steps to complete.\n"
            f"- `taskflow_step(action=\"dispatch\")` to dispatch more ready steps.\n"
            f"Let your subagents do the reading — you focus on coordination."
            f"</system-reminder>"
        )
    else:
        # 现有消息：催 sessions_spawn
        return (
            f"<system-reminder source=\"delegation_pressure\">"
            f"You have made {read_calls} read/exploration calls this turn. "
            f"Consider delegating the remaining work: call sessions_spawn with "
            f"functional_role='researcher' (for investigation) or 'executor' (for implementation). "
            f"Tools remain available."
            f"</system-reminder>"
        )
```

### escalated（阈值 12，按 has_taskflow_dispatch 区分）

```python
def _escalated_message(self, read_calls: int, has_taskflow_dispatch: bool) -> str:
    if has_taskflow_dispatch:
        # TaskFlow 版：agent 已在委派但仍大量读——停下来等结果
        return (
            f"<system-reminder source=\"delegation_pressure\">"
            f"[DELEGATION PRESSURE — ESCALATED] "
            f"You have made {read_calls} read calls while TaskFlow subagents are working. "
            f"STOP reading. Your subagents are doing the work. "
            f"Call `taskflow_step(action=\"wait_all\")` to wait for results, "
            f"then `taskflow_step(action=\"resume\")` to inject them. "
            f"Do not continue reading on your own."
            f"</system-reminder>"
        )
    else:
        # 现有消息：升级催 sessions_spawn
        return (
            f"<system-reminder source=\"delegation_pressure\">"
            f"[DELEGATION PRESSURE — ESCALATED] "
            f"You have made {read_calls} read/exploration calls. Delegate NOW: "
            f"call sessions_spawn with functional_role='researcher' or 'executor'. "
            f"Then use sessions_messages / subagents_list / sessions_yield to collect the result. "
            f"Do not continue reading on your own."
            f"</system-reminder>"
        )
```

---

## 6. 配置

**文件**：`config/features/agent_side/delegation_pressure.py`

```python
DELEGATION_PRESSURE: DelegationPressureConfig = {
    "enabled": True,
    "warn_after": 7,                    # 现有
    "block_after": 12,                  # 现有
    "plan_nudge_after": 5,              # 新增
    "pressure_tools": (...),            # 现有不变
    "excluded_tools": (...),            # 现有不变
}
```

---

## 7. 改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `agent/middlewares/delegation_pressure/core.py` | `__init__` 加工具可用性缓存（`_has_todowrite`/`_has_taskflow`）；`_PressureState` 加 4 字段；`abefore_model` 加 plan_nudge 检查（含 `has_planning_tool` 门控）+ warn/escalated 按 has_taskflow_dispatch 分支；`_post_tool` 加 todowrite/taskflow 追踪 + TaskFlow dispatch reset；`_after_agent_impl` 全 reset（含 has_*）；加 `_plan_message()`（动态构建按可用工具）；`_warn_message`/`_escalated_message` 加 has_taskflow_dispatch 参数；加 `_taskflow_dispatched()` | ~90 |
| `config/features/agent_side/delegation_pressure.py` | 加 `"plan_nudge_after": 5` | ~1 |
| `tests/agent/middlewares/test_delegation_pressure.py` | 加 20 个测试（见 §8） | ~120 |
| **总计** | | ~211 |

---

## 8. 测试

| # | 场景 | 预期 |
|---|------|------|
| 1 | 5 次读 + 无 todo + 无 flow + 两个工具都可用 | plan_nudge 触发，消息含 todowrite 和 taskflow_flow |
| 2 | 5 次读 + 无 todo + 无 flow + 只有 todowrite 可用 | plan_nudge 触发，消息只含 todowrite |
| 3 | 5 次读 + 无 todo + 无 flow + 只有 taskflow 可用 | plan_nudge 触发，消息只含 taskflow_flow |
| 4 | 5 次读 + 无 todo + 无 flow + 两个工具都不可用 | plan_nudge 不触发 |
| 5 | 5 次读 + has_todowrite=True | plan_nudge 不触发（已规划） |
| 6 | 5 次读 + has_taskflow=True | plan_nudge 不触发（已规划） |
| 7 | plan_nudge 后继续读到 7 | warn 触发 |
| 8 | plan_nudge 后 agent 创建 flow + dispatch | has_taskflow_dispatch=True → counter reset |
| 9 | has_taskflow_dispatch=True + 7 次读 | warn 触发，消息含 "wait_all" / "dispatch"（TaskFlow 版） |
| 10 | has_taskflow_dispatch=True + 12 次读 | escalated 触发，消息含 "STOP reading" / "wait_all"（TaskFlow 版） |
| 11 | has_taskflow_dispatch=False + 7 次读 | warn 触发，消息含 "sessions_spawn"（现有版） |
| 12 | has_taskflow_dispatch=False + 12 次读 | escalated 触发，消息含 "sessions_spawn"（现有版） |
| 13 | taskflow_step(action='dispatch') 成功 | counter reset（和 sessions_spawn 一样） |
| 14 | taskflow_flow(action='plan') auto-dispatch 成功 | has_taskflow_dispatch=True + counter reset |
| 15 | taskflow_step(action='dispatch') 失败（无 ready step） | has_taskflow_dispatch 不变，counter 不 reset |
| 16 | turn 结束 → 新 turn | 全部字段 reset（包括 has_*） |
| 17 | sessions_spawn accepted | counter reset（现有行为不变） |
| 18 | 子 agent session | 不触发（_active 排除 child/swarm） |
| 19 | 跨 turn 新任务 | 全部 reset → 新任务正确催规划 |
| 20 | 跨 turn 同任务续（wait_all 不 dispatch） | has_taskflow=False → plan_nudge 触发但 advisory 可忽略 |

---

## 9. Kill criteria

- 如果 `plan_nudge_after=5` 过早触发，调高到 6
- 如果 `_taskflow_dispatched` 的标记匹配误报率高（response 文本含 "dispatched" 但实际没 spawn），收窄匹配（只匹配 "status=accepted" / "status=pending" / "subagent spawned"）
- 如果工具可用性缓存在 `__init__` 里调用 `build_main_tools()` 有副作用（工具初始化太重），改为延迟检查（首次 `abefore_model` 时查一次 + 缓存）
- 如果 agent 收到 plan_nudge 后仍然不规划，接受——gate 是 advisory 不阻止，和现有 warn/escalated 一致
- 如果跨 turn 同任务续（wait_all 不 dispatch）时 plan_nudge 误触发，接受——advisory 可忽略
