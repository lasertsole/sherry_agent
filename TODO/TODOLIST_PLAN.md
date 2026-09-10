# sherry_agent HTN 实现方案 — TaskFlow DAG 复用 + ulw-execute 编排

> 参考来源：oh-my-openagent-dev (D:\selfProj\oh-my-openagent-dev)
>
> - DAG 调度器（**仅作概念参考，不再在 todolist 内实现**）: `packages/senpi-task/src/dag/` (types.ts, scheduler.ts, store.ts, recovery.ts)
> - ulw-execute skill: `packages/shared-skills/skills/ulw-execute/SKILL.md`
> - ulw-plan skill: `packages/shared-skills/skills/ulw-plan/`
> - planner prompt: `packages/prompts-core/prompts/ultrawork/planner.md`
> - mass-ulw protocol: `docs/reference/mass-ulw-protocol.md`
>   日期: 2026-09-07

## Adaptation note (2026-09-10)

**DAG 现在由既有 TaskFlow 系统提供（taskflow-dag-phase1，commits 9ce33ef..2824034），本设计复用而不是再造第二个 DAG。**

- TaskFlow 的 step 现在携带 `depends_on`（step-id 列表）与 `status ∈ {blocked, ready, dispatched, done}`；`taskflow_run_task` 接受 `depends_on`，依赖未满足时只登记 `blocked` 不派发；`taskflow_resume` 把对应 step 标记 `done` 并解锁依赖它的步骤（不自动派发）；新增 `taskflow_dispatch(flow_id, step_ids)` 批量派发 `ready` 步骤；新增 `taskflow_wait_all(flow_id, ...)` 按 flow 范围等待本流派发的子会话。
- 没有 DB migration：所有 DAG 字段都放在 `state_json` 里。
- 因此本方案**删除** `agent/tools/todolist/dag/*`（`dag/types.py`、`dag/scheduler.py`）以及 todos.db 中用于第二个调度器的 `wave_index`/`depends_on` 列；todolist 层只保留会话级追踪与 UI，DAG 执行完全委派给 TaskFlow。
- **下文的核心目标保持不变**：`todowrite`/`todoread`、E1-E7 强制层、压缩免疫的系统提示词注入、Sisyphus 完成契约、续作强制器、委派路由，以及前端 TodoDock 计划全部保留。变化仅限于"DAG 调度器住在哪里"。

## 设计哲学

全面采用 oh-my-openagent-dev 的 HTN（分层任务网络）体系：

```
Plan (.omo/plans/*.md)
  └─ Wave 0: [Checkbox A] [Checkbox B]          ← 并行，无依赖
  └─ Wave 1: [Checkbox C] (depends on A, B)      ← 等 Wave 0 完成
  └─ Wave 2: [Final Verification Wave]            ← 全局收尾

每个 Checkbox → 分解为 atomic sub-tasks → 委派给 subagent workers
  └─ Worker 返回 DoneClaim
       └─ AdversarialVerify (独立验证)
            └─ FullyDone → 标记 checkbox 完成
```

**核心原则**（来自 ulw-execute SKILL.md:6-8）：

> YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.
> YOU DO NOT WRITE CODE. YOU DO NOT EDIT PRODUCT FILES.
> EVERY unit of implementation MUST be delegated to a spawned subagent.

---

## 架构总览

```
Layer 9  │ UI 组件层         │ TodoDock.vue + TodoItem.vue (PrimeVue) + DAG 状态显示（数据来自 TaskFlow）
Layer 8  │ 前端状态层         │ useTodoList.ts (模块级单例) + useDagRun.ts（读 taskflow_summary）
Layer 7  │ 实时通信层         │ WS: todo_updated 推送 + taskflow 状态刷新
Layer 6  │ 压缩保护层 ★      │ build_system_prompt 注入当前 todos + boulder 状态
Layer 5  │ DAG 调度层 ★      │ 委派给 TaskFlow: depends_on + blocked/ready/dispatched/done
Layer 4  │ 编排执行层 ★      │ ulw-execute: plan→checkbox→sub-task→worker→verify
Layer 3  │ 工具层            │ todowrite + todoread（DAG 由 taskflow_* 工具家族执行）
Layer 2  │ 服务层            │ TodoService + EvidenceLedger（DAG 状态查询委派 TaskFlow）
Layer 1  │ 数据存储层         │ todos.db (WAL) + boulder.json + ledger.jsonl + plans/*.md + taskflow_registry.db
---------│-------------------│
E1       │ 系统提示词强制     │ AGENTS.md: MANDATORY orchestrator doctrine
E2       │ 工具描述强制       │ todowrite docstring 格式规则（DAG 指引指向 TaskFlow）
E3       │ 续作强制器 ★      │ after_agent 中间件: idle+未完成→自动续作 (含退避/停滞/abort检测)
E4       │ 转换屏障 ★       │ TaskFlow step 未 done / subagent 运行中禁止标记 completed (prompt + code 双保险)
E5       │ Sisyphus 验证 ★  │ DoneClaim → AdversarialVerify → FullyDone
E6       │ 委派路由 ★       │ category-based delegation router
E7       │ 意图识别器 ★★    │ before_model: E7a arming(无计划+任务意图→注入引导) + E7b plan-active(有计划→追加reminder)
```

---

## Layer 1: 数据存储层

