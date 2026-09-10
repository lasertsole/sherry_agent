# sherry_agent 规划与纪律层 — Planning & Discipline Layer on top of TaskFlow

> 参考来源：oh-my-openagent-dev（omo）
>
> - ulw-execute skill: `packages/shared-skills/skills/ulw-execute/SKILL.md`
> - ulw-plan skill: `packages/shared-skills/skills/ulw-plan/`
> - planner prompt: `packages/prompts-core/prompts/ultrawork/planner.md`
> - mass-ulw protocol: `docs/reference/mass-ulw-protocol.md`
> - todo-continuation-enforcer: omo todo continuation 中间件
> - 日期：2026-09-10（TaskFlow DAG 落地：commits `9ce33ef..2824034`）

## 0. 合并 / 重定位说明（2026-09-10）

本文档由两份旧稿合并并重新定位而成：

| 旧稿 | 处理 | 说明 |
| ---- | ---- | ---- |
| `TODO/TODOLIST_PLAN.md` | 保留并重写 | 核心实现（数据层 / 服务层 / 工具层 / 编排 / 压缩保护 / 前端） |
| `TODO/TODOLIST_ENFORCEMENT.md` | **已删除并合并进本文** | 强制执行层 E1–E7 的全部内容折入 §9，不再单独存在 |

重定位后的主题是 **Agent 规划与纪律层（planning & discipline layer）**，它建立在既有 TaskFlow 系统之上：

- 它**不是任务引擎**，**不是 DAG**，**不实现调度器**。
- 会话级、轻量、系统提示词注入的计划/清单由 `todowrite` / `todoread` 承担，**压缩免疫**。
- 跨会话、可持久、会派发子代理的执行由 **TaskFlow** 承担（`depends_on`、`blocked/ready/dispatched/done`、`taskflow_dispatch`、`taskflow_wait_all`）。
- 两层用 `flow_id` / `step_id` 关联；**同一份状态只有一个权威源**，不跨层复制。

历史说明：旧稿曾计划在 `agent/tools/todolist/` 下自建 DAG 调度器（`dag/types.py`、`dag/scheduler.py` 以及 `DagScheduler`/`DagNode`/`DagRun` 这套类型）。这些**均已移除**，能力全部委派给 TaskFlow。完整清单见 §14。

---

## 1. 定位：与 TaskFlow 的关系

### 1.1 两层分工

| 维度 | 会话计划/清单 | 执行流（DAG） |
| ---- | ------------- | ------------- |
| 归属 | sherry 规划与纪律层（本文） | TaskFlow（既有系统，不属本文交付） |
| 作用范围 | 单个 session，轻量 | 跨 session，durable |
| 状态内容 | todo 的 content / status / priority / category / delegation | step 的 `depends_on` / `status` / `child_session_key` / results |
| 持久化 | `todos.db`（会话级） | `taskflow_registry.db` + flow `state_json` |
| 注入方式 | 系统提示词 block（每次压缩后重建，天然免疫压缩） | 工具返回文本（`taskflow_summary` 回读） |
| 主要职责 | 计划、可见性、纪律强制（E1–E7） | 依赖满足、阻塞/解锁、并行派发、有界等待、结果注入 |
| 是否调度 | 否 | 是（`taskflow_run_task` / `taskflow_dispatch` / `taskflow_wait_all` / `taskflow_resume`） |

一句话：**规划与纪律层负责"想清楚、盯住、逼完成"，TaskFlow 负责"真的跑、跨会话、管依赖"。**

### 1.2 边界：什么住在哪

| 关注点 | 权威源 | 说明 |
| ------ | ------ | ---- |
| todo 是否完成 | `todos.db` | 只有 `todowrite` 能改 todo 状态 |
| step 是否完成 / 依赖是否满足 | TaskFlow `state_json` | 只有 `taskflow_resume` 能把 step 标 `done` 并解锁后继 |
| 子会话是否还在跑 | subagent registry | `get_run_by_child_session_key` + `is_live_unended_run` |
| 计划文件进度（checkbox） | `.omo/plans/*.md` | orchestrator 编辑 `- [ ]` → `- [x]` |
| 执行证据 | `.omo/ledger.jsonl` | `EvidenceLedger` 追加 |
| 活跃工作状态 | `.omo/boulder.json` | 计划激活/恢复 |

### 1.3 关联方式（唯一事实来源）

todo 可携带两个可选字段指向 TaskFlow：

- `flow_id`：关联的 TaskFlow flow id。
- `step_id`：关联的 TaskFlow step id（形如 `step-2`），用于回读该 step 的 DAG 状态。

关联遵循 **不镜像** 原则：

- todo 层**不**复制 `depends_on`、**不**保存波次、**不**缓存 step 状态。
- 需要 DAG 状态时调用只读的 `taskflow_summary(flow_id)`，把 `blocked/ready/dispatched/done` 映射回 UI/提示即可。
- 需要推进 DAG 时调用 TaskFlow 工具，todo 层只更新对应的 todo 状态。

---

## 2. 设计哲学与核心原则

全面采用 omo 的 HTN（分层任务网络）体系：计划文件 → checkbox → 原子 sub-task → 委派 subagent workers → 对抗性验证。

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

这里的"Wave"是**计划文件里的叙述单位**，不是 todolist 的调度数据结构。真正"什么时候能派发下一个"由 TaskFlow 的 `depends_on` + `blocked/ready` 决定。

**核心原则**（`ulw-execute/SKILL.md:6-8`）：

> YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.
> YOU DO NOT WRITE CODE. YOU DO NOT EDIT PRODUCT FILES.
> EVERY unit of implementation MUST be delegated to a spawned subagent.

不依赖模型自觉。通过 7 层强制形成闭环：

```
事前 (消息到达)        事中                        事后
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
│ MANDATORY │  │ + hook 告知         │  └──────────────────────┘
│ 格式规则  │  └────────────────────┘  ┌──────────────────────┐
└──────────┘                            │ E4 转换屏障 (code) ★ │
                                          │ TaskFlow step 状态 + │
                                          │ subagent 存活 → 硬阻断│
                                          └──────────────────────┘
```

一句话逻辑：模型不自觉做计划 → E7 注入引导；模型偷懒停下来 → E3 拉回；模型虚假完成 → E5 阻断；模型自己写代码 → E1 doctrine 威慑；模型提前标完成 → E4 硬阻断。

---

## 3. 架构总览

