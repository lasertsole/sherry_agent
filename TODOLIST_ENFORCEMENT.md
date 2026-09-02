# TodoList 强制执行层（Enforcement Layers）

> 配套文件: TODOLIST_PLAN.md（核心实现 Layer 1-7）
> 参考项目: oh-my-openagent-dev (D:\selfProj\oh-my-openagent-dev)
> 日期: 2026-09-02

## 设计哲学

参考 oh-my-openagent-dev 的 5 层软强制架构，适配到 sherry_agent。

**核心原则**: 不阻塞工具，通过"提示词威慑 + 后置自动续作 + 多点提醒"形成闭环，即使模型偷懒停下来，系统也会自动把它拉回来。

```
事前                事中                    事后
┌──────────┐  ┌──────────────┐  ┌──────────────────┐
│ E1 系统   │  │ E6 委派与     │  │ E3 续作强制器 ★   │
│ 提示词强制 │  │ Fan-out       │  │ idle + 未完成 todo│
│ MANDATORY │  │ #1 fan-out    │  │ → 自动注入续作     │
│ +hook告知  │  │ #2 category   │  │ → 复用 auto_turn  │
└──────────┘  │ #3 委派指令   │  └──────────────────┘
┌──────────┐  │ #4 转换屏障   │  ┌──────────────────┐
│ E2 工具   │  └──────────────┘  │ E5 验证提醒(可选) │
│ 描述强制   │                     │ 子代理完成后强制  │
│ MANDATORY │                     │ todowrite 更新    │
│ 格式规则   │                     └──────────────────┘
└──────────┘
```

## 架构总览

```
Layer 7  │ UI 组件层        │ ← 见 TODOLIST_PLAN.md
Layer 6  │ 前端状态层        │
Layer 5  │ 实时通信层        │
Layer 4  │ 压缩保护层 ★     │
Layer 3  │ 工具层           │
Layer 2  │ 服务层           │
Layer 1  │ 数据存储层        │
---------│------------------│
E1       │ 系统提示词强制    │ AGENTS.md MANDATORY 语言 + hook 存在性告知
E2       │ 工具描述强制      │ todowrite docstring MANDATORY 格式规则
E3       │ 续作强制器 ★     │ after_agent 中间件：idle + 未完成 todo → 自动续作
E5       │ 验证提醒 (可选)   │ 子代理完成后强制 todowrite 更新
E6       │ 委派与 Fan-out ★ │ #1 fan-out reminder + #2 category 路由 + #3 委派指令 + #4 转换屏障
```

---

## E1: 系统提示词强制 — 事前预防

- **修改文件**: `workspace/template/{en,zh}/AGENTS.md`
- **代码量**: 0 行代码（纯文本编辑）
- **阶段**: Phase 1

sherry_agent 的 `AGENTS.md` 被加载进系统提示词（`prompt_builder.py` 的 `_read_static_files()`）。直接添加强制段：

```markdown
## Task Management (CRITICAL)

**DEFAULT BEHAVIOR**: Create todos BEFORE starting any non-trivial task (2+ steps).

### When to Create Todos (MANDATORY)

- Multi-step task (2+ steps) → ALWAYS create todos first
- Uncertain scope → ALWAYS (todos clarify thinking)
- User request with multiple items → ALWAYS

### Workflow (NON-NEGOTIABLE)

1. IMMEDIATELY on receiving request: todowrite to plan atomic steps.
2. Before starting each step: Mark in_progress (only ONE at a time)
3. After completing each step: Mark completed IMMEDIATELY (NEVER batch)

### Anti-Patterns (BLOCKING)

- Skipping todos on multi-step tasks — user has no visibility
- Batch-completing multiple todos — defeats real-time tracking

**FAILURE TO USE TODOS ON NON-TRIVIAL TASKS = INCOMPLETE WORK.**
```

在 `prompt_builder.py` 的 `_build_todo_block()` 末尾追加 hook 存在性告知（心理威慑）：

```python
def _build_todo_block(session_id: str) -> str:
    # ... 现有逻辑 ...
    lines.append("\nYour todo list is tracked by the continuation system. "
                 "Incomplete todos will trigger automatic continuation.")
    return "\n".join(lines)
```

---

## E2: 工具描述强制 — 格式质量保证

