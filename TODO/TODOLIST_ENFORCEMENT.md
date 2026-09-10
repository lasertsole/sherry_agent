# TodoList 强制执行层（Enforcement Layers）— omo 全量适配版

> 配套文件: TODOLIST_PLAN.md（核心实现 Layer 1-9）
> 参考项目: oh-my-openagent-dev (D:\selfProj\oh-my-openagent-dev)
> 日期: 2026-09-07

## Adaptation note (2026-09-10)

**DAG 现在由既有 TaskFlow 系统提供（taskflow-dag-phase1，commits 9ce33ef..2824034），本设计的强制执行层复用而不是再造第二个 DAG。**

- TaskFlow step 携带 `depends_on` 与 `status ∈ {blocked, ready, dispatched, done}`；`taskflow_run_task` 依赖未满足时登记 `blocked` 不派发；`taskflow_resume` 标 `done` 并解锁依赖项（不自动派发）；`taskflow_dispatch` 批量派发 `ready` 步骤；`taskflow_wait_all` 按 flow 范围等待。所有字段在 `state_json`，无 DB migration。
- 因此 E4（转换屏障）与 E6（委派路由）改以 **TaskFlow step 状态 + subagent registry 存活检测**表达（见下），todolist 层只调用 TaskFlow 工具，不实现调度器。
- **E1-E7 七层强制保持不变**，代码目标与职责都保留；本文档的核心约束（orchestrator doctrine、工具描述规则、续作强制器、转换屏障、Sisyphus 契约、委派路由、意图识别、压缩免疫注入、TodoDock 前端计划）在适配后仍然全部存在。

## 设计哲学

全面采用 oh-my-openagent-dev 的强制执行架构：ulw-execute orchestrator doctrine + todo-continuation-enforcer + Sisyphus 完成契约 + 委派路由。

**核心原则**（`ulw-execute/SKILL.md:6-8`）：

> YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.
> YOU DO NOT WRITE CODE. YOU DO NOT EDIT PRODUCT FILES.
> EVERY unit of implementation MUST be delegated to a spawned subagent.

不依赖模型自觉。通过 7 层强制形成闭环：模型不自觉做计划 → E7 意图识别器注入引导；模型偷懒停下来 → E3 续作强制器拉回；模型虚假完成 → E5 Sisyphus 验证阻断；模型自己写代码 → E1 orchestrator doctrine 威慑。

```
事前 (消息到达)       事中                        事后
┌──────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ E7 意图   │  │ E6 委派路由 ★     │  │ E3 续作强制器 ★★     │
│ 识别器 ★★│  │ #a fan-out         │  │ idle + 未完成 todo   │
│ before_   │  │ #b category 路由  │  │ + 退避 + 停滞 + abort │
│ model    │  │ #c 委派指令         │  │ + 恢复模式            │
│ 注入引导  │  │ #d 转换屏障        │  └──────────────────────┘
│ prompt   │  └────────────────────┘  ┌──────────────────────┐
└──────────┘  ┌────────────────────┐  │ E5 Sisyphus 验证 ★★ │
┌──────────┐  │ E1 系统 prompt     │  │ DoneClaim →          │
│ E2 工具   │  │ orchestrator       │  │ AdversarialVerify →  │
│ 描述强制  │  │ doctrine           │  │ FullyDone             │
│ MANDATORY │  │ +hook告知          │  └──────────────────────┘
│ 格式规则  │  └────────────────────┘  ┌──────────────────────┐
└──────────┘                            │ E4 转换屏障 (code) ★ │
                                          │ subagent_id 运行检查 │
                                          │ → 硬阻断              │
                                          └──────────────────────┘
```

---

## 架构总览

```
Layer 9  │ UI 组件层         │ ← 见 TODOLIST_PLAN.md
Layer 8  │ 前端状态层        │
Layer 7  │ 实时通信层        │
Layer 6  │ 压缩保护层 ★     │ ← 见 TODOLIST_PLAN.md
Layer 5  │ DAG 调度层 ★     │ ← 委派给 TaskFlow（depends_on + blocked/ready/dispatched/done）
Layer 4  │ 编排执行层 ★     │ ← ulw-execute
Layer 3  │ 工具层           │
Layer 2  │ 服务层           │
Layer 1  │ 数据存储层        │
---------│-------------------│
E1       │ 系统提示词强制    │ AGENTS.md orchestrator doctrine + hook 存在性告知
E2       │ 工具描述强制      │ todowrite docstring MANDATORY 格式规则 + ulw-execute 委派规则
E3       │ 续作强制器 ★★    │ after_agent 中间件: idle+未完成 todo → 自动续作 (退避/停滞/abort/恢复)
E4       │ 转换屏障 ★       │ prompt: TaskFlow step 未 done/subagent 未返回不得标 done + code: TaskFlow step 状态 + is_live_unended_run 硬阻断
E5       │ Sisyphus 验证 ★★ │ DoneClaim → AdversarialVerify → FullyDone (5 gates)
E6       │ 委派路由 ★       │ #a fan-out + #b category 路由 + #c 委派指令 + #d 转换屏障（对接 taskflow_* 工具）
E7       │ 意图识别器 ★★    │ before_model: E7a arming(无计划→注入引导) + E7b plan-active(有计划→追加reminder)
```

---

## E1: 系统提示词强制 — orchestrator doctrine + 事前预防

- **修改文件**: `workspace/template/{en,zh}/AGENTS.md`
- **代码量**: 0 行代码（纯文本编辑）
- **阶段**: Phase 1

sherry_agent 的 `AGENTS.md` 被加载进系统提示词（`prompt_builder.py` 的 `_read_static_files()`）。直接添加 ulw-execute orchestrator doctrine：

```markdown
## Task Management & Orchestration (CRITICAL)

### Orchestrator Doctrine (MANDATORY)

YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.

- You DO NOT write code. You DO NOT edit product files.
- EVERY unit of implementation MUST be delegated to a spawned subagent.
- You create plans, decompose tasks, delegate work, and verify completion.

### When to Create Todos (MANDATORY)

- Multi-step task (2+ steps) → ALWAYS create todos first
- Uncertain scope → ALWAYS (todos clarify thinking)
- User request with multiple items → ALWAYS

### Workflow (NON-NEGOTIABLE)

1. IMMEDIATELY on receiving request: todowrite to plan atomic steps.
2. For each checkbox: decompose into atomic sub-tasks for ONE worker.
3. DELEGATE every sub-task via task tool — route by category.
4. Before starting each step: Mark in_progress (only ONE at a time)
5. After subagent returns: verify via acceptance criteria, then mark completed.

### Delegation (when working on todos)

- delegation="self": trivial tasks (<10 lines, single file) — do yourself
- delegation="subagent": complex tasks (multi-file, >100 lines, complex logic)
  — delegate to subagent via task tool, then set subagent_id field
- Code edits, test writes, and fixes are good delegation candidates
- Declare dependencies with taskflow_run_task(depends_on=[...]); dispatch ready
  steps together with taskflow_dispatch, then wait for them

### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Wait for subagent to return before updating the todo status
- If subagent failed, mark todo as cancelled and re-plan

### Completion Contract (Sisyphus)

- DoneClaim: When you believe a task is done, CLAIM it — but do NOT mark completed yet.
- AdversarialVerify: Run the acceptance criteria. Probe for stale state, dirty worktree, leftover resources.
- FullyDone: Only after verification passes, mark the checkbox completed.
- If verification fails, the task is NOT done — re-dispatch or fix.

### Anti-Patterns (BLOCKING)

- Writing code yourself when delegation is available — ORCHESTRATOR NEVER IMPLEMENTS
- Skipping todos on multi-step tasks — user has no visibility
- Batch-completing multiple todos — defeats real-time tracking
- Marking completed before subagent returns — TRANSITION BARRIER VIOLATION
- Marking completed without verification — SISYPHUS VIOLATION

**FAILURE TO FOLLOW ORCHESTRATOR DOCTRINE = INCOMPLETE WORK.**
```

