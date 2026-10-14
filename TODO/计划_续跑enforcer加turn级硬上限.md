# 计划：续跑 enforcer 加 turn 级硬上限 + 资源上限补齐

- 建档日期：2026-10-13
- 状态：**计划（未开工）**
- **2026-10-14 更新**：`审计_Sherry过度工程.md` 将 PlanContinuationEnforcer 标记为**过度工程**——"auto-turn 延长在交互场景下多余，agent 自然会继续工作"。本文 §3（turn 计数器）**不与此结论冲突**：turn 计数器是 enforcer **已开启时**的安全 guard（简单 in-memory counter，非 LLM 机制），防止"模型每轮微改计划 → 停滞永不触发 → 无限 inject 循环"。审计的建议是让 enforcer opt-in（自主场景开、用户在场关），本文的 turn 计数器正是在"已开启"场景下兜底。两者互补：审计管"何时开 enforcer"，本文管"开了之后如何限界"。
- 触发问题：`PlanContinuationEnforcer` 的 `aafter_agent` 在每轮结束时检查计划
  是否完成，未完成则通过 `MAYBE_TRIGGER_AUTO_TURN` 注入续跑指令。停滞检测
  （3 次不变快照 + 2 次恢复）能捕获"无进展"场景，但如果模型每轮都微改计划
  （改变一个 todo 状态、加一个 step），停滞永不触发，续跑 inject→auto-turn→inject
  循环没有硬上限。对标 hermes-agent 的 goal-loop `max_turns=20` 机制。
- 结论先行：
  1. **缺口在 enforcer 侧**——auto-turn service 是无状态 fire-and-forget，
     计数必须在 enforcer 的注入决策点做。
  2. **自然插入点是 `mark_injected`**——它是唯一确认投递成功的 chokepoint
     （`_inject` 成功调 trigger 后才调它），所有续跑/恢复路径都经过它。
  3. **真实用户消息重置计数器**——通过检查 last HumanMessage 的
     `metadata["origin"]` 区分续跑 turn（`"todo_continuation"`）和真实用户 turn。
     子代理完成通知（`origin="subagent_announce"`）也重置——它是新事件，不是续跑。
  4. **到上限后注入停止指令 + 设 stopped 标志**——stopped 标志防止停止指令
     自身触发新一轮 inject（停止指令也走 `_inject`→`mark_injected`）。

---

## 1. 现状分析

### 1.1 续跑路径完整流程

```
aafter_agent (turn 结束时)
  │
  ├─ middleware_enabled? → no → return
  ├─ session_id 为空? → return
  ├─ abort 错误? → return
  ├─ record_turn / record_todowrite  ← todowrite 使用计数
  │
  ├─ 读取 todos + flows
  ├─ 全部完成? → st.reset() → return          ← 计数器在这里被清理
  │
  ├─ check_stagnation? (3 次不变快照)
  │    ├─ should_enter_recovery? (2 次恢复)  → inject RECOVERY_PROMPT
  │    └─ 否 → return (停滞但不恢复)
  │
  ├─ is_in_cooldown? → return                  ← 指数退避
  │
  └─ inject CONTINUATION_PROMPT → _inject()
       │
       ├─ resolve MAYBE_TRIGGER_AUTO_TURN
       ├─ await trigger(session_key, HumanMessage)
       └─ st.mark_injected(session_id)          ← 唯一确认投递点
```

### 1.2 已有上限

| 防护 | 值 | 位置 | 作用域 |
|---|---|---|---|
| 停滞检测 | 3 次不变快照 | `stagnation_tracker.py:49-68` | 每 session |
| 恢复尝试 | 2 | `stagnation_tracker.py:89-99` | 每 session |
| 冷却退避 | 2s→60s 指数 | `stagnation_tracker.py:71-81` | 每 session |
| 主代理迭代预算 | 90（`main_agent_max_iterations`） | `iteration_budget.py:16` | 每 invoke |
| 子代理迭代预算 | 60（`worker_max_iterations`） | `iteration_budget.py:17` | 每 invoke |
| 子代理 goal-loop | 5（`goal_max_turns`） | `completion_judge.py:22` | 每次 spawn |
| TaskFlow replan | 3（`REPLAN_MAX`） | `taskflow_replan.py:43` | 每个 flow |
| StepJudge 重试 | 2（`STEP_JUDGE["max_retries"]`） | `step_judge.py:24` | 每个 step |
| 递归深度 | 2（`max_spawn_depth`，硬上限） | `subagent/config.py:7,15` | 进程级 |
| 每父并发子代理 | 5（`max_children_per_agent`） | `subagent/config.py:19` | 每请求 session |
| 计划步骤上限 | 12（`PLAN_MAX_STEPS`，仅 `taskflow_plan`） | `taskflow_plan.py:25` | 每个 plan 生成 |

### 1.2.1 子代理 goal-loop 达到上限后的行为

`goal_max_turns = 5`（`config/features/agent_side/completion_judge.py:22`），
在 `_run_goal_loop`（`spawn/core.py:853-890`）中执行：