- **修改文件**: `agent/tools/todolist/tools/todowrite.py`（docstring）+ `agent/tools/todolist/tools/__init__.py`（builder）
- **代码量**: ~10 行
- **阶段**: Phase 1

在 `build_todolist_tools()` 中覆盖 todowrite 描述，注入 MANDATORY 格式规则：

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

## Task Management
- One in_progress at a time. Complete it before starting the next.
- Mark completed immediately after finishing each item.
"""

def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    # E2: 覆盖 todowrite 描述，注入格式规则
    todowrite.description += _TODOWRITE_FORMAT_RULES
    return list(_TODOLIST_TOOLS)
```

---

## E3: 续作强制器 — 事后闭环核心 ★

这是最关键的强制层——**不依赖模型自觉**。turn 结束后如果有未完成 todo，系统自动注入续作消息把 LLM 拉回来。

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

### 新建文件 1: `agent/tools/todolist/stagnation_tracker.py`

```python
"""停滞检测 + 退避冷却，防止续作无限循环。"""

import time

_MAX_STAGNATION = 3        # 连续 3 次无变化 → 停止
_BASE_COOLDOWN_S = 2.0    # 基础冷却秒数
_MAX_COOLDOWN_S = 60.0    # 最大冷却秒数

# per-session 状态
_stagnation_count: dict[str, int] = {}
_last_snapshot: dict[str, str] = {}    # session_id → "content=status|content=status" 快照
_last_inject_time: dict[str, float] = {}  # session_id → 上次注入的 timestamp

def check_stagnation(session_id: str, todos: list[dict]) -> bool:
    """返回 True 表示已停滞，应停止续作。"""
    snapshot = "|".join(f"{t['content']}={t['status']}" for t in todos)
    if _last_snapshot.get(session_id) == snapshot:
        _stagnation_count[session_id] = _stagnation_count.get(session_id, 0) + 1
    else:
        _stagnation_count[session_id] = 0
    _last_snapshot[session_id] = snapshot
    return _stagnation_count[session_id] >= _MAX_STAGNATION

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

def reset(session_id: str) -> None:
    """用户接管或 todo 全部完成时重置。"""
    _stagnation_count.pop(session_id, None)
    _last_snapshot.pop(session_id, None)
    _last_inject_time.pop(session_id, None)
```

### 新建文件 2: `agent/middlewares/todo_continuation.py`

```python
"""Turn 结束后检查未完成 todo，自动注入续作 prompt。

复用 auto_turn.py 的 maybe_trigger_auto_turn() 基础设施，
实现"模型偷懒停下来 → 2 秒后自动拉回来继续工作"的闭环。
"""

from langgraph.types import AgentState
from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from agent.tools.todolist.stagnation_tracker import (
    check_stagnation, is_in_cooldown, mark_injected, reset,
)

_CONTINUATION_PROMPT = """[SYSTEM DIRECTIVE: TODO CONTINUATION]

Incomplete tasks remain in your todo list. Continue working on the next pending task.

- Proceed without asking for permission
- Mark each task complete when finished
- Do not stop until all tasks are done
- If you believe all work is complete, the system is questioning your completion claim.
  Critically re-examine each todo item, verify the work was actually done, and update accordingly.