### 1a. Plan 文件 (.omo/plans/*.md)

计划文件是 checkbox 格式的 Markdown，定义了完整的 HTN 分解：

```markdown
# <Plan Name>

## Goal

<详细目标：计划名、路径、具体终态、交付模式、验证方式>

## Context

<项目背景、约束、已知信息>

## TODOs

### Wave 0: <Wave 描述>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>
- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: deep
  - Verification: <exact command + assertion>
  - Files in scope: <path3>

### Wave 1: <Wave 描述> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: visual
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### 1b. Boulder 状态 (.omo/boulder.json)

持久化工作状态，复用 omo 的 schema（`ulw-execute/SKILL.md:78-96`）：

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": ".omo/plans/<plan-name>.md",
      "plan_name": "<plan-name>",
      "session_ids": ["sherry:<session_id>"],
      "status": "active",
      "worktree_path": null,
      "created_at": "2026-09-07T00:00:00Z"
    }
  }
}
```

session_id 前缀用 `sherry:` 以区分 omo 的 `codex:` 前缀。

### 1c. 证据账本 (.omo/ledger.jsonl)

每行一个 JSON 对象，记录每个 checkbox 的执行证据（`ulw-execute/SKILL.md:172-183`）：

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "artifact": ".omo/evidence/xxx.txt", "adversarial_classes": {"stale_state": "not-applicable: no cached artifacts", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
```

### 1d. todos.db — 会话级 TODO 存储

- **新建文件**: `agent/tools/todolist/registry/store_sqlite.py`

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    plan_ref     TEXT    DEFAULT NULL,
    flow_id      TEXT    DEFAULT NULL,
    step_id      TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
```

字段说明：

| 字段        | 说明                                                              |
| ----------- | ----------------------------------------------------------------- |
| `plan_ref`  | 关联的 `.omo/plans/*.md` 文件路径                                 |
| `flow_id`   | 关联的 TaskFlow flow id（DAG 调度归属 TaskFlow，不在 todos 内建）  |
| `step_id`   | 关联的 TaskFlow step id（如 `step-2`），用于回读 DAG 状态          |

> **DAG 变更（2026-09-10）**：旧 schema 的 `wave_index` 与 `depends_on` 列**已删除**。
> 波次/依赖不再由 todos.db 表达；依赖图、`blocked/ready/dispatched/done` 状态与解锁逻辑
> 全部由 TaskFlow 提供（`taskflow_run_task(..., depends_on=[...])`、`taskflow_dispatch`、
> `taskflow_resume` 的 unlock、`taskflow_wait_all`）。todo 只需通过 `flow_id`/`step_id`
> 指向对应的 flow step，DAG 状态用 `taskflow_summary(flow_id)` 回读。

### CRUD 接口

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """全量替换：DELETE + INSERT（事务）"""


async def get_todos(session_id: str) -> list[dict]:
    """按 position 排序读取"""


def get_todos_sync(session_id: str) -> list[dict]:
    """同步路径，用于系统提示词注入（无事件循环场景）"""


async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """读取某个 TaskFlow flow 关联的 todo（用于把 flow 的 DAG 状态映射回 UI）"""
```

> 旧接口 `get_todos_by_wave()` / `get_dependency_frontier()` **已删除**：`wave_index` 与
> `depends_on` 列不再存在，frontier 计算改由 TaskFlow 内部的 `deps_satisfied` /
> `unlock_dependents` 完成。todolist 层要判断"下一步能做什么"，调用
> `taskflow_summary(flow_id)` 查看各 step 的 `status`（`ready` 即可派发）与 `depends_on`，
> 再对 `ready` 步骤调用 `taskflow_dispatch(flow_id, step_ids)`。

---

## Layer 2: 服务层

- **新建文件**: `agent/tools/todolist/service.py`

### TodoService

```python
class TodoService:
    @staticmethod
    async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
        validated = _validate_todos(todos)
        # E4: 转换屏障 — subagent 仍在运行时禁止标记 completed
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

    @staticmethod
    async def get_todos(session_id: str) -> list[dict]:
        return await store.get_todos(session_id)

    @staticmethod
    async def get_flow_progress(session_id: str, flow_id: str) -> dict:
        """把 TaskFlow 的 DAG 状态映射回 todo 视图（不重算 frontier）。

        DAG 调度住在 TaskFlow：本方法只调用只读的 taskflow_summary 回读
        step 的 status/depends_on，供 UI 显示或续作提示使用。真正的派发/解锁
        由 taskflow_dispatch / taskflow_resume 完成，todolist 层不自己调度。
        """
        from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary

        linked = await store.get_todos_by_flow(session_id, flow_id)
        summary_text = await taskflow_summary.ainvoke({"flow_id": flow_id})
        return {"todos": linked, "taskflow_summary": summary_text}
```

> **已删除**：原 `TodoService.get_wave_frontier()`（基于 `depends_on` + 本地 completed
> 集合计算可执行项）。该职责现由 TaskFlow 的 `deps_satisfied()` / `unlock_dependents()`
> 承担；todolist 层不再持有 DAG 副本，避免双份调度器漂移。

### EvidenceLedger

- **新建文件**: `agent/tools/todolist/evidence_ledger.py`

```python
class EvidenceLedger:
    LEDGER_PATH = ".omo/ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None:
        entry["timestamp"] = datetime.utcnow().isoformat()
        with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @classmethod
    def read_all(cls) -> list[dict]:
        if not os.path.exists(cls.LEDGER_PATH):
            return []
        with open(cls.LEDGER_PATH, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
```

### WS 推送

复用 sherry_agent 已有的 Pattern A — `relation_register` 直发（参考 `server/service/heartbeat.py` 的 `push_heartbeat_updated()`）：

```python
async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    from runtime import relation_register
    import json

    ws = relation_register.get_websocket_by_session_id(session_id)
    if ws:
        await ws.send_text(
            json.dumps(
                {"event": "todo_updated", "session_id": session_id, "content": {"todos": todos}}
            )
        )
```

---

## Layer 3: 工具层

### todowrite — 全量替换，写即读

- **新建文件**: `agent/tools/todolist/tools/todowrite.py`

```python
@tool("todowrite")
async def todowrite(
    todos: list[dict],
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Update the todo list for the current session (full replacement).

    Pass the COMPLETE list every time.
    Status: pending|in_progress|completed|cancelled.
    Priority: high|medium|low.
    Category (optional): quick|deep|ultrabrain|visual|git|writing.
    Delegation (optional): self|subagent.
    Subagent_id (optional): child_session_key returned by task tool.
    Plan_ref (optional): .omo/plans/*.md path.
    Flow_id (optional): TaskFlow flow id this todo tracks.
    Step_id (optional): TaskFlow step id (e.g. step-2) for DAG status.

    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_run_task(flow_id, task, depends_on=[...]); the blocked/ready/
    dispatched/done status and unlock-on-resume are owned by TaskFlow.
    """
    result = await service.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER
    return output
```

### todoread — 显式读取

- **新建文件**: `agent/tools/todolist/tools/todoread.py`

```python
@tool("todoread")
async def todoread(
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### 工具注册

- **新建文件**: `agent/tools/todolist/tools/__init__.py`

```python
_TODOLIST_TOOLS = [todowrite, todoread]


def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES
    return list(_TODOLIST_TOOLS)
```

### SKILL.md

- **新建文件**: `skills/todolist/SKILL.md`

```markdown
---
name: todolist
description: Session-scoped task tracking with delegation routing and TaskFlow-backed DAG execution. Use for 3+ step work.
scope: main_only
---

# TodoList — 分层任务跟踪

## 何时使用

- 开始 3+ 步骤的复杂工作时，创建 todo 列表
- 每完成一步，更新状态（pending -> in_progress -> completed）
- 全部完成后，用空列表 [] 清除

## 工具

- `todowrite(todos)`: 全量替换当前 todo 列表（每次传完整列表）
- `todoread()`: 从数据库读取当前列表（不确定状态时使用）

## 状态: pending | in_progress | completed | cancelled

## 优先级: high | medium | low

## 委派字段 (E6, 可选):

- category: quick|deep|ultrabrain|visual|git|writing — 路由裁决
- delegation: self|subagent — 是否委派给 subagent
- subagent_id: 派出 subagent 后填入 child_session_key

## DAG 字段（委派给 TaskFlow，可选）:

- plan_ref: .omo/plans/*.md 路径
- flow_id: 关联的 TaskFlow flow id
- step_id: 关联的 TaskFlow step id（如 step-2）

DAG 调度**不在 todolist 内实现**。声明依赖请用
`taskflow_run_task(flow_id, task, depends_on=["step-1"])`；`blocked/ready/dispatched/done`
状态、解锁与并行派发由 TaskFlow 提供（`taskflow_dispatch`、`taskflow_resume`、
`taskflow_wait_all`）。用 `taskflow_summary(flow_id)` 回读步骤状态。

## 规则

- 每次调用 todowrite 传入完整列表，不是增量更新
- 同时只有一个 in_progress 任务
- delegation="subagent" 时，subagent 返回前不得标记 completed
- 有依赖关系的步骤登记到 TaskFlow step 并声明 depends_on，不要自己重算波次
```

---

## Layer 4: 编排执行层 ★（ulw-execute）

- **新建文件**: `skills/ulw-execute/SKILL.md`

这是从 omo 的 `packages/shared-skills/skills/ulw-execute/SKILL.md` 移植的核心编排 skill。适配 sherry_agent 的基础设施（subagent spawn 工具、auto_turn、SQLite 等）。

### 编排流程（5 Phase）

```
Phase 1: Select the plan
  → 读 .omo/boulder.json
  → 列 .omo/plans/*.md
  → 匹配 plan-name 或恢复活跃工作

Phase 2: Create or update Boulder state
  → 写 .omo/boulder.json（session_id 前缀 sherry:）
  → 注册所有 Phase 和 Task 为 todos

Phase 3: Execute the next checkbox
  → 读计划，找到第一个未勾选的 column-0 checkbox
  → 分解为 atomic sub-tasks（一个 worker 一次运行可完成）
  → 把带依赖的 checkbox 登记为 TaskFlow step:
      taskflow_run_task(flow_id, task, depends_on=[...])
  → 依赖未满足的步骤由 TaskFlow 记为 blocked（不派发）
  → 对 ready 步骤调用 taskflow_dispatch(flow_id, step_ids) 并行派发
  → DELEGATE EVERYTHING — 路由每个 sub-task 到 delegation router

Phase 4: Verify and record evidence
  → 5 gates: plan reread → automated verification → manual QA → adversarial QA → cleanup
  → 证据写入 .omo/ledger.jsonl

Phase 5: Mark progress
  → 编辑计划 checkbox: - [ ] → - [x]
  → 重新读计划，确认剩余数减少
  → 追加 task-completed ledger entry
  → 继续下一个 checkbox，不询问是否继续
```

### Delegation Router — 委派路由器

来自 `ulw-execute/SKILL.md:134-148`，适配 sherry_agent 的 subagent 工具：

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

### 并行投递通道决策

来自 `ulw-execute/SKILL.md:100-113`，适配 sherry_agent：

| 拓扑                    | 条件                  | 策略                          |
| ----------------------- | --------------------- | ----------------------------- |
| 独立通道 → 并行 workers | 分离文件，无共享契约  | 一次并行 spawn burst          |
| 有序依赖通道 → 波次串行 | C 需要 A 和 B 先完成  | 等 Wave N 完成后执行 Wave N+1 |
| 重叠通道 → team         | 同模块/契约，并发更快 | 串行化或手动协调              |

---

## Layer 5: DAG 调度层 ★（委派给 TaskFlow）

**本节不再新建调度器。** 原设计中的 `agent/tools/todolist/dag/scheduler.py`、
`agent/tools/todolist/dag/types.py`（以及 `DagScheduler`/`DagNode`/`DagRun`/`wave`/
`frontier`/`critical path`/`bottlenecks` 这套本地类型）**已删除**。DAG 能力由既有
TaskFlow 系统提供（taskflow-dag-phase1，commits `9ce33ef..2824034`），本方案复用它的 API：

| 计划层概念（HTN）              | TaskFlow 提供的对应能力                                                                 |
| ------------------------------ | --------------------------------------------------------------------------------------- |
| Checkbox 之间的依赖            | step 的 `depends_on`（step-id 列表，如 `["step-1"]`，按 id 而非列表位置）                |
| Wave / 波次                    | 不再显式建 wave；依赖满足的步骤状态为 `ready`，可被批量派发（等价于"当前波次"）          |
| 节点状态                       | `status ∈ {blocked, ready, dispatched, done}`（放在 `state_json`，无 DB migration）      |
| Frontier（依赖已满足的可执行项）| `ready` 状态 + `taskflow_dispatch` 校验 `deps_satisfied`，无需本地重算                   |
| 依赖未满足时阻塞               | `taskflow_run_task(..., depends_on=[...])` 只登记 `blocked`，不 spawn                    |
| 完成一个节点解锁后继           | `taskflow_resume` 把对应 step 标 `done` 并 `unlock_dependents`（blocked→ready，不自动派发）|
| 并行派发                       | `taskflow_dispatch(flow_id, step_ids)` 批量派发 ready 步骤（全量校验后顺序 spawn）       |
| 等待并行子会话                 | `taskflow_wait_all(flow_id, timeout_seconds, ...)` 按 flow 范围有界轮询                  |
| 回读 DAG 状态                  | `taskflow_summary(flow_id)` 渲染每步 status/depends_on + 各状态计数                      |

### 计划 → TaskFlow 的落地方式

1. 计划激活时用 `taskflow_create(flow_id, description, initial_state)` 建流。
2. 每个 checkbox/sub-task 用 `taskflow_run_task(flow_id, task, depends_on=[...])` 登记：
   - 无依赖 → 立即 `dispatched`（派出 detached 子会话）；
   - 依赖未满足 → 记为 `blocked` 且**不派发**；未知依赖 id 直接报错且不改状态。
3. 前置步骤结果送达后，`taskflow_resume(flow_id, child_session_key, result)` 注入结果：
   该 step 变 `done`，依赖它的 `blocked` step 解锁为 `ready`（返回新增 ready 的 step id）。
   **resume 不自动派发。**
4. 对返回的 ready step 调用 `taskflow_dispatch(flow_id, step_ids)` 批量并行派发。
5. `taskflow_wait_all(flow_id, ...)` 等本流派发的子会话 settle，再对每个子会话
   `taskflow_resume`，回到第 4 步，直到所有 step `done`。
6. `taskflow_finish(flow_id, summary)` 收尾；中途用 `taskflow_summary` 随时回读。

### step 状态机（TaskFlow 权威定义）

```
blocked   (依赖尚未全部 done；run_task 只登记、不派发)
  → ready   (依赖已满足，等待派发；由 taskflow_resume 解锁)
  → dispatched (已派发 detached 子会话，child_session_key 落库)
  → done    (已通过 taskflow_resume 把子会话结果注入流状态)
```

> **已知限制（phase1）**：`done` 只表示"结果已注入"，不代表子会话执行成功；本阶段没有
> step 级 `failed`/`skipped`，也不做失败感知解锁或重试（属后续 gap #8）。
> `critical path` / `bottlenecks` / 拓扑排序可视化**未实现**，不再作为本方案交付项；
> 若将来需要，应作为 TaskFlow 的能力补充，而不是在 todolist 里重建。

### todolist 层需要调用的 TaskFlow 工具

| 工具                                                              | 用途                                       |
| ----------------------------------------------------------------- | ------------------------------------------ |
| `taskflow_create`                                                 | 为计划建 flow                              |
| `taskflow_run_task(..., depends_on=[...])`                        | 登记步骤并按依赖决定是否派发               |
| `taskflow_dispatch(flow_id, step_ids)`                            | 批量派发 ready 步骤（并行）                |
| `taskflow_wait_all(flow_id, ...)`                                 | 等待本流派发的子会话 settle                |
| `taskflow_resume(flow_id, child_session_key, result)`             | 注入结果、标 done、解锁后继                |
| `taskflow_summary(flow_id)`                                       | 回读每步 status/depends_on 与状态计数      |

> 参考实现见 `skills/builtin/core/taskflow/SKILL.md` 与
> `agent/tools/taskflow/tools/{_shared.py,taskflow_run_task.py,taskflow_dispatch.py,
> taskflow_resume.py,taskflow_wait_all.py,taskflow_summary.py}`。

---

## Layer 6: 压缩保护层 ★

- **修改文件**: `workspace/prompt_builder.py`

sherry_agent 的 `build_system_prompt()` 在每次上下文压缩后都会被 `Summarization` 中间件重新调用。在这里注入当前 todos + boulder 状态 + 知识摘要，天然免疫压缩。

### 注入内容

| Block                      | 数据源                                  | 上下文开销 | 说明                                             |
| -------------------------- | --------------------------------------- | ---------- | ------------------------------------------------ |
| `_build_todo_block()`      | todos.db                                | ~10 行     | 当前 todo 列表 + 状态 + TaskFlow flow/step 关联  |
| `_build_boulder_block()`   | .omo/boulder.json                       | ~5 行      | 活跃工作状态                                     |
| `_build_knowledge_block()` | .omo/knowledge/<plan>/plan-summary.json | ~20 行     | key_failures + key_successes + reusable_patterns |

### 实现

```python
def _build_todo_block(session_id: str) -> str:
    """从 DB 读取当前 todos，注入系统提示词"""
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync

    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        cat = t.get("category", "quick")
        dlg = t.get("delegation", "self")
        tag_parts = [cat]
        if dlg != "self":
            tag_parts.append(dlg)
        # DAG 归属 TaskFlow：只标注关联的 flow/step，不在此重算波次
        flow_id = t.get("flow_id")
        step_id = t.get("step_id")
        if flow_id:
            tag_parts.append(f"flow:{flow_id}")
        if step_id:
            tag_parts.append(step_id)
        tag = f"({', '.join(tag_parts)})"
        lines.append(f"- [{icon.get(t['status'], '○')}] {tag} {t['content']} ({t['priority']})")
    lines.append("\nUpdate todos via the todowrite tool. Pass the COMPLETE list each time.")
    lines.append(
        "Dependency scheduling is owned by TaskFlow; declare depends_on via "
        "taskflow_run_task and read status with taskflow_summary."
    )
    lines.append(
        "Your todo list is tracked by the continuation system. "
        "Incomplete todos will trigger automatic continuation."
    )
    return "\n".join(lines)


def _build_boulder_block(session_id: str) -> str:
    """注入 boulder 状态（活跃工作信息）"""
    import json, os

    boulder_path = ".omo/boulder.json"
    if not os.path.exists(boulder_path):
        return ""
    with open(boulder_path, "r") as f:
        boulder = json.load(f)
    active = boulder.get("works", {}).get(boulder.get("active_work_id", ""), {})
    if not active or active.get("status") != "active":
        return ""
    lines = ["## Active Work"]
    lines.append(f"- Plan: {active.get('active_plan', '?')}")
    lines.append(f"- Status: {active.get('status', '?')}")
    incomplete = [
        t for t in get_todos_sync(session_id) if t["status"] in ("pending", "in_progress")
    ]
    lines.append(f"- Remaining: {len(incomplete)} unchecked checkboxes")
    return "\n".join(lines)
```

### 为什么这比 omo 的 5 层防御更轻量

| omo 的 5 层             | sherry_agent 的方案                                                     |
| ----------------------- | ----------------------------------------------------------------------- |
| Prune 保护列表          | 不需要（todos 在 system prompt 中）                                     |
| 压缩前快照 + 压缩后恢复 | 不需要（system prompt 每次重建从 DB 实时读取）                          |
| 8 段式压缩上下文注入    | 不需要（todos 不在对话历史中）                                          |
| 60s 压缩保护窗口        | 不需要（无续作注入器需要保护）                                          |
| 续作强制器              | 需要，但简化为 after_agent 中间件                                       |
| 知识摘要注入            | 新增 `_build_knowledge_block()` ~25 行（详见 NUDGE_EXTRACTION_PLAN.md） |
| **总计 ~600+ 行代码**   | **~65 行代码（prompt 注入）+ ~130 行（续作中间件）**                    |

---

## Layer 7: 后端 WS 推送层

已在 Layer 2 的 `service.update_todos()` 中实现（`_push_todo_update`）。复用通用 WS（`/sessions/ws`）。

### 消息格式

```json
{
  "event": "todo_updated",
  "session_id": "xxx",
  "content": {
    "todos": [
      {
        "content": "实现登录",
        "status": "completed",
        "priority": "high",
        "flow_id": "login-flow",
        "step_id": "step-1",
        "taskflow_status": "done"
      },
      {
        "content": "写测试",
        "status": "in_progress",
        "priority": "medium",
        "flow_id": "login-flow",
        "step_id": "step-2",
        "taskflow_status": "ready"
      }
    ]
  }
}
```

> `taskflow_status` 是 `taskflow_summary(flow_id)` 回读的 step 状态
> （`blocked|ready|dispatched|done`）；前端不再从 todos.db 计算波次/依赖。

### WS 重连重发

前端 WS 重连后发 `todo_refresh` 消息，后端从 DB 读取并推送：

```python
# server/trigger/core.py 中新增 todo_refresh 消息处理
async def handle_todo_refresh(session_id: str, websocket):
    from agent.tools.todolist.service import TodoService

    todos = await TodoService.get_todos(session_id)
    await websocket.send_text(
        json.dumps({"event": "todo_updated", "session_id": session_id, "content": {"todos": todos}})
    )
```

---

## Layer 8: 前端状态层

- **新建文件**: `client/app/composables/useTodoList.ts`

复用 `useSubagentTasks.ts` 的模块级单例模式。

```typescript
import { ref, computed } from "vue";
import { on, emit } from "~/composables/mitt";
import { useUiStore } from "~/stores/ui";

interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  category?: "quick" | "deep" | "ultrabrain" | "visual" | "git" | "writing";
  delegation?: "self" | "subagent";
  subagent_id?: string | null;
  // DAG 归属 TaskFlow：这里只保存关联 id 与回读的 step 状态
  flow_id?: string | null;
  step_id?: string | null;
  taskflow_status?: "blocked" | "ready" | "dispatched" | "done" | null;
  depends_on?: string[] | null;
}

const todos = ref<Todo[]>([]);
let subscribed = false;

function setupListeners(): void {
  if (subscribed) return;
  subscribed = true;

  on("ws:todo_updated", (payload: any) => {
    todos.value = payload?.content?.todos ?? [];
  });

  on("ws:reconnected", () => {
    emit("ws:send", {
      type: "todo_refresh",
      session_id: currentSessionId.value,
    });
  });
}

const todoState = computed<"hide" | "open" | "close">(() => {
  if (todos.value.length === 0) return "hide";
  const allDone = todos.value.every(
    (t) => t.status === "completed" || t.status === "cancelled",
  );
  return allDone ? "close" : "open";
});

const dockVisible = computed(() => todoState.value === "open");
const doneCount = computed(
  () =>
    todos.value.filter(
      (t) => t.status === "completed" || t.status === "cancelled",
    ).length,
);

// 按 TaskFlow flow 分组显示（DAG 状态来自 taskflow_status，前端不重算依赖）
const flowGroups = computed(() => {
  const order = { blocked: 0, ready: 1, dispatched: 2, done: 3 } as const;
  const grouped: Record<string, Todo[]> = {};
  for (const t of todos.value) {
    const key = t.flow_id ?? "unlinked";
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(t);
  }
  return Object.entries(grouped)
    .map(([flow_id, items]) => ({
      flow_id,
      items: [...items].sort(
        (a, b) =>
          (order[a.taskflow_status ?? "ready"] ?? 1) -
          (order[b.taskflow_status ?? "ready"] ?? 1),
      ),
    }))
    .sort((a, b) => a.flow_id.localeCompare(b.flow_id));
});

export function useTodoList() {
  const uiStore = useUiStore();
  setupListeners();
  return {
    todos,
    flowGroups,
    collapsed: computed(() => uiStore.todoDockCollapsed),
    toggleCollapsed: () => uiStore.toggleTodoDock(),
    dockVisible,
    doneCount,
  };
}
```

---

## Layer 9: UI 组件层

### TodoDock.vue — 按 TaskFlow flow 分组显示

- **新建文件**: `client/app/components/chat/TodoDock.vue`

```vue
<template>
  <Transition name="dock-slide">
    <div v-if="dockVisible" class="todo-dock">
      <div class="dock-header flex items-center justify-between px-3 py-2">
        <span class="text-sm text-color-secondary">
          {{ t("todolist.progress", { done: doneCount, total: todos.length }) }}
        </span>
        <Button text rounded size="small" @click="collapsed = !collapsed">
          <i :class="['pi', collapsed ? 'pi-chevron-down' : 'pi-chevron-up']" />
        </Button>
      </div>
      <div v-show="!collapsed" class="todo-list overflow-y-auto max-h-42">
        <div v-for="group in flowGroups" :key="group.flow_id" class="flow-group">
          <div
            v-if="group.flow_id !== 'unlinked'"
            class="flow-label text-xs text-color-secondary px-3 py-0.5"
          >
            TaskFlow: {{ group.flow_id }}
          </div>
          <TodoItem v-for="(todo, i) in group.items" :key="i" :todo="todo" />
        </div>
      </div>
    </div>
  </Transition>
</template>
```

### TodoItem.vue

- **新建文件**: `client/app/components/chat/TodoItem.vue`

```vue
<template>
  <div class="todo-item flex items-center gap-2 px-3 py-1">
    <Checkbox
      :model-value="todo.status === 'completed'"
      :indeterminate="todo.status === 'in_progress'"
      :readonly="true"
    />
    <span
      v-if="todo.category && todo.category !== 'quick'"
      class="text-xs px-1.5 py-0.5 rounded bg-surface-200 text-color-secondary"
    >
      {{ todo.category }}
    </span>
    <span
      :class="[
        'text-sm',
        todo.status === 'completed' || todo.status === 'cancelled'
          ? 'line-through text-color-secondary'
          : 'text-color',
      ]"
    >
      {{ todo.content }}
    </span>
    <i
      v-if="todo.delegation === 'subagent'"
      class="pi pi-external-link text-xs text-color-secondary"
      v-tooltip="'Delegated to subagent'"
    />
    <span
      v-if="todo.status === 'in_progress'"
      class="pulse-dot w-2 h-2 rounded-full bg-primary animate-pulse"
    />
  </div>
</template>
```

---

## 完整文件清单

| #   | 操作 | 文件路径                                        | 参考来源                                                          |
| --- | ---- | ----------------------------------------------- | ----------------------------------------------------------------- |
| 1   | 新建 | `agent/tools/todolist/__init__.py`              | taskflow/**init**.py                                              |
| 2   | 新建 | `agent/tools/todolist/config.py`                | taskflow/config.py（简化）                                        |
| 3   | 新建 | `agent/tools/todolist/registry/store_sqlite.py` | taskflow/registry/store_sqlite.py + flow_id/step_id 关联列（wave/depends_on 已删除） |
| 4   | 新建 | `agent/tools/todolist/service.py`               | TodoService + EvidenceLedger + WS 推送 + E4 转换屏障              |
| 5   | 新建 | `agent/tools/todolist/evidence_ledger.py`       | omo ulw-execute ledger.jsonl                                      |
| 6   | ~~新建~~ | ~~`agent/tools/todolist/dag/scheduler.py`~~ | **REMOVED — DAG 调度委派给 TaskFlow**（`taskflow_run_task`/`taskflow_dispatch`/`taskflow_resume`/`taskflow_wait_all`） |
| 7   | ~~新建~~ | ~~`agent/tools/todolist/dag/types.py`~~     | **REMOVED — DAG step 类型/状态由 TaskFlow `StepStatus` 提供**（`state_json` 内的 `blocked/ready/dispatched/done`） |
| 8   | 新建 | `agent/tools/todolist/tools/__init__.py`        | taskflow/tools/**init**.py + E2 格式规则                          |
| 9   | 新建 | `agent/tools/todolist/tools/todowrite.py`       | taskflow/tools/taskflow_create.py + fan-out reminder              |
| 10  | 新建 | `agent/tools/todolist/tools/todoread.py`        | 新设计                                                            |
| 11  | 新建 | `agent/tools/todolist/stagnation_tracker.py`    | omo todo-continuation-enforcer constants                          |
| 12  | 新建 | `agent/middlewares/task_intent.py`              | E7 意图识别与引导 (before_model 中间件)                           |
| 13  | 修改 | `agent/tools/__init__.py`                       | 加入 build_todolist_tools                                         |
| 14  | 新建 | `skills/todolist/SKILL.md`                      | skills/taskflow/SKILL.md + TaskFlow DAG 委派说明                  |
| 15  | 新建 | `skills/ulw-execute/SKILL.md`                   | omo ulw-execute/SKILL.md（移植）                                  |
| 16  | 修改 | `workspace/prompt_builder.py`                   | _build_todo_block + _build_boulder_block + _build_knowledge_block |
| 17  | 修改 | `workspace/template/{en,zh}/AGENTS.md`          | E1 orchestrator doctrine + E6 委派指令 + E4 转换屏障              |
| 18  | 新建 | `agent/middlewares/todo_continuation.py`        | E3 续作强制器                                                     |
| 19  | 修改 | `agent/core.py`                                 | 注册 TaskIntentMiddleware + TodoContinuationEnforcer              |
| 20  | 修改 | `client/app/stores/ui.ts`                       | 新增 todoDockCollapsed + persist                                  |
| 21  | 新建 | `client/app/composables/useTodoList.ts`         | useSubagentTasks.ts 模式 + 按 TaskFlow flow 分组                  |
| 22  | 修改 | `client/app/composables/ws.ts`                  | todo_updated 事件分发 + ws:send                                   |
| 23  | 修改 | `server/trigger/core.py`                        | todo_refresh 消息处理                                             |
| 24  | 新建 | `client/app/components/chat/TodoDock.vue`       | PrimeVue + 按 TaskFlow flow 分组                                  |
| 25  | 新建 | `client/app/components/chat/TodoItem.vue`       | PrimeVue + category/delegation 显示                               |
| 26  | 修改 | `client/app/pages/home/index/[sid].vue`         | 插入 TodoDock                                                     |
| 27  | 修改 | `client/app/i18n/locales/{en,zh,ja,ko}.json`    | todolist 域                                                       |

> 强制层细节（E1-E7）见 [TODOLIST_ENFORCEMENT.md](./TODOLIST_ENFORCEMENT.md)
>
> **DAG 执行（2026-09-10 变更）**：文件 #6/#7（`agent/tools/todolist/dag/*`）已删除。
> todolist 层不实现调度器，改为调用既有 TaskFlow 工具家族：
> `taskflow_create` / `taskflow_run_task(..., depends_on=[...])` / `taskflow_dispatch` /
> `taskflow_wait_all` / `taskflow_resume` / `taskflow_summary`
> （实现见 commits `9ce33ef..2824034`，无 DB migration，DAG 字段在 `state_json`）。

---

## 实现顺序

```
Phase 0: TaskFlow DAG（已完成，本方案复用，不再实现）
  0. taskflow-dag-phase1 — depends_on + blocked/ready/dispatched/done +
     taskflow_dispatch + taskflow_wait_all + unlock-on-resume（commit 9ce33ef..2824034）

Phase 1: 后端数据层 + 工具层 + 压缩保护 + 强制语言 + 意图识别 (Layer 1-6 + E1 + E2 + E7 + E4-prompt + E6a/b/c)
  1. store_sqlite.py — DB 表 + CRUD + flow_id/step_id 关联列（DAG 列已删除）
  2. service.py — TodoService + EvidenceLedger + WS 推送 + E4/E6 校验
  3. config.py — 常量
  4. （已删除）dag/types.py + dag/scheduler.py —— DAG 委派给 TaskFlow，无本地调度器
  5. todowrite.py + todoread.py — @tool 定义 + E6a fan-out
  6. tools/__init__.py — build_todolist_tools + E2 格式规则
  7. agent/tools/__init__.py — 注册
  8. SKILL.md (todolist + ulw-execute)
  9. prompt_builder.py — _build_todo_block + _build_boulder_block
  10. AGENTS.md 模板 — E1 orchestrator doctrine + E6c 委派 + E4-prompt
  11. task_intent.py — E7 意图识别 before_model 中间件

Phase 2: Continuation Enforcer (E3) + 转换屏障代码级 (E4-code) — 详见 TODOLIST_ENFORCEMENT.md
  12. stagnation_tracker.py — 停滞检测 + 退避 + abort 检测
  13. todo_continuation.py — after_agent 中间件
  14. agent/core.py — 注册 TaskIntentMiddleware + TodoContinuationEnforcer 中间件
  15. service.py — E4-code: subagent_id 运行检查

Phase 3: 前端通信层 (Layer 7-8)
  16. ws.ts — todo_updated 事件分发 + ws:send
  17. server/trigger/core.py — todo_refresh
  18. useTodoList.ts — 模块单例 + WS + 按 TaskFlow flow 分组

Phase 4: 前端 UI 层 (Layer 9)
  19. TodoItem.vue + TodoDock.vue — PrimeVue + 按 TaskFlow flow 分组 + 显示 taskflow_status
  20. [sid].vue — 布局集成
  21. i18n — 4 语言

Phase 5: Sisyphus 验证 (E5，可选)
  22. 扩展 SubagentCompletionDrainMiddleware — DoneClaim/AdversarialVerify
```

---

## 关键设计决策

| 决策       | 选择                                            | 理由                                                              |
| ---------- | ----------------------------------------------- | ----------------------------------------------------------------- |
| HTN 架构   | Plan → Waves → Checkboxes → Sub-tasks → Workers | omo ulw-execute 完整 HTN 模型                                     |
| DAG 调度   | 复用 TaskFlow（`depends_on` + `blocked/ready/dispatched/done`） | 既有 taskflow-dag-phase1；不再造第二个调度器 |
| 状态持久化 | boulder.json + ledger.jsonl + todos.db + taskflow_registry.db | 工作状态/证据/会话 TODO/DAG 状态分层              |
| 压缩保护   | 系统提示词注入                                  | ~40 行代码，天然免疫压缩                                          |
| 续作强制   | after_agent 中间件 + auto_turn                  | 复用 sherry 已有基础设施                                          |
| 意图识别   | before_model 中间件 + 启发式检测 + 双模式注入   | 参考 omo ultrawork(arming) + ulw-execute-continuation(input hook) |
| 验证契约   | Sisyphus: DoneClaim→AdversarialVerify→FullyDone | omo ulw-execute SKILL.md:184-211                                  |
| 委派路由   | category-based delegation router                | omo ulw-execute SKILL.md:134-148                                  |
| 前端       | Vue 3 + PrimeVue + 按 TaskFlow flow 分组        | sherry_agent 现有 UI 栈；DAG 状态来自 taskflow_summary            |

---

## 与旧方案的对比

| 维度       | 旧方案（折中）       | 新方案（omo + ulw-execute）                                          |
| ---------- | -------------------- | -------------------------------------------------------------------- |
| HTN 分解   | 无（扁平 todo 列表） | Plan → Waves → Checkboxes → Sub-tasks                                |
| 依赖管理   | 无                   | TaskFlow `depends_on` + `blocked/ready/dispatched/done`              |
| 并行执行   | 提示词建议           | `taskflow_dispatch` 批量派发 ready 步骤 + `taskflow_wait_all` 等待   |
| 关键路径   | 无                   | 未实现（phase1 范围外；如需应由 TaskFlow 补充）                      |
| 瓶颈检测   | 无                   | 未实现（phase1 范围外；如需应由 TaskFlow 补充）                      |
| 状态持久化 | todos.db             | boulder.json + ledger.jsonl + todos.db + TaskFlow `state_json`       |
| 验证契约   | 无                   | Sisyphus: DoneClaim→AdversarialVerify→FullyDone                      |
| 编排模型   | LLM 自行编排         | ulw-execute: Orchestrator NEVER implements                           |
| 委派路由   | category 字段        | category-based delegation router                                     |
| 续作强制   | 基础退避             | 退避 + 停滞 + abort 检测 + 恢复模式                                  |
| 意图识别   | 无                   | E7a arming(首次注入/短提醒) + E7b plan-active steering(追加reminder) |