```
第 1 次 invoke（turn 1，已经发生）
  ↓
while True:
  ├─ judge_completion(task, last_response, evidence)
  │    └─ 辅助 LLM（temperature=0，fail-open→DONE）
  │
  ├─ DONE → 立即返回（正常完成）
  │
  ├─ CONTINUE:
  │    ├─ turns_used >= goal_max_turns (5)?
  │    │    └─ YES → 返回 RunOutcome(status=OK, error="goal_loop_budget_exhausted")
  │    │              → _finalize_child_run 完成运行
  │    │              → 子代理以"完成"状态结束（不是失败）
  │    │              → announce pipeline 将结果送回主 agent
  │    │              → 不再注入续跑
  │    │
  │    └─ NO → 注入 judge 的 continuation_prompt 作为 HumanMessage
  │              → 重新 ainvoke 同一个 child agent（同一个 checkpointer 线程，
  │                继续同一对话）
  │              → turns_used += 1
  │              → 回到 while True 顶部
```

**达到上限后的完整链路：**

1. `turns_used >= 5` → 返回 `RunOutcome(status=OK, error="goal_loop_budget_exhausted")`
   （`core.py:880-883`）
2. `_finalize_child_run`（`core.py:1149`）完成运行——子代理以**成功状态**结束
   （不是 timeout/failed），只是 `error` 字段标记了"预算耗尽"
3. announce pipeline 将子代理的最终输出发送回主 agent
4. 主 agent 收到子代理结果，继续自己的工作流

**关键设计选择：**
- **不是失败**——`status=OK` 意味着子代理正常结束，只是没有完全达成目标
  （可能做了部分工作）。主 agent 可以决定是否再 spawn 一个新的子代理继续
- **fail-open**——judge 出错（模型失败、JSON 解析失败）时降级到 `DONE`
  （`completion_judge.py:139-140, 168-170`），所以 judge 不会卡住子代理
- **无 wall-clock 兜底**（缺口 #1）——goal-loop 5 turns × 每 turn 60 iterations
  = 最坏 ~300 次模型+工具调用，但无总时长限制。如果模型每次调用都很慢
  （例如复杂推理），5 turns 可能运行很久。缺口 #1 的 `run_timeout_seconds=600`
  补这个——到 10 分钟强制 timeout
- **per-spawn 重置**——每次 spawn 都从 `turns_used=1` 开始，
  不累积跨 spawn 的计数（和本方案的 #3/#4 主 agent 续跑计数器不同，
  后者累积跨 turn 的续跑计数）

### 1.3 缺口

**无连续续跑 turn 计数器。** 如果模型每轮都微改计划：
- 停滞检测：快照变化 → streak 清零 → 不触发
- 恢复尝试：不进入恢复路径
- 冷却退避：每次注入后等几秒，但不限制总次数
- 迭代预算：每轮 90 次，但轮数无上限
- → inject → auto-turn → aafter_agent → inject → auto-turn → ... 无限循环

### 1.4 auto-turn service 无状态

- `_INFLIGHT: dict[str, asyncio.Task]` 只持有当前运行的 runner，turn 结束后 pop
- 无 per-session 总计数器
- 不能在 runner 侧计数——runner 是 fire-and-forget 投递，决策在 enforcer 侧

### 1.5 stagnation_tracker 现有状态

```python
# 6 个 per-session dict，全部 in-memory
_stagnation_count: dict[str, int] = {}        # 不变快照 streak
_last_snapshot: dict[str, str] = {}           # "content=status|..."
_last_inject_time: dict[str, float] = {}      # 最后注入时间
_last_failure_time: dict[str, float] = {}     # 最后停滞时间
_recovery_attempts: dict[str, int] = {}       # 恢复尝试次数
_turns_since_todowrite: dict[str, int] = {}   # 未调 todowrite 的 turn 数
```

`reset(session_id)` 清理全部 6 个 dict。无 `StateKey` / `TypedState`——续跑子系统
完全用模块级 dict，不用状态寄存器。

---

## 2. 对标 hermes-agent

hermes-agent 的两层防护：

### 2.1 内层：IterationBudget（tool 循环）

```python
class IterationBudget:
    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True
```

- 主代理 90、子代理 50（硬计数器，线程安全）
- **每 invoke 重置**（`turn_context.py:258`：`agent.iteration_budget = IterationBudget(agent.max_iterations)`）
- 耗尽后 break + 注入一条"总结你自己"的无工具 API 调用
- 有 `refund()` 机制（压缩、RPC 工具调用不消耗预算）

### 2.2 外层：Goal-loop turn budget（turn 循环）

```python
@dataclass
class GoalState:
    goal: str
    status: str = "active"          # active | paused | done | cleared
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS  # 20
```

- `evaluate_after_turn` 在每轮结束后递增 `turns_used`
- Completion judge（辅助 LLM，temperature=0，fail-open）决定 DONE/CONTINUE/WAIT
- 到 `turns_used >= max_turns` → `status="paused"` + 用户可操作消息
  `"⏸ Goal paused — 20/20 turns used. Use /goal resume to keep going, or /goal clear to stop."`
- WAIT 不消耗 turn（等待异步工作）
- **per-goal 持久化**（SessionDB `state_meta`），`/goal resume` 重置计数

### 2.3 Sherry 的对应关系

