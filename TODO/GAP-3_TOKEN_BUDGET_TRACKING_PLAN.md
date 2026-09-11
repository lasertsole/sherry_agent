# GAP-3 Token/Cost 预算跟踪 — 实施计划

> 日期: 2026-09-11
> 来源: LONG_RUNNING_TASK_GAP_ANALYSIS.md §3 (行 77-91)
> 决策: task_flows 表新增 token 列 + taskflow_resume 时聚合 + taskflow_budget 工具
> 预估工时: 1 天

---

## 1. 问题

`mes_memory.db` 的 messages 表不记录 token 用量。stream driver 的 done frame 携带 `input_tokens`/`output_tokens`/`model_name`，但没有按 TaskFlow 聚合。无法知道"这个任务花了多少 token/费用"。长程任务可能消耗大量 token，无预算预警会失控。

**当前状态：**

- `StreamDriver`（`server/service/stream_driver.py:45`）在 done frame 中报告 `input_tokens`/`output_tokens`/`model_name`
- `task_flows` 表无 token/cost 列
- `taskflow_resume`（`agent/tools/taskflow/tools/taskflow_resume.py`）注入子代理结果时，不追踪子代理的 token 消耗
- 无 `taskflow_budget` 工具

---

## 2. 设计决策

### 2.1 DB 迁移：task_flows 表新增列

```sql
ALTER TABLE task_flows ADD COLUMN total_tokens INTEGER DEFAULT 0;
ALTER TABLE task_flows ADD COLUMN total_cost REAL DEFAULT 0.0;
ALTER TABLE task_flows ADD COLUMN token_budget INTEGER DEFAULT 0;
```

使用 additive migration 模式（与 `add_images_column` / `add_audio_video_columns` 一致，`context_engine/store/db.py:118-143`）。

### 2.2 聚合策略：taskflow_resume 时追加

`taskflow_resume` 已有 `child_session_key` 参数。子代理完成时，announce 管线 deliver 结果到父会话，模型调用 `taskflow_resume` 注入结果。此时：

1. 从 `mes_memory.db` 查询 child session 的最后一条 AI 消息的 `usage_metadata`（LangChain AIMessage 的 `usage_metadata` 含 `input_tokens`/`output_tokens`/`total_tokens`）
2. 累加到 flow 的 `total_tokens`

但 `mes_memory.db` 的 messages 表不存储 `usage_metadata`。因此采用**参数传入**方案：

- `taskflow_resume` 新增可选参数 `token_usage: dict | None`
- 调用方（模型或 announce 管线）传入子代理的 token 统计
- 如果未传入，跳过聚合（向后兼容）

### 2.3 关键决策

| 决策点    | 选择                                 | 理由                                                  |
| --------- | ------------------------------------ | ----------------------------------------------------- |
| 存储      | task_flows 表新增 3 列               | 持久化，跨重启存活；additive migration 不破坏现有数据 |
| 聚合时机  | `taskflow_resume` 接收 `token_usage` | 已有 child_session_key 关联点；可选参数不破坏现有调用 |
| cost 计算 | `total_tokens * MODEL_PRICE` 查表    | 简单查表，无需复杂计费系统                            |
| 预算超限  | `taskflow_summary` 中追加告警        | 非阻塞告警（不自动暂停，避免误杀任务）                |
| 工具      | 新增 `taskflow_budget` 工具          | set_budget + get_usage 两个 action                    |

---

## 3. 涉及文件

| 文件                                              | 操作                                     | 预估行数 |
| ------------------------------------------------- | ---------------------------------------- | -------- |
| `agent/tools/taskflow/registry/store_sqlite.py`   | 修改：`_CREATE_TABLE_SQL` + migration    | ~20      |
| `agent/tools/taskflow/registry/store_sqlite.py`   | 修改：`_row_to_flow` + `_SELECT_COLUMNS` | ~10      |
| `agent/tools/taskflow/tools/taskflow_resume.py`   | 修改：接收 token_usage + 聚合            | ~15      |
| `agent/tools/taskflow/tools/taskflow_budget.py`   | **新建**                                 | ~80      |
| `agent/tools/taskflow/tools/__init__.py`          | 修改：注册 taskflow_budget               | ~3       |
| `agent/tools/taskflow/__init__.py`                | 修改：导出                               | ~2       |
| `config/num.py`                                   | 修改：新增模型价格表                     | ~10      |
| `tests/agent/tools/taskflow/test_token_budget.py` | **新建**                                 | ~100     |

---

## 4. 详细设计