### hook 存在性告知

在 `prompt_builder.py` 的 `_build_todo_block()` 末尾追加（心理威慑）：

```python
lines.append(
    "\nYour todo list is tracked by the continuation system. "
    "Incomplete todos will trigger automatic continuation."
)
lines.append(
    "Completion is verified by the Sisyphus contract — unverified claims will be rejected."
)
```

---

## E2: 工具描述强制 — 格式 + 委派规则

- **修改文件**: `agent/tools/todolist/tools/todowrite.py`（docstring）+ `agent/tools/todolist/tools/__init__.py`（builder）
- **代码量**: ~15 行
- **阶段**: Phase 1

在 `build_todolist_tools()` 中覆盖 todowrite 描述，注入 MANDATORY 格式规则 + ulw-execute 委派规则：

```python
# agent/tools/todolist/tools/__init__.py

_TODOWRITE_FORMAT_RULES = """

## Todo Format (MANDATORY)

Each todo title MUST encode four elements: WHERE, WHY, HOW, and EXPECTED RESULT.
Format: "[WHERE] [HOW] to [WHY] - expect [RESULT]"

## Granularity Rules
Each todo MUST be a single atomic action completable in 1-3 tool calls.
**Size test**: Can you complete this todo by editing one file or running one command?
If not, it's too big — split it.

## Orchestrator Rules (MANDATORY)
- One in_progress at a time. Complete it before starting the next.
- Mark completed immediately after finishing each item.
- delegation="subagent" todos: spawn subagent FIRST, then set subagent_id.
- Do NOT mark completed while subagent is still running (Transition Barrier).
- Do NOT mark completed without running acceptance criteria (Sisyphus).

## Delegation Fields (optional but recommended)
- category: quick|deep|ultrabrain|visual|git|writing — routing verdict for subagent dispatch
- delegation: self|subagent — whether this todo should be delegated
- subagent_id: set after dispatching a subagent (use the child_session_key returned by task tool)

## DAG / TaskFlow Fields (optional)
- plan_ref: .omo/plans/*.md path
- flow_id: linked TaskFlow flow id (DAG lives in TaskFlow, not in todos.db)
- step_id: linked TaskFlow step id (e.g. step-2); read its status via taskflow_summary
- To declare dependencies, call taskflow_run_task(..., depends_on=[...]); do NOT
  re-implement wave/frontier scheduling in the todolist layer
"""


def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES
    return list(_TODOLIST_TOOLS)
```

---

## E3: 续作强制器 — 事后闭环核心 ★★

这是最关键的强制层——**不依赖模型自觉**。turn 结束后如果有未完成 todo，系统自动注入续作消息把 LLM 拉回来。

### 与 omo todo-continuation-enforcer 的对应关系

| omo 机制                       | sherry_agent 适配                                            | 说明             |
| ------------------------------ | ------------------------------------------------------------ | ---------------- |
| `session.idle` event listener  | `session_state.py:detect_state()` 三态检测                   | sherry 已有      |
| 2.5s countdown timer           | `auto_turn.py` 的 fire-and-forget + `_watch_user_takeover()` | sherry 已有等价  |
| `CONTINUATION_COOLDOWN_MS`     | `_BASE_COOLDOWN_S = 2.0`                                     | 退避基础冷却     |
| `MAX_CONSECUTIVE_FAILURES = 3` | `_MAX_STAGNATION = 3`                                        | 停滞阈值         |
| `MAX_STAGNATION_COUNT`         | 同上                                                         | 连续无变化上限   |
| `FAILURE_RESET_WINDOW_MS`      | `_FAILURE_RESET_WINDOW_S = 300`                              | 失败计数重置窗口 |
| user message cancel            | `_watch_user_takeover()` 0.5s 轮询                           | sherry 已有      |
| assistant activity cancel      | `detect_state() == answering`                                | sherry 已有      |
| tool execution cancel          | subagent 仍在运行检测                                        | E4 转换屏障      |
| abort error cancel             | `_is_abort_error()` 检测                                     | 新增             |
| exponential backoff            | `cooldown = min(base * 2^min(failures, 5), max)`             | 已有             |
| recovery mode                  | `_RECOVERY_PROMPT`                                           | 新增             |
| `_INFLIGHT` idempotency        | `auto_turn.py:_INFLIGHT` dict                                | sherry 已有      |

### sherry 已有的基础设施

| 能力           | 状态      | 位置                                                                         |
| -------------- | --------- | ---------------------------------------------------------------------------- |
| 空闲检测       | ✅        | `session_state.py:detect_state()` 三态（ws_task/answering/idle）             |
| idle 消息注入  | ✅        | `auto_turn.py:maybe_trigger_auto_turn()` fire-and-forget                     |
| 用户接管保护   | ✅        | `auto_turn.py:_watch_user_takeover()` 0.5s 轮询                              |
| 幂等性         | ✅        | `auto_turn.py:_INFLIGHT` dict 防重复                                         |
| busy 消息排队  | ✅        | `steering_queue.py:enqueue_steering()` + `SubagentCompletionDrainMiddleware` |
| post-turn 钩子 | ❌ 需新建 | `after_agent` 中间件                                                         |
| 停滞检测       | ❌ 需新建 | stagnation_tracker                                                           |
| 退避冷却       | ❌ 需新建 | stagnation_tracker                                                           |
| abort 检测     | ❌ 需新建 | stagnation_tracker                                                           |
| 恢复模式       | ❌ 需新建 | stagnation_tracker                                                           |

### 新建文件 1: `agent/tools/todolist/stagnation_tracker.py`