| hermes-agent | Sherry | 差异 |
|---|---|---|
| `IterationBudget` (90/50, per-invoke) | `IterationBudget` middleware (90/60) | 已有，不需改 |
| `GoalState.turns_used` (20, per-goal) | **无** ← 本方案要加 | Sherry 的续跑 enforcer 对标 hermes 的 goal loop |
| 到上限 → `paused` + 用户消息 | 到上限 → inject STOP + stopped 标志 | 行为一致 |
| 用户 `/goal resume` 重置 | 真实用户消息重置 | Sherry 自动检测，不需用户手动命令 |
| 持久化（SessionDB） | in-memory | Sherry 的续跑子系统全部 in-memory，保持一致 |

---

## 3. 设计方案（仅当 enforcer 已开启时生效）

> **与 `审计_Sherry过度工程.md` 的关系**：审计认定 PlanContinuationEnforcer 整体过度工程，建议用户在场场景默认关。本节的 turn 计数器不改变这一结论——它是一个 in-memory counter + stopped flag（非 LLM 机制），只在 enforcer **已开启**（自主多步场景）时兜底"无限 inject 循环"。如果按审计建议在用户在场场景默认关 enforcer，本节不生效。两者互补。

### 3.1 Config（`config/features/agent_side/todolist_infra.py`）

TypedDict 加一个成员 + dict 加一个值：

```python
class TodoListInfraConfig(TypedDict):
    ...
    #: 连续续跑 turn 上限。计的是 mark_injected 确认投递的续跑/恢复指令数，
    #: 不含子代理完成通知（origin≠"todo_continuation"）触发的 turn。
    #: 到上限后注入一条"停止并总结"指令，然后停止注入直到真实用户消息重置。
    max_continuation_turns: int

TODOLIST_INFRA: TodoListInfraConfig = {
    ...
    "max_continuation_turns": 15,
}
```

**默认值 15 的理由：**
- 停滞场景约 5 轮（3 触发 + 2 恢复），15 给"有进展"场景 3 倍宽裕
- 子代理完成通知（`origin="subagent_announce"`）会重置计数器——
  TaskFlow 多步流程每步子代理完成都是一次重置，不受 15 限制
- 比 hermes 的 20 保守，比 deepagents 的 9999 严格得多
- 每轮有 IterationBudget(90) 兜底，15 轮 = 最多 1350 次模型+工具调用

### 3.2 Stagnation tracker（`agent/tools/todolist/stagnation_tracker.py`）

新增两个 per-session dict + 四个函数，修改 `mark_injected` 和 `reset`：

```python
# config 绑定
_MAX_CONTINUATION_TURNS = TODOLIST_INFRA["max_continuation_turns"]

# 新增 per-session dict
_continuation_turns: dict[str, int] = {}   # 成功投递的续跑指令计数
_stopped: dict[str, bool] = {}              # 上限触达标志

# 新增函数
def continuation_turns(session_id: str) -> int:
    """返回连续续跑 turn 计数。"""
    return _continuation_turns.get(session_id, 0)

def is_stopped(session_id: str) -> bool:
    """上限触达后停止注入，直到真实用户消息重置。"""
    return _stopped.get(session_id, False)

def mark_stopped(session_id: str) -> None:
    """标记 session 已触达续跑上限。"""
    _stopped[session_id] = True

def reset_continuation_turns(session_id: str) -> None:
    """重置续跑计数 + 停止标志（真实用户消息到达时调用）。
    不清停滞/恢复状态——模型可能仍在同一段停滞里。"""
    _continuation_turns.pop(session_id, None)
    _stopped.pop(session_id, None)

# 修改 mark_injected —— 增加计数
def mark_injected(session_id: str) -> None:
    _last_inject_time[session_id] = _now()
    _continuation_turns[session_id] = _continuation_turns.get(session_id, 0) + 1

# 修改 reset —— 清理新 dict
def reset(session_id: str) -> None:
    _stagnation_count.pop(session_id, None)
    _last_snapshot.pop(session_id, None)
    _last_inject_time.pop(session_id, None)
    _last_failure_time.pop(session_id, None)
    _recovery_attempts.pop(session_id, None)
    _turns_since_todowrite.pop(session_id, None)
    _continuation_turns.pop(session_id, None)   # 新增
    _stopped.pop(session_id, None)               # 新增
```

**设计要点：**
- `mark_injected` 是唯一确认投递的 chokepoint（`_inject` 成功调 trigger 后
  才调它），计数在这里递增最准确——失败的注入（trigger 返回 BUSY/ALREADY_PENDING
  或抛异常）不递增
- `_stopped` 防止"停止指令"自身触发新一轮 inject：停止指令也走
  `_inject`→`mark_injected`（计数 +1），但 `mark_stopped` 在下一轮
  `aafter_agent` 被 `is_stopped` 拦截
- `reset_continuation_turns` 只清这两个 dict，不清停滞/恢复状态
  （真实用户消息后停滞状态应保留——模型可能仍在同一段停滞里）

### 3.3 续跑 enforcer（`agent/middlewares/todo_continuation/core.py`）

三处改动：

**(a) 顶部新增：真实用户消息重置计数器**

在 `aafter_agent` 的 session_id 校验之后（:305 之后）、abort 检查之前：

