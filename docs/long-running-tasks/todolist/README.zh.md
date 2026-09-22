# 规划与纪律层 — 建立在 TaskFlow 之上

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 会话级、轻量的规划与纪律层，建立在既有 TaskFlow 系统之上。它**不是任务引擎**，**不是 DAG**，**不实现调度器**。会话级计划由 `todowrite` / `todoread` 管理（通过系统提示词注入，天然免疫压缩）。跨会话、可持久、会派发子代理的执行由 **TaskFlow** 承担（`depends_on`、`blocked/ready/dispatched/done`、`taskflow_dispatch`、`taskflow_wait_all`）。两层用 `flow_id` / `step_id` 关联；同一份状态只有一个权威源，不跨层复制。

---

## 目录

- [定位：与 TaskFlow 的关系](#定位与-taskflow-的关系)
- [设计哲学与核心原则](#设计哲学与核心原则)
- [架构总览](#架构总览)
- [数据层](data/README.zh.md)
  - [计划文件 (workspace/sessions/<session_id>/plans/*.md)](data/README.zh.md#计划文件-workspacesessionssession_idplansmd)
  - [Boulder 状态 (src/data/boulder.json)](data/README.zh.md#boulder-状态-srcdataboulderjson)
  - [证据账本 (src/data/evidence-ledger.jsonl)](data/README.zh.md#证据账本-srcdataevidence-ledgerjsonl)
  - [todos.db — 会话级 TODO 存储](data/README.zh.md#todosdb--会话级-todo-存储)
- [服务层](#服务层)
- [工具层](#工具层)
- [编排执行层](#编排执行层)
- [压缩保护层](#压缩保护层)
- [强制执行层 E1–E7](enforcement/README.zh.md)
  - [E1: 系统提示词强制 — orchestrator doctrine](enforcement/README.zh.md#e1-系统提示词强制--orchestrator-doctrine)
  - [E2: 工具描述强制 — 格式 + 委派规则](enforcement/README.zh.md#e2-工具描述强制--格式--委派规则)
  - [E3: 续作强制器 — 事后闭环核心 ★★](enforcement/README.zh.md#e3-续作强制器--事后闭环核心-)
  - [E4: 转换屏障 — 双保险](enforcement/README.zh.md#e4-转换屏障--双保险)
  - [E5: Sisyphus 完成契约 ★★](enforcement/README.zh.md#e5-sisyphus-完成契约-)
  - [E6: 委派路由 ★](enforcement/README.zh.md#e6-委派路由-)
  - [E7: 意图识别与引导 — 事前触发器 ★★](enforcement/README.zh.md#e7-意图识别与引导--事前触发器-)
- [前端](#前端)

---

## 定位：与 TaskFlow 的关系

### 两层分工

| 维度     | 会话计划/清单                                       | 执行流（DAG）                                                                             |
| -------- | --------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| 归属     | 规划与纪律层（本文）                                | TaskFlow（既有系统）                                                                      |
| 作用范围 | 单个 session，轻量                                  | 跨 session，durable                                                                       |
| 状态内容 | todo 的 content/status/priority/category/delegation | step 的 depends_on/status/child_session_key/results                                       |
| 持久化   | `todos.db`（会话级）                                | `taskflow_registry.db` + flow `state_json`                                                |
| 注入方式 | 系统提示词 block（每次压缩后重建，天然免疫压缩）    | 工具返回文本（`taskflow_summary` 回读）                                                   |
| 主要职责 | 计划、可见性、纪律强制（E1–E7）                     | 依赖满足、阻塞/解锁、并行派发、有界等待、结果注入                                         |
| 是否调度 | 否                                                  | 是（`taskflow_run_task` / `taskflow_dispatch` / `taskflow_wait_all` / `taskflow_resume`） |

一句话：**规划与纪律层负责"想清楚、盯住、逼完成"，TaskFlow 负责"真的跑、跨会话、管依赖"。**

### 边界：什么住在哪

| 关注点                       | 权威源                | 说明                                                   |
| ---------------------------- | --------------------- | ------------------------------------------------------ |
| todo 是否完成                | `todos.db`            | 只有 `todowrite` 能改 todo 状态                        |
| step 是否完成 / 依赖是否满足 | TaskFlow `state_json` | 只有 `taskflow_resume` 能把 step 标 `done` 并解锁后继  |
| 子会话是否还在跑             | subagent registry     | `get_run_by_child_session_key` + `is_live_unended_run` |
| 计划文件进度（checkbox）     | `workspace/sessions/<session_id>/plans/*.md`     | orchestrator 编辑 `- [ ]` → `- [x]`                    |
| 执行证据                     | `src/data/evidence-ledger.jsonl`   | `EvidenceLedger` 追加                                  |
| 活跃工作状态                 | `src/data/boulder.json`   | 计划激活/恢复                                          |

### 关联方式（唯一事实来源）

todo 可携带两个可选字段指向 TaskFlow：

- `flow_id`：关联的 TaskFlow flow id。
- `step_id`：关联的 TaskFlow step id（形如 `step-2`），用于回读该 step 的 DAG 状态。

**不镜像**原则：todo 层不复制 `depends_on`、不保存波次、不缓存 step 状态。需要 DAG 状态时调用只读的 `taskflow_summary(flow_id)`，把 `blocked/ready/dispatched/done` 映射回 UI/提示即可。

---

## 设计哲学与核心原则

全面采用 HTN（分层任务网络）体系：计划文件 → checkbox → 原子 sub-task → 委派 subagent workers → 对抗性验证。

```
Plan (workspace/sessions/<session_id>/plans/*.md)
  └─ Wave 0: [Checkbox A] [Checkbox B]          ← 并行，无依赖
  └─ Wave 1: [Checkbox C] (depends on A, B)      ← 等 Wave 0 完成
  └─ Wave 2: [Final Verification Wave]            ← 全局收尾

每个 Checkbox → 分解为 atomic sub-tasks → 委派给 subagent workers
  └─ Worker 返回 DoneClaim
       └─ AdversarialVerify (独立验证)
            └─ FullyDone → 标记 checkbox 完成
```

"Wave"是**计划文件里的叙述单位**，不是 todolist 的调度数据结构。真正"什么时候能派发下一个"由 TaskFlow 的 `depends_on` + `blocked/ready` 决定。

**核心原则**：YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER. 你不写代码，不编辑产品文件，每个实现单元必须委派给派生的子代理。

不依赖模型自觉，通过 7 层强制形成闭环：

```
Before (message arrives)    During                     After
┌──────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ E7 Intent │  │ E6 Delegation ★    │  │ E3 Continuation ★★  │
│ Recognizer│  │ #a fan-out         │  │ idle + incomplete    │
│ ★★       │  │ #b category route  │  │ todo + backoff +     │
│ before_   │  │ #c delegation cmd  │  │ stagnation + abort   │
│ model     │  │ #d barrier         │  │ + recovery mode      │
│ inject    │  └────────────────────┘  └──────────────────────┘
└──────────┘  ┌────────────────────┐  ┌──────────────────────┐
┌──────────┐  │ E1 System prompt   │  │ E5 Sisyphus verify ★★│
│ E2 Tool  │  │ orchestrator       │  │ DoneClaim →          │
│ desc      │  │ doctrine           │  │ AdversarialVerify →  │
│ MANDATORY │  │ + hook notice      │  │ FullyDone             │
│ format    │  └────────────────────┘  └──────────────────────┘
└──────────┘                            ┌──────────────────────┐
                                          │ E4 Transition barrier│
                                          │ TaskFlow step status+│
                                          │ subagent alive → block│
                                          └──────────────────────┘
```

一句话逻辑：模型不自觉做计划 → E7 注入引导；模型偷懒停下来 → E3 拉回；模型虚假完成 → E5 阻断；模型自己写代码 → E1 doctrine 威慑；模型提前标完成 → E4 硬阻断。

---

## 架构总览

```
Layer 9  │ UI 组件层         │ TodoDock.vue + TodoItem.vue (PrimeVue)，按 TaskFlow flow 分组
Layer 8  │ 前端状态层         │ useTodoList.ts (模块级单例) + 按 flow 分组
Layer 7  │ 实时通信层         │ WS: todo_updated 推送 + todo_refresh 重连重发
Layer 6  │ 压缩保护层 ★      │ build_system_prompt 注入当前 todos + boulder 状态
Layer 5  │ DAG 执行层 ★      │ 由 TaskFlow 提供: depends_on + blocked/ready/dispatched/done（本层不实现）
Layer 4  │ 编排执行层 ★      │ ulw-execute: plan→checkbox→sub-task→worker→verify，调用 TaskFlow
Layer 3  │ 工具层            │ todowrite + todoread（DAG 由 taskflow_* 工具家族执行）
Layer 2  │ 服务层            │ TodoService + EvidenceLedger（DAG 状态查询委派 TaskFlow）
Layer 1  │ 数据存储层         │ todos.db (WAL) + boulder.json + ledger.jsonl + plans/*.md + taskflow_registry.db
─────────│──────────────────│
E1       │ 系统提示词强制     │ AGENTS.md: MANDATORY orchestrator doctrine
E2       │ 工具描述强制       │ todowrite docstring 格式规则
E3       │ 续作强制器 ★★     │ after_agent 中间件: idle+未完成→自动续作（含退避/停滞/abort/恢复）
E4       │ 转换屏障 ★        │ TaskFlow step 未 done / subagent 运行中禁止标 completed
E5       │ Sisyphus 验证 ★★  │ DoneClaim → AdversarialVerify → FullyDone（5 gates）
E6       │ 委派路由 ★        │ #a fan-out + #b category 路由 + #c 委派指令 + #d 转换屏障
E7       │ 意图识别器 ★★     │ before_model: arming(无计划+任务意图→注入引导) + plan-active(有计划→追加 reminder)
```

**Layer 5 的 DAG 能力不是本层实现的**，它由 TaskFlow（`agent/tools/taskflow/tools/*`）提供。本层只调用、不重建。

---

## 服务层

### TodoService

```python
class TodoService:
    @staticmethod
    async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
        validated = _validate_todos(todos)
        # E4：转换屏障 —— TaskFlow step 未 done / subagent 运行中时阻断 completed
        for todo in validated:
            if todo["status"] == "completed":
                _assert_transition_allowed(todo)
        await store.replace_all(session_id, validated)
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def get_flow_progress(session_id: str, flow_id: str) -> dict:
        """把 TaskFlow 的 DAG 状态映射回 todo 视图（不重算 frontier）。
        DAG 调度住在 TaskFlow；本方法只调用只读的 taskflow_summary。"""
        from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary
        linked = await store.get_todos_by_flow(session_id, flow_id)
        summary_text = await taskflow_summary.ainvoke({"flow_id": flow_id})
        return {"todos": linked, "taskflow_summary": summary_text}
```

### EvidenceLedger

`agent/tools/todolist/evidence_ledger.py` 是 append-only 的 JSONL 账本（`src/data/evidence-ledger.jsonl`，每行一个 JSON 对象）。`append` + `read_all` 仍是全部存储契约；会话级视图读取同一份共享文件：

```python
class EvidenceLedger:
    LEDGER_PATH = "src/data/evidence-ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None: ...          # 一行 JSON + UTC 时间戳
    @classmethod
    def read_all(cls) -> list[dict]: ...
    @classmethod
    def for_session(cls, session_key: str) -> SessionEvidenceLedger: ...
    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]: ...
    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int: ...
```

陈旧性是推导出来的、从不写入：`mark_stale_for_path` 追加一行 `{"event": "stale", "file_path": …}`，读取侧认定某条证据行陈旧，当且仅当更晚的 stale 事件命名了其 `command` 中包含的路径。`agent/tools/todolist/evidence_recorder.py` 把它接入工具（失败开放、吞掉异常）：`terminal` / `python_repl` 为识别出的验证命令追加一行（`EVIDENCE_LEDGER["auto_record"]`，默认 `True`），`write_file` / `patch_file` 为被编辑路径追加 stale 事件（`EVIDENCE_LEDGER["auto_stale"]`，默认 `True`）。`agent/tools/taskflow/evidence_collector.py` 渲染给判别器与 `taskflow_finish` 证据门查看的摘要。

### WS 推送

```python
async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    from runtime import relation_register
    ws = relation_register.get_websocket_by_session_id(session_id)
    if ws:
        await ws.send_text(json.dumps({
            "event": "todo_updated", "session_id": session_id,
            "content": {"todos": todos}
        }))
```

复用 `relation_register` 直发模式，推送 `todo_updated` 事件。

---

## 工具层

### todowrite — 全量替换，写即读

```python
@tool("todowrite")
async def todowrite(todos: list[dict], session_id: Annotated[str, InjectedState("session_id")] = "") -> str:
    """Update the todo list for the current session (full replacement).
    Pass the COMPLETE list every time.
    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_run_task(flow_id, task, depends_on=[...])."""
    result = await service.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER  # E6a: 首次调用追加 fan-out 决策提醒
    return output
```

### todoread — 显式读取

```python
@tool("todoread")
async def todoread(session_id: Annotated[str, InjectedState("session_id")] = "") -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### 工具注册（E2 注入点）

```python
def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES  # E2 格式规则
    return list(_TODOLIST_TOOLS)
```

在 `build_todolist_tools()` 中覆盖 todowrite 描述，注入 MANDATORY 格式规则 + 委派规则：

- 每个 todo 标题必须编码 WHERE、WHY、HOW、EXPECTED RESULT。
- 原子粒度：1-3 次工具调用可完成。
- 同时只有一个 in_progress。
- delegation="subagent" 时，subagent 返回前不得标记 completed。

### SKILL.md

定义何时使用 todolist（3+ 步复杂工作）、可用工具、状态/优先级/委派字段、DAG 字段（委派给 TaskFlow）以及规则。

### knowledge — 按计划身份隔离

`knowledge` 工具（`agent/tools/todolist/knowledge/`）以**计划身份**为键，而非计划名。归属来自三个来源（`ownership.association_plan_refs()`）：本会话的 `plan_ref` 状态键、本会话某条 todo 的 `plan_ref`（SQL 按 `session_id` 过滤）、`src/data/boulder.json` 中 `plan_name` 匹配且 `session_ids` 含本会话的 work。`identity.resolve_plan_identity()` 把该名称映射到规范化计划路径并派生存储目录 `workspace/knowledge/plans/<plan_key>/`，其中 `plan_key = sha1(相对仓库根的计划路径)[:12]`；每个目录内的 `meta.json` 记录可读的 `plan_name` / `plan_ref`。由此：

- 计划文件不同的同名计划**物理隔离**——各自写自己的 key 目录，互不覆盖；
- 通过 boulder `session_ids` 共享**同一计划文件**的所有会话解析同一路径，因此**多会话协同不受影响**（共享同一目录）；
- 计划文件无法解析的会话写入兜底身份 `session-<sha1(session_id)[:8]>`——全 id 哈希让前 8 字符相同的会话 id 彼此隔离；
- 名称命中多个路径时优先本会话 `plan_ref`；仍然歧义（且不是本会话自己的兜底名）时给出可诊断错误并拒绝，绝不静默写到别处。

`list` 只返回本会话关联的计划（可读名 + key）；`read` / `write` 访问他会话或歧义计划会被拒绝（绝不静默返回空）。旧的名称为键目录 `workspace/knowledge/plans/<plan-name>/` 在 key 目录出现前保持可读；写入总是落在 key 目录。`clear_session` 删除本会话私有身份目录，保留通过 boulder `session_ids` 与其他会话共享的计划；旧目录永不删除。boulder 文件缺失/损坏、`session_id` 为空或计划未知时一律安全拒绝——不抛异常，也不发生跨会话读取。

---

## 编排执行层

### 5 Phase 流程

```
Phase 1: Select the plan → 读 src/data/boulder.json，列 workspace/sessions/<session_id>/plans/*.md，匹配或恢复
Phase 2: Create or update Boulder state → 写 boulder.json，注册所有 Phase 和 Task 为 todos
Phase 3: Execute next checkbox（调度全部交给 TaskFlow）
  → 读计划，找到第一个未勾选的 checkbox
  → 分解为 atomic sub-tasks
  → taskflow_run_task(flow_id, task, depends_on=[...]) 登记
  → blocked 不派发；ready 步骤 taskflow_dispatch 批量派发
  → taskflow_wait_all → taskflow_resume（注入结果，解锁后继）
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: Verify and record evidence → 5 gates → src/data/evidence-ledger.jsonl
Phase 5: Mark progress → 编辑 checkbox - [ ] → - [x]，不询问是否继续
```

### 计划 → TaskFlow 的落地方式

1. `taskflow_create(flow_id, description, initial_state)` — 建流。
2. `taskflow_run_task(flow_id, task, depends_on=[...])` — 登记：无依赖 → `dispatched`；依赖未满足 → `blocked` 不派发；未知依赖 → 报错不改状态。
3. `taskflow_resume(flow_id, child_session_key, result)` — 注入结果：step 变 `done`，blocked 依赖解锁为 `ready`。**resume 不自动派发。**
4. `taskflow_dispatch(flow_id, step_ids)` — 批量并行派发 ready 步骤。
5. `taskflow_wait_all(flow_id, timeout, poll_interval)` — 等本流派发的子会话 settle。
6. `taskflow_finish(flow_id, summary)` — 收尾；`taskflow_summary` 随时回读。

### step 状态机（TaskFlow 权威定义）

```
blocked (依赖未全部 done；run_task 只登记、不派发)
  → ready (依赖已满足，等待派发；由 taskflow_resume 解锁)
  → dispatched (已派发 detached 子会话，child_session_key 落库)
  → done (已通过 taskflow_resume 把子会话结果注入流状态)
```

### 计划层概念 → TaskFlow 能力映射

| 计划层概念（HTN）    | TaskFlow 提供的对应能力                                           |
| -------------------- | ----------------------------------------------------------------- |
| Checkbox 之间的依赖  | step 的 `depends_on`（step-id 列表）                              |
| Wave / 波次          | 不显式建 wave；`ready` 状态等价于"当前波次"                       |
| 节点状态             | `status ∈ {blocked, ready, dispatched, done}` 在 `state_json`     |
| Frontier             | `ready` 状态 + `taskflow_dispatch` 校验 `deps_satisfied`          |
| 依赖未满足时阻塞     | `taskflow_run_task(..., depends_on=[...])` 记 `blocked`，不 spawn |
| 完成一个节点解锁后继 | `taskflow_resume` 标 `done` + `unlock_dependents`                 |
| 并行派发             | `taskflow_dispatch(flow_id, step_ids)` 批量派发 ready 步骤        |
| 等待并行子会话       | `taskflow_wait_all(...)` 按 flow 范围有界轮询                     |
| 回读 DAG 状态        | `taskflow_summary(flow_id)` 渲染每步 status/depends_on + 计数     |

### 并行投递通道决策

| 拓扑                    | 条件                  | 策略                                        |
| ----------------------- | --------------------- | ------------------------------------------- |
| 独立通道 → 并行 workers | 分离文件，无共享契约  | 一次并行 spawn burst（`taskflow_dispatch`） |
| 有序依赖通道 → 波次串行 | C 需要 A 和 B 先完成  | 等前置 step `done` 后执行（`depends_on` + `resume` 解锁） |
| 重叠通道 → team         | 同模块/契约，并发更快 | 串行化或手动协调                            |

---

## 压缩保护层

`build_system_prompt()` 在每次上下文压缩后被 `Summarization` 中间件重新调用。在这里注入当前 todos + boulder 状态，天然免疫压缩。

### 注入内容

| Block                      | 数据源            | 上下文开销 | 说明                                             |
| -------------------------- | ----------------- | ---------- | ------------------------------------------------ |
| `_build_todo_block()`      | todos.db          | ~10 行     | 当前 todo 列表 + 状态 + TaskFlow flow/step 关联  |
| `_build_boulder_block()`   | src/data/boulder.json | ~5 行      | 活跃工作状态                                     |
| `_build_knowledge_block()` | workspace/knowledge/plans/&lt;plan_key&gt;/ | ~20 行     | key_failures + key_successes + reusable_patterns（按身份解析） |

### 实现

```python
def _build_todo_block(session_id: str) -> str:
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        tag_parts = [t.get("category", "quick")]
        if t.get("delegation", "self") != "self":
            tag_parts.append(t["delegation"])
        if t.get("flow_id"):
            tag_parts.append(f"flow:{t['flow_id']}")
        if t.get("step_id"):
            tag_parts.append(t["step_id"])
        lines.append(f"- [{icon.get(t['status'], '○')}] ({', '.join(tag_parts)}) {t['content']} ({t['priority']})")
    lines.append("Update todos via todowrite. Pass the COMPLETE list each time.")
    lines.append("Dependency scheduling is owned by TaskFlow; declare depends_on via taskflow_run_task.")
    lines.append("Your todo list is tracked by the continuation system. Incomplete todos will trigger automatic continuation.")
    lines.append("Completion is verified by the Sisyphus contract — unverified claims will be rejected.")
    return "\n".join(lines)
```

### 为什么比 omo 的 5 层防御更轻量

| omo 的 5 层             | 本方案                                           |
| ----------------------- | ------------------------------------------------ |
| Prune 保护列表          | 不需要（todos 在 system prompt 中）              |
| 压缩前快照 + 压缩后恢复 | 不需要（system prompt 每次从 DB 重建）           |
| 8 段式压缩上下文注入    | 不需要（todos 不在对话历史中）                   |
| 60s 压缩保护窗口        | 不需要（无续作注入器需要保护）                   |
| 续作强制器              | 需要，简化为 after_agent 中间件（E3）            |
| 知识摘要注入            | 新增 `_build_knowledge_block()`（约 25 行）      |
| **总计 ~600+ 行代码**   | **~65 行（prompt 注入）+ ~130 行（续作中间件）** |

---

## 前端

### 前端状态层 (useTodoList.ts)

模块级单例，WebSocket 驱动，按 TaskFlow flow 分组：

```typescript
interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  category?: "quick" | "deep" | "ultrabrain" | "visual" | "git" | "writing";
  delegation?: "self" | "subagent";
  flow_id?: string | null;
  step_id?: string | null;
  taskflow_status?: "blocked" | "ready" | "dispatched" | "done" | null;
}
```

WS 事件：`todo_updated`（变更推送）+ `todo_refresh`（重连重发）。

### UI 组件

- **TodoDock.vue**：按 `flow_id` 分组显示；进度计数；可折叠。
- **TodoItem.vue**：Checkbox (PrimeVue)、category 徽章、委派图标、in_progress 脉冲点。

### WS 消息格式

```json
{
  "event": "todo_updated",
  "session_id": "xxx",
  "content": {
    "todos": [
      {
        "content": "Implement login",
        "status": "completed",
        "priority": "high",
        "flow_id": "login-flow",
        "step_id": "step-1",
        "taskflow_status": "done"
      }
    ]
  }
}
```

`taskflow_status` 是 `taskflow_summary(flow_id)` 回读的 step 状态；前端不重算波次/依赖。