```python
"""停滞检测 + 退避冷却 + abort 检测 + 恢复模式。

参考 omo todo-continuation-enforcer constants:
- CONTINUATION_COOLDOWN_MS → _BASE_COOLDOWN_S
- MAX_CONSECUTIVE_FAILURES → _MAX_STAGNATION
- MAX_STAGNATION_COUNT → 同上
- FAILURE_RESET_WINDOW_MS → _FAILURE_RESET_WINDOW_S
"""

import time

_MAX_STAGNATION = 3  # 连续 3 次无变化 → 停止
_BASE_COOLDOWN_S = 2.0  # 基础冷却秒数
_MAX_COOLDOWN_S = 60.0  # 最大冷却秒数
_FAILURE_RESET_WINDOW_S = 300  # 5 分钟内无失败 → 重置计数
_MAX_RECOVERY_ATTEMPTS = 2  # 恢复模式最大尝试次数

# per-session 状态
_stagnation_count: dict[str, int] = {}
_last_snapshot: dict[str, str] = {}  # session_id → "content=status|content=status" 快照
_last_inject_time: dict[str, float] = {}  # session_id → 上次注入的 timestamp
_last_failure_time: dict[str, float] = {}  # session_id → 上次失败（停滞）的 timestamp
_recovery_attempts: dict[str, int] = {}  # session_id → 恢复模式尝试次数


def check_stagnation(session_id: str, todos: list[dict]) -> bool:
    """返回 True 表示已停滞（连续 N 次无变化），应停止续作。"""
    snapshot = "|".join(f"{t['content']}={t['status']}" for t in todos)
    if _last_snapshot.get(session_id) == snapshot:
        _stagnation_count[session_id] = _stagnation_count.get(session_id, 0) + 1
        _last_failure_time[session_id] = time.monotonic()
    else:
        # 检查失败重置窗口
        last_fail = _last_failure_time.get(session_id)
        if last_fail and (time.monotonic() - last_fail) > _FAILURE_RESET_WINDOW_S:
            _stagnation_count[session_id] = 0
        else:
            _stagnation_count[session_id] = _stagnation_count.get(session_id, 0)
    _last_snapshot[session_id] = snapshot
    return _stagnation_count.get(session_id, 0) >= _MAX_STAGNATION


def is_in_cooldown(session_id: str) -> bool:
    """是否在退避冷却期内。"""
    now = time.monotonic()
    last = _last_inject_time.get(session_id)
    if last is None:
        return False
    failures = _stagnation_count.get(session_id, 0)
    cooldown = min(_BASE_COOLDOWN_S * (2 ** min(failures, 5)), _MAX_COOLDOWN_S)
    return (now - last) < cooldown


def mark_injected(session_id: str) -> None:
    """记录本次注入时间。"""
    _last_inject_time[session_id] = time.monotonic()


def should_enter_recovery(session_id: str) -> bool:
    """停滞后是否应进入恢复模式。"""
    if _stagnation_count.get(session_id, 0) >= _MAX_STAGNATION:
        attempts = _recovery_attempts.get(session_id, 0)
        return attempts < _MAX_RECOVERY_ATTEMPTS
    return False


def enter_recovery(session_id: str) -> None:
    """进入恢复模式，重置停滞计数但增加恢复尝试数。"""
    _stagnation_count[session_id] = 0
    _recovery_attempts[session_id] = _recovery_attempts.get(session_id, 0) + 1


def reset(session_id: str) -> None:
    """用户接管或 todo 全部完成时重置全部状态。"""
    _stagnation_count.pop(session_id, None)
    _last_snapshot.pop(session_id, None)
    _last_inject_time.pop(session_id, None)
    _last_failure_time.pop(session_id, None)
    _recovery_attempts.pop(session_id, None)


def is_abort_error(error: Exception) -> bool:
    """检测是否为 abort 类错误（用户取消、超时等）。

    参考 omo todo-continuation-enforcer 的 abort 检测：
    当 abort 错误发生时，不触发续作。
    """
    error_str = str(error).lower()
    abort_markers = [
        "abort",
        "cancelled",
        "interrupted by user",
        "operation cancelled",
        "timeout",
    ]
    return any(marker in error_str for marker in abort_markers)
```

### 新建文件 2: `agent/middlewares/todo_continuation.py`

```python
"""Turn 结束后检查未完成 todo，自动注入续作 prompt。

复用 auto_turn.py 的 maybe_trigger_auto_turn() 基础设施，
实现"模型偷懒停下来 → 2 秒后自动拉回来继续工作"的闭环。

参考 omo todo-continuation-enforcer:
- session.idle → detect_state() == "idle"
- 2.5s countdown → auto_turn fire-and-forget
- user message cancel → _watch_user_takeover()
- assistant activity cancel → detect_state() != "idle"
- tool execution cancel → subagent 仍在运行 (E4)
- abort error cancel → is_abort_error()
- exponential backoff → is_in_cooldown()
- stagnation detection → check_stagnation()
- recovery mode → should_enter_recovery() / enter_recovery()
"""

from langgraph.types import AgentState
from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from agent.tools.todolist.stagnation_tracker import (
    check_stagnation,
    is_in_cooldown,
    mark_injected,
    reset,
    should_enter_recovery,
    enter_recovery,
    is_abort_error,
)

_CONTINUATION_PROMPT = """[SYSTEM DIRECTIVE: TODO CONTINUATION]

Incomplete tasks remain in your todo list. Continue working on the next pending task.

- Proceed without asking for permission
- Mark each task complete when finished
- Do not stop until all tasks are done
- If you believe all work is complete, the system is questioning your completion claim.
  Critically re-examine each todo item, verify the work was actually done, and update accordingly.

{todo_status}"""

_RECOVERY_PROMPT = """[SYSTEM DIRECTIVE: RECOVERY MODE]

Stagnation detected — the todo list has not changed across multiple continuation attempts.

Recovery actions:
1. Run todoread to see the exact current state
2. For each incomplete task, ask: "Is this actually done but unmarked, or genuinely incomplete?"
3. If done: mark completed via todowrite (verify first — Sisyphus contract)
4. If incomplete: re-plan the task — break it down differently or delegate to a subagent
5. If blocked: mark cancelled and note the blocker

Do NOT repeat the same actions that led to stagnation.

{todo_status}"""


def _build_status_block(todos: list[dict]) -> str:
    done = sum(1 for t in todos if t["status"] in ("completed", "cancelled"))
    total = len(todos)
    remaining = [t for t in todos if t["status"] in ("pending", "in_progress")]
    lines = [f"[Status: {done}/{total} completed, {len(remaining)} remaining]"]
    lines.append("Remaining tasks:")
    for t in remaining:
        icon = {"pending": "○", "in_progress": "◐"}.get(t["status"], "○")
        step_ref = ""
        if t.get("flow_id") and t.get("step_id"):
            step_ref = f" (flow {t['flow_id']} / {t['step_id']})"
        lines.append(f"- [{icon}] {t['content']} ({t['priority']}){step_ref}")
    return "\n".join(lines)


class TodoContinuationEnforcer:
    """after_agent 中间件：turn 结束后检查未完成 todo。

    放在 middleware 列表最内层（Summarization 之后），
    确保在压缩后、系统提示词重建后运行。
    """

    async def aafter_agent(self, handler, request, config, *, key, state):
        result = await handler(request, config=config, key=key, state=state)

        session_id = state.get("session_id", "")
        if not session_id:
            return result

        # abort 错误检测 — 不触发续作
        if isinstance(result, Exception) and is_abort_error(result):
            return result

        # 从 DB 同步读取当前 todos
        todos = get_todos_sync(session_id)
        if not todos:
            reset(session_id)
            return result

        # 过滤未完成项
        incomplete = [t for t in todos if t["status"] in ("pending", "in_progress")]
        if not incomplete:
            reset(session_id)
            return result

        # 停滞检测：连续 N 次无变化
        if check_stagnation(session_id, todos):
            # 恢复模式
            if should_enter_recovery(session_id):
                enter_recovery(session_id)
                prompt = _RECOVERY_PROMPT.format(todo_status=_build_status_block(todos))
                mark_injected(session_id)
                try:
                    from server.service.auto_turn import maybe_trigger_auto_turn

                    session_key = f"agent:main:session:{session_id}"
                    await maybe_trigger_auto_turn(session_key, prompt)
                except Exception:
                    pass
            # 否则不续作（已耗尽恢复尝试）
            return result

        # 退避冷却
        if is_in_cooldown(session_id):
            return result

        # 构建续作 prompt
        prompt = _CONTINUATION_PROMPT.format(todo_status=_build_status_block(todos))

        # 复用 auto_turn 基础设施注入消息
        mark_injected(session_id)
        try:
            from server.service.auto_turn import maybe_trigger_auto_turn

            session_key = f"agent:main:session:{session_id}"
            await maybe_trigger_auto_turn(session_key, prompt)
        except Exception:
            pass  # 续作失败不影响主流程

        return result
```

### 修改文件: `agent/core.py`

在 middleware 列表中注册（放在 Summarization 之后，因为它需要在压缩后运行）：

```python
_agent = create_agent(
    model=main_llm.bind(temperature=temperature),
    state_schema=StateSchema,
    checkpointer=checkpointer,
    tools=get_agent_tools(),
    middleware=[
        ContextEngineHook(),
        MultimodalProcessor(),
        IterationBudget(90),
        ToolGuardrails(),
        ToolCallNormalize(),
        SubagentCompletionDrainMiddleware(),
        OutputRepetitionGuard(),
        HeartbeatStaleness(),
        HumanInTheLoop(HITLConfig()),
        Summarization(...),
        TodoContinuationEnforcer(),  # ← E3: 最内层，turn 真正结束后检查
    ],
)
```

