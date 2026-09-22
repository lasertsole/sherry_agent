# 🧩 TaskFlow 引擎 — DAG、重试、预算、截止时间与面板

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Long-Running Tasks](../README.zh.md) 的一部分：持久化 DAG 引擎、步骤重试策略、Token/成本预算、截止时间、结果校验、进度报告、空闲检测与会话面板。

---

## 🧩 TaskFlow DAG 引擎

### 状态枚举（`agent/tools/taskflow/config.py`）

```python
TABLE_NAME = "task_flows"          # config.py:5
INITIAL_REVISION = 1               # config.py:8

class TaskFlowStatus(StrEnum):     # config.py:11
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class StepStatus(StrEnum):         # config.py:21
    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})   # config.py:35
```

### 表结构（`agent/tools/taskflow/registry/store_sqlite.py`）

```sql
CREATE TABLE IF NOT EXISTS task_flows (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL,
    session_id TEXT NOT NULL DEFAULT ''
);
```

DAG 本身（`steps[]`、`results[]`、`depends_on`、`creator_session_key`）完全存放在 `state_json` 中——新增 DAG 字段无需迁移表结构。token/成本/截止时间列来自增量 DDL（`_TOKEN_COLUMN_DDL`、`_DEADLINE_COLUMN_DDL`、`_SESSION_ID_COLUMN_DDL`，`store_sqlite.py:83-107`）。`session_id` 是隔离列（由 `idx_taskflow_session_status` 索引）：创建时写入，之后每次读取/变更都按它过滤（`WHERE flow_id = ? AND session_id = ?`、`WHERE session_id = ? AND status IN (…)`）。WAL 模式每个进程只切换一次，且每条语句之前都会执行 `PRAGMA busy_timeout = 5000`（`agent/tools/pub_base/sqlite_store.py:79-139`）。

### 步骤状态机

```
blocked ──(依赖满足)──▶ ready ──(派发)──▶ dispatched ──(恢复)──▶ done
```

| 状态 | 含义 | 由谁设置 |
| :--- | :--- | :--- |
| `blocked` | 至少有一个 `depends_on` 尚未 `done`；不生成子 Agent | `taskflow_run_task` |
| `ready` | 所有依赖已满足，等待派发 | 注册时，或恢复后的 `unlock_dependents` |
| `dispatched` | 已派发一个分离的子会话；记录 `child_session_key` 与 `dispatched_at` | `taskflow_run_task` / `taskflow_dispatch` |
| `done` | 已由 `taskflow_resume` 注入结果 | `taskflow_resume` |