### 4.1 config/num.py — 模型价格表

```python
# === Token/Cost Budget (GAP-3) ===
# Per-1M-token pricing (USD). Used for cost estimation. Update as needed.
MODEL_PRICING_PER_M_TOKENS: dict[str, dict[str, float]] = {
    # model_name: {"input": price, "output": price}
    "glm-5": {"input": 0.5, "output": 1.5},
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "kimi-latest": {"input": 0.55, "output": 2.19},
    "_default": {"input": 1.0, "output": 3.0},
}

# Token budget warning threshold (% of budget used)
BUDGET_WARN_THRESHOLD = 0.80
```

### 4.2 store_sqlite.py — Schema 迁移

```python
# _CREATE_TABLE_SQL 新增列：
_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0
);
"""

# _SELECT_COLUMNS_SQL 新增列：
_SELECT_COLUMNS_SQL = (
    f"SELECT flow_id, state_json, wait_json, expected_revision, status, "
    f"child_session_key, total_tokens, total_cost, token_budget "
    f"FROM {TABLE_NAME}"
)

# _row_to_flow 新增字段：
def _row_to_flow(row: tuple) -> dict:
    (
        flow_id, state_json, wait_json, expected_revision, status,
        child_session_key, total_tokens, total_cost, token_budget,
    ) = row
    return {
        "flow_id": flow_id,
        "state": _load_json(state_json) or {},
        "wait": _load_json(wait_json),
        "expected_revision": int(expected_revision),
        "status": status,
        "child_session_key": child_session_key,
        "total_tokens": int(total_tokens or 0),
        "total_cost": float(total_cost or 0.0),
        "token_budget": int(token_budget or 0),
    }
```

新增 additive migration 函数（与 `context_engine/store/db.py` 模式一致）：

```python
def _ensure_token_columns(db) -> None:
    """Additive migration: add token tracking columns if absent."""
    for col, ddl in [
        ("total_tokens", "ALTER TABLE task_flows ADD COLUMN total_tokens INTEGER DEFAULT 0"),
        ("total_cost", "ALTER TABLE task_flows ADD COLUMN total_cost REAL DEFAULT 0.0"),
        ("token_budget", "ALTER TABLE task_flows ADD COLUMN token_budget INTEGER DEFAULT 0"),
    ]:
        try:
            db.execute(ddl)
        except Exception:
            pass  # Column already exists
```

在 `_init_db()` 和 `_ensure_tables_sync()` 中调用。

### 4.3 update_flow() — 支持 token 字段更新

在 `update_flow()` 的参数中新增：

```python
async def update_flow(
    flow_id: str,
    expected_revision: int,
    *,
    state: dict | None = None,
    wait: dict | None | _Unset = UNSET,
    status: str | None = None,
    child_session_key: str | None | _Unset = UNSET,
    total_tokens: int | None = None,
    total_cost: float | None = None,
    token_budget: int | None = None,
) -> dict:
```

在 assignments 构建中新增：

```python
    if total_tokens is not None:
        assignments.append("total_tokens = ?")
        params.append(int(total_tokens))
    if total_cost is not None:
        assignments.append("total_cost = ?")
        params.append(float(total_cost))
    if token_budget is not None:
        assignments.append("token_budget = ?")
        params.append(int(token_budget))
```

### 4.4 taskflow_resume.py — 聚合 token 用量

```python
@tool("taskflow_resume")
async def taskflow_resume(
    flow_id: str,
    child_session_key: str = "",
    result: str = "",
    expected_revision: int | None = None,
    token_usage: dict | None = None,  # ← 新增: {"input_tokens": N, "output_tokens": M, "model_name": "..."}
) -> str:
    # ... 现有逻辑 ...

    # === 新增：聚合 token 用量 ===
    update_kwargs: dict[str, Any] = {
        "state": state,
        "wait": None,
        "status": new_status,
    }
    if token_usage:
        from config.num import MODEL_PRICING_PER_M_TOKENS

        new_tokens = (flow.get("total_tokens") or 0) + (
            (token_usage.get("input_tokens") or 0)
            + (token_usage.get("output_tokens") or 0)
        )

        model_name = token_usage.get("model_name", "") or ""
        pricing = MODEL_PRICING_PER_M_TOKENS.get(
            model_name, MODEL_PRICING_PER_M_TOKENS["_default"]
        )
        cost_delta = (
            (token_usage.get("input_tokens") or 0) * pricing["input"] / 1_000_000
            + (token_usage.get("output_tokens") or 0) * pricing["output"] / 1_000_000
        )
        new_cost = (flow.get("total_cost") or 0.0) + cost_delta

        update_kwargs["total_tokens"] = new_tokens
        update_kwargs["total_cost"] = round(new_cost, 6)

    try:
        updated = await store_sqlite.update_flow(
            flow_id, revision, **update_kwargs,
        )
    # ... 现有错误处理 ...
```