### E3 工作流程

```
turn 结束（model 无 tool_call，agent loop 退出）
  → Summarization.aafter_agent（压缩后重建系统提示词，注入最新 todos + boulder 状态）
  → TodoContinuationEnforcer.aafter_agent
      → is_abort_error? → skip (abort 检测)
      → get_todos_sync(session_id) 从 DB 读取
      → 过滤未完成项
      → check_stagnation: 快照对比，连续 N 次无变化?
          → should_enter_recovery? → enter_recovery → RECOVERY_PROMPT
          → 否则停止续作
      → is_in_cooldown: 退避冷却期内? → skip
      → 构建 continuation prompt（含完整 todo 状态 + 波次信息）
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _run_auto_turn() → _drive_turn() → async_generate()
          → _watch_user_takeover() 0.5s 轮询用户接管
      → 用户发消息 → detect_state() 变 busy → 取消续作 → reset()
```

### E3 文件清单

| #   | 操作 | 文件路径                                     | 代码量              |
| --- | ---- | -------------------------------------------- | ------------------- |
| 1   | 新建 | `agent/tools/todolist/stagnation_tracker.py` | ~90 行              |
| 2   | 新建 | `agent/middlewares/todo_continuation.py`     | ~110 行             |
| 3   | 修改 | `agent/core.py`                              | ~2 行（注册中间件） |

---

## E4: 转换屏障 — 双保险

### Phase 1: Prompt 级

已在 E1 的 AGENTS.md 中包含：

```markdown
### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Do NOT mark a TaskFlow-linked todo completed while its TaskFlow step is not `done`
  (step status `blocked`/`ready`/`dispatched` all mean "not finished")
- Wait for subagent to return AND to inject its result via taskflow_resume before
  updating the todo status; a step becomes `done` only when resume injects a result
- If subagent failed, mark todo as cancelled and re-plan
```

### Phase 2: 代码级硬阻断

- **修改文件**: `agent/tools/todolist/service.py`
- **代码量**: ~25 行
- **阶段**: Phase 2

屏障有两道来源，**都不在 todolist 内建调度器**：

1. **TaskFlow step 状态**（DAG 权威在 TaskFlow）：todo 关联了 `flow_id`/`step_id` 时，
   只有该 step 为 `done` 才允许把 todo 标 `completed`。step 为 `blocked`/`ready`/
   `dispatched` 都算未完成，需先 `taskflow_wait_all` → `taskflow_resume` 注入结果。
2. **subagent registry 存活检测**：todo 关联了 `subagent_id` 时，用
   `get_run_by_child_session_key` + `is_live_unended_run` 判断子会话是否仍在运行。

```python
# agent/tools/todolist/service.py

from agent.tools.subagent.registry import get_run_by_child_session_key, is_live_unended_run
from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary


async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
    validated = _validate_todos(todos)

    # E4: 转换屏障 — 双来源
    for todo in validated:
        if todo["status"] != "completed":
            continue

        # 来源 1: TaskFlow step 状态（DAG 由 TaskFlow 拥有）
        flow_id, step_id = todo.get("flow_id"), todo.get("step_id")
        if flow_id and step_id:
            step_status = _read_taskflow_step_status(flow_id, step_id)
            if step_status is not None and step_status != "done":
                raise TodoStoreError(
                    f"Cannot mark todo completed: TaskFlow step {step_id} "
                    f"(flow {flow_id}) is '{step_status}'. Call taskflow_wait_all then "
                    "taskflow_resume to inject the result before marking completed."
                )

        # 来源 2: subagent registry 存活检测
        if todo.get("subagent_id") and _is_subagent_running(todo["subagent_id"]):
            raise TodoStoreError(
                f"Cannot mark todo completed: subagent {todo['subagent_id']} "
                "is still running. Wait for it to finish first."
            )

    await store.replace_all(session_id, validated)
    latest = await store.get_todos(session_id)
    await _push_todo_update(session_id, latest)
    return latest


def _is_subagent_running(child_session_key: str) -> bool:
    """检查 subagent 是否仍在运行（RUNNING 或 INTERRUPTED）。"""
    run = get_run_by_child_session_key(child_session_key)
    if run is None:
        return False  # 找不到 run record，不阻断
    return is_live_unended_run(run)  # True = RUNNING/INTERRUPTED（仍在运行）


def _read_taskflow_step_status(flow_id: str, step_id: str) -> str | None:
    """从 taskflow_summary 输出里解析指定 step 的 status（只读，不调度）。

    DAG 状态完全由 TaskFlow 维护；这里只做屏障判定所需的回读。
    """
    text = asyncio.run(taskflow_summary.ainvoke({"flow_id": flow_id}))  # 或同步封装
    for line in str(text).splitlines():
        if line.strip().startswith(f"- [{step_id}]"):
            parts = line.split()
            if len(parts) >= 3:
                return parts[2]  # [step_id] status task -> ...
    return None
```

**关键 API**（已确认存在于 sherry_agent）:

| 方法                                              | 文件路径                                      | 说明                                   |
| ------------------------------------------------- | --------------------------------------------- | -------------------------------------- |
| `get_run_by_child_session_key(child_session_key)` | `agent/tools/subagent/registry/queries.py:55` | 按 child session key 查找 run record   |
| `has_run_ended(run)`                              | `agent/tools/subagent/registry/helpers.py:53` | True = TERMINAL（已结束）              |
| `is_live_unended_run(run)`                        | `agent/tools/subagent/registry/helpers.py:48` | True = RUNNING/INTERRUPTED（仍在运行） |

所有函数从 `agent.tools.subagent.registry` 包统一导出（`__init__.py`）。

> **交叉引用**：E4 的 step 状态来源是 TaskFlow（`blocked/ready/dispatched/done`，
> 见 `skills/builtin/core/taskflow/SKILL.md`），todolist 层不持有 DAG 副本，也不再需要
> `wave_index`/`depends_on` 本地字段。

---

## E5: Sisyphus 完成契约 ★★

来自 omo `ulw-execute/SKILL.md:184-211`，这是对"虚假完成"的最终防线。

### 完成契约三阶段

```
Worker 返回结果
  → DoneClaim: orchestrator 收到 subagent 的完成声明
  → AdversarialVerify: 独立验证（不是信任 worker 的自述）
      Gate 1: Plan reread — 重读计划，确认 checkbox 的 acceptance criteria
      Gate 2: Automated verification — 运行计划中指定的验证命令
      Gate 3: Manual QA — 人工或 agent 检查可观测行为
      Gate 4: Adversarial QA — 对抗性检查（stale state / dirty worktree / leftover resources）
      Gate 5: Cleanup — 清理临时资源（tmux session、临时文件等）
  → FullyDone: 所有 gate 通过 → 标记 checkbox completed
  → NOT done: 任一 gate 失败 → 重新 dispatch 或修复
```

### 实现方式

- **修改文件**: `agent/middlewares/subagent_completion_drain.py`（扩展）
- **新建文件**: `agent/tools/todolist/verifier.py`
- **代码量**: ~80 行
- **阶段**: Phase 5（可选，有子代理使用场景时再加）

#### verifier.py