```
Layer 9  │ UI 组件层         │ TodoDock.vue + TodoItem.vue (PrimeVue)，按 TaskFlow flow 分组（数据来自 taskflow_summary）
Layer 8  │ 前端状态层         │ useTodoList.ts (模块级单例) + 按 flow 分组
Layer 7  │ 实时通信层         │ WS: todo_updated 推送 + todo_refresh 重连重发
Layer 6  │ 压缩保护层 ★      │ build_system_prompt 注入当前 todos + boulder 状态
Layer 5  │ DAG 执行层 ★      │ 由 TaskFlow 提供: depends_on + blocked/ready/dispatched/done（本层不实现）
Layer 4  │ 编排执行层 ★      │ ulw-execute: plan→checkbox→sub-task→worker→verify，调用 TaskFlow
Layer 3  │ 工具层            │ todowrite + todoread（DAG 由 taskflow_* 工具家族执行）
Layer 2  │ 服务层            │ TodoService + EvidenceLedger（DAG 状态查询委派 TaskFlow）
Layer 1  │ 数据存储层         │ todos.db (WAL) + boulder.json + ledger.jsonl + plans/*.md + taskflow_registry.db
─────────│-------------------│
E1       │ 系统提示词强制     │ AGENTS.md: MANDATORY orchestrator doctrine
E2       │ 工具描述强制       │ todowrite docstring 格式规则（DAG 指引指向 TaskFlow）
E3       │ 续作强制器 ★★     │ after_agent 中间件: idle+未完成→自动续作（含退避/停滞/abort/恢复）
E4       │ 转换屏障 ★        │ TaskFlow step 未 done / subagent 运行中禁止标 completed（prompt + code 双保险）
E5       │ Sisyphus 验证 ★★  │ DoneClaim → AdversarialVerify → FullyDone（5 gates）
E6       │ 委派路由 ★        │ #a fan-out + #b category 路由 + #c 委派指令 + #d 转换屏障（对接 taskflow_* 工具）
E7       │ 意图识别器 ★★     │ before_model: E7a arming(无计划+任务意图→注入引导) + E7b plan-active(有计划→追加 reminder)
```

关键标注：**Layer 5 的 DAG 能力不是本层实现的**，它由 TaskFlow 提供（`agent/tools/taskflow/tools/*` + `skills/builtin/core/taskflow/SKILL.md`）。本层只调用、不重建。

---

## 4. 数据层

### 4a. Plan 文件 (.omo/plans/*.md)

计划文件是 checkbox 格式的 Markdown，定义完整的 HTN 分解：

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

### 4b. Boulder 状态 (.omo/boulder.json)

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

`session_id` 前缀用 `sherry:` 以区分 omo 的 `codex:` 前缀。

### 4c. 证据账本 (.omo/ledger.jsonl)

