# 规划与纪律层 — 建立在 TaskFlow 之上

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 会话级、轻量的规划与纪律层，建立在既有 TaskFlow 系统之上。它**不是任务引擎**，**不是 DAG**，**不实现调度器**。会话级计划由 `todowrite` / `todoread` 管理（通过系统提示词注入，天然免疫压缩）。跨会话、可持久、会派发子代理的执行由 **TaskFlow** 承担（`depends_on`、`blocked/ready/dispatched/done`、`taskflow_dispatch`、`taskflow_wait_all`）。两层用 `flow_id` / `step_id` 关联；同一份状态只有一个权威源，不跨层复制。

---

## 目录

- [定位：与 TaskFlow 的关系](#定位与-taskflow-的关系)
- [设计哲学与核心原则](#设计哲学与核心原则)
- [架构总览](#架构总览)
- [数据层](#数据层)
- [服务层](#服务层)
- [工具层](#工具层)
- [编排执行层](#编排执行层)
- [压缩保护层](#压缩保护层)
- [强制执行层 E1–E7](#强制执行层-e1e7)
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
| 计划文件进度（checkbox）     | `.omo/plans/*.md`     | orchestrator 编辑 `- [ ]` → `- [x]`                    |
| 执行证据                     | `.omo/ledger.jsonl`   | `EvidenceLedger` 追加                                  |
| 活跃工作状态                 | `.omo/boulder.json`   | 计划激活/恢复                                          |

### 关联方式（唯一事实来源）

todo 可携带两个可选字段指向 TaskFlow：

- `flow_id`：关联的 TaskFlow flow id。
- `step_id`：关联的 TaskFlow step id（形如 `step-2`），用于回读该 step 的 DAG 状态。

**不镜像**原则：todo 层不复制 `depends_on`、不保存波次、不缓存 step 状态。需要 DAG 状态时调用只读的 `taskflow_summary(flow_id)`，把 `blocked/ready/dispatched/done` 映射回 UI/提示即可。

---

## 设计哲学与核心原则

全面采用 HTN（分层任务网络）体系：计划文件 → checkbox → 原子 sub-task → 委派 subagent workers → 对抗性验证。

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

"Wave"是**计划文件里的叙述单位**，不是 todolist 的调度数据结构。真正"什么时候能派发下一个"由 TaskFlow 的 `depends_on` + `blocked/ready` 决定。

**核心原则**：YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER. 你不写代码，不编辑产品文件，每个实现单元必须委派给派生的子代理。

不依赖模型自觉，通过 7 层强制形成闭环：

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

**Layer 5 的 DAG 能力不是本层实现的**，它由 TaskFlow 提供。本层只调用、不重建。

---

## 数据层

### 计划文件 (.omo/plans/*.md)

Checkbox 格式的 Markdown，定义完整的 HTN 分解：

```markdown
# <计划名>

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

### Wave 1: <Wave 描述> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder 状态 (.omo/boulder.json)

持久化工作状态，`session_id` 前缀用 `sherry:`：

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

### 证据账本 (.omo/ledger.jsonl)

每行一个 JSON 对象，记录每个 checkbox 的执行证据：

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "adversarial_classes": {"stale_state": "not-applicable", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
```

### todos.db — 会话级 TODO 存储

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

> **设计边界**：`todos.db` **不持有** `depends_on` / 波次 / step 状态。依赖图、`blocked/ready/dispatched/done` 与解锁逻辑全部由 TaskFlow 提供。todo 只通过 `flow_id`/`step_id` 指向对应 flow step，DAG 状态用 `taskflow_summary(flow_id)` 回读。

CRUD 接口：`replace_all`（全量替换）、`get_todos`（按 position 排序）、`get_todos_sync`（同步路径，用于系统提示词注入）、`get_todos_by_flow`（读取某个 flow 关联的 todo）。

---

## 服务层

### TodoService

- `update_todos`：全量替换 + E4 转换屏障校验 + WS 推送。
- `get_todos`：从 DB 读取。
- `get_flow_progress`：把 TaskFlow 的 DAG 状态映射回 todo 视图（不重算 frontier）。调用只读的 `taskflow_summary` 回读 step 的 status/depends_on。

### EvidenceLedger

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
        ...
```

### WS 推送

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

在 `build_todolist_tools()` 中覆盖 todowrite 描述，注入 MANDATORY 格式规则 + 委派规则：

- 每个 todo 标题必须编码 WHERE、WHY、HOW、EXPECTED RESULT。
- 原子粒度：1-3 次工具调用可完成。
- 同时只有一个 in_progress。
- delegation="subagent" 时，subagent 返回前不得标记 completed。

### SKILL.md

定义何时使用 todolist（3+ 步复杂工作）、可用工具、状态/优先级/委派字段、DAG 字段（委派给 TaskFlow）以及规则。

---

## 编排执行层

### 5 Phase 流程

```
Phase 1: Select the plan → 读 .omo/boulder.json，列 .omo/plans/*.md，匹配或恢复
Phase 2: Create or update Boulder state → 写 boulder.json，注册所有 Phase 和 Task 为 todos
Phase 3: Execute next checkbox（调度全部交给 TaskFlow）
  → 读计划，找到第一个未勾选的 checkbox
  → 分解为 atomic sub-tasks
  → taskflow_run_task(flow_id, task, depends_on=[...]) 登记
  → blocked 不派发；ready 步骤 taskflow_dispatch 批量派发
  → taskflow_wait_all → taskflow_resume（注入结果，解锁后继）
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: Verify and record evidence → 5 gates → .omo/ledger.jsonl
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
| 有序依赖通道 → 波次串行 | C 需要 A 和 B 先完成  | 等前置 step `done` 解锁后执行               |
| 重叠通道 → team         | 同模块/契约，并发更快 | 串行化或手动协调                            |

---

## 压缩保护层

`build_system_prompt()` 在每次上下文压缩后被 `Summarization` 中间件重新调用。在这里注入当前 todos + boulder 状态，天然免疫压缩。

### 注入内容

| Block                      | 数据源            | 上下文开销 | 说明                                             |
| -------------------------- | ----------------- | ---------- | ------------------------------------------------ |
| `_build_todo_block()`      | todos.db          | ~10 行     | 当前 todo 列表 + 状态 + TaskFlow flow/step 关联  |
| `_build_boulder_block()`   | .omo/boulder.json | ~5 行      | 活跃工作状态                                     |
| `_build_knowledge_block()` | .omo/knowledge/   | ~20 行     | key_failures + key_successes + reusable_patterns |

### 为什么比 omo 的 5 层防御更轻量

| omo 的 5 层             | 本方案                                           |
| ----------------------- | ------------------------------------------------ |
| Prune 保护列表          | 不需要（todos 在 system prompt 中）              |
| 压缩前快照 + 压缩后恢复 | 不需要（system prompt 每次从 DB 重建）           |
| 8 段式压缩上下文注入    | 不需要（todos 不在对话历史中）                   |
| 60s 压缩保护窗口        | 不需要（无续作注入器需要保护）                   |
| 续作强制器              | 需要，简化为 after_agent 中间件（E3）            |
| **总计 ~600+ 行代码**   | **~65 行（prompt 注入）+ ~130 行（续作中间件）** |

---

## 强制执行层 E1–E7

### E1: 系统提示词强制 — orchestrator doctrine

添加到 `AGENTS.md`，零代码纯文本。核心内容：

- YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER
- 多步任务（2+ 步）→ 必须先创建 todos
- 每步标记 in_progress → 子代理返回后验证 → 标记 completed
- 转换屏障：subagent 运行中 / TaskFlow step 未 done 时不得标记 completed
- Sisyphus 契约：DoneClaim → AdversarialVerify → FullyDone

### E2: 工具描述强制 — 格式 + 委派规则

在 `build_todolist_tools()` 中覆盖 todowrite 描述，注入 MANDATORY 格式规则：

- 标题编码 WHERE/WHY/HOW/RESULT
- 原子粒度（1-3 次工具调用）
- 同时只有一个 in_progress
- 不得在 subagent 返回前标记 completed（转换屏障）
- 不得在未验证前标记 completed（Sisyphus）

### E3: 续作强制器 — 事后闭环核心 ★★

turn 结束后如果有未完成 todo，系统自动注入续作消息把 LLM 拉回来。不依赖模型自觉。

**关键常量**：

- `_MAX_STAGNATION = 3`（连续 3 次无变化 → 停止）
- `_BASE_COOLDOWN_S = 2.0`（基础退避）
- `_MAX_COOLDOWN_S = 60.0`（最大退避）
- `_FAILURE_RESET_WINDOW_S = 300`（5 分钟无失败 → 重置）
- `_MAX_RECOVERY_ATTEMPTS = 2`（恢复模式上限）

**工作流程**：turn 结束 → 读 todos → 过滤未完成 → 停滞检测 → 退避冷却 → 构建 continuation prompt → `maybe_trigger_auto_turn()` 注入。用户发消息 → 取消续作 → reset()。

**文件**：`stagnation_tracker.py`（~90 行）+ `todo_continuation.py`（~110 行）+ `agent/core.py`（~2 行注册）。

### E4: 转换屏障 — 双保险

**Prompt 级**（E1 AGENTS.md）：subagent 运行中 / TaskFlow step 未 done 时不得标 completed。

**代码级硬阻断**（`service.py`），两个来源都不在 todolist 内建调度器：

1. **TaskFlow step 状态**：todo 关联 `flow_id`/`step_id` → 只有 `done` 允许标 `completed`，`blocked`/`ready`/`dispatched` 都阻断。
2. **subagent registry 存活检测**：todo 关联 `subagent_id` → `get_run_by_child_session_key` + `is_live_unended_run` 判断子会话是否仍在运行。

### E5: Sisyphus 完成契约 ★★

三阶段完成验证，对"虚假完成"的最终防线：

```
Worker 返回 → DoneClaim → AdversarialVerify（5 gates）→ FullyDone / NOT done
  Gate 1: Plan reread（重读计划，确认验收标准）
  Gate 2: Automated verification（运行验证命令）
  Gate 3: Manual QA（人工或 agent 检查）
  Gate 4: Adversarial QA（对抗性检查：stale state / dirty worktree / leftover resources）
  Gate 5: Cleanup（清理临时资源）
```

**文件**：`verifier.py`（~80 行）+ `subagent_completion_drain.py` 扩展（~15 行，追加验证提醒）。

### E6: 委派路由 ★

**生命周期**：LLM 创建 todo（含 category + delegation）→ #a fan-out reminder → #b category 告诉派哪类 subagent → #c AGENTS.md 指导如何委派 → 带依赖步骤 taskflow_run_task(depends_on) → ready 步骤 taskflow_dispatch → LLM 更新 subagent_id + flow_id/step_id → #d 转换屏障 → taskflow_wait_all → taskflow_resume → E5 验证 → 标记 completed。

**Category 路由表**：

| Category     | 路由到                           | 说明               |
| ------------ | -------------------------------- | ------------------ |
| `quick`      | subagent spawn (default model)   | 机械、单文件、样板 |
| `deep`       | subagent spawn (reasoning model) | 复杂调试、跨模块   |
| `ultrabrain` | subagent spawn (最强 model)      | 困难逻辑问题       |
| `visual`     | subagent spawn                   | 前端、UI/UX        |
| `git`        | subagent spawn                   | git 操作           |
| `writing`    | subagent spawn                   | 文档               |

### E7: 意图识别与引导 — 事前触发器 ★★

双模式设计，解决"模型不自觉调用计划工具"：

| 场景                    | 模式           | 行为                      |
| ----------------------- | -------------- | ------------------------- |
| 首次任务请求，无计划    | E7a            | 注入完整引导 prompt       |
| 已 armed + 新任务       | E7a 轻量       | 注入短提醒                |
| 有活跃计划 + 用户发消息 | E7b            | 追加 plan-active reminder |
| 有活跃计划 + turn 结束  | (E3)           | E7 不干预                 |
| 非任务（问答/闲聊）     | 跳过           | 不注入                    |
| 压缩后 + 新任务         | E7a 重新 armed | 重新注入完整引导          |

**意图检测**：轻量级启发式，不调用 LLM（零延迟、零成本）——问答模式 → 非任务；闲聊模式 → 非任务；任务关键词 → 任务；长消息非问答 → 可能是任务。

**防循环设计**：E7b 优先于 E7a；E7a once-per-session；E7b 只在 before_model 运行；`_is_system_directive()` 过滤系统注入消息；E3 有退避冷却；压缩后 re-arm。

**文件**：`agent/middlewares/task_intent.py`（~160 行）+ `agent/core.py`（~2 行注册）。

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

`taskflow_status` 是 `taskflow_summary(flow_id)` 回读的 step 状态；前端不重算波次/依赖。