```python
"""Sisyphus 完成验证器。

参考 omo ulw-execute/SKILL.md:184-211 的 5-gate 验证。
"""

import json
import os
from datetime import datetime
from agent.tools.todolist.evidence_ledger import EvidenceLedger


class SisyphusVerifier:
    @staticmethod
    async def verify_checkbox(
        session_id: str,
        todo: dict,
        plan_path: str,
        checkbox_label: str,
    ) -> tuple[bool, dict]:
        """运行 5-gate 验证，返回 (passed, evidence)。"""
        evidence = {
            "event": "task-completed",
            "plan": plan_path,
            "task": checkbox_label,
            "session_id": session_id,
            "adversarial_classes": {},
            "cleanup": [],
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Gate 1: Plan reread
        plan_content = _read_plan(plan_path)
        acceptance = _extract_acceptance_criteria(plan_content, checkbox_label)
        if not acceptance:
            evidence["adversarial_classes"]["plan_reread"] = "failed: acceptance criteria not found"
            return False, evidence
        evidence["adversarial_classes"]["plan_reread"] = "passed"

        # Gate 2: Automated verification
        # Gate 3: Manual QA
        # Gate 4: Adversarial QA — probe for stale state, dirty worktree, leftover resources
        # Gate 5: Cleanup
        # (实际实现中这些 gate 由 orchestrator LLM 执行，这里只记录证据)

        EvidenceLedger.append(evidence)
        return True, evidence


def _read_plan(plan_path: str) -> str:
    if not os.path.exists(plan_path):
        return ""
    with open(plan_path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_acceptance_criteria(plan_content: str, checkbox_label: str) -> str | None:
    """从计划文件中提取 checkbox 的 acceptance criteria。"""
    lines = plan_content.split("\n")
    for i, line in enumerate(lines):
        if checkbox_label in line and "- [ ]" in line:
            # 收集该 checkbox 后的缩进行（verification 等）
            criteria_lines = []
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].startswith("  - ") or lines[j].startswith("    "):
                    criteria_lines.append(lines[j].strip())
                elif lines[j].startswith("- ["):
                    break
            return "\n".join(criteria_lines) if criteria_lines else None
    return None
```

#### subagent_completion_drain.py 扩展

```python
_VERIFICATION_REMINDER = (
    "\n\n[SYSTEM REMINDER] Subagent completed. "
    "Before marking the todo as completed, you MUST:\n"
    "1. Run todoread to check current state\n"
    "2. Verify the work against acceptance criteria (Sisyphus contract)\n"
    "3. Probe for stale state, dirty worktree, leftover resources\n"
    "4. Only then mark completed via todowrite\n"
    "Unverified completion = SISYPHUS VIOLATION = Lost progress."
)


async def abefore_model(self, handler, request, config, *, key, state):
    # 现有逻辑：drain steering queue
    carriers = await drain_steering(session_id)
    if carriers:
        # E5: 追加 Sisyphus 验证提醒
        for carrier in carriers:
            if isinstance(carrier.content, str):
                carrier.content += _VERIFICATION_REMINDER
    return {"messages": carriers} if carriers else None
```

### E5 文件清单

| #   | 操作 | 文件路径                                         | 代码量 |
| --- | ---- | ------------------------------------------------ | ------ |
| 1   | 新建 | `agent/tools/todolist/verifier.py`               | ~80 行 |
| 2   | 修改 | `agent/middlewares/subagent_completion_drain.py` | ~15 行 |

---

## E6: 委派路由 — Todo 与 Subagent 联动 ★

参考 omo ulw-execute 的 delegation router（`SKILL.md:134-148`），适配到 sherry_agent。

**核心目标**: 在 TodoList 执行过程中，让 LLM 知道何时该派 subagent、派哪类 subagent、何时可以标记完成。

> **DAG 交叉引用（2026-09-10）**：委派产生的子会话由既有 TaskFlow 追踪。带依赖的步骤用
> `taskflow_run_task(flow_id, task, depends_on=[...])` 登记，依赖未满足时 step 为 `blocked`
> 且不派发；结果注入用 `taskflow_resume`（标 `done` 并解锁后继）；并行派发用
> `taskflow_dispatch`；等待用 `taskflow_wait_all`。E6 只负责"路由到哪个 category/是否委派"，
> 不负责调度，DAG 状态一律以 TaskFlow 为准。

### 生命周期

```
LLM 创建 todo（含 category + delegation 字段）
  → #a fan-out reminder 提醒考虑委派（每 session 首次）
  → #b category 告诉 LLM 派哪类 subagent
  → #c AGENTS.md 指导如何委派
  → 带依赖的步骤: taskflow_run_task(..., depends_on=[...]) → TaskFlow 记 blocked 或 dispatched
  → 对 ready 步骤: taskflow_dispatch(flow_id, step_ids) 派发 → 拿到 child_session_key
  → LLM 更新 todo 的 subagent_id + flow_id/step_id 字段
  → #d-prompt: "TaskFlow step 未 done / subagent 未返回前不得标记 done"
  → #d-code: update_todos() 检查 TaskFlow step 状态 + is_live_unended_run() → 硬阻断
  → taskflow_wait_all → taskflow_resume 注入结果(step done) → E5 Sisyphus 验证 → LLM 标记 completed
```

### E6a: Fan-out Reminder（#a）— 事中触发

- **修改文件**: `agent/tools/todolist/tools/todowrite.py`
- **代码量**: ~10 行
- **阶段**: Phase 1

每 session 首次 todowrite 调用时，在工具返回值末尾追加 fan-out 决策提醒：

```python
# agent/tools/todolist/tools/todowrite.py

_FANOUT_REMINDER = """

[SYSTEM REMINDER] Consider whether any of these tasks should be delegated to subagents.
- Set delegation="subagent" for tasks that are independent with disjoint write scopes
- Set delegation="self" for interdependent or trivial tasks
- Declare dependencies with taskflow_run_task(..., depends_on=[...]); TaskFlow blocks
  and unlocks steps, then taskflow_dispatch batches the ready ones in parallel
- Route by category: quick|deep|ultrabrain|visual|git|writing
"""

_reminded_sessions: set[str] = set()  # 模块级，每 session 仅触发一次


@tool("todowrite")
async def todowrite(todos, session_id=""):
    result = await service.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    # E6a: 首次调用追加 fan-out reminder
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER
    return output
```

### E6b: Category + Delegation 字段（#b + #4a）— Schema + 校验

- **修改文件**: `store_sqlite.py`（schema）、`service.py`（校验）、`tools/__init__.py`（E2 工具描述）、`prompt_builder.py`（Layer 4 显示）
- **代码量**: ~30 行
- **阶段**: Phase 1

**service.py 校验**:

```python
_VALID_CATEGORIES = {"quick", "deep", "ultrabrain", "visual", "git", "writing"}
_VALID_DELEGATIONS = {"self", "subagent"}


def _validate_todos(todos: list[dict]) -> list[dict]:
    for t in todos:
        if t.get("category", "quick") not in _VALID_CATEGORIES:
            t["category"] = "quick"  # 默认降级
        if t.get("delegation", "self") not in _VALID_DELEGATIONS:
            t["delegation"] = "self"  # 默认降级
    return todos
```

### E6c: Delegation Instruction（#c）— 纯 prompt

已在 E1 的 AGENTS.md 中包含 Delegation 小节。

### E6d: Transition Barrier（#d）— 见 E4

已在 E4 中详述（prompt 级 + 代码级硬阻断）。屏障的判定来源是 **TaskFlow step 状态**
（`blocked/ready/dispatched/done`，DAG 归 TaskFlow）+ **subagent registry 存活检测**
（`get_run_by_child_session_key` / `is_live_unended_run`），todolist 层不再自建 DAG 状态。

### E6 委派路由表

来自 `ulw-execute/SKILL.md:134-148`：

| Category            | 路由到                           | 说明                                 |
| ------------------- | -------------------------------- | ------------------------------------ |
| `quick` (low)       | subagent spawn (default model)   | 机械、单文件、样板、配置/copy — 默认 |
| `deep` (high)       | subagent spawn (reasoning model) | 复杂调试、研究密集型或跨模块工作     |
| `ultrabrain` (high) | subagent spawn (最强 model)      | 一个真正困难的逻辑问题               |
| `visual` (medium)   | subagent spawn                   | 前端、UI/UX、样式、动画              |
| `git` (low)         | subagent spawn                   | git 操作                             |
| `writing` (low)     | subagent spawn                   | 文档和散文                           |

