# ⏳ 长时任务：TaskFlow、预算、截止时间、记忆与连续性

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何执行跨越多个回合的长期工作：一个持久化的 SQLite DAG 引擎（`taskflow_*`，共 12 个工具）跨对话回合跟踪有依赖关系的步骤，将每个步骤派发给一个分离的子 Agent 执行，按预算聚合 token/成本开销，由后台 sweeper 让超期或空闲的 flow 过期，并通过三层记忆系统、压缩前的记忆落盘、摘要与 TaskFlow 的桥接、工具输出单行摘要、跨会话连续性，以及把活动 flow 自动注入系统提示词，把上下文一路传递下去。

事实来源：`agent/tools/taskflow/**`、`agent/tools/memory.py`、`agent/tools/memory_tiered.py`、`agent/middlewares/memory_flush.py`、`agent/middlewares/summarization.py`（LT-7 区块）、`agent/middlewares/task_intent.py`、`agent/middlewares/todo_continuation.py`、`context_engine/session_continuity.py`、`workspace/prompt_builder.py`、`pub/func/message/tool_output_prune.py`、`agent/tools/subagent/registry/sweeper.py`、`config/features/**`。下文中的每一处常量、签名与行号都已对照这些代码逐一核实。

## 目录

- [概览](#-概览)
- [TaskFlow DAG 引擎](#-taskflow-dag-引擎)
- [Token / 成本预算](#-token--成本预算)
- [任务截止时间](#-任务截止时间)
- [进度报告](#-进度报告)
- [空闲检测](#-空闲检测)
- [分层记忆](#-分层记忆)
- [压缩前的记忆落盘](#-压缩前的记忆落盘)
- [摘要 ↔ TaskFlow 协调](#-摘要--taskflow-协调)
- [工具输出摘要](#-工具输出摘要)
- [会话连续性](#-会话连续性)
- [TaskFlow 自动恢复](#-taskflow-自动恢复)
- [配置注册表](#-配置注册表)
- [架构图](#-架构图)
- [API 参考](#-api-参考)
- [测试](#-测试)
- [已知局限](#-已知局限)

## 🎯 概览

长时任务栈让主 Agent 能把一个多回合的任务分解为一个**持久化 flow**，其步骤之间可以互相依赖，把每个就绪步骤派发给子 Agent 执行，并在进程重启后继续存在。共有七个子系统协作：

| # | 子系统 | 入口 | 持久化位置 |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG 引擎** | `agent/tools/taskflow/` | `data/taskflow_registry.db`（SQLite，WAL） |
| 2 | **Token / 成本预算** | `taskflow_budget`、`taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **截止时间** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **空闲检测** | sweeper `_scan_stale_waiting_taskflows` | `wait_json` 中的过期标记 |
| 5 | **分层记忆** | `memory` 工具动作 + `agent/tools/memory_tiered.py` | `workspace/memory/*.md` + `facts/*.md` |
| 6 | **压缩前落盘** | `agent/middlewares/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 7 | **连续性与自动恢复** | `context_engine/session_continuity.py`、`workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + 提示词区块 |

贯穿始终的设计契约是**错误即文本**：工具从不把业务错误抛给模型，而是返回以 `Error:` 开头的人类可读字符串。每个后台钩子都是**失败开放（fail-open）**的——注册表不可用或 sweeper 崩溃只会退化为“没有长时任务上下文”，绝不会破坏一个回合。

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
    deadline_ts REAL
);
```

DAG 本身（`steps[]`、`results[]`、`depends_on`、`creator_session_key`）完全存放在 `state_json` 中——新增 DAG 字段无需迁移表结构。token/成本/截止时间列由增量迁移加入（`_TOKEN_COLUMN_DDL`、`_DEADLINE_COLUMN_DDL`，`store_sqlite.py:83-129`）。WAL 模式每个进程只切换一次，且每条语句之前都会执行 `PRAGMA busy_timeout = 5000`（`store_sqlite.py:234-271`）。

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

`done` 的含义是**“已注入结果”**，而不是“子 Agent 成功”（参见[已知局限](#-已知局限)）。没有 `status` 字段的旧步骤会被推导：带 `child_session_key` 的步骤视为 `dispatched`，否则视为 `ready`（`_shared.py:85`，`step_status`）。

### `depends_on` 语义

`deps_satisfied(step, steps)`（`_shared.py:99`）为真，当且仅当每个 `depends_on` id 都存在于当前步骤列表中**且**为 `done`。缺失/为空的 `depends_on` 视为自然满足。未知的依赖 id 永远不满足，而**自依赖永远不满足**——因此自引用步骤会安全地保持阻塞，而不会陷入解锁死循环。`unlock_dependents(steps)`（`_shared.py:137`）对列表只做**单遍扫描**，从结构上杜绝了依赖环导致死循环。`taskflow_run_task` 会在任何派发或状态变更**之前**拒绝未知的依赖 id。

### 工具族（12 个工具）

所有工具都是 `async`，装饰为 `@tool("taskflow_…")`，由 `build_taskflow_tools()`（`tools/__init__.py:43-55`）打上 `metadata={"scope": "main_only"}` 和 `handle_tool_error=True`。只有主 Agent 可以管理共享 flow 状态；子 Agent 工具策略会无条件丢弃整个工具族。

| 工具 | 用途 |
| :--- | :--- |
| `taskflow_create` | 在版本 1 创建 flow；可选设置 `deadline_hours` |
| `taskflow_run_task` | 注册一个步骤并派发（或记录为 `blocked`） |
| `taskflow_dispatch` | 全有或全无地批量派发多个就绪步骤 |
| `taskflow_wait_all` | 按 flow 范围有界轮询，等待已派发步骤落定 |
| `taskflow_resume` | 幂等地注入子结果、解锁后继步骤、聚合 token |
| `taskflow_set_waiting` | 带上原因把 flow 置为 `waiting` |
| `taskflow_summary` | 只读回读（也是冲突后的重读手段） |
| `taskflow_progress` | 人类可读的进度/完成度报告 |
| `taskflow_budget` | 查询或设置 token/成本预算 |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | 终态转换 |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:38
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

### 派发——`taskflow_dispatch` 批量语义

`taskflow_dispatch(flow_id, step_ids, expected_revision=None, session_id="")`（`taskflow_dispatch.py:36`）在生成任何子 Agent **之前**校验**每一个** id。当步骤状态为 `ready`，或 `blocked` 但依赖已满足时，它就是可派发的。未知 id、重复 id，或已经 `dispatched`/`done` 的步骤，都会整体拒绝该次调用且不产生任何派发。成功后，步骤通过共享的 `_dispatch.dispatch_child` 接缝（`_dispatch.py:10`）顺序派发，并在**一次** `update_flow` 调用中持久化。若批次中途派发失败，循环停止，已派发的子 Agent 会被持久化，确保没有子会话被静默丢弃，错误信息同时列出失败与已派发的 step id。flow 级别的 `child_session_key` 刻意不被改动——每个步骤自己的 child key 才是权威。

### 等待——`taskflow_wait_all` 的 flow 范围

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")`（`taskflow_wait_all.py:110`）**只**轮询记录在*当前* flow 的 `dispatched` 步骤上的子会话，因此无关的后台子 Agent 永远不会阻塞返回。未知或已被清理的 run 会被视为已落定（永不挂起）。轮询间隔被夹紧到 `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]`（0.05 秒）。超时后返回部分报告，并提示对已落定的子 Agent 调用 `taskflow_resume`，然后再次调用 `wait_all`。它刻意**不**复用 `sessions_yield`，后者只在请求方会话的全部子 Agent 落定时才触发。

### 乐观锁

每一次变更都走 `UPDATE … WHERE flow_id = ? AND expected_revision = ?`，并把版本号恰好加 1（`store_sqlite.py:460-481`）。匹配到零行即表示冲突：

```python
# FlowConflictError 消息（store_sqlite.py:173）
"TaskFlow '<id>' revision conflict: expected_revision=2 but latest revision=3;
 re-read with taskflow_summary and retry with expected_revision=3"
```

当子 Agent **已经派发**但写入竞争失败时，`update_flow_with_conflict_retry()`（`_shared.py:191`）会重新读取最新的 flow，通过 `build_state` 回调重建状态，并最多重试 `PERSIST_MAX_ATTEMPTS = 3` 次。若 flow 已消失或变为终态，或重试耗尽，它会返回一个列出所有已派发 `child_session_key` 的错误，并指示调用方**按 key 回收，绝不重新派发**。

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

## 🧠 分层记忆

三层，区别在于*如何*抵达模型：

| 层 | 存储 | 位置 | 是否进入提示词？ |
| :--- | :--- | :--- | :--- |
| **L1 —— 精炼记忆** | `MEMORY.md`（Agent 笔记）+ `USER.md`（用户画像） | `workspace/memory/`（`MEMORY_DIR`） | 是——冻结快照，始终注入 |
| **L2 —— 结构化事实** | `facts/{category}.md`，固定的 5 个类别 | `workspace/memory/facts/`（`FACTS_DIR`） | 仅一行索引；完整内容按需读取 |
| **L3 —— 原始历史** | `mes_memory.db`（SQLite、WAL、FTS5） | `src/store/mes_memory/mes_memory.db` | 否——由 `context_engine` / `message_search` 检索 |

文件是**以独占一行的分隔符 `§` 分隔的纯文本条目**——`ENTRY_DELIMITER = "\n§\n"`（`agent/tools/memory.py:53`），没有 YAML frontmatter，也没有项目符号前缀。条目可以跨多行。

第 1 层由 `MemoryStore` 管理（`memory.py:104`）：每个文件的字符上限为 `2200`（memory）和 `1375`（user），注入扫描（`_MEMORY_THREAT_PATTERNS`，`memory.py:68`）会拒绝提示词注入与凭证外泄内容，配合跨平台文件锁、原子写入和精确匹配去重。实时条目会立即改动，而提示词使用的是一份在 `load_from_disk()` 时捕获的**冻结快照**，以保持会话期内前缀缓存稳定。

第 2 层由 `TieredMemoryStore` 管理（`agent/tools/memory_tiered.py:19`），类别为 `environment`、`project`、`decisions`、`user_prefs`、`tool_lessons`（`TIERED_MEMORY["facts_categories"]`），每个文件的上限为 `TIERED_MEMORY["facts_char_limit"]` = 4000 字符。文件溢出时，**最旧**的条目先被淘汰；完全重复的条目会被报告为已存在。

事实操作是**单个 `memory` 工具的动作**，而不是独立的工具（`memory.py:641`）：

```python
# MemoryActionSchema.action
Literal["add", "replace", "remove", "fact_add", "fact_read", "fact_search"]
```

| 动作 | 必需参数 | 返回 |
| :--- | :--- | :--- |
| `fact_add` | `content`、`target`（类别） | JSON `{"success", "message", "category", "entry_count", "usage"}` |
| `fact_read` | `target` = 类别或 `"all"` | JSON `{"success": true, "facts": {category: text}}` |
| `fact_search` | `content`（子串查询） | JSON `{"success": true, "results": [{"category", "fact"}], "count"}` |

`prompt_builder.build_system_prompt` 通过 `format_for_system_prompt` 注入 L1 快照，并追加 L2 索引 `FACTS (on-demand, use memory tool with fact_read/fact_search): …`（`workspace/prompt_builder.py:271-282`）。`memory` 工具被打上 `scope="main_only"`，因此子 Agent 永远看不到它。

## 🔥 压缩前的记忆落盘

在摘要中间件丢弃旧消息之前，`agent/middlewares/memory_flush.py` 给廉价模型最后一次机会，把持久事实写入 `MEMORY.md`。触发条件为 `should_flush(discarded_messages, estimated_tokens)`（`memory_flush.py:43`）：

```python
if not MEMORY_FLUSH["enabled"]:
    return False
total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
if total_chars >= MEMORY_FLUSH["force_flush_chars"]:   # 50_000
    return True
return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]   # 8_000
```

触发时，`run_memory_flush`（异步）/ `run_memory_flush_sync` 通过注入的工厂构建模型，并使用单个纯文本抽取提示词（`_FLUSH_PROMPT`，`memory_flush.py:19`），其输出是一个以 `§` 分隔的 `Environment / Project / Decision / User / Tool` 事实列表。空结果或字面量 `(none)` 会被跳过。抽取出的文本交给 `MemoryStore.append_entries(new_entries)`（`memory.py:281`），后者按 `§` 切分，对每个候选做注入扫描，与现有集合去重，追加，并在超过 2200 字符时淘汰最旧条目，最后做一次原子写入。`append_entries` 始终写入 `MEMORY.md`。每条失败路径都返回 `False` 并被吞掉——落盘永远不会阻塞压缩。

⚠️ **接线状态。** `Summarization.__init__` 接受 `memory_store` / `llm_factory`（二者默认均为 `None`，`summarization.py:623-624`），且仅在二者都设置时调用落盘，位置在 `_apply_compression`（`summarization.py:1703`）与 `_aapply_compression`（`summarization.py:1791`）内。当前生产实例——主 Agent `agent/core.py:170` 与子 Agent `agent/tools/subagent/spawn/core.py:784`——并**未**传入它们，因此落盘功能已实现并有测试覆盖，但在某个调用点提供 store 与形如 `factory(model=…, max_tokens=…, timeout=…)` 的工厂之前一直处于潜伏状态。

## 🔗 摘要 ↔ TaskFlow 协调

当压缩构建其 LLM 提示词时，`_get_taskflow_context_sync(session_id)`（`agent/middlewares/summarization.py:262`）会渲染本会话的活动 flow，并把它作为摘要提示词的**最后**一部分追加（`_build_summary_prompt`，`summarization.py:1431-1434`）：

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

该区块以 `## Current TaskFlow State (authoritative)` 为标题（`summarization.py:286`），对于本会话拥有的至多三个 flow（通过 `requester_session_key(session_id)` 匹配），列出 flow id/状态、描述、`done/total` 进度与状态分解、最后两个已完成的步骤、前两个待处理步骤，以及任何等待原因。它复用了 DAG 辅助函数 `step_status` 与 `steps_summary`，并且完全失败开放（`except Exception → ""`）。确定性回退摘要（`_build_static_fallback_summary`）**不**包含该区块；它只是 LLM 提示词的补充。

## ✂️ 工具输出摘要

在非 LLM 剪枝过程中，较大的旧 `ToolMessage` 内容通常被清成一个标记。`pub/func/message/tool_output_prune.py` 用一行**工具专属摘要**替换了裸标记 `_PRUNE_MARKER = "[Old tool result content cleared]"`（`tool_output_prune.py:22`），让模型仍能保留关于该结果内容的线索：

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_summary"   -> "[taskflow_summary] {len} chars"
"taskflow_run_task"  -> "[taskflow_run_task] dispatched, {len} chars"
"taskflow_resume"    -> "[taskflow_resume] injected result, {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`（`tool_output_prune.py:103`）从最新到最旧遍历消息，遇到摘要消息即停止，保护最新的 `prune_protect_tokens`（40 000）个 token，跳过受保护工具（`{"memory", "skill_view", "skill_list"}`），并且只有当释放的 token 达到 `prune_min_reduction_tokens`（5 000）时才提交。被替换的消息是 `model_copy` 克隆，携带 `additional_kwargs["status"] = "compacted"` 与 `["original_length"]`。摘要上限为 200 字符；任何模板异常都会回退到标记。它由 `Summarization._run_non_llm_strategies` 调用（`summarization.py:1538`）。

## 🔄 会话连续性

会话被清除时，`context_engine/session_continuity.py` 会持久化一份结束状态，供下一个会话提供连续性。`server/DAO/messages.py::clear_session` 把 `auto_save_on_session_end(session_id)` 作为**第 0 步**，在任何删除之前调用（`server/DAO/messages.py:27-33`）。该函数会：

1. 通过 `runtime.relation_register` 解析 `channel_id`/`chat_id`（`_get_channel_chat_for_session`，`session_continuity.py:167`）。
2. 读取最近 3 个回合，并把最后一条 AI 回复裁剪到 `_MAX_SUMMARY_CHARS = 500`（`session_continuity.py:28`）。
3. 收集该会话的活动 flow id。
4. 调用 `save_session_end_state(...)`，写入 `src/data/session_continuity/{safe-key}.json`（`session_continuity.py:25`），字段为 `last_session_id`、`ended_at`、`ended_ts`、`summary`、`taskflow_ids`。

下一个会话通过 `build_continuity_prompt(session_id)`（`session_continuity.py:80`）读取它，它由 `workspace/prompt_builder.py:169` 的 `_build_continuity_block` 调用，并在构建完整提示词时注入（`prompt_builder.py:289-295`）：

```
## Last Session (continuity)
Last conversation ended with: <摘要 ≤ 500 字符>
Related tasks: <至多 3 个 flow id>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

会话永远不会收到自己的状态（`last_session_id == session_id → ""`）。由于查询同时要求 **channel id 与 chat id**，没有渠道绑定的纯 WebSocket 会话拿不到连续性区块。存储是文件系统上的 JSON（以 `channel:chat` 为键，退化时以 `session_id` 为键），而不是数据库。

## ♻️ TaskFlow 自动恢复

活动 flow 会被重新浮现到系统提示词中，使新会话能接手未完成的工作。三个独立的读取者使用同一套配方——`requester_session_key(session_id)` + `get_active_flows_sync()` + `state["creator_session_key"]` 过滤：

| 读取者 | 位置 | 用途 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | 系统提示词中的 `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization.py:262` | 压缩摘要提示词中的 TaskFlow 区块（LT-7） |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:186` | 持久化连续性状态中的 `taskflow_ids` |

`creator_session_key` 在创建 flow 时被写入（`taskflow_create.py:38`），值为 `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"`（`_shared.py:21`）。`get_active_flows_sync()`（`store_sqlite.py:538`）仅返回 `running` 与 `waiting` 的 flow，按版本排序，使用无需事件循环的 stdlib `sqlite3` 路径；失败时返回 `[]`。

系统提示词区块（`prompt_builder.py:140`）形如：

```
## Pending TaskFlows
- [running] flow-1: "<描述>" | 2/5 steps done | next: step-3 "<任务>"
Use taskflow_summary to inspect a flow and continue execution.
```

它最多三个 flow，并且在以文件过滤方式构建提示词（`selected_file_names is not None`）时被抑制。每次读取都失败开放。

## ⚙️ 配置注册表

所有可调项都放在 `config/features/` 下，形成一个手工打造的**按对象划分的 `TypedDict` 注册表**。每个模块定义一个 `class XxxConfig(TypedDict)` 以及一个模块级常量 `XXX: XxxConfig = {…}`。感知环境的模块定义一个构建函数 `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig`，它读取 `env or os.environ`，并在导入时物化常量。唯一的环境辅助函数是 `_env_int(name, default, env)`（`config/features/_env.py:9`），它接受 `1/true/yes/on` 与 `0/false/no/off/""`，并且从不抛异常。

该注册表当前包含 **38 个 feature 对象**——Agent 侧 20 个（`config/features/agent_side/`），基础设施侧 18 个（`config/features/infra_side/`）——通过各包的 `__init__.py` 重新导出，并由 `config/features/__init__.py` 汇总。消费方代码直接导入常量并索引它（例如 `ITERATION_BUDGET["default_max_iterations"]`）；不存在 `get_feature`/`load_feature` 访问器。`config/__init__.py:38-39` 从 `GATEWAY` 派生出 `API_HOST`/`API_PORT`。

与本文档最相关的常量：

| 配置对象 | 字段 | 值 |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA`（`agent_side/taskflow_infra.py`） | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING`（`infra_side/model_pricing.py`） | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `TIERED_MEMORY`（`agent_side/tiered_memory.py`） | `facts_char_limit` | 4000 |
| | `facts_index_max_chars` | 200（已声明；实时代码未消费） |
| | `facts_categories` | environment, project, decisions, user_prefs, tool_lessons |
| `MEMORY_FLUSH`（`agent_side/memory_flush.py`） | `enabled` | `MEMORY_FLUSH_ENABLED`（默认 1） |
| | `model` | `MEMORY_FLUSH_MODEL`（默认 ""） |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION`（`agent_side/summarization.py`） | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY`（`infra_side/mes_memory.py`） | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ 架构图

```
                          ┌──────────────────────────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (12, main_only)   │          │  add/fact_add/…      │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │ TieredMemoryStore(L2)│                 │ ─ FACTS index (L2)     │
│ state_json DAG    │          │ mes_memory.db (L3)   │                 │ ─ Pending TaskFlows    │
└────────┬──────────┘          └──────────────────────┘                 │ ─ Last Session         │
         │                                                              └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
                                                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+LT-7 TaskFlow)  │   │
└──────────────────────────────┘                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

## 📚 API 参考

### TaskFlow 工具

| 工具 | 签名 | 返回 |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | 创建的 id/状态/版本（+ 截止时间） |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, session_id)` | 已派发步骤，或带待满足依赖的 `blocked` |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | 已派发的 step id + 版本 |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | 每个步骤的落定报告（完整或部分） |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None)` | 恢复后的状态、解锁的步骤、步骤状态计数 |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None)` | waiting 状态 + 版本 |
| `taskflow_summary` | `(flow_id)` | 完整 flow 状态，含等待/截止时间状态 |
| `taskflow_progress` | `(flow_id)` | 完成度 %、分解、后续步骤、预计剩余 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None)` | 预算报告，或设置确认 |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None)` | 终态 `done` |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None)` | 终态 `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None)` | 终态 `cancelled` |

### memory 工具动作

| 动作 | 签名 | 返回 |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON 成功/错误 |
| `fact_add` | `memory(action="fact_add", target=<类别>, content=<事实>)` | JSON `{success, message, category, entry_count, usage}` |
| `fact_read` | `memory(action="fact_read", target=<类别>\|"all")` | JSON `{success, facts: {category: text}}` |
| `fact_search` | `memory(action="fact_search", content=<查询>)` | JSON `{success, results: [{category, fact}], count}` |

### 关键函数与常量

| 符号 | 位置 | 作用 |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | 生命周期 / DAG 枚举 |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py:402` | 乐观锁变更 |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py:538` | 跨会话活动 flow 读取 |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py:489,505` | sweeper 查询 |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:99,137` | DAG 状态转换 |
| `update_flow_with_conflict_retry` | `_shared.py:191` | 绝不丢失已派子 Agent 的持久化 |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | 截止时间执行 |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | 空闲检测标记 |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization.py:262` | LT-7 摘要协调 |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | 自动恢复提示词区块 |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:103` | 工具输出单行摘要 |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | 连续性保存钩子 |
| `should_flush` / `run_memory_flush` | `agent/middlewares/memory_flush.py:43,65` | 压缩前落盘 |
| `append_entries` | `agent/tools/memory.py:281` | 批量追加 MEMORY.md |

## 🧪 测试

TaskFlow 测试位于 `tests/agent/tools/taskflow/`（十四个 `unit` 测试文件加一个共享的 `conftest.py`）：

| 测试文件 | 覆盖内容 |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD、版本自增、乐观并发冲突、WAL、同步访问器、active/waiting/terminal 过滤 |
| `test_step_graph.py` | `deps_satisfied`、`mark_step_done`、`unlock_dependents`、旧状态推导、自依赖保护 |
| `test_summary_dag.py` | `taskflow_summary` 的 DAG 渲染 |
| `test_resume_dag.py` | 恢复标记 done、解锁后继、部分完成、幂等空操作 |
| `test_taskflow_tools.py` | 跨重启的完整 create→run→resume→finish、冲突、终态转换 |
| `test_taskflow_dispatch.py` | 批量派发、全有或全无校验、批次中途失败的持久化 |
| `test_dag_e2e.py` | 跨进程重启的完整并行 DAG flow |
| `test_conflict_persistence.py` | 冲突后已派子 Agent 的持久化、重试耗尽 |
| `test_taskflow_wait_all.py` | flow 范围等待、超时部分报告、无关的存活子 Agent |
| `test_run_task_dag.py` | 阻塞注册、满足后派发、未知依赖错误 |
| `test_taskflow_progress.py` | 完成度/分解/后续步骤/预计剩余/waiting |
| `test_token_budget.py` | token 聚合、成本计算、预算查询/设置/警告/超限 |
| `test_deadline.py` | `deadline_hours`、摘要渲染、sweeper 过期 |
| `test_idle_detection.py` | active/stale 等待状态、sweeper 标记、存活子 Agent 跳过 |

跨领域测试套件：`tests/agent/middlewares/test_memory_flush.py`（落盘阈值与 `append_entries`）、`tests/agent/tools/test_memory_tiered.py`（分层事实）、`tests/context_engine/test_session_continuity.py`（连续性保存/提示词）、`tests/agent/middlewares/test_todo_continuation.py`（回合结束续跑）、`tests/pub/func/message/test_tool_output_prune.py`（单行摘要）、以及 `tests/workspace/test_prompt_builder_taskflow.py`（待处理 flow 的提示词注入）。

用标准的 uv/pytest 工具只跑这一区块：

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py tests/agent/tools/test_memory_tiered.py -q
```

要跑完整的进程隔离套件，使用 `uv run python tests/run_tests_split.py`（Group A 跑 `unit` 文件，Group B 跑 `module`/`integration` 文件）。

## ⚠️ 已知局限

- **`done` 不等于成功。** 步骤的 `done` 只表示“已注入结果”；不存在 `failed`/`skipped` 步骤状态。即使子 Agent 报告错误，`taskflow_resume` 仍会把步骤标记为 `done` 并解锁后继。感知失败的步骤转换被有意推迟。
- **`taskflow_wait_all` 按设计限定于单个 flow。** 它只等待记录在给定 flow 的已派发步骤上的子 Agent，未知/已清理的 run 计入已落定。不存在“等待所有活动 flow”的全局原语。
- **空闲检测是提示性的。** sweeper 会把 `stale_detected_at` / `stale_child_session_key` 写入 `wait_json`，但从不自动把过期 `waiting` flow 置为失败；需要人或模型对该标记采取行动。
- **压缩前落盘处于潜伏状态。** `Summarization` 的生产实例（主 Agent 与子 Agent）未传入 `memory_store` / `llm_factory`，因此在某个调用点接线之前落盘不会运行；代码已实现并有测试，但目前不生效。
- **连续性依赖渠道。** `build_continuity_prompt` 同时需要 channel id 与 chat id，因此没有渠道绑定的会话拿不到连续性区块。存储是磁盘上按 key 划分的 JSON，而不是数据库。
- **三处重复的活动 flow 扫描。** `prompt_builder._build_taskflow_block`、`summarization._get_taskflow_context_sync` 与 `session_continuity._get_active_taskflow_ids_sync` 各自独立实现了同一查询；必须保持同步。
- **注册表规模是 38 而不是 35。** 配置注册表包含 38 个 feature 对象（Agent 侧 20 + 基础设施侧 18）；基础设施侧契约测试只数到 17，因为它遗漏了 `MODEL_PRICING`。
- **`facts_index_max_chars` 已声明但未使用。** `TIERED_MEMORY` 字段存在；没有实时代码读取它。
- **包导出缺口。** `agent/tools/taskflow/__init__.py` 只重新导出十一个名字；`taskflow_dispatch` 与 `taskflow_wait_all` 可通过 `build_taskflow_tools()` 获取，但被包 `__all__` 遗漏。
- **LT-7 的 TaskFlow 区块仅限 LLM 提示词。** LLM 失败时使用的确定性回退摘要不包含 `## Current TaskFlow State`。
- **Token 记账由调用方提供。** 只有当 `taskflow_resume` 收到 `token_usage` 字典时才计算成本；未提供时注入的步骤贡献零 token 与零成本。