{todo_status}"""

def _build_status_block(todos: list[dict]) -> str:
    done = sum(1 for t in todos if t["status"] in ("completed", "cancelled"))
    total = len(todos)
    remaining = [t for t in todos if t["status"] in ("pending", "in_progress")]
    lines = [f"[Status: {done}/{total} completed, {len(remaining)} remaining]"]
    lines.append("Remaining tasks:")
    for t in remaining:
        icon = {"pending": "○", "in_progress": "◐"}.get(t["status"], "○")
        lines.append(f"- [{icon}] {t['content']} ({t['priority']})")
    return "\n".join(lines)

class TodoContinuationEnforcer:
    """after_agent 中间件：turn 结束后检查未完成 todo。"""

    async def aafter_agent(self, handler, request, config, *, key, state):
        result = await handler(request, config=config, key=key, state=state)

        session_id = state.get("session_id", "")
        if not session_id:
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

        # 停滞检测：连续 N 次无变化 → 停止
        if check_stagnation(session_id, todos):
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

### E3 文件清单

| #   | 操作 | 文件路径                                     | 代码量              |
| --- | ---- | -------------------------------------------- | ------------------- |
| 1   | 新建 | `agent/tools/todolist/stagnation_tracker.py` | ~50 行              |
| 2   | 新建 | `agent/middlewares/todo_continuation.py`     | ~80 行              |
| 3   | 修改 | `agent/core.py`                              | ~2 行（注册中间件） |

### E3 工作流程

```
turn 结束（model 无 tool_call，agent loop 退出）
  → Summarization.aafter_agent（压缩后重建系统提示词，注入最新 todos）
  → TodoContinuationEnforcer.aafter_agent
      → get_todos_sync(session_id) 从 DB 读取
      → 过滤未完成项
      → 停滞检测：快照对比，连续 3 次无变化 → 停止
      → 退避冷却：上次注入后未过冷却期 → 跳过
      → 构建 continuation prompt（含完整 todo 状态）
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _run_auto_turn() → _drive_turn() → async_generate()
          → _watch_user_takeover() 0.5s 轮询用户接管
      → 用户发消息 → detect_state() 变 busy → 取消续作 → reset()
```

---

## E5: 验证提醒（可选）— 子代理完成后强制更新

- **修改文件**: `agent/middlewares/subagent_completion_drain.py`（扩展）
- **代码量**: ~30 行
- **阶段**: Phase 5（可选，有子代理使用场景时再加）

sherry_agent 的 `SubagentCompletionDrainMiddleware` 在 `before_model` 中 drain 子代理完成消息。可以在 drain 出来的 carrier 消息中追加验证提醒：

```python
# 扩展 SubagentCompletionDrainMiddleware 或新建中间件

_VERIFICATION_REMINDER = (
    "\n\n[SYSTEM REMINDER] Subagent completed. "
    "Run todoread to check current state, "
    "then mark completed tasks via todowrite IMMEDIATELY. "
    "Unmarked = Untracked = Lost progress."
)

async def abefore_model(self, handler, request, config, *, key, state):
    # 现有逻辑：drain steering queue
    carriers = await drain_steering(session_id)
    if carriers:
        # E5: 追加验证提醒
        for carrier in carriers:
            if isinstance(carrier.content, str):
                carrier.content += _VERIFICATION_REMINDER
    return {"messages": carriers} if carriers else None
```

参考先例：`ToolGuardrails._wrap_tool_call_impl()` 第 343-348 行已在 WARN 时向 ToolMessage 追加警告文本，模式完全一致。

---

## E6: 委派与 Fan-out — Todo 与 Subagent 联动 ★

参考 oh-my-openagent-dev 的 4 个机制（todo-fanout-reminder / ulw-plan category / ulw-loop 委派指令 / ultrawork 转换屏障），适配到 sherry_agent。

**核心目标**: 在 TodoList 执行过程中，让 LLM 知道何时该派 subagent、派哪类 subagent、何时可以标记完成。

### 生命周期

```
LLM 创建 todo（含 category + delegation 字段）
  → #1 fan-out reminder 提醒考虑委派（每 session 首次）
  → #2 category 告诉 LLM 派哪类 subagent
  → #3 AGENTS.md 指导如何委派
  → LLM 调 task 工具派 subagent → 拿到 child_session_key
  → LLM 更新 todo 的 subagent_id 字段
  → #4d-prompt: "不得在 subagent 返回前标记 done"
  → #4d-code: update_todos() 检查 has_run_ended() → 硬阻断
  → subagent 返回 → LLM 标记 completed
```

### Schema 变更（Layer 1）

在 `todos` 表基础上新增 3 列：

```sql
ALTER TABLE todos ADD COLUMN category     TEXT    DEFAULT 'quick';
ALTER TABLE todos ADD COLUMN delegation   TEXT    DEFAULT 'self';
ALTER TABLE todos ADD COLUMN subagent_id  TEXT    DEFAULT NULL;
```

| 字段          | 可选值                                                         | 说明                                                    |
| ------------- | -------------------------------------------------------------- | ------------------------------------------------------- |
| `category`    | `quick` / `deep` / `ultrabrain` / `visual` / `git` / `writing` | #2: 路由裁决，告诉 LLM 该 todo 应由哪类 subagent 执行   |
| `delegation`  | `self` / `subagent`                                            | #4a: 委派策略（omo 还有 `team`，sherry 暂无 team mode） |
| `subagent_id` | nullable string                                                | #4b: 关联的 subagent child_session_key                  |

### E6a: Fan-out Reminder（#1）— 事中触发

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
- Spawn all independent subagents for the current wave first, then wait
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

参考: omo 的 `todo-fanout-reminder` 组件（`omo-senpi/src/components/todo-fanout-reminder/reminder.ts`），但简化为模块级 Set 去重，不依赖 ultrawork 模式武装。

### E6b: Category + Delegation 字段（#2 + #4a）— Schema + 校验

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

**E2 工具描述扩展**（`tools/__init__.py` 的 `_TODOWRITE_FORMAT_RULES` 追加）:

```python
_TODOWRITE_FORMAT_RULES += """
## Delegation Fields (optional but recommended)