Sizing 决策（每个 checkbox，dispatch 前）：

- **可拆分的工作拆分。** 当 checkbox 分解为独立片段时，dispatch 为一批 `quick` worker 并行
- **内聚的困难工作保持整体。** 当拆分会切断共享推理时，发整体给 `deep` 或 `ultrabrain`

### E6 文件清单

| #   | 操作 | 文件路径                                        | 机制              | 代码量 | 阶段      |
| --- | ---- | ----------------------------------------------- | ----------------- | ------ | --------- |
| 1   | 修改 | `agent/tools/todolist/registry/store_sqlite.py` | #b#4a schema      | ~5 行  | Phase 1   |
| 2   | 修改 | `agent/tools/todolist/service.py`               | #b 校验 + #d-code | ~20 行 | Phase 1+2 |
| 3   | 修改 | `agent/tools/todolist/tools/todowrite.py`       | #a fan-out        | ~10 行 | Phase 1   |
| 4   | 修改 | `agent/tools/todolist/tools/__init__.py`        | E2 描述扩展       | ~5 行  | Phase 1   |
| 5   | 修改 | `workspace/template/{en,zh}/AGENTS.md`          | #c+#d-prompt      | 0 行   | Phase 1   |
| 6   | 修改 | `workspace/prompt_builder.py`                   | #b 显示           | ~10 行 | Phase 1   |
| 7   | 修改 | `client/app/components/chat/TodoItem.vue`       | 前端显示          | ~20 行 | Phase 4   |

---

## E7: 意图识别与引导 — 事前触发器 ★★

这是解决"模型不自觉调用计划工具和技能"问题的核心层。

**问题**: E1 系统提示词说了 MANDATORY，但 LLM 在长系统提示词中遵守指令的可靠性很差。尤其当用户问题看起来简单直接时，模型会直接开始写代码而不是先做计划。技能描述虽然注入在系统提示词的 `<available_skills>` XML 中，但模型不会主动去读 SKILL.md 文件。

### omo 的实际架构（双组件模型）

omo 用 **两个独立组件** 处理不同场景（而非单一中间件）：

| omo 组件                   | 触发条件                            | input hook 行为                                | agent_end hook 行为                             |
| -------------------------- | ----------------------------------- | ---------------------------------------------- | ----------------------------------------------- |
| `ultrawork` (arming)       | 用户消息含 `ulw`/`ultrawork` 关键词 | 首次：注入完整 17KB 指令；已 armed：注入短提醒 | (无)                                            |
| `ulw-execute-continuation` | boulder work 活跃（有计划）         | **追加** steering reminder 到用户消息          | 注入完整续作指令（含计划状态、下一个 checkbox） |

关键设计点：

1. **ultrawork 的 once-per-session arming**（`ultrawork/index.ts:232-273`）：
   - `armedSessionIds: Set<string>` — 每个 session 只注入一次完整指令
   - 后续触发只注入短提醒: `"ultrawork mode is already armed for this session"`
   - 压缩后 re-arm: `session_compact` event → 清除 armed 标记 → 下次触发重新注入完整指令
   - 已有 `<ultrawork-mode>` 标签的消息 → markArmed 但不注入（避免重复）

2. **ulw-execute-continuation 的 input hook**（`ulw-execute-continuation/index.ts:42-61`）：
   - 检测 `findContinuableBoulderWork(cwd, sessionId)` — boulder 有 active/paused work
   - 如果 continuable → `action: "transform"` — **追加** reminder 到用户消息末尾
   - reminder 内容: "An active Prometheus ulw-execute plan is present... read `.omo/boulder.json` and the active plan file..."
   - **每条用户消息都会追加**（非 once-per-session）

3. **ulw-execute-continuation 的 agent_end hook**（`index.ts:64-108`）：
   - 检测 continuable work → 注入完整续作指令（含 checklist 进度、下一个 checkbox label）
   - `CONTINUATION_LIMIT = 8` — 最多连续续作 8 次
   - `lastSignature` 停滞检测: `work_id:updated_at:completed/total`，无变化则跳过

4. **ulw-loop 和 ulw-execute-continuation 的优先级**（`ulw-loop/index.ts:104-106`）：
   - 如果 boulder continuation 活跃 → ulw-loop **跳过**（`reason: "boulder-continuation-active"`）

### sherry_agent 适配方案

omo 用 `ulw` 关键词触发 arming，sherry_agent 的用户不会输入 `ulw`。因此 sherry 需要 **隐式意图检测** + **双模式注入**：

- **E7a: 首次 arming**（无计划 + 任务意图）→ 注入完整 orchestrator 引导 prompt
- **E7b: 计划活跃 steering**（有计划 + 用户消息）→ 追加短 reminder

| 场景                          | omo 组件                             | sherry E7 模式     | 行为                         |
| ----------------------------- | ------------------------------------ | ------------------ | ---------------------------- |
| 用户说"实现登录"，无计划      | ultrawork (first_arm)                | **E7a**            | 注入完整引导 prompt          |
| 已 armed + 用户说"再修个 bug" | ultrawork (remention)                | **E7a 轻量**       | 注入短提醒                   |
| 有活跃计划 + 用户发任何消息   | ulw-execute-continuation (input)     | **E7b**            | 追加 plan-active reminder    |
| 有活跃计划 + turn 结束        | ulw-execute-continuation (agent_end) | **E3**             | E3 续作强制器已覆盖          |
| 用户问"什么是 REST API?"      | (无触发)                             | **跳过**           | 非任务意图                   |
| 压缩后                        | ultrawork (post_compact_rearm)       | **E7a 重新 armed** | 系统提示词重建 = 重新 arming |

### 设计方案

- **新建文件**: `agent/middlewares/task_intent.py`
- **代码量**: ~160 行
- **阶段**: Phase 1

### 意图检测策略

轻量级启发式检测，不调用 LLM（零延迟、零成本）：

```python
import re

_TASK_KEYWORDS = {
    # English
    "implement",
    "fix",
    "create",
    "add",
    "refactor",
    "build",
    "deploy",
    "test",
    "update",
    "migrate",
    "write",
    "setup",
    "configure",
    "integrate",
    "optimize",
    "debug",
    "resolve",
    "enhance",
    "rewrite",
    "convert",
    # Chinese
    "实现",
    "修复",
    "创建",
    "添加",
    "重构",
    "构建",
    "部署",
    "测试",
    "更新",
    "迁移",
    "编写",
    "设置",
    "配置",
    "集成",
    "优化",
    "调试",
    "解决",
    "增强",
    "重写",
    "转换",
}

_QUESTION_PATTERNS = [
    r"^(what|how|why|where|who|when|can you|is it|are you|do you)\b",
    r"^(什么是|怎么|为什么|哪里|谁|什么时候|能否|是否|是不是|能不能)",
    r"^(explain|describe|tell me about)",
    r"^(解释|说明|介绍一下)",
]

_CHAT_PATTERNS = [
    r"^(hello|hi|hey|thanks|thank you|ok|good|great|bye)\b",
    r"^(你好|谢谢|好的|再见|嗯|哦)",
]


def _detect_task_intent(content: str) -> bool:
    """检测用户消息是否为任务请求。

    策略:
    1. 如果匹配问答模式 → 非任务
    2. 如果匹配闲聊模式 → 非任务
    3. 如果包含任务关键词 → 任务
    4. 否则 → 非任务（保守，不注入）
    """
    text = content.strip().lower()

    # 长消息更可能是任务
    is_long = len(content) > 100

    # 检查问答模式（短消息）
    if not is_long:
        for pattern in _QUESTION_PATTERNS:
            if re.match(pattern, text):
                return False

    # 检查闲聊模式（短消息）
    if not is_long:
        for pattern in _CHAT_PATTERNS:
            if re.match(pattern, text):
                return False

    # 检查任务关键词
    for keyword in _TASK_KEYWORDS:
        if keyword in text:
            return True

    # 长消息且非问答/闲聊 → 可能是任务
    if is_long:
        return True

    return False
```