```python
from langchain_core.messages import HumanMessage

# 真实用户消息重置续跑计数器。auto-turn 续跑（origin="todo_continuation"）
# 不重置——它就是计数器要计的东西。子代理完成通知（origin="subagent_announce"）
# 也重置——它是新事件，不是续跑。
if not _is_continuation_turn(state):
    st.reset_continuation_turns(session_id)
```

新增 helper（模块级）：
```python
def _is_continuation_turn(state: dict) -> bool:
    """True 当当前 turn 由续跑 enforcer 的注入触发（origin=todo_continuation）。

    扫描 state["messages"] 的最后一条 HumanMessage。续跑注入的
    HumanMessage 携带 metadata={"origin": "todo_continuation", "internal": True}。
    其他 origin（用户消息、子代理通知、workspace notice）都返回 False。
    """
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            origin = (msg.metadata or {}).get("origin", "")
            return origin == "todo_continuation"
    return False
```

**(b) incomplete-plan 检查之后、stagnation 之前：stopped 拦截 + 上限检查**

在 `:335`（`if not incomplete: st.reset(...); return None`）之后、
`:337`（`flow_block = ...`）之前：

```python
# 续跑上限触达后停止注入，直到真实用户消息重置。
if st.is_stopped(session_id):
    logger.info(
        "plan continuation: session {} stopped (continuation cap reached)",
        session_id,
    )
    return None

# 连续续跑 turn 上限：注入停止指令 + 标记 stopped。
if st.continuation_turns(session_id) >= _MAX_CONTINUATION_TURNS:
    flow_block = _build_flow_block(flows)
    flow_status = f"\n{flow_block}" if flow_block else ""
    logger.info(
        "plan continuation: session {} hit continuation cap ({}); "
        "injecting stop directive",
        session_id,
        _MAX_CONTINUATION_TURNS,
    )
    await self._inject(
        session_id,
        _STOP_PROMPT.format(
            todo_status=_build_status_block(todos),
            flow_status=flow_status,
            used=st.continuation_turns(session_id),
            cap=_MAX_CONTINUATION_TURNS,
        ),
    )
    st.mark_stopped(session_id)
    return None
```

新增模块级常量和 prompt：
```python
_MAX_CONTINUATION_TURNS = TODOLIST_INFRA["max_continuation_turns"]

_STOP_PROMPT = (
    "{todo_status}{flow_status}\n\n"
    "[SYSTEM] The automatic continuation budget is exhausted "
    "({used}/{cap} turns). "
    "Stop calling tools and provide a final summary of what you accomplished "
    "and what remains. The user can continue by sending a new message."
)
```

注意：`flow_block` 的计算提前到上限检查处（原本在 `:337`），因为停止指令
也需要包含计划状态。

**(c) `_inject` 不改**——已有的 `mark_injected` 调用会递增计数器，
停止指令也走这条路径。

### 3.4 完整执行流

```
用户消息 → turn 运行 → aafter_agent:
  ├─ _is_continuation_turn? No (origin≠"todo_continuation") → reset_continuation_turns
  ├─ 计划未完成?
  │    ├─ is_stopped? No
  │    ├─ continuation_turns >= 15? No
  │    ├─ stagnation? No
  │    ├─ cooldown? No
  │    └─ inject CONTINUATION_PROMPT → mark_injected (counter=1) → auto-turn
  └─ auto-turn → turn 运行 → aafter_agent:
       ├─ _is_continuation_turn? Yes (origin="todo_continuation") → 不重置
       ├─ 计划未完成?
       │    ├─ is_stopped? No
       │    ├─ continuation_turns >= 15? No (counter=1)
       │    └─ inject → counter=2 → auto-turn → ... → counter=15
       └─ counter=15 → inject STOP_PROMPT → mark_injected (counter=16)
            → mark_stopped → auto-turn (停止指令) → aafter_agent:
                 ├─ is_stopped? Yes → return None (不注入)
                 └─ 等待用户消息
用户消息 → reset_continuation_turns (counter=0, stopped=False) → 新一轮

子代理完成 → auto-turn (origin="subagent_announce") → aafter_agent:
  ├─ _is_continuation_turn? No (origin≠"todo_continuation") → reset_continuation_turns
  └─ 续跑计数从 0 重新开始（子代理完成是新事件，不是续跑）
```

---

## 4. 文件清单

### 修改文件（3 个）

| 文件 | 改动 |
|---|---|
| `config/features/agent_side/todolist_infra.py` | TypedDict 加 `max_continuation_turns: int`；dict 加 `"max_continuation_turns": 15` |
| `agent/tools/todolist/stagnation_tracker.py` | 新增 `_continuation_turns` + `_stopped` dict；新增 `continuation_turns()` / `is_stopped()` / `mark_stopped()` / `reset_continuation_turns()`；`mark_injected` 递增计数；`reset` 清理新 dict |
| `agent/middlewares/todo_continuation/core.py` | 新增 `_is_continuation_turn()` helper；`aafter_agent` 顶部加用户消息重置；incomplete 之后加 stopped 拦截 + 上限检查 + STOP_PROMPT 注入；新增 `_MAX_CONTINUATION_TURNS` 和 `_STOP_PROMPT` 常量 |

### 测试文件（1 个，扩展现有）

| 文件 | 新增测用例 |
|---|---|
| `tests/agent/middlewares/test_todo_continuation.py` | 7 个新测用例（见下） |

### 新增测用例