每行一个 JSON 对象，记录每个 checkbox 的执行证据（`ulw-execute/SKILL.md:172-183`）：

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "artifact": ".omo/evidence/xxx.txt", "adversarial_classes": {"stale_state": "not-applicable: no cached artifacts", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
```

### 4d. todos.db — 会话级 TODO 存储

- **新建文件**：`agent/tools/todolist/registry/store_sqlite.py`

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

| 字段 | 说明 |
| ---- | ---- |
| `plan_ref` | 关联的 `.omo/plans/*.md` 文件路径 |
| `flow_id` | 关联的 TaskFlow flow id（DAG 归 TaskFlow，不在 todos 内建） |
| `step_id` | 关联的 TaskFlow step id（如 `step-2`），用于回读 DAG 状态 |

> **设计边界**：todos.db **不持有** `depends_on` / 波次 / step 状态。依赖图、
> `blocked/ready/dispatched/done` 与解锁逻辑全部由 TaskFlow 提供
> （`taskflow_run_task(..., depends_on=[...])`、`taskflow_dispatch`、
> `taskflow_resume` 的 unlock、`taskflow_wait_all`）。todo 只通过 `flow_id`/`step_id`
> 指向对应 flow step，DAG 状态用 `taskflow_summary(flow_id)` 回读。
>
> **已移除**：旧 schema 的 `wave_index` 与 `depends_on` 列**已删除**，`wave_index` 不再
> 作为调度依据（被移除内容见 §14）。

#### CRUD 接口

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

## 5. 服务层

- **新建文件**：`agent/tools/todolist/service.py`

### 5.1 TodoService

```python
class TodoService:
    @staticmethod
    async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
        validated = _validate_todos(todos)
        # E4: 转换屏障 — TaskFlow step 未 done / subagent 仍在运行时禁止标 completed
        for todo in validated:
            if todo["status"] == "completed":
                _assert_transition_allowed(todo)
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

> **已移除**：原 `TodoService.get_wave_frontier()`（基于 `depends_on` + 本地 completed
> 集合计算可执行项）。该职责现由 TaskFlow 的 `deps_satisfied()` / `unlock_dependents()`
> 承担；todolist 层不再持有 DAG 副本，避免双份调度器漂移。

### 5.2 EvidenceLedger

- **新建文件**：`agent/tools/todolist/evidence_ledger.py`

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

### 5.3 WS 推送

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

## 6. 工具层（todowrite / todoread + E2 + E6a）

### 6.1 todowrite — 全量替换，写即读

- **新建文件**：`agent/tools/todolist/tools/todowrite.py`

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

### 6.2 todoread — 显式读取

- **新建文件**：`agent/tools/todolist/tools/todoread.py`

```python
@tool("todoread")
async def todoread(
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### 6.3 工具注册（E2 注入点）

- **新建文件**：`agent/tools/todolist/tools/__init__.py`

```python
_TODOLIST_TOOLS = [todowrite, todoread]


def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES  # E2 格式规则，详见 §9
    return list(_TODOLIST_TOOLS)
```

E6a 的 fan-out reminder（`_FANOUT_REMINDER`，每 session 首次 todowrite 追加）与 E2 的完整规则文本见 §9。

### 6.4 SKILL.md

- **新建文件**：`skills/builtin/core/todolist/SKILL.md`（必须在 allowed root `skills/builtin/` 下，否则 loader 忽略；见 `config/path.py:41-43`）

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

## 7. 编排执行层（ulw-execute skill；调用 TaskFlow）

- **新建文件**：`skills/builtin/core/ulw-execute/SKILL.md`（同 allowed root 约束）

这是从 omo 的 `packages/shared-skills/skills/ulw-execute/SKILL.md` 移植的核心编排 skill，适配 sherry_agent 的基础设施（subagent spawn 工具、auto_turn、SQLite 等）。

### 7.1 编排流程（5 Phase）

```
Phase 1: Select the plan
  → 读 .omo/boulder.json
  → 列 .omo/plans/*.md
  → 匹配 plan-name 或恢复活跃工作

Phase 2: Create or update Boulder state
  → 写 .omo/boulder.json（session_id 前缀 sherry:）
  → 注册所有 Phase 和 Task 为 todos

Phase 3: Execute the next checkbox（调度全部交给 TaskFlow）
  → 读计划，找到第一个未勾选的 column-0 checkbox
  → 分解为 atomic sub-tasks（一个 worker 一次运行可完成）
  → 把带依赖的 checkbox 登记为 TaskFlow step:
      taskflow_run_task(flow_id, task, depends_on=[...])
  → 依赖未满足的步骤由 TaskFlow 记为 blocked（不派发）
  → 对 ready 步骤调用 taskflow_dispatch(flow_id, step_ids) 并行派发
  → taskflow_wait_all(flow_id) 等本流派发的子会话 settle
  → taskflow_resume(flow_id, child_session_key, result) 注入结果并解锁后继
  → DELEGATE EVERYTHING — 路由每个 sub-task 到 delegation router（见 §9 E6）

Phase 4: Verify and record evidence
  → 5 gates: plan reread → automated verification → manual QA → adversarial QA → cleanup
  → 证据写入 .omo/ledger.jsonl

Phase 5: Mark progress
  → 编辑计划 checkbox: - [ ] → - [x]
  → 重新读计划，确认剩余数减少
  → 追加 task-completed ledger entry
  → 继续下一个 checkbox，不询问是否继续
```

### 7.2 计划 → TaskFlow 的落地方式

1. 计划激活时用 `taskflow_create(flow_id, description, initial_state)` 建流。
2. 每个 checkbox/sub-task 用 `taskflow_run_task(flow_id, task, depends_on=[...])` 登记：
   - 无依赖 → 立即 `dispatched`（派出 detached 子会话）；
   - 依赖未满足 → 记为 `blocked` 且**不派发**；未知依赖 id 直接报错且不改状态。
3. 前置步骤结果送达后，`taskflow_resume(flow_id, child_session_key, result)` 注入结果：
   该 step 变 `done`，依赖它的 `blocked` step 解锁为 `ready`（返回新增 ready 的 step id）。
   **resume 不自动派发。**
4. 对返回的 ready step 调用 `taskflow_dispatch(flow_id, step_ids)` 批量并行派发。
5. `taskflow_wait_all(flow_id, timeout_seconds, poll_interval_seconds)` 等本流派发的子会话
   settle，再对每个子会话 `taskflow_resume`，回到第 4 步，直到所有 step `done`。
6. `taskflow_finish(flow_id, summary)` 收尾；中途用 `taskflow_summary` 随时回读。

### 7.3 step 状态机（TaskFlow 权威定义）

```
blocked   (依赖尚未全部 done；run_task 只登记、不派发)
  → ready   (依赖已满足，等待派发；由 taskflow_resume 解锁)
  → dispatched (已派发 detached 子会话，child_session_key 落库)
  → done    (已通过 taskflow_resume 把子会话结果注入流状态)
```

> **已知限制（phase1）**：`done` 只表示"结果已注入"，不代表子会话执行成功；本阶段没有
> step 级 `failed`/`skipped`，也不做失败感知解锁或重试（属后续 gap #8，见
> `LONG_RUNNING_TASK_GAP_ANALYSIS.md`）。`critical path` / `bottlenecks` / 拓扑排序可视化
> **未实现**，不再作为本方案交付项；若将来需要，应作为 TaskFlow 的能力补充，而不是在
> todolist 里重建。

### 7.4 todolist 层需要调用的 TaskFlow 工具

| 工具 | 用途 |
| ---- | ---- |
| `taskflow_create(flow_id, description, initial_state)` | 为计划建 flow |
| `taskflow_run_task(flow_id, task, label, depends_on=[...], expected_revision)` | 登记步骤并按依赖决定是否派发 |
| `taskflow_dispatch(flow_id, step_ids, expected_revision)` | 批量派发 ready 步骤（并行） |
| `taskflow_wait_all(flow_id, timeout_seconds, poll_interval_seconds)` | 等待本流派发的子会话 settle |
| `taskflow_resume(flow_id, child_session_key, result, expected_revision)` | 注入结果、标 done、解锁后继（幂等） |
| `taskflow_summary(flow_id)` | 回读每步 status/depends_on 与状态计数 |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | 终态收尾 |
| `taskflow_set_waiting(flow_id, wait_reason, expected_revision)` | 流级等待 |

参考实现见 `skills/builtin/core/taskflow/SKILL.md` 与
`agent/tools/taskflow/tools/{_shared.py,taskflow_run_task.py,taskflow_dispatch.py,
taskflow_resume.py,taskflow_wait_all.py,taskflow_summary.py}`。

### 7.5 计划层概念 → TaskFlow 能力映射

| 计划层概念（HTN） | TaskFlow 提供的对应能力 |
| ----------------- | ----------------------- |
| Checkbox 之间的依赖 | step 的 `depends_on`（step-id 列表，如 `["step-1"]`，按 id 而非列表位置） |
| Wave / 波次 | 不再显式建 wave；依赖满足的步骤状态为 `ready`，可被批量派发（等价于"当前波次"） |
| 节点状态 | `status ∈ {blocked, ready, dispatched, done}`（放在 `state_json`，无 DB migration） |
| Frontier（依赖已满足的可执行项） | `ready` 状态 + `taskflow_dispatch` 校验 `deps_satisfied`，无需本地重算 |
| 依赖未满足时阻塞 | `taskflow_run_task(..., depends_on=[...])` 只登记 `blocked`，不 spawn |
| 完成一个节点解锁后继 | `taskflow_resume` 把对应 step 标 `done` 并 `unlock_dependents`（blocked→ready，不自动派发） |
| 并行派发 | `taskflow_dispatch(flow_id, step_ids)` 批量派发 ready 步骤（全量校验后顺序 spawn） |
| 等待并行子会话 | `taskflow_wait_all(...)` 按 flow 范围有界轮询 |
| 回读 DAG 状态 | `taskflow_summary(flow_id)` 渲染每步 status/depends_on + 各状态计数 |

### 7.6 并行投递通道决策（来自 `ulw-execute/SKILL.md:100-113`）

| 拓扑 | 条件 | 策略 |
| ---- | ---- | ---- |
| 独立通道 → 并行 workers | 分离文件，无共享契约 | 一次并行 spawn burst（`taskflow_dispatch` 批量派发） |
| 有序依赖通道 → 波次串行 | C 需要 A 和 B 先完成 | 等前置 step `done` 解锁后执行（`depends_on` + `resume` unlock） |
| 重叠通道 → team | 同模块/契约，并发更快 | 串行化或手动协调 |

委派路由（category / delegation 字段、fan-out、委派指令）见 §9 E6。

---

## 8. 压缩保护层（prompt 注入）

- **修改文件**：`workspace/prompt_builder.py`

sherry_agent 的 `build_system_prompt()` 在每次上下文压缩后都会被 `Summarization` 中间件重新调用。在这里注入当前 todos + boulder 状态 + 知识摘要，天然免疫压缩。

### 8.1 注入内容

| Block | 数据源 | 上下文开销 | 说明 |
| ----- | ------ | ---------- | ---- |
| `_build_todo_block()` | todos.db | ~10 行 | 当前 todo 列表 + 状态 + TaskFlow flow/step 关联 |
| `_build_boulder_block()` | .omo/boulder.json | ~5 行 | 活跃工作状态 |
| `_build_knowledge_block()` | .omo/knowledge/<plan>/plan-summary.json | ~20 行 | key_failures + key_successes + reusable_patterns |

### 8.2 实现

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
    lines.append(
        "Completion is verified by the Sisyphus contract — unverified claims will be rejected."
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

`_build_todo_block()` 末尾的两条追加句是 E1 的"hook 存在性告知"（心理威慑），与 §9 E1 一致。

### 8.3 为什么这比 omo 的 5 层防御更轻量

| omo 的 5 层 | sherry_agent 的方案 |
| ----------- | ------------------- |
| Prune 保护列表 | 不需要（todos 在 system prompt 中） |
| 压缩前快照 + 压缩后恢复 | 不需要（system prompt 每次重建从 DB 实时读取） |
| 8 段式压缩上下文注入 | 不需要（todos 不在对话历史中） |
| 60s 压缩保护窗口 | 不需要（无续作注入器需要保护） |
| 续作强制器 | 需要，但简化为 after_agent 中间件（E3） |
| 知识摘要注入 | 新增 `_build_knowledge_block()` ~25 行（详见 NUDGE_EXTRACTION_PLAN.md） |
| **总计 ~600+ 行代码** | **~65 行代码（prompt 注入）+ ~130 行（续作中间件）** |

---

## 9. 强制执行层 E1–E7（去重后的完整版）

> 本节是旧 `TODOLIST_ENFORCEMENT.md` 的完整内容，已去重并区分"本层实现"与"委派给 TaskFlow"。

### E1: 系统提示词强制 — orchestrator doctrine + 事前预防

- **修改文件**：`workspace/template/{en,zh}/AGENTS.md`
- **代码量**：0 行代码（纯文本编辑）
- **阶段**：Phase A

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

**hook 存在性告知**：`_build_todo_block()` 末尾追加两条（见 §8.2）——续作系统会追踪未完成 todo、Sisyphus 会拒绝未验证的完成声明。

---

### E2: 工具描述强制 — 格式 + 委派规则

- **修改文件**：`agent/tools/todolist/tools/todowrite.py`（docstring）+ `agent/tools/todolist/tools/__init__.py`（builder）
- **代码量**：~15 行
- **阶段**：Phase B

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

### E3: 续作强制器 — 事后闭环核心 ★★

这是最关键的强制层：**不依赖模型自觉**。turn 结束后如果有未完成 todo，系统自动注入续作消息把 LLM 拉回来。

#### 与 omo todo-continuation-enforcer 的对应关系

| omo 机制 | sherry_agent 适配 | 说明 |
| -------- | ----------------- | ---- |
| `session.idle` event listener | `session_state.py:detect_state()` 三态检测 | sherry 已有 |
| 2.5s countdown timer | `auto_turn.py` 的 fire-and-forget + `_watch_user_takeover()` | sherry 已有等价 |
| `CONTINUATION_COOLDOWN_MS` | `_BASE_COOLDOWN_S = 2.0` | 退避基础冷却 |
| `MAX_CONSECUTIVE_FAILURES = 3` | `_MAX_STAGNATION = 3` | 停滞阈值 |
| `MAX_STAGNATION_COUNT` | 同上 | 连续无变化上限 |
| `FAILURE_RESET_WINDOW_MS` | `_FAILURE_RESET_WINDOW_S = 300` | 失败计数重置窗口 |
| user message cancel | `_watch_user_takeover()` 0.5s 轮询 | sherry 已有 |
| assistant activity cancel | `detect_state() == answering` | sherry 已有 |
| tool execution cancel | subagent 仍在运行检测 | E4 转换屏障 |
| abort error cancel | `_is_abort_error()` 检测 | 新增 |
| exponential backoff | `cooldown = min(base * 2^min(failures, 5), max)` | 已有 |
| recovery mode | `_RECOVERY_PROMPT` | 新增 |
| `_INFLIGHT` idempotency | `auto_turn.py:_INFLIGHT` dict | sherry 已有 |

#### sherry 已有的基础设施

| 能力 | 状态 | 位置 |
| ---- | ---- | ---- |
| 空闲检测 | ✅ | `session_state.py:detect_state()` 三态（ws_task/answering/idle） |
| idle 消息注入 | ✅ | `auto_turn.py:maybe_trigger_auto_turn()` fire-and-forget |
| 用户接管保护 | ✅ | `auto_turn.py:_watch_user_takeover()` 0.5s 轮询 |
| 幂等性 | ✅ | `auto_turn.py:_INFLIGHT` dict 防重复 |
| busy 消息排队 | ✅ | `steering_queue.py:enqueue_steering()` + `SubagentCompletionDrainMiddleware` |
| post-turn 钩子 | ❌ 需新建 | `after_agent` 中间件 |
| 停滞检测 | ❌ 需新建 | stagnation_tracker |
| 退避冷却 | ❌ 需新建 | stagnation_tracker |
| abort 检测 | ❌ 需新建 | stagnation_tracker |
| 恢复模式 | ❌ 需新建 | stagnation_tracker |

#### 新建文件 1: `agent/tools/todolist/stagnation_tracker.py`

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

#### 新建文件 2: `agent/middlewares/todo_continuation.py`

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

#### 修改文件: `agent/core.py`

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

#### E3 工作流程

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
      → 构建 continuation prompt（含完整 todo 状态 + flow/step 关联）
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _run_auto_turn() → _drive_turn() → async_generate()
          → _watch_user_takeover() 0.5s 轮询用户接管
      → 用户发消息 → detect_state() 变 busy → 取消续作 → reset()
```

#### E3 文件清单

| # | 操作 | 文件路径 | 代码量 |
| - | ---- | -------- | ------ |
| 1 | 新建 | `agent/tools/todolist/stagnation_tracker.py` | ~90 行 |
| 2 | 新建 | `agent/middlewares/todo_continuation.py` | ~110 行 |
| 3 | 修改 | `agent/core.py` | ~2 行（注册中间件） |

---

### E4: 转换屏障 — 双保险（TaskFlow step 状态 + subagent 存活）

#### Phase 1: Prompt 级

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

#### Phase 2: 代码级硬阻断

- **修改文件**：`agent/tools/todolist/service.py`
- **代码量**：~25 行
- **阶段**：Phase A（prompt 部分）+ Phase B（code 部分）

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

**关键 API**（已确认存在于 sherry_agent）：

| 方法 | 文件路径 | 说明 |
| ---- | -------- | ---- |
| `get_run_by_child_session_key(child_session_key)` | `agent/tools/subagent/registry/queries.py:55` | 按 child session key 查找 run record |
| `has_run_ended(run)` | `agent/tools/subagent/registry/helpers.py:53` | True = TERMINAL（已结束） |
| `is_live_unended_run(run)` | `agent/tools/subagent/registry/helpers.py:48` | True = RUNNING/INTERRUPTED（仍在运行） |

所有函数从 `agent.tools.subagent.registry` 包统一导出（`__init__.py`）。

> E4 的 step 状态来源是 TaskFlow（`blocked/ready/dispatched/done`，见
> `skills/builtin/core/taskflow/SKILL.md`），todolist 层不持有 DAG 副本。

---

### E5: Sisyphus 完成契约 ★★

来自 omo `ulw-execute/SKILL.md:184-211`，这是对"虚假完成"的最终防线。

#### 完成契约三阶段

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

#### 实现方式

- **修改文件**：`agent/middlewares/subagent_completion_drain.py`（扩展）
- **新建文件**：`agent/tools/todolist/verifier.py`
- **代码量**：~80 行
- **阶段**：Phase A（无子代理场景可后置）

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

#### E5 文件清单

| # | 操作 | 文件路径 | 代码量 |
| - | ---- | -------- | ------ |
| 1 | 新建 | `agent/tools/todolist/verifier.py` | ~80 行 |
| 2 | 修改 | `agent/middlewares/subagent_completion_drain.py` | ~15 行 |

---

### E6: 委派路由 — Todo 与 Subagent 联动 ★

参考 omo ulw-execute 的 delegation router（`SKILL.md:134-148`），适配到 sherry_agent。

**核心目标**：在 TodoList 执行过程中，让 LLM 知道何时该派 subagent、派哪类 subagent、何时可以标记完成。

> **DAG 交接**：委派产生的子会话由既有 TaskFlow 追踪。带依赖的步骤用
> `taskflow_run_task(flow_id, task, depends_on=[...])` 登记，依赖未满足时 step 为 `blocked`
> 且不派发；结果注入用 `taskflow_resume`（标 `done` 并解锁后继）；并行派发用
> `taskflow_dispatch`；等待用 `taskflow_wait_all`。E6 只负责"路由到哪个 category/是否委派"，
> 不负责调度，DAG 状态一律以 TaskFlow 为准。

#### 生命周期

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

#### E6a: Fan-out Reminder（#a）— 事中触发

- **修改文件**：`agent/tools/todolist/tools/todowrite.py`
- **代码量**：~10 行
- **阶段**：Phase B

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

#### E6b: Category + Delegation 字段（#b + #4a）— Schema + 校验

- **修改文件**：`store_sqlite.py`（schema）、`service.py`（校验）、`tools/__init__.py`（E2 工具描述）、`prompt_builder.py`（§8 显示）
- **代码量**：~30 行
- **阶段**：Phase B

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

#### E6c: Delegation Instruction（#c）— 纯 prompt

已在 E1 的 AGENTS.md 中包含 Delegation 小节。

#### E6d: Transition Barrier（#d）— 见 E4

已在 E4 中详述（prompt 级 + 代码级硬阻断）。屏障的判定来源是 **TaskFlow step 状态**
（`blocked/ready/dispatched/done`，DAG 归 TaskFlow）+ **subagent registry 存活检测**
（`get_run_by_child_session_key` / `is_live_unended_run`），todolist 层不再自建 DAG 状态。

#### E6 委派路由表

来自 `ulw-execute/SKILL.md:134-148`：

| Category | 路由到 | 说明 |
| -------- | ------ | ---- |
| `quick` (low) | subagent spawn (default model) | 机械、单文件、样板、配置/copy — 默认 |
| `deep` (high) | subagent spawn (reasoning model) | 复杂调试、研究密集型或跨模块工作 |
| `ultrabrain` (high) | subagent spawn (最强 model) | 一个真正困难的逻辑问题 |
| `visual` (medium) | subagent spawn | 前端、UI/UX、样式、动画 |
| `git` (low) | subagent spawn | git 操作 |
| `writing` (low) | subagent spawn | 文档和散文 |

Sizing 决策（每个 checkbox，dispatch 前）：

- **可拆分的工作拆分**：当 checkbox 分解为独立片段时，dispatch 为一批 `quick` worker 并行。
- **内聚的困难工作保持整体**：当拆分会切断共享推理时，发整体给 `deep` 或 `ultrabrain`。

#### E6 文件清单

| # | 操作 | 文件路径 | 机制 | 代码量 | 阶段 |
| - | ---- | -------- | ---- | ------ | ---- |
| 1 | 修改 | `agent/tools/todolist/registry/store_sqlite.py` | #b schema | ~5 行 | Phase B |
| 2 | 修改 | `agent/tools/todolist/service.py` | #b 校验 + #d-code | ~20 行 | Phase B |
| 3 | 修改 | `agent/tools/todolist/tools/todowrite.py` | #a fan-out | ~10 行 | Phase B |
| 4 | 修改 | `agent/tools/todolist/tools/__init__.py` | E2 描述扩展 | ~5 行 | Phase B |
| 5 | 修改 | `workspace/template/{en,zh}/AGENTS.md` | #c+#d-prompt | 0 行 | Phase A |
| 6 | 修改 | `workspace/prompt_builder.py` | #b 显示 | ~10 行 | Phase A |
| 7 | 修改 | `client/app/components/chat/TodoItem.vue` | 前端显示 | ~20 行 | Phase C |

---

### E7: 意图识别与引导 — 事前触发器 ★★

这是解决"模型不自觉调用计划工具和技能"问题的核心层。

**问题**：E1 系统提示词说了 MANDATORY，但 LLM 在长系统提示词中遵守指令的可靠性很差。尤其当用户问题看起来简单直接时，模型会直接开始写代码而不是先做计划。技能描述虽然注入在系统提示词的 `<available_skills>` XML 中，但模型不会主动去读 SKILL.md 文件。

#### omo 的实际架构（双组件模型）

omo 用 **两个独立组件** 处理不同场景（而非单一中间件）：

| omo 组件 | 触发条件 | input hook 行为 | agent_end hook 行为 |
| -------- | -------- | --------------- | ------------------- |
| `ultrawork` (arming) | 用户消息含 `ulw`/`ultrawork` 关键词 | 首次：注入完整 17KB 指令；已 armed：注入短提醒 | (无) |
| `ulw-execute-continuation` | boulder work 活跃（有计划） | **追加** steering reminder 到用户消息 | 注入完整续作指令（含计划状态、下一个 checkbox） |

关键设计点：

1. **ultrawork 的 once-per-session arming**（`ultrawork/index.ts:232-273`）：
   - `armedSessionIds: Set<string>` — 每个 session 只注入一次完整指令。
   - 后续触发只注入短提醒: `"ultrawork mode is already armed for this session"`。
   - 压缩后 re-arm: `session_compact` event → 清除 armed 标记 → 下次触发重新注入完整指令。
   - 已有 `<ultrawork-mode>` 标签的消息 → markArmed 但不注入（避免重复）。
2. **ulw-execute-continuation 的 input hook**（`ulw-execute-continuation/index.ts:42-61`）：
   - 检测 `findContinuableBoulderWork(cwd, sessionId)` — boulder 有 active/paused work。
   - 如果 continuable → `action: "transform"` — **追加** reminder 到用户消息末尾。
   - **每条用户消息都会追加**（非 once-per-session）。
3. **ulw-execute-continuation 的 agent_end hook**（`index.ts:64-108`）：
   - 检测 continuable work → 注入完整续作指令。
   - `CONTINUATION_LIMIT = 8` — 最多连续续作 8 次。
   - `lastSignature` 停滞检测: `work_id:updated_at:completed/total`，无变化则跳过。
4. **优先级**（`ulw-loop/index.ts:104-106`）：
   - 如果 boulder continuation 活跃 → ulw-loop **跳过**（`reason: "boulder-continuation-active"`）。

#### sherry_agent 适配方案

omo 用 `ulw` 关键词触发 arming，sherry_agent 的用户不会输入 `ulw`。因此 sherry 需要 **隐式意图检测** + **双模式注入**：

- **E7a: 首次 arming**（无计划 + 任务意图）→ 注入完整 orchestrator 引导 prompt。
- **E7b: 计划活跃 steering**（有计划 + 用户消息）→ 追加短 reminder。

| 场景 | omo 组件 | sherry E7 模式 | 行为 |
| ---- | -------- | -------------- | ---- |
| 用户说"实现登录"，无计划 | ultrawork (first_arm) | **E7a** | 注入完整引导 prompt |
| 已 armed + 用户说"再修个 bug" | ultrawork (remention) | **E7a 轻量** | 注入短提醒 |
| 有活跃计划 + 用户发任何消息 | ulw-execute-continuation (input) | **E7b** | 追加 plan-active reminder |
| 有活跃计划 + turn 结束 | ulw-execute-continuation (agent_end) | **E3** | E3 续作强制器已覆盖 |
| 用户问"什么是 REST API?" | (无触发) | **跳过** | 非任务意图 |
| 压缩后 | ultrawork (post_compact_rearm) | **E7a 重新 armed** | 系统提示词重建 = 重新 arming |

#### 设计方案

- **新建文件**：`agent/middlewares/task_intent.py`
- **代码量**：~160 行
- **阶段**：Phase A

#### 意图检测策略

轻量级启发式检测，不调用 LLM（零延迟、零成本）：

```python
import re

_TASK_KEYWORDS = {
    # English
    "implement", "fix", "create", "add", "refactor", "build", "deploy", "test",
    "update", "migrate", "write", "setup", "configure", "integrate", "optimize",
    "debug", "resolve", "enhance", "rewrite", "convert",
    # Chinese
    "实现", "修复", "创建", "添加", "重构", "构建", "部署", "测试", "更新",
    "迁移", "编写", "设置", "配置", "集成", "优化", "调试", "解决", "增强",
    "重写", "转换",
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

#### 双模式引导 prompt

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

#### 中间件实现

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

#### 修改: `agent/core.py`

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

#### 压缩后 re-arm

Summarization 中间件压缩成功后，调用 `rearm_after_compact(session_id)` 清除 armed 标记。下次用户消息触发 E7a 时重新注入完整引导 prompt（参考 omo `session_compact` event → `rearmOnCompact`）。

```python
# 在 Summarization 中间件或 TodoContinuationEnforcer 的 aafter_agent 中:
from agent.middlewares.task_intent import rearm_after_compact

if compression_happened:
    rearm_after_compact(session_id)
```

#### E7 工作流程

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

#### E7 文件清单

| # | 操作 | 文件路径 | 代码量 |
| - | ---- | -------- | ------ |
| 1 | 新建 | `agent/middlewares/task_intent.py` | ~160 行 |
| 2 | 修改 | `agent/core.py` | ~2 行（注册中间件） |

#### E7 与其他层的协作

| 场景 | E7 模式 | 行为 | 其他层行为 |
| ---- | ------- | ---- | ---------- |
| 首次任务请求，无计划 | E7a | **注入完整引导** → 模型加载 skill + 创建 todo | E1 持续提示 MANDATORY |
| 已 armed + 新任务请求 | E7a | **注入短提醒** → 不重复完整 prompt | E1 持续提示 |
| 有活跃计划 + 用户发消息 | E7b | **追加 plan-active reminder** → 引导读 boulder.json + 计划 | E1 系统提示词有 todo block |
| 有活跃计划 + turn 结束 | (E3) | E7 不干预（E7b 只在 before_model 运行） | E3 续作注入完整计划状态 |
| 续作注入（E3 触发的 turn） | (跳过) | _is_system_directive 过滤 → 不触发 E7 | E3 续作 prompt 驱动模型 |
| 用户追问"进度怎么样了" | (跳过) | 非任务意图 → 不注入 | 模型直接回答 |
| 压缩后 + 新任务请求 | E7a | rearm → 重新注入完整引导 | E1 系统提示词已重建 |
| 模型忽略引导，直接写代码 | (已注入) | 模型仍不遵守 | E4 转换屏障 + E5 Sisyphus 兜底 |
| 简单问题"2+2=?" | (跳过) | 非任务 → 不注入 | 模型直接回答 |

#### 防循环设计

E7 和 E3 之间可能形成循环。防循环机制：

1. **E7b 优先于 E7a** — 有活跃计划时走 E7b（追加 reminder），不走 E7a（不注入完整引导）。
2. **E7a once-per-session** — `_armed_sessions` Set，已 armed 只注入短提醒。
3. **E7b 只在 before_model 运行** — E3 的 after_agent 续作不会触发 E7b。
4. **`_is_system_directive()`** — 过滤掉 E3/E7 注入的消息，不误判为用户消息。
5. **E3 有退避冷却** — 即使 E3 误触发续作，退避机制限制频率。
6. **压缩后 re-arm** — 压缩清除 armed 标记，但 E1 系统提示词同时重建（todo block 仍在）。

#### 强制机制适配性评估

| 层级 | 适合度 | 代码量 | 难度 | 阶段 | sherry 基础设施复用 |
| ---- | ------ | ------ | ---- | ---- | ------------------- |
| E1 系统提示词 | 完全适合 | 0 行代码 | 极低 | Phase A | AGENTS.md 已加载进 prompt |
| E2 工具描述 | 完全适合 | ~15 行 | 低 | Phase B | builder 模式已有 |
| E3 续作强制器 | 适合 | ~200 行 | 中 | Phase A | 复用 auto_turn.py 全套基础设施 |
| E4 转换屏障 | 完全适合 | ~20 行 | 低 | Phase A/B | 复用 subagent registry 查询 API + TaskFlow step 状态 |
| E5 Sisyphus 验证 | 适合 | ~95 行 | 中 | Phase A | 复用 SubagentCompletionDrain |
| E6 委派路由 | 适合 | ~75 行 | 中 | Phase B | 复用 subagent registry 查询 API |
| E7 意图识别器 | 完全适合 | ~160 行 | 低 | Phase A | 复用 before_model + 消息注入模式 |

#### 不适合 sherry_agent 的 omo 机制

| omo 机制 | 为什么不适合 / 如何适配 |
| -------- | ----------------------- |
| ultrawork mode 系统 + `<ultrawork-mode>` 标签 | 适配为 E7a once-per-session arming（`_armed_sessions` Set） |
| ulw-execute-continuation input hook | 适配为 E7b plan-active steering（`_has_active_boulder()` → 追加 reminder） |
| Bootstrap todo 拦截（tool.execute.before） | sherry 无此 hook；且系统提示词注入已避免此问题 |
| 8 段式压缩上下文注入 | sherry 的系统提示词注入已天然免疫压缩，不需要 |
| 60s 压缩保护窗口 | 无续作注入器需要保护，不需要 |

> 注意：旧方案中"完成门控（plan 文件复选框）不适合 sherry"的判断已被推翻——本方案引入了 `.omo/plans/*.md` 计划文件 + boulder 状态 + 证据账本，完成门控现在是核心机制。

---

## 10. 前端（TodoDock / TodoItem，按 flow 分组）

### 10.1 Layer 8: 前端状态层

- **新建文件**：`client/app/composables/useTodoList.ts`

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

### 10.2 Layer 9: UI 组件层

#### TodoDock.vue — 按 TaskFlow flow 分组显示

- **新建文件**：`client/app/components/chat/TodoDock.vue`

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

#### TodoItem.vue

- **新建文件**：`client/app/components/chat/TodoItem.vue`

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

### 10.3 WS 消息格式与重连重发

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

WS 重连后发 `todo_refresh` 消息，后端从 DB 读取并推送：

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

## 11. 完整文件清单

| # | 操作 | 文件路径 | 参考来源 |
| - | ---- | -------- | -------- |
| 1 | 新建 | `agent/tools/todolist/__init__.py` | taskflow 包结构 |
| 2 | 新建 | `agent/tools/todolist/config.py` | taskflow/config.py（简化） |
| 3 | 新建 | `agent/tools/todolist/registry/store_sqlite.py` | taskflow/registry/store_sqlite.py + flow_id/step_id 关联列 |
| 4 | 新建 | `agent/tools/todolist/service.py` | TodoService + EvidenceLedger + WS 推送 + E4 转换屏障 |
| 5 | 新建 | `agent/tools/todolist/evidence_ledger.py` | omo ulw-execute ledger.jsonl |
| 6 | 新建 | `agent/tools/todolist/tools/__init__.py` | taskflow/tools/__init__.py + E2 格式规则 |
| 7 | 新建 | `agent/tools/todolist/tools/todowrite.py` | taskflow/tools/taskflow_create.py + E6a fan-out reminder |
| 8 | 新建 | `agent/tools/todolist/tools/todoread.py` | 新设计 |
| 9 | 新建 | `agent/tools/todolist/stagnation_tracker.py` | omo todo-continuation-enforcer constants |
| 10 | 新建 | `agent/tools/todolist/verifier.py` | E5 Sisyphus 5-gate |
| 11 | 新建 | `agent/middlewares/task_intent.py` | E7 意图识别与引导 (before_model 中间件) |
| 12 | 新建 | `agent/middlewares/todo_continuation.py` | E3 续作强制器 (after_agent 中间件) |
| 13 | 修改 | `agent/tools/__init__.py` | 加入 build_todolist_tools |
| 14 | 新建 | `skills/builtin/core/todolist/SKILL.md` | skills/builtin/core/taskflow/SKILL.md + TaskFlow DAG 委派说明 |
| 15 | 新建 | `skills/builtin/core/ulw-execute/SKILL.md` | omo ulw-execute/SKILL.md（移植） |
| 16 | 修改 | `workspace/prompt_builder.py` | _build_todo_block + _build_boulder_block + _build_knowledge_block |
| 17 | 修改 | `workspace/template/{en,zh}/AGENTS.md` | E1 orchestrator doctrine + E6 委派指令 + E4 转换屏障 |
| 18 | 修改 | `agent/core.py` | 注册 TaskIntentMiddleware + TodoContinuationEnforcer |
| 19 | 修改 | `agent/middlewares/subagent_completion_drain.py` | E5 Sisyphus 验证提醒 |
| 20 | 修改 | `client/app/stores/ui.ts` | 新增 todoDockCollapsed + persist |
| 21 | 新建 | `client/app/composables/useTodoList.ts` | useSubagentTasks.ts 模式 + 按 TaskFlow flow 分组 |
| 22 | 修改 | `client/app/composables/ws.ts` | todo_updated 事件分发 + ws:send |
| 23 | 修改 | `server/trigger/core.py` | todo_refresh 消息处理 |
| 24 | 新建 | `client/app/components/chat/TodoDock.vue` | PrimeVue + 按 TaskFlow flow 分组 |
| 25 | 新建 | `client/app/components/chat/TodoItem.vue` | PrimeVue + category/delegation 显示 |
| 26 | 修改 | `client/app/pages/home/index/[sid].vue` | 插入 TodoDock |
| 27 | 修改 | `client/app/i18n/locales/{en,zh,ja,ko}.json` | todolist 域 |

> **不含任何 DAG / 调度器文件**：旧稿中的 `agent/tools/todolist/dag/*`（scheduler/types）
> 与本方案的 `DagScheduler`/`DagNode`/`DagRun` 类型**已移除**，DAG 由 TaskFlow 提供。
> 详细说明见 §14。
>
> DAG 执行调用面（既有 TaskFlow，不属本表新建项）：
> `taskflow_create` / `taskflow_run_task(..., depends_on=[...])` / `taskflow_dispatch` /
> `taskflow_wait_all` / `taskflow_resume` / `taskflow_summary`
> （实现见 commits `9ce33ef..2824034`，无 DB migration，DAG 字段在 `state_json`）。

---

## 12. 实施顺序 / 优先级

### Phase A — 行为强制（最高价值）

优先级最高，直接改变模型行为，且大量复用 sherry 既有基础设施：

1. **E7 意图识别**（`agent/middlewares/task_intent.py`）— 解决"模型不做计划"。
2. **E1 orchestrator doctrine**（`AGENTS.md`）— 零代码，纯 prompt，威慑基线。
3. **E5 Sisyphus 验证**（`verifier.py` + drain 扩展）— 阻断"虚假完成"。
4. **E3 续作强制器**（`stagnation_tracker.py` + `todo_continuation.py` + `core.py`）— 拉回偷懒的模型。
5. **计划可见性注入**（`prompt_builder.py` 的 `_build_todo_block` / `_build_boulder_block`）— 压缩免疫的可见性。

> 注：E3/E7 依赖 `get_todos_sync()`，因此 Phase A 需要一个最小可用的会话级 todos 读取路径（可与 Phase B 的 `store_sqlite.py` 同步落地，或先用一个薄读取器）。

### Phase B — 会话级存储与工具

1. `agent/tools/todolist/registry/store_sqlite.py` — DB 表 + CRUD + `flow_id`/`step_id` 关联列。
2. `agent/tools/todolist/service.py` — TodoService + EvidenceLedger + WS 推送 + E4/E6 校验。
3. `agent/tools/todolist/config.py` — 常量。
4. `todowrite.py` + `todoread.py` — `@tool` 定义 + E6a fan-out。
5. `tools/__init__.py` — `build_todolist_tools` + E2 格式规则。
6. `agent/tools/__init__.py` — 注册。
7. `skills/builtin/core/todolist/SKILL.md` + `skills/builtin/core/ulw-execute/SKILL.md`（allowed root 下）。
8. **待定决策**：`todowrite` 是否需要自己的 `todos.db`？（见 §13）

### Phase C — 前端（nice-to-have）

1. `client/app/composables/useTodoList.ts` — 模块单例 + WS + 按 TaskFlow flow 分组。
2. `client/app/composables/ws.ts` — `todo_updated` 事件分发 + `ws:send`。
3. `server/trigger/core.py` — `todo_refresh`。
4. `TodoDock.vue` + `TodoItem.vue` — PrimeVue + 显示 `taskflow_status`。
5. `[sid].vue` 布局集成 + `ui.ts` + i18n（4 语言）。

### 明确不在范围内（Out of Scope）

- **任何新的 DAG / 调度器**：不在 todolist 层重建 `depends_on`、wave、frontier、拓扑排序或关键路径。
  DAG 全部由 TaskFlow 提供；如需 `critical path` / `bottlenecks` / step 级 `failed` / 重试，
  应作为 TaskFlow 的后续能力（gap #8）而非本层交付。
- 任务引擎级别的持久化、跨会话 flow 状态、子代理派发、等待与恢复。

---

## 13. 关键设计决策（含待定项）

| 决策 | 选择 | 理由 |
| ---- | ---- | ---- |
| 本层定位 | Agent 规划与纪律层（非任务引擎、非 DAG） | DAG 已由 TaskFlow 提供，避免双调度器漂移 |
| 会话计划 | `todowrite` / `todoread`（会话级、轻量） | 压缩免疫、实时可见 |
| DAG 调度 | 复用 TaskFlow（`depends_on` + `blocked/ready/dispatched/done`） | 既有 taskflow-dag-phase1；不再造第二个调度器 |
| 两层关联 | todo 携带 `flow_id`/`step_id`，不镜像 step 状态 | 单一事实来源，避免跨层数据分叉 |
| 状态持久化 | boulder.json + ledger.jsonl + todos.db + taskflow_registry.db | 工作状态/证据/会话 TODO/DAG 状态分层 |
| 压缩保护 | 系统提示词注入 | ~40 行代码，天然免疫压缩 |
| 续作强制 | after_agent 中间件 + auto_turn | 复用 sherry 已有基础设施 |
| 意图识别 | before_model 中间件 + 启发式检测 + 双模式注入 | 参考 omo ultrawork(arming) + ulw-execute-continuation(input hook) |
| 验证契约 | Sisyphus: DoneClaim→AdversarialVerify→FullyDone | omo ulw-execute SKILL.md:184-211 |
| 委派路由 | category-based delegation router | omo ulw-execute SKILL.md:134-148 |
| 前端 | Vue 3 + PrimeVue + 按 TaskFlow flow 分组 | sherry_agent 现有 UI 栈；DAG 状态来自 taskflow_summary |

### 待定项：会话 todo 存储的两种方案（Open Decision）

**问题**：`todowrite` 是否需要一个独立的 `todos.db`（会话级存储），还是只做 TaskFlow 的薄视图？

| 方案 | 做法 | 优点 | 缺点 |
| ---- | ---- | ---- | ---- |
| **A. 独立 `todos.db`** | todo 状态存 `todos.db`；DAG 状态仍存 TaskFlow；用 `flow_id`/`step_id` 关联 | 压缩免疫的注入稳定；todo 与 DAG 状态解耦；会话级计划可脱离 flow 存在 | 多一份存储；需要维护关联一致性 |
| **B. TaskFlow-only 薄视图** | todo 不落库，`todoread` 直接由 `taskflow_summary(flow_id)` 投影出清单 | 单一存储，无同步成本 | 无 flow 的纯会话计划无处安放；注入需每次解析 summary；`get_todos_sync` 无同步数据源，压缩保护变复杂 |

**推荐默认：方案 A（独立 `todos.db`，`flow_id`/`step_id` 只作关联）**。原因：

1. E3 续作强制器与 E7 需要**无事件循环的同步读取** `get_todos_sync()`，独立表最直接。
2. 计划/清单不一定绑定 TaskFlow flow（例如用户只是要求列个 3 步清单），TaskFlow-only 会丢失这类纯会话计划。
3. 系统提示词注入需要稳定、低成本的快照来源，DB 读取比解析 `taskflow_summary` 文本更可靠。
4. 唯一事实来源仍成立：todo 完成状态归 `todos.db`，DAG step 状态归 TaskFlow，二者靠 id 关联而非复制。

> 若未来 TaskFlow 提供结构化的 "flow → checklist" 只读投影，可重新评估方案 B；默认先按 A 落地。

---

## 14. 附：与旧稿的差异 / 被移除内容

### 14.1 与旧稿的对比

| 维度 | 旧方案（折中） | 本方案（omo + ulw-execute，TaskFlow 之上） |
| ---- | -------------- | ------------------------------------------ |
| 本层定位 | 含 DAG 调度器的任务系统 | 规划与纪律层（非任务引擎、非 DAG） |
| HTN 分解 | 无（扁平 todo 列表） | Plan → Checkboxes → Sub-tasks → Workers |
| 依赖管理 | 无 / 自建 | TaskFlow `depends_on` + `blocked/ready/dispatched/done` |
| 并行执行 | 提示词建议 | `taskflow_dispatch` 批量派发 ready 步骤 + `taskflow_wait_all` |
| 关键路径 | 无 | 未实现（由 TaskFlow 提供则另议，不属本层） |
| 瓶颈检测 | 无 | 未实现（同上） |
| 状态持久化 | todos.db | boulder.json + ledger.jsonl + todos.db + TaskFlow `state_json` |
| 验证契约 | 无 | Sisyphus: DoneClaim→AdversarialVerify→FullyDone |
| 编排模型 | LLM 自行编排 | ulw-execute: Orchestrator NEVER implements |
| 委派路由 | category 字段 | category-based delegation router |
| 续作强制 | 基础退避 | 退避 + 停滞 + abort 检测 + 恢复模式 |
| 意图识别 | 无 | E7a arming + E7b plan-active steering |

### 14.2 被移除 / 委派的内容（历史说明）

以下工件在旧稿中出现，**现已全部移除或委派**。列出仅为历史清晰，**它们并不存在于代码库**，也不是本方案的交付项：

| 已移除 / 委派的工件 | 种类 | 现状 |
| ------------------- | ---- | ---- |
| `agent/tools/todolist/dag/types.py` | 文件 | **已移除**；step 类型/状态由 TaskFlow 提供（`state_json` 中的 `blocked/ready/dispatched/done`） |
| `agent/tools/todolist/dag/scheduler.py` | 文件 | **已移除**；调度委派给 `taskflow_run_task` / `taskflow_dispatch` / `taskflow_resume` / `taskflow_wait_all` |
| `DagScheduler` | 类型 | **已移除**；等价能力是 TaskFlow 的 step 依赖调度 |
| `DagNode` | 类型 | **已移除**；等价能力是 TaskFlow 的 step（`step_id` / `depends_on` / `status`） |
| `DagRun` | 类型 | **已移除**；等价能力是 TaskFlow 的 flow（`taskflow_registry.db` + `state_json`） |
| `wave_index` 作为调度字段 | schema 列 | **已移除**；波次不再显式表达，改为 `ready` 状态等价物 |
| `todos.db.depends_on` 列 | schema 列 | **已移除**；依赖声明走 `taskflow_run_task(depends_on=[...])` |
| `TodoService.get_wave_frontier()` / `get_todos_by_wave()` / `get_dependency_frontier()` | 接口 | **已移除**；frontier 由 TaskFlow `deps_satisfied` / `unlock_dependents` 计算 |
| critical path / bottlenecks / 拓扑排序可视化 | 特性 | **未实现且不交付**；如需应由 TaskFlow 补充 |

**合并说明**：旧 `TODO/TODOLIST_ENFORCEMENT.md` 的全部 E1–E7 内容已折入本文 §9，该文件已从仓库删除（`git rm`）。本文不再引用任何外部配套 todolist 文件。