### 双模式引导 prompt

```python
# E7a: 完整引导（首次 arming，无计划时注入）
_TASK_STEERING_PROMPT = """[SYSTEM DIRECTIVE: TASK INTENT DETECTED]

This message appears to be a work request. Before responding, assess the scope:

## If this is a multi-step task (2+ steps):

1. Load the ulw-execute skill to understand the orchestration workflow.
   Read the skill file at its <location> path shown in <available_skills>.

2. You are an ORCHESTRATOR, not an implementer:
   - Create a plan (in .omo/plans/ if applicable) or use todowrite to register tasks
   - Set proper category, delegation, flow_id/step_id fields
   - For dependencies, register TaskFlow steps with depends_on and let TaskFlow
     block/unlock/parallel-dispatch — do NOT build a second DAG
   - DELEGATE implementation to subagents — you do NOT write code

3. Workflow:
   a. todowrite: register all tasks with atomic granularity
   b. For dependent steps: taskflow_run_task(..., depends_on=[...]); then
      taskflow_dispatch the ready ones and taskflow_wait_all
   c. For delegation="subagent" tasks: spawn subagent via task tool
   d. Wait for subagent return → taskflow_resume → verify → mark completed
   e. For delegation="self" tasks: execute directly

## If this is a simple single-step task:
Respond directly — no plan needed.

## If this is a question (not a task):
Respond directly — ignore this directive.

Decision: Is this a multi-step task? If yes, create todos FIRST."""

# E7a 轻量: 短提醒（已 armed 时注入，避免重复完整 prompt）
_TASK_STEERING_REMINDER = (
    "[SYSTEM DIRECTIVE: ORCHESTRATOR MODE ARMED]\n"
    "Orchestrator mode is already active for this session. "
    "The full directive above remains binding — re-read it and continue. "
    "Create todos FIRST for any multi-step work."
)

# E7b: 计划活跃 steering reminder（有活跃计划时追加到用户消息后）
_PLAN_ACTIVE_REMINDER = (
    "\n\n<sherry-ulw-execute>\n"
    "An active ulw-execute plan is present in this working directory.\n"
    "Before continuing, read `.omo/boulder.json` and the active plan file to "
    "determine what remains; use the ledger and plan as the source of truth.\n"
    "Continue the current work with evidence-bound execution; do not start "
    "unrelated work until every top-level checkbox is `- [x]`.\n"
    "</sherry-ulw-execute>"
)
```

### 中间件实现

```python
# agent/middlewares/task_intent.py

"""E7: 意图识别与引导 — before_model 中间件。

双模式设计，参考 omo 的两个独立组件:
- ultrawork (arming):  once-per-session 注入完整指令 / 已 armed 注入短提醒
- ulw-execute-continuation (input hook): 有活跃计划时追加 steering reminder

sherry_agent 用 before_model 中间件 + 消息注入实现等价效果。

防循环机制:
- E7b 优先于 E7a: 有活跃计划时走 E7b（追加 reminder），不走 E7a
- E7a once-per-session: _armed_sessions Set，压缩后重新 armed
- _is_system_directive(): 过滤掉 E3/E7 注入的消息，不误判为用户消息
"""

import re
from typing import Any
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from loguru import logger

# (上面的 _TASK_KEYWORDS, _QUESTION_PATTERNS, _CHAT_PATTERNS, _detect_task_intent)
# (上面的 _TASK_STEERING_PROMPT, _TASK_STEERING_REMINDER, _PLAN_ACTIVE_REMINDER)

# once-per-session arming ledger (模块级，参考 omo armedSessionIds)
_armed_sessions: set[str] = set()


class TaskIntentMiddleware(AgentMiddleware):
    """before_model: 双模式引导 — E7a (arming) + E7b (plan-active steering)。

    放在 middleware 列表中 MultimodalProcessor 之后，
    确保图片等多模态内容已处理后再做意图检测。
    """

    async def abefore_model(self, state: AgentState, runtime=None) -> dict[str, Any] | None:
        try:
            messages = state.get("messages", []) if isinstance(state, dict) else []
            if not messages:
                return None

            # 找最后一条 HumanMessage（用户最新消息，非系统 directive）
            last_human = None
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage) and not _is_system_directive(msg):
                    last_human = msg
                    break
            if not last_human:
                return None

            content = last_human.content
            if not isinstance(content, str) or not content.strip():
                return None

            session_id = state.get("session_id", "") if isinstance(state, dict) else ""

            # ── E7b: 计划活跃 steering（优先于 E7a）──────────────────
            # 参考 omo ulw-execute-continuation input hook:
            # 有活跃 boulder work → 追加 reminder 到用户消息后
            # 每条用户消息都追加（非 once-per-session）
            if _has_active_boulder():
                reminder = HumanMessage(content=_PLAN_ACTIVE_REMINDER.strip())
                logger.info(
                    "TaskIntentMiddleware E7b: plan-active reminder for session {}", session_id
                )
                return {"messages": [reminder]}

            # ── E7a: 首次 arming / 短提醒 ──────────────────────────
            # 参考 omo ultrawork arming:
            # 无计划 + 任务意图 → 注入完整引导 (首次) 或短提醒 (已 armed)
            if not _detect_task_intent(content):
                return None

            is_armed = session_id in _armed_sessions
            if is_armed:
                # 已 armed: 注入短提醒（不重复完整 prompt）
                steering = HumanMessage(content=_TASK_STEERING_REMINDER)
                logger.info(
                    "TaskIntentMiddleware E7a: re-arming reminder for session {}", session_id
                )
            else:
                # 首次: 注入完整引导
                _armed_sessions.add(session_id)
                steering = HumanMessage(content=_TASK_STEERING_PROMPT)
                logger.info(
                    "TaskIntentMiddleware E7a: first-arm steering for session {}", session_id
                )

            return {"messages": [steering]}
        except Exception:
            logger.exception("TaskIntentMiddleware: failed; continuing without steering")
            return None

    def before_model(self, state: AgentState, runtime=None) -> dict[str, Any] | None:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(self.abefore_model(state, runtime))
            except Exception:
                return None
        return None


def _is_system_directive(msg: HumanMessage) -> bool:
    """检测是否为系统注入的 directive 消息（避免把 E3 续作/E7 引导误判为用户消息）。"""
    content = msg.content
    if isinstance(content, str) and content.startswith("[SYSTEM DIRECTIVE"):
        return True
    if isinstance(content, str) and content.startswith("<sherry-ulw-execute>"):
        return True
    meta = getattr(msg, "metadata", None) or {}
    return bool(meta.get("internal"))


def _has_active_boulder() -> bool:
    """检查是否有活跃的 boulder work（active 或 paused）。

    参考 omo findContinuableBoulderWork():
    - getWorkForSession(cwd, sessionId) → work 存在
    - work.status === "active" || work.status === "paused"
    - getPlanChecklist(planPath).total > 0
    """
    import json, os

    boulder_path = ".omo/boulder.json"
    if not os.path.exists(boulder_path):
        return False
    try:
        with open(boulder_path, "r") as f:
            boulder = json.load(f)
        active_id = boulder.get("active_work_id", "")
        work = boulder.get("works", {}).get(active_id, {})
        status = work.get("status")
        if status not in ("active", "paused"):
            return False
        # 检查计划文件是否有 checkbox
        plan_path = work.get("active_plan", "")
        if plan_path and os.path.exists(plan_path):
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
            if "- [ ]" in plan_content or "- [x]" in plan_content:
                return True
        return False
    except Exception:
        return False


def rearm_after_compact(session_id: str) -> None:
    """压缩后重新 armed（参考 omo session_compact event handler）。

    在 Summarization 中间件压缩成功后调用，
    下次 E7a 触发时重新注入完整引导 prompt。
    """
    _armed_sessions.discard(session_id)
```