1. **`test_continuation_turn_counter_increments`**——mock trigger，验证 `mark_injected` 递增 `_continuation_turns`
2. **`test_continuation_cap_injects_stop_and_sets_stopped`**——设 `max_continuation_turns=3`，注入 3 次后第 4 次 `aafter_agent` 注入停止指令 + `is_stopped` 为 True
3. **`test_stopped_prevents_further_injections`**——`is_stopped=True` 时 `aafter_agent` 不注入
4. **`test_real_user_message_resets_counter`**——state 的 last HumanMessage `origin≠"todo_continuation"` 时 `reset_continuation_turns` 被调用
5. **`test_continuation_turn_does_not_reset`**——state 的 last HumanMessage `origin="todo_continuation"` 时计数器不清
6. **`test_subagent_announce_resets_counter`**——`origin="subagent_announce"` 重置计数器（不是续跑）
7. **`test_reset_clears_continuation_state`**——`reset()` 清理 `_continuation_turns` 和 `_stopped`

---

## 5. 边界场景

| 场景 | 行为 | 理由 |
|---|---|---|
| 模型每轮微改计划 | 15 轮后注入停止指令 | 停滞检测看不到"有进展但不完成"，计数器兜底 |
| 模型停滞（不变 3 次） | 停滞触发 → 恢复 2 次 → 停止 | 停滞先于计数器上限触发（~5 < 15），行为不变 |
| 子代理完成 | `origin="subagent_announce"` → 重置计数器 | 子代理完成是新事件，不是续跑 |
| TaskFlow 多步流程 | 每步子代理完成重置计数器 | 计数器只计连续续跑 turn，子代理完成的 turn 不计 |
| 用户发新消息 | `origin≠"todo_continuation"` → 重置 | 用户介入打破循环 |
| 用户在停止后发消息 | 重置 stopped + counter → 新一轮 | 用户决定继续 |
| server 重启 | 所有 in-memory dict 丢失 → 重置 | 和现有停滞/恢复状态一致 |
| abort 错误（用户取消） | `is_abort_error` → 不注入 | 已有逻辑，不变 |
| 计划全部完成 | `reset()` 清理全部 dict | 已有逻辑，不变 |
| 停止指令的 trigger 失败 | `mark_injected` 不调（trigger 抛异常被 catch） | 计数不递增，stopped 不设 → 下一轮重试 |

---

## 6. 不做的事

- **不改 auto-turn service**——计数在 enforcer 侧（决策点），不在 runner 侧（投递点）
- **不持久化计数器**——stagnation_tracker 全部 in-memory，server 重启后重置，和现有停滞/恢复状态一致
- **不用 StateKey/TypedState**——续跑子系统全部用模块级 dict，保持一致
- **不改 `_inject`**——已有的 `mark_injected` 调用点就是计数递增点
- **不改子代理的 goal-loop**——子代理的 `goal_max_turns=5` 已经有界，本方案只管主 agent 的续跑循环
- **不改 IterationBudget**——它是 model-call 级限制（90/turn），本方案是 turn 级限制，两者正交
- **不做 wall-clock 超时**——那是另一个缺口（`run_timeout_seconds=0.0`），~~独立排期~~
  → 见第 8 节方案

---

## 7. Completion Judge 性价比分析（借鉴 hermes-agent？）

### 7.1 hermes-agent 的 completion judge

hermes-agent 的 goal loop 每轮结束后调一个辅助 LLM（temperature=0）判断 DONE/CONTINUE/WAIT：
- **DONE** → 停止循环
- **CONTINUE** → 注入 continuation prompt 再来一轮
- **WAIT** → 不消耗 turn，等待异步工作完成

### 7.2 Sherry 已有的判断机制

Sherry 的主 agent 续跑路径是**唯一没有 LLM 判断的站点**——其他三个站点都有：

| 站点 | 判断机制 | 位置 |
|---|---|---|
| 子代理 goal loop | CompletionJudge（辅助 LLM，DONE/CONTINUE） | `subagent/spawn/completion_judge.py` |
| TaskFlow step | StepJudge（辅助 LLM，PASS/RETRY/BLOCK） | `taskflow/step_judge.py` |
| TaskFlow finish | GOAL_GATE（辅助 LLM，GOAL_MET/GOAL_NOT_MET） | `taskflow/tools/taskflow_finish.py:38-103` |
| **主 agent 续跑** | **无 LLM 判断**——纯状态检查 + 停滞检测 | `todo_continuation/core.py` |

### 7.3 成本

| 项 | 值 | 来源 |
|---|---|---|
| 每次调用的输入 token | ~3–5K（系统 prompt ~220 + todo 状态 + 上一轮输出截断 8000 字符） | `completion_judge.py:49-91` |
| 每次调用的输出 token | ~50–150（单行 JSON） | judge prompt 约束 |
| 模型 | 辅助模型（Qwen 9B Q4 本地 / 便宜远程），temperature=0 | `models/LLMs/auxiliary_llm/core.py` |
| 延迟 | 本地 0.5–2s；远程取决于 API | 9B Q4 模型 |
| 每会话额外调用数 | N（N = 续跑 turn 数），15 轮上限场景下最多 15 次 | — |
| 复用成本 | **低**——`build_auxiliary_llm(temperature=0)` 工厂、fail-open 模式、JSON 解析全部已有 | 三个 judge 站点已验证 |