`done` 的含义是**“已注入结果”**，而不是“子 Agent 成功”（参见[已知局限](../README.zh.md#-已知局限)）。没有 `status` 字段的旧步骤会被推导：带 `child_session_key` 的步骤视为 `dispatched`，否则视为 `ready`（`_shared.py:85`，`step_status`）。

### `depends_on` 语义

`deps_satisfied(step, steps)`（`_shared.py:99`）为真，当且仅当每个 `depends_on` id 都存在于当前步骤列表中**且**为 `done`。缺失/为空的 `depends_on` 视为自然满足。未知的依赖 id 永远不满足，而**自依赖永远不满足**——因此自引用步骤会安全地保持阻塞，而不会陷入解锁死循环。`unlock_dependents(steps)`（`_shared.py:137`）对列表只做**单遍扫描**，从结构上杜绝了依赖环导致死循环。`taskflow_run_task` 会在任何派发或状态变更**之前**拒绝未知的依赖 id。

### 工具族（14 个工具）

所有工具都是 `async`，装饰为 `@tool("taskflow_…")`，由 `build_taskflow_tools()`（`tools/__init__.py:58-76`）打上 `metadata={"scope": "main_only"}` 和 `handle_tool_error=True`。只有主 Agent 可以管理共享 flow 状态；子 Agent 工具策略会无条件丢弃整个工具族。

| 工具 | 用途 |
| :--- | :--- |
| `taskflow_create` | 在版本 1 创建 flow；可选设置 `deadline_hours` |
| `taskflow_run_task` | 注册一个步骤（可选 `validation_criteria` / `retry_policy`）并派发（或记录为 `blocked`） |
| `taskflow_dispatch` | 全有或全无地批量派发多个就绪步骤 |
| `taskflow_update_steps` | 全量替换 steps 列表（增/删/重排/改 task 或 depends_on；dispatched/done 安全规则；孤儿 child 警告） |
| `taskflow_wait_all` | 按 flow 范围有界轮询，等待已派发步骤落定（对带策略的落定步骤自动重试） |
| `taskflow_resume` | 幂等地注入子结果、解锁后继步骤、聚合 token（感知失败重试 + 步骤判别器） |
| `taskflow_set_waiting` | 带上原因把 flow 置为 `waiting` |
| `taskflow_summary` | 只读回读（也是冲突后的重读手段） |
| `taskflow_progress` | 人类可读的进度/完成度报告 |
| `taskflow_budget` | 查询或设置 token/成本预算 |
| `taskflow_list` | 本会话面板（`active` / `all` / 状态名） |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | 终态转换 |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:40
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    session_id: SessionId = "",
) -> str
```

### 派发——`taskflow_dispatch` 批量语义

`taskflow_dispatch(flow_id, step_ids, expected_revision=None, session_id="")`（`taskflow_dispatch.py:36`）在生成任何子 Agent **之前**校验**每一个** id。当步骤状态为 `ready`，或 `blocked` 但依赖已满足时，它就是可派发的。未知 id、重复 id，或已经 `dispatched`/`done` 的步骤，都会整体拒绝该次调用且不产生任何派发。成功后，步骤通过共享的 `_dispatch.dispatch_child` 接缝（`_dispatch.py:10`）顺序派发，并在**一次** `update_flow` 调用中持久化。若批次中途派发失败，循环停止，已派发的子 Agent 会被持久化，确保没有子会话被静默丢弃，错误信息同时列出失败与已派发的 step id。flow 级别的 `child_session_key` 刻意不被改动——每个步骤自己的 child key 才是权威。

### 更新——`taskflow_update_steps` 全量替换

`taskflow_update_steps(flow_id, steps, expected_revision=None, session_id="")`（`taskflow_update_steps.py:191`）全量替换 flow 的 steps 列表（对 TaskFlow 而言类似 `todowrite`）：存储的 DAG 就是你传入的列表，因此可以增、删、重排步骤，或重写其 `task`/`depends_on`。安全规则：`step_id` 必须唯一；每个 `depends_on` 必须引用新列表内存在的 id（禁止自依赖）；`dispatched` 步骤保留其 `child_session_key`，不可降级为 `ready`/`blocked`；`done` 步骤不可修改 task/depends_on/status；新增步骤必须为 `ready`/`blocked`（派发仍走 `taskflow_dispatch`）；终态 flow 拒绝该调用。删除仍在运行的 `dispatched` 步骤会成功，但返回非阻塞的 `Warning:`（含 child key）——请先 kill 该 child，或用 `taskflow_wait_all`/`taskflow_resume` 让它落定。并发沿用同一乐观锁：`expected_revision` 不匹配时返回最新 revision，供重读后重试。

### 等待——`taskflow_wait_all` 的 flow 范围

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")`（`taskflow_wait_all.py:110`）**只**轮询记录在*当前* flow 的 `dispatched` 步骤上的子会话，因此无关的后台子 Agent 永远不会阻塞返回。未知或已被清理的 run 会被视为已落定（永不挂起）。轮询间隔被夹紧到 `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]`（0.05 秒）。超时后返回部分报告，并提示对已落定的子 Agent 调用 `taskflow_resume`，然后再次调用 `wait_all`。它刻意**不**复用 `sessions_yield`，后者只在请求方会话的全部子 Agent 落定时才触发。

### 乐观锁

每一次变更都走 `UPDATE … WHERE flow_id = ? AND expected_revision = ?`，并把版本号恰好加 1（`store_sqlite.py:393-406`）。匹配到零行即表示冲突：

```python
# FlowConflictError 消息（store_sqlite.py:177）
"TaskFlow '<id>' revision conflict: expected_revision=2 but latest revision=3;
 re-read with taskflow_summary and retry with expected_revision=3"
```

当子 Agent **已经派发**但写入竞争失败时，`update_flow_with_conflict_retry()`（`_shared.py:191`）会重新读取最新的 flow，通过 `build_state` 回调重建状态，并最多重试 `PERSIST_MAX_ATTEMPTS = 3` 次。若 flow 已消失或变为终态，或重试耗尽，它会返回一个列出所有已派发 `child_session_key` 的错误，并指示调用方**按 key 回收，绝不重新派发**。

## 🔁 步骤重试策略

步骤可以携带一个声明式的 `retry_policy`，让失败的子 Agent 被自动重新派发，而不是被直接当成本步骤的最终结果。策略、计数器与替换子会话 key 全部存放在 `state_json` 中（无需迁移表结构），重试辅助函数位于 `agent/tools/taskflow/tools/_retry.py`。