### 修改: `agent/core.py`

在 middleware 列表中注册（放在 MultimodalProcessor 之后）：

```python
_agent = create_agent(
    model=main_llm.bind(temperature=temperature),
    state_schema=StateSchema,
    checkpointer=checkpointer,
    tools=get_agent_tools(),
    middleware=[
        ContextEngineHook(),
        MultimodalProcessor(),
        TaskIntentMiddleware(),  # ← E7: 意图识别，在多模态处理之后
        IterationBudget(90),
        ToolGuardrails(),
        ToolCallNormalize(),
        SubagentCompletionDrainMiddleware(),
        OutputRepetitionGuard(),
        HeartbeatStaleness(),
        HumanInTheLoop(HITLConfig()),
        Summarization(...),
        TodoContinuationEnforcer(),  # E3: after_agent 续作
    ],
)
```

### 压缩后 re-arm

Summarization 中间件压缩成功后，调用 `rearm_after_compact(session_id)` 清除 armed 标记。下次用户消息触发 E7a 时重新注入完整引导 prompt（参考 omo `session_compact` event → `rearmOnCompact`）。

```python
# 在 Summarization 中间件或 TodoContinuationEnforcer 的 aafter_agent 中:
from agent.middlewares.task_intent import rearm_after_compact

if compression_happened:
    rearm_after_compact(session_id)
```

### E7 工作流程

```
用户消息到达 (HumanMessage)
  → MultimodalProcessor.abefore_model (处理图片等多模态内容)
  → TaskIntentMiddleware.abefore_model
      → 找最后一条非 directive 的 HumanMessage
      │
      ├─ E7b: _has_active_boulder()?
      │   → YES: 追加 _PLAN_ACTIVE_REMINDER（每条用户消息都追加）
      │   → return  (E7b 优先，不走 E7a)
      │
      └─ E7a: _detect_task_intent(content)?
          → NO: return (非任务，不注入)
          → YES:
              ├─ session_id in _armed_sessions?
              │   → YES: 注入 _TASK_STEERING_REMINDER（短提醒）
              │   → NO:  _armed_sessions.add() → 注入 _TASK_STEERING_PROMPT（完整引导）
  → IterationBudget, ToolGuardrails, ...
  → 模型处理

[压缩成功]
  → rearm_after_compact(session_id) → 清除 armed 标记
  → 下次 E7a 触发 → 重新注入完整引导 prompt
```

### E7 文件清单

| #   | 操作 | 文件路径                           | 代码量              |
| --- | ---- | ---------------------------------- | ------------------- |
| 1   | 新建 | `agent/middlewares/task_intent.py` | ~160 行             |
| 2   | 修改 | `agent/core.py`                    | ~2 行（注册中间件） |

### E7 与其他层的协作

| 场景                       | E7 模式  | 行为                                                       | 其他层行为                     |
| -------------------------- | -------- | ---------------------------------------------------------- | ------------------------------ |
| 首次任务请求，无计划       | E7a      | **注入完整引导** → 模型加载 skill + 创建 todo              | E1 持续提示 MANDATORY          |
| 已 armed + 新任务请求      | E7a      | **注入短提醒** → 不重复完整 prompt                         | E1 持续提示                    |
| 有活跃计划 + 用户发消息    | E7b      | **追加 plan-active reminder** → 引导读 boulder.json + 计划 | E1 系统提示词有 todo block     |
| 有活跃计划 + turn 结束     | (E3)     | E7 不干预（E7b 只在 before_model 运行）                    | E3 续作注入完整计划状态        |
| 续作注入（E3 触发的 turn） | (跳过)   | _is_system_directive 过滤 → 不触发 E7                      | E3 续作 prompt 驱动模型        |
| 用户追问"进度怎么样了"     | (跳过)   | 非任务意图 → 不注入                                        | 模型直接回答                   |
| 压缩后 + 新任务请求        | E7a      | rearm → 重新注入完整引导                                   | E1 系统提示词已重建            |
| 模型忽略引导，直接写代码   | (已注入) | 模型仍不遵守                                               | E4 转换屏障 + E5 Sisyphus 兜底 |
| 简单问题"2+2=?"            | (跳过)   | 非任务 → 不注入                                            | 模型直接回答                   |

### 防循环设计

E7 和 E3 之间可能形成循环。防循环机制：

1. **E7b 优先于 E7a** — 有活跃计划时走 E7b（追加 reminder），不走 E7a（不注入完整引导）
2. **E7a once-per-session** — `_armed_sessions` Set，已 armed 只注入短提醒
3. **E7b 只在 before_model 运行** — E3 的 after_agent 续作不会触发 E7b
4. **\_is_system_directive()** — 过滤掉 E3/E7 注入的消息，不误判为用户消息
5. **E3 有退避冷却** — 即使 E3 误触发续作，退避机制限制频率
6. **压缩后 re-arm** — 压缩清除 armed 标记，但 E1 系统提示词同时重建（todo block 仍在）

---

## 强制机制适配性评估

| 层级             | 适合度   | 代码量   | 难度 | 阶段      | sherry 基础设施复用              |
| ---------------- | -------- | -------- | ---- | --------- | -------------------------------- |
| E1 系统提示词    | 完全适合 | 0 行代码 | 极低 | Phase 1   | AGENTS.md 已加载进 prompt        |
| E2 工具描述      | 完全适合 | ~15 行   | 低   | Phase 1   | builder 模式已有                 |
| E3 续作强制器    | 适合     | ~200 行  | 中   | Phase 2   | 复用 auto_turn.py 全套基础设施   |
| E4 转换屏障      | 完全适合 | ~20 行   | 低   | Phase 1+2 | 复用 subagent registry 查询 API  |
| E5 Sisyphus 验证 | 适合     | ~95 行   | 中   | Phase 5   | 复用 SubagentCompletionDrain     |
| E6 委派路由      | 适合     | ~75 行   | 中   | Phase 1+2 | 复用 subagent registry 查询 API  |
| E7 意图识别器    | 完全适合 | ~160 行  | 低   | Phase 1   | 复用 before_model + 消息注入模式 |

---

## 不适合 sherry_agent 的 omo 机制

| omo 机制                                      | 为什么不适合 / 如何适配                                                    |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| ultrawork mode 系统 + `<ultrawork-mode>` 标签 | 适配为 E7a once-per-session arming（`_armed_sessions` Set）                |
| ulw-execute-continuation input hook           | 适配为 E7b plan-active steering（`_has_active_boulder()` → 追加 reminder） |
| Bootstrap todo 拦截（tool.execute.before）    | sherry 无此 hook；且系统提示词注入已避免此问题                             |
| 8 段式压缩上下文注入                          | sherry 的系统提示词注入已天然免疫压缩，不需要                              |
| 60s 压缩保护窗口                              | 无续作注入器需要保护，不需要                                               |

> 注意：旧方案中"完成门控（plan 文件复选框）不适合 sherry"的判断已被推翻——新方案引入了 `.omo/plans/*.md` 计划文件 + boulder 状态 + 证据账本，完成门控现在是核心机制。