- category: quick|deep|ultrabrain|visual|git|writing — routing verdict for subagent dispatch
- delegation: self|subagent — whether this todo should be delegated
- subagent_id: set after dispatching a subagent (use the child_session_key returned by task tool)
"""
```

**Layer 4 系统提示词显示**（`prompt_builder.py` 的 `_build_todo_block()`）:

```python
def _build_todo_block(session_id: str) -> str:
    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        cat = t.get("category", "quick")
        dlg = t.get("delegation", "self")
        tag = f"({cat}, {dlg})" if dlg != "self" else f"({cat})"
        lines.append(f"- [{icon.get(t['status'], '○')}] {tag} {t['content']} ({t['priority']})")
    lines.append("\nUpdate todos via the todowrite tool. Pass the COMPLETE list each time.")
    lines.append("Your todo list is tracked by the continuation system. "
                 "Incomplete todos will trigger automatic continuation.")
    return "\n".join(lines)
```

显示效果:

```
## Current Todo List
- [○] (deep, subagent) 实现用户登录模块 (high)
- [◐] (quick) 修复 typo (low)
- [●] (git) 提交代码 (medium)
```

### E6c: Delegation Instruction（#3）— 纯 prompt

- **修改文件**: `workspace/template/{en,zh}/AGENTS.md`
- **代码量**: 0 行
- **阶段**: Phase 1

在 E1 的 `Task Management` 段中追加 Delegation 小节:

```markdown
### Delegation (when working on todos)

- delegation="self": trivial tasks (<10 lines, single file) — do yourself
- delegation="subagent": complex tasks (multi-file, >100 lines, complex logic)
  — delegate to subagent via task tool, then set subagent_id field
- Code edits, test writes, and fixes are good delegation candidates
- Spawn all independent subagents for the current wave FIRST, then wait
```

### E6d: Transition Barrier（#4b）— 双保险

#### Phase 1: Prompt 级

在 E1 的 `Task Management` 段中追加:

```markdown
### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Wait for subagent to return before updating the todo status
- If subagent failed, mark todo as cancelled and re-plan
```

#### Phase 2: 代码级硬阻断

- **修改文件**: `agent/tools/todolist/service.py`
- **代码量**: ~15 行
- **阶段**: Phase 2

复用 sherry_agent 的 subagent registry 查询 API:

```python
# agent/tools/todolist/service.py

async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
    validated = _validate_todos(todos)

    # E6d: 转换屏障 — 检查 subagent 是否仍在运行
    for todo in validated:
        if todo["status"] == "completed" and todo.get("subagent_id"):
            if await _is_subagent_running(todo["subagent_id"]):
                raise TodoStoreError(
                    f"Cannot mark todo completed: subagent {todo['subagent_id']} "
                    "is still running. Wait for it to finish first."
                )

    await store.replace_all(session_id, validated)
    latest = await store.get_todos(session_id)
    await _push_todo_update(session_id, latest)
    return latest


async def _is_subagent_running(child_session_key: str) -> bool:
    """检查 subagent 是否仍在运行（RUNNING 或 INTERRUPTED）。"""
    from agent.tools.subagent.registry import get_run_by_child_session_key, has_run_ended
    run = get_run_by_child_session_key(child_session_key)
    if run is None:
        return False  # 找不到 run record，不阻断
    return not has_run_ended(run)  # True = 仍在运行
```

**关键 API**（已确认存在于 sherry_agent）:

| 方法                                              | 文件路径                                      | 说明                                   |
| ------------------------------------------------- | --------------------------------------------- | -------------------------------------- |
| `get_run_by_child_session_key(child_session_key)` | `agent/tools/subagent/registry/queries.py:55` | 按 child session key 查找 run record   |
| `has_run_ended(run)`                              | `agent/tools/subagent/registry/helpers.py:53` | True = TERMINAL（已结束）              |
| `is_live_unended_run(run)`                        | `agent/tools/subagent/registry/helpers.py:48` | True = RUNNING/INTERRUPTED（仍在运行） |

所有函数从 `agent.tools.subagent.registry` 包统一导出（`__init__.py`）。

### E6 工作流程

```
1. LLM 收到用户请求
   → todowrite 创建 todos（含 category + delegation 字段）
   → E6a: 工具返回值末尾追加 fan-out reminder（首次触发）
   → LLM 根据 reminder + E6c AGENTS.md 指令，决定哪些 todo 委派

2. LLM 对 delegation="subagent" 的 todo:
   → 调用 task 工具派 subagent → 拿到 child_session_key
   → todowrite 更新该 todo 的 subagent_id = child_session_key
   → E6d-prompt: "不得在 subagent 返回前标记 done"

3. subagent 执行完成
   → E5: SubagentCompletionDrainMiddleware drain 完成消息 + 验证提醒
   → LLM 收到通知
   → todowrite 标记 todo completed
   → E6d-code: update_todos() 检查 has_run_ended() → 通过（已结束）
   → Layer 4: 系统提示词更新，显示 completed 状态
   → Layer 5: WS 推送 todo_updated → 前端更新
```

### E6 文件清单

| #   | 操作 | 文件路径                                        | 机制               | 代码量 | 阶段      |
| --- | ---- | ----------------------------------------------- | ------------------ | ------ | --------- |
| 1   | 修改 | `agent/tools/todolist/registry/store_sqlite.py` | #2#4a schema       | ~5 行  | Phase 1   |
| 2   | 修改 | `agent/tools/todolist/service.py`               | #2 校验 + #4d-code | ~20 行 | Phase 1+2 |
| 3   | 修改 | `agent/tools/todolist/tools/todowrite.py`       | #1 fan-out         | ~10 行 | Phase 1   |
| 4   | 修改 | `agent/tools/todolist/tools/__init__.py`        | E2 描述扩展        | ~5 行  | Phase 1   |
| 5   | 修改 | `workspace/template/{en,zh}/AGENTS.md`          | #3+#4d-prompt      | 0 行   | Phase 1   |
| 6   | 修改 | `workspace/prompt_builder.py`                   | #2 显示            | ~10 行 | Phase 1   |
| 7   | 修改 | `client/app/components/chat/TodoItem.vue`       | 前端显示           | ~20 行 | Phase 4   |

---

## 强制机制适配性评估

| 层级            | 适合度   | 代码量   | 难度 | 阶段      | sherry 基础设施复用             |
| --------------- | -------- | -------- | ---- | --------- | ------------------------------- |
| E1 系统提示词   | 完全适合 | 0 行代码 | 极低 | Phase 1   | AGENTS.md 已加载进 prompt       |
| E2 工具描述     | 完全适合 | ~10 行   | 低   | Phase 1   | builder 模式已有                |
| E3 续作强制器   | 适合     | ~130 行  | 中   | Phase 2   | 复用 auto_turn.py 全套基础设施  |
| E5 验证提醒     | 部分适合 | ~30 行   | 低   | Phase 5   | 复用 SubagentCompletionDrain    |
| E6 委派+Fan-out | 适合     | ~75 行   | 中   | Phase 1+2 | 复用 subagent registry 查询 API |

---

## 不适合 sherry_agent 的 omo 机制

| omo 机制                                   | 为什么不适合                                                   |
| ------------------------------------------ | -------------------------------------------------------------- |
| 完成门控（plan 文件复选框）                | sherry 没有 plan 文件概念，todo 就是唯一的进度跟踪             |
| 运行时动态描述覆盖                         | sherry 无 hook 体系，但 E2 在 builder 阶段静态覆盖（等价效果） |
| Bootstrap todo 拦截（tool.execute.before） | sherry 无此 hook；且系统提示词注入已避免此问题                 |
| 8 段式压缩上下文注入                       | sherry 的系统提示词注入已天然免疫压缩，不需要                  |
| 60s 压缩保护窗口                           | 无续作注入器需要保护，不需要                                   |