```python
# retry_policy on a step (stored by taskflow_run_task)
{"max_retries": 2, "retry_delay_seconds": 30.0, "retry_on": ["timeout", "rate_limit"]}
```

| 字段 | 类型 | 默认值 | 含义 |
| :--- | :--- | :--- | :--- |
| `max_retries` | 非负整数 | `0` | 最大**重新**派发次数 |
| `retry_delay_seconds` | 非负数值 | `60.0`（`DEFAULT_RETRY_DELAY_SECONDS`） | 每次重新派发前休眠的退避时长 |
| `retry_on` | `list[str]` | `[]` | 触发重试的失败类型；为空表示所有可分类失败 |

`validate_policy()`（`_retry.py:120`）会在 `taskflow_run_task` 阶段拒绝格式非法的策略——非 dict 策略、负数/非整数 `max_retries`、负数/非数值 `retry_delay_seconds`，或不是字符串列表的 `retry_on`——在任何派发或写入之前返回 `Error:` 字符串。在恢复时，缺失/非法的已存策略会退化为 `None`（`normalize_policy`），从而回退为无重试行为，而不是让该次调用失败。

`retry_count`（存放在步骤上，默认 `0`）统计的是**重新派发**次数，从不统计最初那次派发：第一个子 Agent 运行时为 `0`，生成第一个替换子 Agent 后为 `1`。只要 `retry_count < max_retries` 就允许重试（`retries_remaining`，`_retry.py:95`）。`apply_redispatch()`（`_retry.py:170`）就地改动步骤——写入新的 `child_session_key`、`dispatched_at`、`status = dispatched`，并递增 `retry_count`。

### 失败分类

`classify_failure(result)`（`_retry.py:56`）是一个基于**结果文本的启发式**分类器。它先把文本转为小写，剥离字面上包含失败词但实际表示成功的措辞（`_NEGATED_FAILURE_PHRASES`，例如 `"no error"`、`"error-free"`），然后返回第一个匹配的有序桶：

| 分类类型 | 触发子串 |
| :--- | :--- |
| `timeout` | `timeout`、`timed out`、`time limit` |
| `rate_limit` | `rate limit`、`rate_limit`、`429`、`too many requests` |
| `error` | `error`、`failed`、`failure`、`exception`、`traceback`、`aborted`、`crashed` |

干净的结果永不重试。当 `retry_on` 非空时，只有匹配的分类类型才重试；当它为空时，任何可分类失败都会重试（`should_retry_failure`，`_retry.py:103`）。

### 两条自动重新派发路径

| 入口 | 触发条件 | 行为 |
| :--- | :--- | :--- |
| `taskflow_wait_all` | 被轮询的子 Agent **死亡**落定且无结果 | `plan_settled_retries()` 在预算尚存时对每个落定步骤重新派发一次，否则以失败说明结果把它标记为 `done` |
| `taskflow_resume` | 注入的结果文本**可分类为失败**且被 `retry_on` 允许 | 生成替换子 Agent、记录该失败结果，并让步骤在新子 Agent 上保持 `dispatched` |

- **`taskflow_wait_all`** 在每个目标落定后调用 `_retry_settled_steps()`（`taskflow_wait_all.py:117`）。没有策略的步骤不产生任何动作（旧 flow 保持逐字节一致的输出）；结果已被注入的子 Agent 会被跳过。每次调用对每个落定步骤最多执行**一个**重试决策——生成并记录替换子 Agent，随后编排者再次调用 `wait_all` 等待它。工具内部不存在后台重试循环。替换子 Agent 通过 `persist_retry_actions()`（`_retry.py:254`）持久化，它借助 `update_flow_with_conflict_retry()` 把计划重新应用到刚读取的步骤列表上，因此并发写入者无法丢弃已生成的替换子 Agent。
- **`taskflow_resume`** 在把步骤标记为 done 之前检查其策略（`taskflow_resume.py:122-144`）。遇到可重试失败时，它先休眠 `retry_delay_seconds`，再生成替换子 Agent；步骤在新子 Agent 上保持 `dispatched`。若替换子 Agent 的生成本身抛异常，步骤会带着失败结果保持 `done`，响应中携带一条列明该异常的 `retry:` 说明。

### 耗尽与失败说明