### 7.4 收益

| 收益 | 说明 |
|---|---|
| 主动判断 vs 被动观察 | 现在续跑 enforcer 只看"计划有没有未完成项"——judge 可以看"输出是否实际完成了目标" |
| 捕获"done-but-unmarked" | 模型完成工作但没标 todo → 现在靠停滞检测 + todowrite nudge 被动提示；judge 可以主动说 DONE |
| WAIT 节省 turn | 当前 enforcer 在"计划未完成但在等异步子代理"时仍注入续跑 → judge 可以说 WAIT 不消耗 turn |

### 7.5 判断：**性价比中等偏低，不推荐优先做**

理由：

1. **现有机制已覆盖主要场景**——计划状态检查（todos+flows 完成→停）+ 停滞检测（3 次不变→恢复）+
   turn 计数器（15 轮→停）三层防护。Judge 的边际收益是"比这三层更早停止"，
   但这三层已经能停。

2. **"done-but-unmarked"已有 6 道防线**——todowrite nudge（10 轮提示）、停滞检测、
   恢复 prompt、续跑 self-critique、SisyphusVerifier、GOAL_GATE。Judge 是第 7 道，
   边际递减明显。

3. **延迟是真实成本**——每次续跑 turn 多 0.5–2s 的辅助 LLM 调用，在 `aafter_agent`
   关键路径上。15 轮续跑 = 7.5–30s 额外延迟。

4. **WAIT 是最有价值的独特功能**——但 Sherry 的续跑路径没有"等待异步工作"的概念
   （子代理完成直接触发 auto-turn，不经续跑 enforcer）。实现 WAIT 需要新基础设施
   （判断哪些 todo 在等异步子代理/TaskFlow step）。

5. **输入形状不同**——子代理 judge 比较单条 response vs task；主 agent 的"工作"
   是整轮的 tool-call 转录，更大更复杂。需要不同的 prompt 设计。

**如果要做，建议**：
- 只在计划快照**变化时**调 judge（复用 stagnation_tracker 的 snapshot diff），
  不在停滞时不必要地调
- 优先实现 WAIT（判断"有未完成项但在等异步工作"→ 不注入），这是现有机制完全缺失的
- DONE 判断的收益不如 WAIT——DONE 能做的事"计划全部完成"检查已经做了

---

## 8. 其余四个缺口修补方案

### 8.1 缺口 #1：默认无 wall-clock 超时

**现状：** `run_timeout_seconds = 0.0`（`agent/tools/subagent/config.py:21`），
注释"0 = no timeout; child agents may run indefinitely"。两条执行路径都被禁用：
- `if timeout_seconds > 0: asyncio.wait_for(...)`（`spawn/core.py:1100`）——跳过
- `if config.run_timeout_seconds <= 0: return`（`followup/core.py:40`）——早返回

**方案：** 把默认值从 `0.0` 改成 `300.0`（5 分钟），两条路径自动生效。

| 文件 | 改动 |
|---|---|
| `agent/tools/subagent/config.py:21` | `run_timeout_seconds: float = 0.0` → `300.0` |

**只需改 1 行。** 现有代码已经完整支持 `> 0` 的值：
- `asyncio.wait_for(child_agent.ainvoke(...), timeout=timeout_seconds)` 在 `core.py:1100`
- `_check_timeouts()` 在 `followup/core.py:34` 会扫描所有 live runs
- `TimeoutError` 被捕获并映射到 `RunOutcomeStatus.TIMEOUT`（`core.py:1137`）

**风险评估：**
- 调用方可通过 `sessions_spawn(run_timeout_seconds=...)` 覆盖——已有参数
- 300s 给 goal-loop 5 turns × 60 iterations 足够（最坏 ~300 次模型调用，每次几秒，
  总计 ~15 分钟——300s 有点紧，可调到 600s）
- 0.0 的注释说"background sweeps provide the safety net"——sweeper 每 60s 扫描
  孤儿，但这是**死后清理**不是**运行时超时**

**推荐值：** `600.0`（10 分钟）——给子代理足够的 goal-loop 时间，但不是无限。

### 8.2 缺口 #2：无总 spawn 次数上限

**现状：** `count_active_runs_for_session`（`registry/queries.py:39-46`）只计
RUNNING+PENDING，不计已完成。in-memory store 在 `_MAX_RETAINED_RUNS` 超限时
驱逐旧 terminal 记录（`memory.py:39-47`），所以无法从现有数据可靠计数 total。
一个 session 可以无限"完成一个 spawn → 开新的"。

**方案：** 在 `stagnation_tracker` 模式下新增一个 per-session spawn 计数器
（in-memory dict），在 `register_run` 时递增，在 `reset` 时清理。

| 文件 | 改动 |
|---|---|
| `agent/tools/subagent/config.py` | 加 `max_total_spawns_per_session: int = 50` |
| `agent/tools/subagent/spawn/depth.py` | 加 `validate_total_spawns(count) -> tuple[bool, str]`，和 `validate_concurrent_children` 并列 |
| `agent/tools/subagent/spawn/core.py:234-237` | `_validate_spawn_request` 中，在 `validate_concurrent_children` 之后加 `validate_total_spawns` 检查 |
| `agent/tools/subagent/registry/run_manager.py:33` | `register_run` 中递增计数器（或用 stagnation_tracker 模式的模块级 dict） |