### 4.5 taskflow_budget.py — 新建

```python
"""taskflow_budget: set and query token/cost budget for a task flow."""

from langchain_core.tools import tool

from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import conflict_error, is_terminal, not_found_error, terminal_error


@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str:
    """Set or query the token/cost budget for a task flow.

    Actions:
    - 'query': return current usage (total_tokens, total_cost, token_budget, remaining).
    - 'set': set the token_budget for this flow (requires token_budget and expected_revision).
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)

    action = (action or "query").strip().lower()

    if action == "query":
        total_tokens = flow.get("total_tokens", 0)
        total_cost = flow.get("total_cost", 0.0)
        budget = flow.get("token_budget", 0)
        remaining = budget - total_tokens if budget > 0 else -1
        pct = (total_tokens / budget * 100) if budget > 0 else -1

        from config.num import BUDGET_WARN_THRESHOLD

        status = "ok"
        if budget > 0:
            if total_tokens >= budget:
                status = "EXCEEDED"
            elif total_tokens / budget >= BUDGET_WARN_THRESHOLD:
                status = "WARNING"

        return (
            f"Budget: flow_id={flow_id}\n"
            f"  tokens: {total_tokens:,}"
            + (f" / {budget:,} ({pct:.1f}%)" if budget > 0 else " (no budget set)")
            + f"\n  cost: ${total_cost:.4f}"
            + (f"\n  remaining: {remaining:,} tokens" if budget > 0 else "")
            + f"\n  status: {status}"
        )

    if action == "set":
        if token_budget is None or token_budget <= 0:
            return "Error: token_budget must be a positive integer for 'set' action."
        if is_terminal(flow["status"]):
            return terminal_error(flow_id, flow["status"])

        revision = int(expected_revision) if expected_revision is not None else flow["expected_revision"]

        try:
            updated = await store_sqlite.update_flow(
                flow_id, revision, token_budget=int(token_budget),
            )
        except FlowConflictError as exc:
            return conflict_error(exc)
        except FlowNotFoundError:
            return not_found_error(flow_id)

        return (
            f"Budget set: flow_id={flow_id}, token_budget={token_budget:,}, "
            f"revision={updated['expected_revision']}"
        )

    return f"Error: unknown action '{action}'. Use 'query' or 'set'."
```

### 4.6 tools/**init**.py — 注册

```python
from .taskflow_budget import taskflow_budget

_TASKFLOW_TOOLS: list[BaseTool] = [
    taskflow_create,
    taskflow_run_task,
    taskflow_set_waiting,
    taskflow_resume,
    taskflow_finish,
    taskflow_fail,
    taskflow_cancel,
    taskflow_summary,
    taskflow_progress,      # GAP-6
    taskflow_budget,        # ← 新增
    taskflow_dispatch,
    taskflow_wait_all,
]
```

---

## 5. 实施顺序