当 `retry_count` 达到 `max_retries` 时，`plan_settled_retries()` 会发出 `exhausted` 动作而非重新派发。步骤被标记为 `done`，并追加一条携带 `retry_exhausted: True` 的失败说明结果，由 `exhausted_note()` + `failure_result_record()` 生成（`_retry.py:178-196`）：

```
retry budget exhausted: step step-4 child agent:main:session:... settled without a
result after 2 retry/retries (max_retries=2); marked done by taskflow_wait_all
```

耗尽的步骤需要显式决策——恢复该失败说明，或让 flow 失败。`wait_all` 与 `resume` 两条路径都遵守 resume 的幂等契约：失败说明携带 `result_hash`，因此重复投递的落定结果绝不会被记录两次。

## 🪙 Token / 成本预算

`taskflow_budget`（`taskflow_budget.py:10`）同时是查询器与设置器：

```python
@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",           # "query" | "set"
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

- **`query`** 报告 `total_tokens`、`total_cost`、预算、剩余 token，以及状态：当 `total_tokens >= budget` 时为 `EXCEEDED`；当用量 ≥ `MODEL_PRICING["budget_warn_threshold"]`（0.80）时为 `WARNING`；否则为 `ok`。未设置预算时，百分比会报告为未设置。
- **`set`** 要求 `token_budget` 为正整数，拒绝终态 flow，并通过乐观锁写入（传入 `expected_revision` 可快速失败）。

当调用方传入 `token_usage` 时，开销在 `taskflow_resume` 中聚合（`taskflow_resume.py:108-122`）：

```python
token_usage={"input_tokens": 1200, "output_tokens": 800, "model_name": "deepseek-chat"}
```

该工具惰性导入 `from config.features import MODEL_PRICING`，在 `MODEL_PRICING["model_pricing_per_m_tokens"]` 中查找模型（回退到 `_default`），并计算：

```
total_tokens = existing_total_tokens + input_tokens + output_tokens
cost_delta   = input_tokens  * pricing["input"]  / 1_000_000
             + output_tokens * pricing["output"] / 1_000_000
total_cost   = round(existing_total_cost + cost_delta, 6)
```

`MODEL_PRICING` 位于 `config/features/infra_side/model_pricing.py`：

| 模型 | 输入（美元 / 100 万 token） | 输出（美元 / 100 万 token） |
| :--- | :--- | :--- |
| `glm-5` | 0.5 | 1.5 |
| `deepseek-chat` | 0.14 | 0.28 |
| `kimi-latest` | 0.55 | 2.19 |
| `_default` | 1.0 | 3.0 |

`budget_warn_threshold` = `0.80`。

## ⏰ 任务截止时间

`taskflow_create` 接受可选的 `deadline_hours`（`taskflow_create.py:17`）：

```python
@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
    deadline_hours: float | None = None,
) -> str
```

当 `deadline_hours` 为正时，工具会把 `deadline_ts = time.time() + deadline_hours * 3600` 存入 `deadline_ts` 列。`taskflow_summary` 会渲染截止时间，并在过期后标记为 `EXCEEDED`。真正的执行者是后台 **sweeper**（`agent/tools/subagent/registry/sweeper.py`），它在每个扫描周期运行 `_expire_overdue_taskflows()`（`sweeper.py:123`）：

```python
overdue = await taskflow_store.get_overdue_flows(time.time())
for flow in overdue:
    state["failure_reason"] = "Deadline exceeded: " + time.strftime("%Y-%m-%d %H:%M", ...)
    update_flow(flow_id, flow["expected_revision"], state=state, status=TaskFlowStatus.FAILED.value)
```

超期的 flow 会被标记为 `failed`；查询会自动排除已经是终态的 flow，而每个 flow 的异常都会被记录并吞掉，因此单条坏数据不会中断整个扫描。

## ✅ 结果校验

步骤可以携带自然语言的**验收标准**，两个工具都接受 `validation_criteria`：

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:40
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,     # stored on the step
    retry_policy: dict | None = None,           # (see above)
    session_id: SessionId = "",
) -> str
```

`taskflow_run_task` 会把去空白后非空的 `validation_criteria` 存到步骤上（`blocked` 与 `dispatched` 两条写入路径都如此）。在恢复时，携带标准的步骤会由**辅助 LLM**（`agent/tools/taskflow/step_judge.py`，温度 0）按这些标准审查：

```python
# agent/tools/taskflow/step_judge.py:28-31
class StepVerdict(StrEnum):
    PASS = "pass"
    RETRY = "retry"
    BLOCK = "block"
```

