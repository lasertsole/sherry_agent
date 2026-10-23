# TaskFlow — 支持 DAG 依赖的持久化多步骤任务流

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> TaskFlow 提供基于 SQLite（乐观锁、WAL 模式）的跨轮次持久化任务流管理。核心能力包括：分离式子代理步骤派发、DAG 依赖管理、批量并行派发、有界轮询等待、幂等结果注入。十四个工具构成完整生命周期 API，与 openclaw managedFlows 接口对齐：`taskflow_create` → `taskflow_run_task` → `taskflow_dispatch` / `taskflow_wait_all` → `taskflow_resume` → `taskflow_finish` / `taskflow_fail` / `taskflow_cancel`，外加 `taskflow_summary`（只读重读）、`taskflow_set_waiting`（挂起等待）、`taskflow_progress`（进度报告）、`taskflow_budget`（Token/成本预算）、`taskflow_update_steps`（全量替换步骤列表）和 `taskflow_list`（会话面板）。

权威源码：`agent/tools/taskflow/tools/*.py`、`agent/tools/taskflow/registry/store_sqlite.py`、`agent/tools/taskflow/config.py`。技能参考：`skills/builtin/core/taskflow/SKILL.md`。

---

## 目录

- [概览](#概览)
- [架构](#架构)
- [状态机](#状态机)
- [工具族（14 个工具）](#工具族14-个工具)
- [乐观锁与冲突重试](#乐观锁与冲突重试)
- [DAG 依赖系统](#dag-依赖系统)
- [并行步骤执行](#并行步骤执行)
- [幂等恢复](#幂等恢复)
- [带已派生子代理的冲突重试](#带已派生子代理的冲突重试)
- [血缘指针](#血缘指针)
- [持久化与连接生命周期](#持久化与连接生命周期)
- [已知限制](#已知限制)

---

## 概览

TaskFlow（`agent/tools/taskflow/`）是基于 SQLite（WAL 模式）的持久化任务流系统，管理跨多个对话轮次的多步骤工作：每个流拥有生命周期（running → waiting → done/failed/cancelled），步骤可声明彼此依赖，结果由分离式子代理会话注入。

核心设计决策：

- **乐观锁**：每次变更将 `expected_revision` 递增 1；通过版本冲突检测并发写入，而非最后写入优先。
- **DAG 存于 state_json**：步骤依赖（`depends_on`）、步骤状态和结果全部存放在流的 `state_json` 列中——DAG 功能无需数据库 schema 迁移。
- **分离式派发**：步骤通过现有 spawn 管线派发分离式子代理会话执行；结果经由 announce/settle-wake 管线回流。
- **幂等恢复**：`(child_session_key, result)` 对被指纹化；重复投递不会注入两次。
- **错误即文本契约**：工具不向 LLM 抛出业务错误，而是返回以 `Error:` 为前缀的可读字符串。

---

## 架构

```
agent/tools/taskflow/
├── __init__.py              # 包导出（重导出 11 个工具）
├── config.py                # TaskFlowStatus、StepStatus 枚举，TERMINAL_STATUSES，TABLE_NAME
├── step_judge.py            # StepJudge —— 辅助 LLM 判别器（pass/retry/block）
├── evidence_collector.py    # 为判别器提示词渲染会话/流证据摘要
├── registry/
│   ├── __init__.py
│   └── store_sqlite.py      # SQLite 持久化：create/get/update，WAL，busy_timeout，
│                            #   FlowConflictError/FlowNotFoundError/FlowExistsError，
│                            #   同步路径（get_flow_sync）
└── tools/
    ├── __init__.py           # build_taskflow_tools() → 14 个工具，scope=main_only
    ├── _dispatch.py          # 可 monkeypatch 的派发接缝（spawn_subagent_direct）
    ├── _shared.py            # DAG 辅助函数 + 冲突重试持久化
    ├── taskflow_create.py    # 创建流，初始版本号 1
    ├── taskflow_run_task.py  # 注册步骤 + 派发（或因依赖未满足而阻塞）
    ├── taskflow_dispatch.py  # 批量派发多个就绪步骤
    ├── taskflow_wait_all.py  # 有界轮询等待已派发步骤完成
    ├── taskflow_resume.py    # 注入结果、标记完成、解锁后继（幂等）
    ├── taskflow_set_waiting.py # 将流挂起为 waiting 状态
    ├── taskflow_summary.py   # 只读重读（亦用于冲突后重读）
    ├── taskflow_progress.py  # 只读进度报告
    ├── taskflow_budget.py    # Token/成本预算查询与设置
    ├── taskflow_update_steps.py # 步骤列表全量替换
    ├── taskflow_list.py      # 会话级流面板
    ├── taskflow_finish.py    # 标记完成（终态）
    ├── taskflow_fail.py      # 标记失败（终态）
    └── taskflow_cancel.py    # 取消流（终态）
```

### 注册方式

工具通过 `agent/tools/taskflow/tools/__init__.py` 中的 `build_taskflow_tools()` 注册，返回全部 14 个工具，标记 `metadata = {"scope": "main_only"}` 和 `handle_tool_error = True`。子代理工具策略无条件丢弃它们——只有主代理管理共享流状态。

---

## 状态机

### 流状态（`TaskFlowStatus`）

```
running ──→ waiting ──→ running（通过 taskflow_resume）
  │                       └──→ done    （taskflow_finish，终态）
  │                       └──→ failed  （taskflow_fail，终态）
  │                       └──→ cancelled（taskflow_cancel，终态）
  └──→ done / failed / cancelled（直接从 running）
```

终态（`done`、`failed`、`cancelled`）不可变：每个变更工具在执行任何状态变更前检查 `is_terminal(status)` 并拒绝操作，返回 `Error: TaskFlow '<id>' is terminal (status=...); no further mutations allowed`。

### 步骤状态（`StepStatus`）

```
blocked → ready → dispatched → done
```

| 状态          | 含义                                                                          | 转换触发                                                     |
| ------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `blocked`     | 依赖尚未全部 `done`；`taskflow_run_task` 仅注册不 spawn                       | `taskflow_resume` 在依赖满足时解锁                           |
| `ready`       | 依赖已满足，等待派发                                                          | `taskflow_dispatch` 派发                                     |
| `dispatched`  | 已 spawn 分离式子代理会话，`child_session_key` 已持久化                       | `taskflow_resume` 注入结果                                   |
| `done`        | 结果已通过 `taskflow_resume` 注入，**且该步骤配置的质量门全部通过**           | （此步骤的终态）                                             |
| `failed`      | 确定性失败：调用方声明 `step_outcome="failure"`，或 `response_schema` 结果在重试预算耗尽后仍不合格 | （此步骤的终态；其依赖者保持 `blocked`）                     |
| `skipped`     | 声明 `step_outcome="skipped"`，或某个依赖被跳过/取消（跳过会向下级联）        | （此步骤的终态）                                             |
| `cancelled`   | `taskflow_cancel` 在该步骤完成前取消了整个流                                  | （此步骤的终态）                                             |

> `done` 表示"结果已注入，且该步骤配置的质量门全部通过"——在**未配置任何期望**（`response_schema`、`judge_criteria`/`validation_criteria`、`retry_policy`）时退化为旧含义"已注入结果"，因此调用方不声明 `step_outcome="failure"` 时，失败的子代理仍可能落到 `done`。 完整规则与每道门各自的作用见[判断门](../gates/README.zh.md)。
>
> `failed`、`skipped`、`cancelled` 都不会解锁依赖者：`failed` 的依赖会让依赖者保持 `blocked`（失败不能被静默跳过），而 `skipped`/`cancelled` 的依赖会把 `skipped` 级联给依赖者，让死分支就地结束而不是让整个流永远等待。

### 期望 → 实际闭环

步骤可以携带**期望**，resume 路径会把**实际**与之配对记录，于是"这一步有没有做到要求的事"只从流状态就能回答。

期望字段（全部可选，由 `taskflow_run_task` 写入并存在步骤上）：

| 字段                  | 含义                                                                                     |
| --------------------- | ---------------------------------------------------------------------------------------- |
| `response_schema`     | 结构化结果必须满足的 JSON Schema（Tier 1）。同时经 spawn 层的 `output_schema` 交给子代理作为输出契约。 |
| `judge_criteria`      | 交给 LLM 步骤判别器的自然语言验收标准（Tier 2）。`judge_model` 可覆盖判别器所用模型。    |
| `expected_params`     | 该步骤本应处理的结构化输入（仅记录，不对其做强制校验）。                                 |
| `input_bindings`      | `{"param": "step-A.structured_result.files"}`——把上游结构化结果以 `## Input parameters` 块追加到本步骤派发的任务文本。 |
| `validation_criteria` | 旧版文本标准；存在时仍会判别（`judge_criteria` 缺省时使用）。                            |
| `retry_policy`        | 既有的失败重试策略（`max_retries`、`retry_delay_seconds`、`retry_on`）。                 |
| `file_deps`           | 本步骤验证所依赖的项目相对源文件；其他步骤写入其中之一时，finish 时的重验会优先执行本步骤。 |

实际字段（由 `taskflow_resume` 写入结果条目）：

| 字段                | 含义                                                                          |
| ------------------- | ----------------------------------------------------------------------------- |
| `step_id`           | 配对键：这条结果属于哪个步骤。                                                |
| `structured_result` | 解析出的结构化值（来自调用方的 `structured_result` 或 `result`）。            |
| `schema_validated`  | `true` / `false` / `null`（未配置 schema）——Tier 1 的判定结论。               |
| `step_outcome`      | 调用方声明时记录（`success` / `failure` / `partial` / `skipped`）。           |

两层质量门按上述顺序串联，且都按步骤可选（具体执行顺序见下方 `taskflow_resume`）。

### 旧步骤兼容

无 `status` 字段的步骤由 `step_status()` 处理：有 `child_session_key` 的步骤视为 `dispatched`，否则视为 `ready`。这保证无该字段的流保持向后兼容。

---

## 工具族（14 个工具）

### taskflow_create

```python
async def taskflow_create(flow_id: str, description: str = "", initial_state: dict | None = None,
                         deadline_hours: float | None = None, resumed_from: str | None = None) -> str
```

创建持久化流，初始 `INITIAL_REVISION = 1`，状态为 `running`。返回流 id、状态和版本号。如果 id 已被占用则抛出 `FlowExistsError`（返回错误字符串，含当前版本号以便重读）。

当新流**取代**旧流时传 `resumed_from=<前驱 flow_id>`：新流记录 `resumed_from`，前驱被盖上 `superseded_by` —— 见[血缘指针](#血缘指针)。未知的前驱在创建任何东西之前就被拒绝；自引用同样被拒绝。

资源上限（在此强制，超限返回 `Error:` 文本）：单个流最多 `TASKFLOW_INFRA["max_steps_per_flow"]`（20）步；单个会话最多 `TASKFLOW_INFRA["max_flows_per_session"]`（20）个**活跃**流——只计 running/waiting，完成或取消的流会释放名额（被取代的流同样释放）。`taskflow_plan` 在花费 planner 调用之前应用同一流数上限。

### taskflow_run_task

```python
async def taskflow_run_task(
    flow_id: str, task: str, label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    aggregate_deps: bool = False,
    response_schema: dict | None = None,
    expected_params: dict | None = None,
    input_bindings: dict | None = None,
    judge_criteria: str | None = None,
    judge_model: str | None = None,
    functional_role: str | None = None,
    step_model: str | None = None,
    step_timeout_seconds: float | None = None,
    priority: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

在流上注册一个步骤并把它派发给分离式子代理。步骤 id 自动递增为 `step-1`、`step-2` 等。

- **无依赖**（或依赖已满足）→ 步骤立即 `dispatched`（已 spawn 子代理）。
- **依赖未满足** → 步骤登记为 `blocked`，不 spawn。未知依赖 id 在任何 spawn 或状态变更之前就拒绝。
- **期望参数** 会写入步骤并在 resume 时生效：`response_schema`（同时作为 `output_schema` 交给 spawn 层）、`judge_criteria` / `judge_model`、`expected_params`、`input_bindings`。
- **执行元信息** 随行下发：`functional_role`（researcher / executor / reviewer / librarian / general）、`step_model`、`step_timeout_seconds`；`priority` 是派发排序提示。
- 未给出的字段不会写进步骤，这也正是"没有期望的流与闭环前格式逐字节一致"的原因。
- 返回 `step_id`、`child_session_key`、`revision`（被阻塞时返回 `pending=[...]` 列出未满足的依赖）。
### taskflow_dispatch

```python
async def taskflow_dispatch(
    flow_id: str, step_ids: list[str],
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

批量派发一个或多个当前就绪的步骤。步骤在 `ready` 状态，或虽然 `blocked` 但依赖已满足时可派发。

- **全有或全无验证**：在任何 spawn 之前验证全部 id。未知 id、已派发/已完成步骤、或依赖未满足的阻塞步骤会拒绝整个调用，零 spawn。
- **批量中途失败**：如果 spawn 在批量中途失败，已 spawn 的步骤在一次 `update_flow` 中持久化，不会遗漏子代理。错误信息标明失败和已派发的步骤 id。
- 流级 `child_session_key` 刻意不被修改——每步骤的 child key 是权威来源。

### taskflow_wait_all

```python
async def taskflow_wait_all(
    flow_id: str, timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 0.5,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

等待此流的已派发子代理会话完成（或超时）。仅轮询此流已派发步骤上记录的子代理会话——无关的后台子代理永远不会阻塞返回。

- 未知/缺失的 run 视为已完成（永不挂起）。
- 超时时返回部分报告，指示用 `taskflow_resume` 处理已完成的子代理并再次调用 `wait_all`。
- 注册表接缝（`get_run_by_child_session_key` / `is_live_unended_run`）延迟导入且可注入，便于测试。

### taskflow_resume

```python
async def taskflow_resume(
    flow_id: str, child_session_key: str = "", result: str = "",
    expected_revision: int | None = None, token_usage: dict | None = None,
    validation_criteria: str | None = None,
    structured_result: dict | None = None,
    step_outcome: str | None = None,
    auto_dispatch: bool = True,
) -> str
```

把已完成的子会话结果注入流状态（幂等）。向 results 追加 `{child_session_key, result, result_hash, injected_at, step_id, schema_validated}`（已知时还包括 `structured_result` / `step_outcome`）。若流处于 `waiting`，则回到 `running`。

**执行顺序** —— 各道门串联、且每个都按步骤可选：`retry_policy`（分类出的失败，或 `schema_validated=false`，在**任何判别调用之前**把步骤重新派发到替换子代理）→ `response_schema`（Tier 1，免费校验；未通过即 `schema_error` 重试）→ `judge_criteria` / `validation_criteria`（Tier 2，辅助 LLM 判别）→ 调用方声明的 `step_outcome`，它压倒以上全部。判定取值、预算、证据感知的判别提示词、fail-open 规则与 `done` 的真实含义，统一记录在[判断门](../gates/README.zh.md)。

**DAG 记账**：质量门通过时把对应步骤标 `done` 并调用 `unlock_dependents()`，把依赖已满足的 `blocked` 步骤转为 `ready`；`failed` 的步骤让其依赖者保持 `blocked`，`skipped`/`cancelled` 的步骤会把 `skipped` 级联给依赖者。返回被解锁的步骤 id。写入在冲突重试时会把已判定的结论重新应用到新读取的状态上，因此两个子代理同时完成时两个结果都会落地（见[带已派生子代理的冲突重试](#带已派生子代理的冲突重试)）。`auto_dispatch=True`（默认）时新就绪的波次会被立即以有界并发批量 spawn——见[自动派发波次](#自动派发波次taskflow_resume)；传 `auto_dispatch=False` 则只解锁，由调用方显式 `taskflow_dispatch`。
### taskflow_set_waiting

```python
async def taskflow_set_waiting(
    flow_id: str, wait_reason: str = "",
    expected_revision: int | None = None,
) -> str
```

将流挂起为 `waiting` 状态，记录等待原因。等待载荷由 `taskflow_resume` 清除。

### taskflow_summary

```python
async def taskflow_summary(flow_id: str) -> str
```

只读地重读整个流状态：状态、revision、child_session_key、description、全部步骤（含状态、depends_on、child_session_key，以及配置的期望——`response_schema` 的 properties/required、`judge_criteria`、`judge_model`、`input_bindings`、`expected_params`、执行元信息和任何 `block_reason` / `fail_reason` / `skip_reason`）、各状态计数、结果（每条含 `step_id`、`schema_validated`、`structured_result`、`step_outcome`、`judge_verdict` / `judge_reason` 以及记录到的 `token_usage`）、wait 负载、summary、failure_reason、cancel_reason。也是版本冲突后的指定重读步骤。
### taskflow_progress

```python
async def taskflow_progress(flow_id: str) -> str
```

只读完成度报告：完成百分比、完整状态分布（`done` / `dispatched` / `ready` / `blocked` / `failed` / `skipped` / `cancelled`）、值得启动的下一步、以及 **Needs a decision** 清单——列出每个 `failed` / `blocked` / `skipped` / `cancelled` 步骤及其记录的原因；当至少两个 `done` 步骤带 `dispatched_at` 时间戳时还给出预计剩余时间。永不修改流。
### taskflow_budget

```python
async def taskflow_budget(
    flow_id: str, action: str = "query", token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

查询（`query`）或设置（`set`）流的 Token/成本预算。`query` 报告 `total_tokens`、`total_cost`、预算、剩余 Token 以及状态（`ok` / 80% 时 `WARNING` / `EXCEEDED`）；`set` 要求正整数 `token_budget`，并通过乐观锁写入。

### taskflow_update_steps

```python
async def taskflow_update_steps(
    flow_id: str, steps: list[dict],
    expected_revision: int | None = None,
) -> str
```

全量替换流的步骤列表（类似 TaskFlow 的 `todowrite`）：可新增、删除、重排步骤，或改写其 `task`/`depends_on`。安全规则保证 `dispatched` 步骤仍绑定其 `child_session_key`，并拒绝改写 `done` 步骤；删除子代理仍在运行的 `dispatched` 步骤会成功，但返回非阻塞的 `Warning:` 并指出子代理 key。

### taskflow_list

```python
async def taskflow_list(status_filter: str = "active") -> str
```

本会话流的只读面板：`"active"`（running + waiting）、`"all"`（含终态）或精确状态名。每次读取都在 SQL 中按所属 `session_id` 过滤；渲染表格将描述截断为 40 字符，并直接从 flow 行读取 `updated_at`（没有该列值的旧行回退到旧活动时间戳）。

### taskflow_finish / taskflow_fail / taskflow_cancel

```python
async def taskflow_finish(flow_id: str, summary: str = "", expected_revision: int | None = None,
                          todo: dict | None = None, plan_path: str | None = None,
                          checkbox_label: str | None = None) -> str
async def taskflow_fail(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
async def taskflow_cancel(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
```

终态转换。`finish` 记录 `summary`，`fail` 记录 `failure_reason`，`cancel` 记录 `cancel_reason`。已派发的子会话会继续运行；在流被取消之前，它们的结果仍可通过 `taskflow_resume` 送达。

`finish` 按顺序用四项检查把守 DONE 转换——**A** 每个步骤都是 `done` 或 `blocked`；**B** 不留未决步骤（`failed`、`skipped`、`cancelled`、`blocked` 的步骤按 id 附原因报出）；**C** 流程证据中没有 `FAIL`、也没有 `[stale]` 行；**E** 可选的目标准入门（流程步骤数达到 `GOAL_GATE["min_steps"]`（3）及以上）。每道门都 fail-open；拒绝文本与目标门的契约记录在[判断门](../gates/README.zh.md)。



`cancel` 会把所有未完成的步骤标为 `cancelled`（连同取消原因）再翻转流终态，这样流终结后步骤状态不会继续声称 `ready` / `dispatched` / `blocked`。
---

## 乐观锁与冲突重试

所有变更通过 `UPDATE ... WHERE flow_id = ? AND expected_revision = ?` 执行。当更新匹配零行时，写入发生冲突（或流已消失）：

- **`FlowConflictError`**：携带最新版本号，调用者可重读并重试。错误文本内嵌可用值：`expected_revision=2 but latest revision=3; re-read with taskflow_summary and retry with expected_revision=3`。
- **`FlowNotFoundError`**：流已被删除。

**重试流程**（从 LLM 视角）：

1. 调用 `taskflow_summary(flow_id)` 重读，获取最新 `revision`。
2. 用 `expected_revision=<最新版本号>` 重新执行变更。
3. 冲突错误文本包含可直接使用的重试值。

终态流不可变：每个变更工具在任何状态变更前检查 `is_terminal(status)`。

---

## DAG 依赖系统

DAG 字段完全存放在 `state_json` 中（无需数据库迁移）。`StepStatus` 枚举定义四种状态，`_shared.py` 中三个纯函数管理转换：

### deps_satisfied(step, steps) → bool

当且仅当每个 `depends_on` id 存在于 `steps` 中且为 `done` 时返回 True。缺失/空的 `depends_on` 平凡满足。未知 dep id 永不满足。**自依赖永不满足**，使自引用步骤安全保持 blocked 而不会产生无限解锁循环。

### mark_step_done(steps, child_session_key) → str | None

标记匹配 `child_session_key` 的步骤为 `done`。幂等：对同一 key 的重复调用返回相同步骤 id。无步骤携带该 child key 时返回 `None`。

### unlock_dependents(steps) → list[str]

对 `steps` 做**单次遍历**：把依赖刚刚满足的 `blocked` 步骤转为 `ready`，返回本次解锁的步骤 id。单次遍历确保依赖环永远无法死循环。解锁是**失败感知**的：以 `failed` 结束（或仍处于 `blocked` / 执行中）的依赖会让依赖者保持 `blocked`，而被 `skipped` 或 `cancelled` 的依赖会把 `skipped` 连同 `skip_reason` 级联给依赖者——死分支就地结束，而不是让整个流永远等待。
---

### 合成——`aggregate_deps`

向 `taskflow_run_task` 传入 `aggregate_deps=true`，即可让合成步骤在其派发任务文本后收到依赖步骤的记录结果。聚合由 `agent/tools/taskflow/tools/_shared.py::build_task_with_dep_results(step, steps, results)` 构建：按 `depends_on` 顺序，用每个依赖步骤的 `child_session_key` 在 flow 的 `{child_session_key, result, result_hash}` 台账中匹配结果，并追加到 `## Upstream Results` 标题之下。没有记录结果的依赖会写入 `no result recorded` 占位符。

该标志存储在步骤上，因此 `taskflow_dispatch`、StepJudge 重试与重试策略路径都会重新推导聚合，而不会改写已存储的 `task`。默认（`aggregate_deps` 缺省或为 false）保持派发文本不变。

## 并行步骤执行

两个工具支持独立步骤的并行执行：

### 批量派发（`taskflow_dispatch`）

在任何 spawn 之前验证整个批次（全有或全无）。随后整个批次通过共享 `_dispatch.dispatch_child` 接缝**并发** spawn，并发上限为 `TASKFLOW_INFRA["max_parallel_dispatch"]`（默认 2：SUBAGENT 泳道才是全局背压，这个上限只决定同时有多少个 spawn 调用在途）。单个 spawn 失败不再中止兄弟步骤——每个成功的子代理都会在一次 `update_flow` 调用中持久化，每个失败的步骤 id 都会出现在应答里（`Error: dispatch failed for step_id(s) …; dispatched=[…]`）。`apply_dispatched_steps()` 辅助函数将已记录的已派发步骤载荷重新应用到新读取的步骤列表上，按 `step_id` 匹配——已 spawn 的子代理不会因并发写入重写步骤列表而丢失。`max_parallel_dispatch = 1` 精确还原历史上的顺序行为。

### 自动派发波次（`taskflow_resume`）

解锁了依赖者的 resume 会顺带把它们启动起来（`auto_dispatch=True`，默认值）：新就绪的波次通过同一接缝、同一 `max_parallel_dispatch` 上限并发 spawn，且 spawn 出的子代理键通过 CAS 重试写入（`update_flow_with_conflict_retry`）持久化，并发写入无法丢掉它们。每个步骤的派发方式与 `taskflow_dispatch` 完全一致（任务绑定 + 判别反馈）。失败只报告、不抛出：spawn 不成的步骤保持 `ready`，resume 的应答里带 `auto_dispatch_failed=<step_id>: <error>` 供手动重试。由于解锁集合来自**实际持久化**的状态，两个完成事件争抢解锁同一步骤时只会 spawn 一次。

### 有界轮询等待（`taskflow_wait_all`）

流级范围：仅轮询此流已派发步骤上记录的子代理会话。不会因无关后台子代理而阻塞。轮询间隔有下限（最小 `0.05s`）。未知/缺失的 run 视为已完成（永不挂起）。超时返回部分报告。

---

### 实时进度

步骤按**扁平列表**上报：每个 flow 的每个步骤只带 `step_id`、任务文本和状态，另有按状态汇总的计数（`agent/tools/taskflow/progress_push.py`）。调度仍然按每个步骤自身的依赖解锁；不存在波次/轮次投影，因此 payload 不会暗示步骤组之间的屏障。

聊天区右上角的悬浮进度框（默认折叠成药丸）同时渲染这些扁平步骤与会话待办清单。两半数据都经 WebSocket 到达：每次任务流变更都会推送 `taskflow_updated`，其载荷与 `taskflow_refresh` 请求的应答完全一致，因此重连后一帧即可恢复看板，全程无需轮询。

## 幂等恢复

`taskflow_resume` 使用 SHA-256 截断为 16 位十六进制字符对 `(child_session_key, result)` 对进行指纹化。注入前检查流的 `results` 列表中是否已存在相同 `result_hash`。若存在，调用为 no-op：既不重新注入也不递增版本号。这使 announce 管线的重复投递变得安全——重复投递不会损坏状态。

---

## 带已派生子代理的冲突重试

当子代理会话已经 spawn 但乐观锁写入竞争失败时，子代理绝不能被静默丢弃。`_shared.py` 中的 `update_flow_with_conflict_retry()` 处理此场景：

1. 成功 spawn 后，调用者使用 `build_state` 回调调用此函数。
2. 遇到 `FlowConflictError` 时：重读流，对最新数据重新调用 `build_state(fresh_flow, attempt)`，最多重试 `PERSIST_MAX_ATTEMPTS = 3` 次。
3. 遇到 `FlowNotFoundError` 或终态新流：返回错误，标明每个已 spawn 的 `child_session_key`——调用者必须按 key 恢复，而非重新派发。
4. 重试次数耗尽：同样返回错误，标明所有 child key。

`build_state` 回调接收新流和尝试次数，可基于最新数据重建状态（例如根据当前步骤计数重新分配 `step_id`）。

---

## 血缘指针

`task_flows` 上两个可空列记录流的演进（`resumed_from TEXT`、`superseded_by TEXT`，与 token 列相同的增量迁移模式——旧库在下次打开时收敛，其行读作 NULL）：

| 列 | 写入时机 | 含义 |
| -- | -------- | ---- |
| `resumed_from` | 流创建时（`taskflow_create(resumed_from=…)`） | 本流延续的前驱 flow_id |
| `superseded_by` | 后继创建时，盖到前驱上 | 取代本流的后继 flow_id |

写入点是**创建**路径，而不是 `taskflow_replan`：replan 在**原地**重生成流的剩余步骤（对同一 `flow_id` 走 `update_flow_with_conflict_retry`），从不分叉新流，也就没有血缘可记。`taskflow_finish` 从不触碰这两列——完成是终态，不是取代。

后果：`get_active_flows_sync` 会排除被取代的流，即便其状态仍是 `running`/`waiting`（续跑 enforcer、进度面板与会话级流数上限都走这个读口），而 `get_all_flows_sync(session_id, "all")` 仍会带着反向指针展示它——历史视图保留它。`taskflow_summary` 渲染两个指针（`resumed_from: …`、`superseded_by: …`）。反向指针的写入是普通的会话级 UPDATE，**刻意不递增** `expected_revision`：血缘是元数据，不能让持有前驱版本号的调用方失效。

---

## 持久化与连接生命周期

`store_sqlite.py` 映射子代理注册表蓝图：

- **数据库**：`agent/tools/taskflow/data/taskflow_registry.db`
- **表**：`task_flows(flow_id TEXT PK, state_json TEXT NOT NULL, wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL, child_session_key TEXT)`
- **WAL 模式**：每个进程通过 `_switch_to_wal_if_needed()` 切换一次；文件已是 WAL 时跳过 pragma。
- **忙等待超时**：每个连接 `5000ms`（第一条语句）。journal-mode 切换不总是遵守此设置，通过 try/except 容错单独处理。
- **异步初始化**：每个进程一次，在调用方事件循环拥有的 `asyncio.Lock` 下；其他循环轮询 `_initialized`（永不触碰外循环的锁——避免非线程安全的 `call_soon` 唤醒）。
- **同步路径**：`get_flow_sync()` 使用标准库 `sqlite3`，通过 `threading.Lock` 保护的一次性建表。失败时记录日志并以 `None` 返回（用于无事件循环的系统提示注入场景）。

---

## 已知限制

- **`done` ≠ 成功**：步骤 `done` 表示"结果已注入**且该步骤配置的质量门全部通过**"。在**未配置任何期望**（`response_schema`、`judge_criteria`/`validation_criteria`、`retry_policy`）时退化为旧含义，因此除非调用方声明 `step_outcome="failure"`，失败的子代理仍会落到 `done`。要让闭环接住失败：声明期望（`response_schema` 的校验是免费的，且在判别调用之前执行）或声明结果；`failed` 会让依赖者保持阻塞，而 `retry_policy` 仍在预算内重新派发已结束的子代理（判别器对 `block` 判定或预算耗尽依旧标记 `blocked`）。 完整规则与每道门各自的作用见[判断门](../gates/README.zh.md)。
- **`taskflow_wait_all` 超时为有界轮询**：永不完成的子代理会话不会自动使流失败。超时返回部分报告。
- **步骤 id 为顺序分配**：注册时分配 `step-{len(steps)+1}`。如果步骤被并发追加，id 在冲突重试时由 `build_state` 重新计算。