```
Step 1: config/num.py — 新增 MODEL_PRICING_PER_M_TOKENS + BUDGET_WARN_THRESHOLD
Step 2: store_sqlite.py — _CREATE_TABLE_SQL 新增列 + _ensure_token_columns() migration
Step 3: store_sqlite.py — _SELECT_COLUMNS_SQL + _row_to_flow 新增字段
Step 4: store_sqlite.py — update_flow() 支持 total_tokens/total_cost/token_budget
Step 5: taskflow_resume.py — 接收 token_usage 参数 + 聚合逻辑
Step 6: taskflow_budget.py — 新建工具
Step 7: tools/__init__.py — 注册 taskflow_budget
Step 8: taskflow/__init__.py — 导出
Step 9: tests/agent/tools/taskflow/test_token_budget.py — 编写测试
Step 10: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

### 6.1 单元测试 (tests/agent/tools/taskflow/test_token_budget.py)

| 测试                                 | 说明                                                        |
| ------------------------------------ | ----------------------------------------------------------- |
| `test_migration_adds_columns`        | 旧 DB（无 token 列）→ migration 后新列存在且默认值为 0      |
| `test_row_to_flow_has_token_fields`  | 读取 flow → total_tokens/total_cost/token_budget 存在       |
| `test_update_flow_sets_total_tokens` | update_flow(total_tokens=500) → 读取返回 500                |
| `test_resume_aggregates_tokens`      | resume + token_usage → total_tokens 累加                    |
| `test_resume_without_token_usage`    | resume 无 token_usage → total_tokens 不变（向后兼容）       |
| `test_resume_calculates_cost`        | resume + token_usage + model_name → total_cost 按价格表计算 |
| `test_budget_query_no_budget`        | 未设置 budget → "no budget set"                             |
| `test_budget_query_with_budget`      | 设置 budget → 显示百分比和剩余                              |
| `test_budget_query_exceeded`         | total_tokens > budget → status=EXCEEDED                     |
| `test_budget_query_warning`          | total_tokens/budget ≥ 80% → status=WARNING                  |
| `test_budget_set`                    | set budget → 确认更新                                       |
| `test_budget_set_requires_revision`  | 冲突 → 返回 conflict error                                  |
| `test_budget_set_terminal_rejected`  | terminal flow → 拒绝                                        |

### 6.2 运行验证

```bash
python -m pytest tests/agent/tools/taskflow/test_token_budget.py -v
python -m pytest tests/agent/tools/taskflow/test_store_sqlite.py -v  # 确保不破坏
ruff check agent/tools/taskflow/tools/taskflow_budget.py agent/tools/taskflow/tools/taskflow_resume.py agent/tools/taskflow/registry/store_sqlite.py
pyright agent/tools/taskflow/tools/taskflow_budget.py agent/tools/taskflow/tools/taskflow_resume.py agent/tools/taskflow/registry/store_sqlite.py
```

---

## 7. 数据流

### 7.1 设置预算

```
Agent 调用 taskflow_budget(flow_id="deploy-v2", action="set", token_budget=50000, expected_revision=1)
  → update_flow("deploy-v2", 1, token_budget=50000)
  → 返回 "Budget set: flow_id=deploy-v2, token_budget=50,000, revision=2"
```

### 7.2 注入结果 + 聚合 token

```
子代理完成 → announce 管线 deliver 结果 → 模型调用:
taskflow_resume(flow_id="deploy-v2", child_session_key="agent:main:session:child-1",
                result="deployment successful", expected_revision=3,
                token_usage={"input_tokens": 5000, "output_tokens": 2000, "model_name": "glm-5"})
  → 注入 result 到 state.results
  → 聚合: total_tokens = 0 + (5000+2000) = 7000
  → cost: 5000 * 0.5/1M + 2000 * 1.5/1M = $0.0055
  → update_flow(state=..., total_tokens=7000, total_cost=0.0055)
  → 返回 "TaskFlow resumed: ... results=1 ..."
```

### 7.3 查询预算

```
Agent 调用 taskflow_budget(flow_id="deploy-v2", action="query")
  → 读取 flow: total_tokens=7000, total_cost=0.0055, token_budget=50000
  → 7000/50000 = 14% < 80% → status=ok
  → 返回:
    "Budget: flow_id=deploy-v2
       tokens: 7,000 / 50,000 (14.0%)
       cost: $0.0055
       remaining: 43,000 tokens
       status: ok"
```

---

## 8. 与其他方案的关系

| 方案             | 关系                                                               |
| ---------------- | ------------------------------------------------------------------ |
| **#4 截止时间**  | 互补。#3 管 token 预算，#4 管时间预算，两者都是资源控制。          |
| **#6 进度报告**  | 协作。进度报告可显示 token 使用进度。                              |
| **#11 空闲检测** | 协作。空闲检测关注 WAITING 状态超时；预算跟踪关注 token 消耗超限。 |
| **LT-2**         | 依赖。LT-2 注入未完成 flow 摘要时可包含预算状态。                  |

---

## 9. 风险与缓解

| 风险                          | 缓解                                                                       |
| ----------------------------- | -------------------------------------------------------------------------- |
| DB migration 在已有数据上失败 | Additive ALTER TABLE + try/except（与 images/audios 列迁移模式一致）       |
| 模型不在价格表中              | `_default` 兜底价格                                                        |
| token_usage 未传入            | 可选参数，不传则跳过聚合（向后兼容）                                       |
| cost 计算不准确               | 明确标注为估算；仅用于告警，非计费                                         |
| 预算超限不自动暂停            | 非阻塞设计：仅告警 status=EXCEEDED，避免误杀任务（自动暂停留 #8 重试策略） |
| 子代理多轮对话的 token 不完整 | 仅记录 resume 时的 token_usage（每步一次），不追踪子代理内部多轮           |