- `pass` 把步骤标记为 `done` 并解锁后继。
- `retry` 通过共享的 `_retry` 缝重新派发该步骤，复用步骤自身的 `retry_count` 预算（`STEP_JUDGE["max_retries"]`，默认 2），并通过 `with_judge_feedback`（`## Previous Attempt Feedback`）把判别器指引附加到替换任务；预算耗尽的步骤标记为 `blocked`。
- `block` 依据判别器原因把步骤标记为 `blocked`。

判别器会看到本流的证据摘要（`agent/tools/taskflow/evidence_collector.py`），且**失败开放**：判别器禁用、模型报错或响应无法解析时降级为 `pass`。没有标准的步骤仍走旧路径——直接标记 `done`，且当存有 `validation_criteria` 时响应仍会在末尾回显 `validation_criteria: …`。有两条护栏值得注意：

- 向 `taskflow_resume` 传入 `validation_criteria` 会**覆盖**已存值（例如根据子 Agent 实际所做的工作收紧或修正它）。
- 判别器仅在步骤真正落定（`redispatched_key is None`）时运行；在重试策略下被重新派发的步骤会保留其标准，并在最终恢复时接受判别。

编排者（主 Agent 模型）仍掌管 flow 级决策，但携带标准的步骤不再依赖模型去发现未满足的标准：判别器可以将其 `block`，或者用重试预算再试一次。

## 📊 进度报告

`taskflow_progress`（`taskflow_progress.py:22`）是一个只读报告，包含完成度百分比、状态分解、后续步骤和预计剩余时间：

```python
@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str
```

输出形态：

```
Progress Report: <flow_id>
  Status: running
  Description: <前 80 个字符>
  Completion: 3/5 steps (60%)
  Breakdown: done=3 · dispatched=1 · ready=0 · blocked=1
  Next steps:
    → [step-4] <任务，前 60 个字符>
    ⊘ [step-5] <任务，前 60 个字符>
  Est. remaining: ~12.5 minutes (based on 3 completed steps)
  Waiting on: <等待原因>              # 仅在 flow 处于 waiting 时出现
  Results injected: 3                  # 仅在存在结果时出现
```

状态图标为 `done=✓`、`dispatched=→`、`ready=○`、`blocked=⊘`（`taskflow_progress.py:14`）。仅当至少有**两个** `done` 步骤带有 `dispatched_at` 时间戳时才会给出估算；它取各次派发时间戳之间的平均间隔，再乘以剩余步骤数。没有任何步骤的 flow 会返回 `Progress: flow_id=…, status=…` 以及 `No steps registered yet.`

## 💤 空闲检测

用 `taskflow_set_waiting` 停靠的 flow，其子 Agent 可能崩溃却永远不会恢复。sweeper 通过 `_scan_stale_waiting_taskflows()` 检测这种情况（`sweeper.py:154`）：

1. 读取所有 `waiting` flow。
2. `timeout_secs = TASKFLOW_INFRA["waiting_timeout_hours"] * 3600`（默认 24 小时）。
3. 对于带 `set_at` 的等待负载，当 `now - set_at <= timeout_secs` 时跳过。
4. 通过 `get_run_by_child_session_key(flow["child_session_key"])` + `is_live_unended_run(run)` 检查子 Agent 存活。若子 Agent 仍存活则跳过；若存活检测的导入本身失败，则把子 Agent 当作已死亡。
5. 否则在 `wait_json` 中打上一个**非破坏性标记**：`stale_detected_at` 与 `stale_child_session_key`。

空闲检测**从不自动把 flow 置为失败**——该标记只是提示性的，并且每个周期都会刷新。与此同时，`taskflow_summary` 会渲染等待状态，当等待超过 `TASKFLOW_INFRA["waiting_timeout_hours"]`（24 小时）时打印 `wait_status: STALE (waiting X.Xh, timeout=24h) — child session may have crashed; consider taskflow_resume with a failure result or re-dispatch`（`taskflow_summary.py:67-90`）。

## 📋 会话面板与隔离

每个会话只能看到自己的数据。`taskflow_summary`、自动恢复的读取者**以及** `taskflow_list` 全部按 `session_id` 隔离，由存储在 SQL 层过滤——属于其他会话的 flow 与不存在的 flow 无法区分（变更时 `FlowNotFoundError`，读取时为 `None`）：

```python
# agent/tools/taskflow/tools/taskflow_list.py:85
@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active", session_id: SessionId = "") -> str
```