**计数器设计：**
```python
# spawn/depth.py 或新文件
_spawn_counts: dict[str, int] = {}  # requester_session_key → total spawns

def increment_spawn_count(session_key: str) -> int:
    _spawn_counts[session_key] = _spawn_counts.get(session_key, 0) + 1
    return _spawn_counts[session_key]

def validate_total_spawns(count: int) -> tuple[bool, str]:
    config = get_config()
    if count >= config.max_total_spawns_per_session:
        return False, f"Total spawns {count} already at max {config.max_total_spawns_per_session}"
    return True, ""

def reset_spawn_count(session_key: str) -> None:
    _spawn_counts.pop(session_key, None)
```

**注意：** 计数器在 `register_run`（`run_manager.py:33`）时递增，不在 spawn 验证时。
验证时读取当前值判断是否超限。Session 清理时调 `reset_spawn_count`。

### 8.3 缺口 #5：`taskflow_create` 无步骤上限

**现状：** `PLAN_MAX_STEPS = 12` 只在 `taskflow_plan.py` 中执行（clamp，不 refuse）。
`taskflow_create` 通过 `initial_state["steps"]` 直接传入，`default_state`
（`_shared.py:57`）原样复制，无任何计数检查。

**方案：** 在 `TASKFLOW_INFRA` 加 config，在 `taskflow_create` 加校验。

| 文件 | 改动 |
|---|---|
| `config/features/agent_side/taskflow_infra.py` | TypedDict 加 `max_steps_per_flow: int`；dict 加 `"max_steps_per_flow": 20` |
| `agent/tools/taskflow/tools/taskflow_create.py:39-46` | 在 `default_state(...)` 返回 `state` 后、`create_flow(...)` 前，检查 `len(state.get("steps") or [])` 超限则返回 `Error:` 文本 |

```python
# taskflow_create.py — 在 default_state 之后
state = default_state(description, initial_state, creator_session_key=creator_key)
step_count = len(state.get("steps") or [])
max_steps = TASKFLOW_INFRA["max_steps_per_flow"]
if step_count > max_steps:
    return f"Error: flow has {step_count} steps, max is {max_steps}. Use taskflow_plan to decompose into fewer steps."
```

**为什么不把 `PLAN_MAX_STEPS` 也改成 config：** `PLAN_MAX_STEPS` 是 planner 的
输出上限（clamp），和 `max_steps_per_flow`（flow 的硬上限，refuse）是不同语义。
保持分离更清晰——planner 告诉模型"最多 12 步"但 clamp 不报错；
`taskflow_create` 的手动传入 20 步是硬上限，超限拒绝。

### 8.4 缺口 #6：无"每 session 最大 flow 数"上限

**现状：** `create_flow`（`store_sqlite.py:253-309`）只校验 `flow_id`/`session_id`
非空，无计数检查。`get_active_flows_sync(session_id)` 可以计数 active flows，
但没有上限校验。子代理可以无限创建新 flow（虽然受深度 2 + 迭代 60 间接限制）。

**方案：** 在 `TASKFLOW_INFRA` 加 config，在 `taskflow_create` 和 `taskflow_plan`
加计数检查。

| 文件 | 改动 |
|---|---|
| `config/features/agent_side/taskflow_infra.py` | TypedDict 加 `max_flows_per_session: int`；dict 加 `"max_flows_per_session": 20` |
| `agent/tools/taskflow/tools/taskflow_create.py:46` | 在 `create_flow` 前检查 `len(get_active_flows_sync(session_id))` 超限则返回 `Error:` 文本 |
| `agent/tools/taskflow/tools/taskflow_plan.py:165` | 同样检查 |

```python
# taskflow_create.py / taskflow_plan.py — 在 create_flow 前
active_flows = get_active_flows_sync(session_id)
max_flows = TASKFLOW_INFRA["max_flows_per_session"]
if len(active_flows) >= max_flows:
    return f"Error: session already has {len(active_flows)} active flows (max {max_flows}). Finish or cancel existing flows before creating new ones."
```

**设计要点：**
- 只计 active flows（running/waiting），不计 completed/cancelled——已完成的 flow
  不占用资源
- 检查放在 tool 层（`taskflow_create` / `taskflow_plan`），不放 store 层
  （`create_flow`）——和现有的"business rule 在 tool 层"约定一致
  （`_shared.py:4-7` 模块文档）
- `get_active_flows_sync` 是 fail-open 的（出错返回 `[]`）——这意味着
  计数检查也是 fail-open 的（出错时不拦截）。可接受——比卡死强

---

## 9. 6 个缺口总览 + 性价比分析

### 9.1 总览表