| `status_filter` | 行 |
| :--- | :--- |
| `"active"`（默认） | 本会话的 `running` + `waiting` |
| `"all"` | 本会话的 flow，包含终态 |
| 其他任意值 | 精确匹配状态（`running`、`waiting`、`done`、`failed`、`cancelled`） |

它是只读的（无需 `expected_revision`），由 `store_sqlite.get_all_flows_sync(session_id, status_filter)` 支撑。同步读取器使用无需事件循环的 stdlib `sqlite3` 路径，按 `expected_revision DESC` 排序（最近活跃的在前），并且失败开放——初始化/读取失败返回 `[]`。`"active"` 委托给 `get_active_flows_sync(session_id)`。

渲染出的面板是一张带固定列的补白文本表，描述裁剪到 40 字符，creator key 裁剪到 16 字符：

```
TaskFlow board (active): 2 flow(s)
flow_id | status  | description                              | steps | creator          | updated_at
--------+-..----+-..----------------------------------------+-..---+----------------+-..----------
flow-1  | running | Build the parser                         | 2/5   | agent:main:sess  | 2026-09-12 14:03:21
flow-2  | waiting | Wait for the upstream review              | 1/3   | agent:main:sess  | 2026-09-12 13:58:07
```

表结构**没有 `updated_at` 列**（无需迁移）。因此 `_last_activity_ts()`（`taskflow_list.py:28`）把“最后更新”推导为 flow 上任何位置持久化的活动时间戳的最大值——`wait.set_at`、每个 `step.dispatched_at`、每个 `result.injected_at`——并渲染为 UTC 时间戳（flow 完全没有时间戳时为 `-`）。空注册表返回 `No task flows found`。

### 三个会话所有的规划工具族

| 工具族 | 会话关联 | 存储 |
| :--- | :--- | :--- |
| **TaskFlow** | `task_flows.session_id` 列（本次变更） | `agent/tools/taskflow/registry/store_sqlite.py` |
| **TodoList** | `session_id` 本身就是表的主键前缀——该工具族从一开始就是会话作用域 | `agent/tools/todolist/registry/store_sqlite.py` |
| **Knowledge** | 按计划身份隔离，在工具层强制：归属来自 `ownership.association_plan_refs()`（本会话 `plan_ref` 状态键、本会话 todo 的 `plan_ref`（SQL 按 `session_id` 过滤）、`src/data/boulder.json` 中 `plan_name` 匹配且 `session_ids` 含本会话的 work）；随后 `identity.resolve_plan_identity()` 把名称映射到规范化计划路径并派生存储键 `sha1(相对仓库根路径)[:12]`。不同文件中的同名计划物理隔离；通过 boulder `session_ids` 共享的同一计划文件对每个列出的会话解析出同一个 `<plan_key>`。对非关联或歧义计划的 `read` / `write` 会给出可诊断错误并拒绝，`list` 只返回关联计划（可读名 + key）；`build_knowledge_block(session_id)` 为提示块解析本会话的主身份；`clear_session` 清理本会话私有身份目录，保留与其他会话共享的计划。 | `agent/tools/todolist/knowledge/identity.py` |

**子 Agent 边界。** 三个工具族都由各自的构建器打上 `metadata["scope"] = "main_only"` 标签（`build_taskflow_tools`、`build_todolist_tools`、`build_knowledge_tools`）。`apply_tool_policy`（`agent/tools/subagent/spawn/inherited_tool_policy.py`）会**最先且无条件**丢弃 `main_only` 工具——先于任何 allow/deny 列表，且 ORCHESTRATOR 解禁也无法覆盖——因此派生出的子 Agent 永远不会拿到 `taskflow_*`、`todowrite`/`todoread` 或 `knowledge` 工具。同样的标签模式此前已覆盖 `memory`、`skill_manage`、`sessions_kill`、`sessions_steer`。真实工具集断言由 `tests/agent/tools/taskflow/test_taskflow_tools.py` 锁定，`_build_child_agent` 边界由 `tests/agent/tools/subagent/test_max_tokens_boost_wiring.py` 锁定。

**跨会话拒绝。** `taskflow_create` 遇到其他会话已占用的 `flow_id` 时只报告已存在、不泄露 revision；对其他会话的 flow 发起变更会得到与未知 id 相同的 "not found" 文本。`tests/agent/tools/taskflow/test_store_sqlite.py`、`test_taskflow_tools.py`、`test_dag_e2e.py` 与 `tests/server/DAO/test_clear_session.py` 覆盖读取/列表/更新/清除路径。