| # | 缺口 | 方案 | 改动量 | 优先级 |
|---|---|---|---|---|
| 1 | 默认无 wall-clock 超时 | `run_timeout_seconds` 默认值 `0.0 → 600.0` | 1 行 | **高** |
| 2 | 无总 spawn 次数上限 | 新增 `_spawn_counts` dict + `max_total_spawns_per_session=50` + `validate_total_spawns` | 4 文件 | 中 |
| 3 | 无 auto-turn 总次数上限 | `_continuation_turns` 计数器 + `max_continuation_turns=15` + stopped 标志 | 3 文件（第 3 节方案） | **高** |
| 4 | 续跑 enforcer 有进展时无界 | 同 #3 | 同 #3 | **高** |
| 5 | `taskflow_create` 无步骤上限 | `max_steps_per_flow=20` config + `taskflow_create` 校验 | 2 文件 | 低 |
| 6 | 无"每 session 最大 flow 数"上限 | `max_flows_per_session=20` config + `taskflow_create`/`taskflow_plan` 校验 | 3 文件 | 低 |

### 9.2 性价比分析

**缺口 #1（wall-clock 超时）——性价比最高，改 1 行堵住两条路径。**

现有代码已完整支持 `run_timeout_seconds > 0`：`asyncio.wait_for` 在
`spawn/core.py:1100` 包裹 `child_agent.ainvoke`，`_check_timeouts` 在
`followup/core.py:34` 扫描 live runs，`TimeoutError` 被捕获映射到
`RunOutcomeStatus.TIMEOUT`（`core.py:1137`）。唯一的问题是默认值 `0.0`
让两条路径都被 `if > 0` / `if <= 0` 跳过。改 1 行默认值即可全部激活。
零风险（调用方可覆盖），零新增代码，直接堵住"子代理可能无限运行"的缺口。

**缺口 #3/#4（续跑 turn 计数器）——性价比高，方案已完整设计。**

这是 6 个缺口中最危险的一个（模型"有进展但不完成"时续跑 enforcer 无限注入）。
方案利用已有的 `mark_injected` chokepoint（唯一确认投递点）做计数，
利用 last HumanMessage 的 `origin` 区分续跑 turn 和真实用户 turn，
利用 `_stopped` 标志防止停止指令自身触发循环。3 个文件改动，
复用 stagnation_tracker 的模块级 dict 模式，不引入新基础设施。
子代理完成通知（`origin="subagent_announce"`）自动重置计数器——
TaskFlow 多步流程不受影响。与缺口 #1 正交：#1 限子代理时长，
#3/#4 限主 agent 续跑轮数。

**缺口 #2（总 spawn 次数）——性价比中等，4 文件但模式成熟。**

现有 `validate_concurrent_children`（`depth.py:35-43`）只限并发
（5/父），不限总数。一个 session 可以"完成一个 → 开新的"无限循环。
方案在 `_validate_spawn_request` 中加 `validate_total_spawns` 检查，
在 `register_run` 中递增计数器，完全复刻 `validate_concurrent_children`
的模式。风险：in-memory store 在 `_MAX_RETAINED_RUNS` 超限时驱逐旧
terminal 记录（`memory.py:39-47`），所以不能从现有数据计数 total——
需要独立的计数器 dict。但这是 stagnation_tracker 已验证过的模式
（模块级 dict + reset 清理）。改动稍多（4 文件）但每处改动都小。

**缺口 #5（`taskflow_create` 步骤上限）——性价比偏低，场景罕见。**

只有 `taskflow_plan` 限 12 步（clamp），`taskflow_create` 通过
`initial_state` 直接传入不限。但"模型手动传入 20+ 步"的场景在实际
使用中罕见——模型通常用 `taskflow_plan`（有 LLM planner + 12 步上限）
而非手动传 `initial_state`。改动只需 2 文件（config + `taskflow_create`
加 3 行校验），但堵的是低概率场景。可以做，但不急。

**缺口 #6（最大 flow 数）——性价比偏低，已有间接限制。**

子代理创建新 flow 受深度上限 2（`max_spawn_depth`）+ 迭代预算 60
（`worker_max_iterations`）间接限制。主 agent 创建 flow 受 IterationBudget
90 限制。理论上一个 session 可以创建无限 flow（每个 flow 几步就完成/取消），
但这需要模型持续主动创建——不是"卡住"的场景而是"做太多"的场景。
改动 3 文件（config + `taskflow_create` + `taskflow_plan` 各加 2 行），
但堵的是低概率场景。`get_active_flows_sync` 是 fail-open 的（出错返回 `[]`），
所以计数检查也是 fail-open 的——可接受但不完美。

### 9.3 推荐实施顺序

```
#1 wall-clock 超时（1 行）          ← 改 1 行，立刻堵住子代理无限运行
  ↓
#3/#4 续跑 turn 计数器（3 文件）    ← 堵住主 agent 续跑无限循环（已有完整设计）
  ↓
#2 总 spawn 次数（4 文件）           ← 堵住 session 无限 spawn
  ↓
#5 步骤上限（2 文件）               ← 低概率，顺手做
#6 flow 数上限（3 文件）            ← 低概率，顺手做
```

### 9.4 Completion Judge 性价比

**不推荐优先做。** 理由见第 7 节详细分析。简述：
- 现有三层防护（计划状态检查 + 停滞 + turn 计数器）已覆盖主要场景
- 每次 turn 多 0.5–2s 辅助 LLM 延迟是真实成本
- "done-but-unmarked"已有 6 道防线，judge 是第 7 道，边际递减
- 如果要做，优先实现 **WAIT 判断**（"有未完成项但在等异步工作"→不注入），
  这是现有机制完全缺失的能力，比 DONE 判断更有价值
