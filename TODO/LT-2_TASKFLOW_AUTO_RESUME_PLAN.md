# LT-2 跨会话 TaskFlow 自动续接 — 实施计划

> 日期: 2026-09-11
> 来源: SESSION_MEMORY_BORROWING_PLAN.md §LT-2 (行 1871-1950)
> 参考: LONG_RUNNING_TASK_GAP_ANALYSIS.md (差距 #5 #6 #9 联动)
> 决策: state_json 存 creator_session_key + 同步查询 + prompt_builder 注入
> 预估工时: 1 天

---

## 1. 问题

TaskFlow 状态持久化在 SQLite（`taskflow_registry.db`），但新会话不会自动检查"上次有没有未完成的 TaskFlow"。用户隔天回来说"继续"，系统不会主动恢复任务上下文，模型需要手动调用 `taskflow_summary` 才知道有未完成任务。

**当前状态：**

- `task_flows` 表有 `child_session_key` 列，但存的是**子代理**的 session key（由 `taskflow_run_task`/`taskflow_dispatch` 写入），**不是**父会话的 key
- `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"` 是父会话 key，但仅传给 `dispatch_child()`，**未持久化**到 flow 记录
- 原设计（SESSION_MEMORY_BORROWING_PLAN.md）用 `child_session_key LIKE '{channel_id}:{chat_id}:%'` 查询，但实际 key 格式是 `agent:main:session:{session_id}`，且 child_session_key 是子代理 key 而非父会话 key
- `build_system_prompt()` 是**同步**函数，无法直接 `await` 异步查询
- `taskflow_create` 工具**没有** `session_id` 注入（`taskflow_run_task` 有），无法在创建时记录创建者会话

---

## 2. 设计决策

### 2.1 关联方式：state_json 存 creator_session_key

```
taskflow_create(flow_id="deploy-v2", description="...", session_id="abc")
  → default_state(description, initial_state, creator_session_key="agent:main:session:abc")
  → create_flow(flow_id, state)  # state_json 内含 creator_session_key
```

无 DB schema 迁移：`creator_session_key` 存在 `state_json` 的 JSON 里，复用现有 `state_json TEXT` 列。

### 2.2 查询方式：同步全表扫描 + Python 过滤

`build_system_prompt()` 是同步函数。新增 `get_active_flows_sync()` 用 stdlib `sqlite3`（复用 `get_flow_sync()` 的模式），返回所有 `status IN ('running', 'waiting')` 的 flow，调用方按 `state["creator_session_key"]` 过滤。

活跃 flow 数量通常 1-5 个，全表扫描可接受。后续如需优化可加 DB 列 + 索引。

### 2.3 关键决策

| 决策点          | 选择                                      | 理由                                                                         |
| --------------- | ----------------------------------------- | ---------------------------------------------------------------------------- |
| 关联方式        | `state_json` 存 `creator_session_key`     | 避免 DB migration（ALTER TABLE）；state_json 已存 description/steps/results  |
| 查询方式        | `get_active_flows_sync()` 同步全表扫描    | `build_system_prompt()` 是同步函数；复用 `get_flow_sync()` 的 stdlib sqlite3 |
| session_id 注入 | 给 `taskflow_create` 添加 `InjectedState` | 与 `taskflow_run_task` 一致；`scope=main_only` 保证 state 有值               |
| prompt 注入位置 | todo/boulder blocks 之后、skills 之前     | 与 todo/boulder 同属"活跃工作"区块，逻辑内聚                                 |
| prompt 注入时机 | 会话启动 + 压缩后 rebuild                 | 系统 prompt 按会话缓存，压缩后 `build_system_prompt()` 重新调用，自动刷新    |
| 注入上限        | 最多 3 个 flow，每个 ~100 chars           | 控制系统 prompt 开销；总计 ~300-400 chars                                    |
| fail-open       | try/except 返回 ""                        | 与 `_build_todo_block()` / `_build_boulder_block()` 一致，不破坏 prompt 生成 |
| 旧 flow 兼容    | 缺失 `creator_session_key` 不匹配         | 向后兼容：旧 flow 不注入（可接受，它们在 LT-2 之前创建）                     |

---

## 3. 涉及文件

| 文件                                              | 操作                             | 预估行数 |
| ------------------------------------------------- | -------------------------------- | -------- |
| `agent/tools/taskflow/tools/_shared.py`           | 修改：`default_state()` 增加参数 | ~3       |
| `agent/tools/taskflow/tools/taskflow_create.py`   | 修改：注入 session_id，传 key    | ~10      |
| `agent/tools/taskflow/registry/store_sqlite.py`   | 新增 `get_active_flows_sync()`   | ~25      |
| `agent/tools/taskflow/registry/__init__.py`       | 导出 `get_active_flows_sync`     | ~2       |
| `workspace/prompt_builder.py`                     | 新增 `_build_taskflow_block()`   | ~35      |
| `tests/agent/tools/taskflow/test_store_sqlite.py` | 新增 sync 查询测试               | ~40      |
| `tests/workspace/test_prompt_builder_taskflow.py` | **新建**                         | ~90      |

---

## 4. 详细设计

### 4.1 agent/tools/taskflow/tools/_shared.py — 修改 default_state()

```python
def default_state(
    description: str,
    initial_state: dict | None,
    *,
    creator_session_key: str = "",
) -> dict:
    """Build the initial state_json payload with guaranteed invariants.

    ``steps`` and ``results`` are always lists (run_task / resume append to
    them); ``description`` and any caller keys are preserved.
    ``creator_session_key`` (LT-2) associates the flow with the parent session
    for cross-session auto-resume injection.
    """
    state = dict(initial_state or {})
    state["description"] = description or str(state.get("description") or "")
    state["steps"] = list(state.get("steps") or [])
    state["results"] = list(state.get("results") or [])
    if creator_session_key:
        state["creator_session_key"] = creator_session_key
    return state
```

### 4.2 agent/tools/taskflow/tools/taskflow_create.py — 注入 session_id

```python
"""taskflow_create: create a durable task flow (openclaw createManaged)."""

from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import INITIAL_REVISION
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowExistsError
from ._shared import default_state, requester_session_key

SessionId = Annotated[str, InjectedState("session_id")]


@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: SessionId = "",
) -> str:
    """Create a durable task flow and return its initial revision.

    A flow tracks multi-step work across turns with optimistic locking: every
    mutation bumps expected_revision, so concurrent writers are detected via
    revision conflicts instead of silent last-write-wins.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    creator_key = requester_session_key(session_id) if session_id else ""
    state = default_state(description, initial_state, creator_session_key=creator_key)
    try:
        flow = await store_sqlite.create_flow(flow_id, state)
    except FlowExistsError:
        existing = await store_sqlite.get_flow(flow_id)
        revision = existing["expected_revision"] if existing else INITIAL_REVISION
        return (
            f"Error: TaskFlow '{flow_id}' already exists (revision={revision}). "
            "Re-read it with taskflow_summary."
        )
    return (
        f"TaskFlow created: flow_id={flow['flow_id']}, status={flow['status']}, "
        f"revision={flow['expected_revision']}"
    )
```

### 4.3 agent/tools/taskflow/registry/store_sqlite.py — 新增 get_active_flows_sync()

```python
# 在 get_flow_sync() 之后新增


def get_active_flows_sync() -> list[dict]:
    """Synchronously read all non-terminal flows (stdlib sqlite3).

    Returns flows with status 'running' or 'waiting'. Caller filters by
    ``state['creator_session_key']`` to scope to the current session.
    Mirrors the sync read pattern of get_flow_sync(): threading.Lock-guarded
    table creation, connect-level busy timeout, failures logged and swallowed
    with an empty list return.
    """
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            cursor = conn.execute(
                _SELECT_COLUMNS_SQL
                + f" WHERE status = ? OR status = ?"
                + f" ORDER BY expected_revision DESC",
                (
                    TaskFlowStatus.RUNNING.value,
                    TaskFlowStatus.WAITING.value,
                ),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [_row_to_flow(row) for row in rows]
    except Exception as e:
        logger.warning("Failed to sync-read active taskflows: {}", e)
        return []
```

需要在文件头部导入 `TaskFlowStatus`：

```python
# 修改现有导入行：
from ..config import INITIAL_REVISION, TABLE_NAME, TaskFlowStatus
```

### 4.4 agent/tools/taskflow/registry/**init**.py — 导出

```python
from .store_sqlite import (
    FlowConflictError,
    FlowExistsError,
    FlowNotFoundError,
    TaskFlowStoreError,
    UNSET,
    create_flow,
    ensure_db,
    get_active_flows_sync,
    get_flow,
    get_flow_sync,
    update_flow,
)

__all__ = [
    "FlowConflictError",
    "FlowExistsError",
    "FlowNotFoundError",
    "TaskFlowStoreError",
    "UNSET",
    "create_flow",
    "ensure_db",
    "get_active_flows_sync",
    "get_flow",
    "get_flow_sync",
    "update_flow",
]
```

### 4.5 workspace/prompt_builder.py — 新增 _build_taskflow_block() + 注入

#### 新增辅助函数

```python
def _build_taskflow_block(session_id: str) -> str:
    """Render pending TaskFlows for this session. Returns "" on none or failure.

    Scans the taskflow registry for non-terminal flows whose creator_session_key
    matches this session, and injects a concise summary so the agent can
    proactively continue unfinished work on session start.

    Format (~300-400 chars for 3 flows):
        ## Pending TaskFlows
        - [running] flow-deploy-v2: "deploy to staging" | 2/5 steps done | next: step-3 "run smoke tests"
        - [waiting] flow-review: "review PR #42" | 1/1 dispatched, awaiting child
        Use taskflow_summary to inspect a flow and continue execution.
    """
    try:
        from agent.tools.taskflow.registry import store_sqlite
        from agent.tools.taskflow.tools._shared import requester_session_key, step_status, steps_summary

        creator_key = requester_session_key(session_id)
        active_flows = store_sqlite.get_active_flows_sync()

        # Filter: only flows created by THIS session
        mine = [
            flow for flow in active_flows
            if (flow.get("state") or {}).get("creator_session_key") == creator_key
        ]
        if not mine:
            return ""

        lines = ["## Pending TaskFlows"]
        for flow in mine[:3]:  # max 3 flows
            state = flow.get("state") or {}
            steps = state.get("steps") or []
            counts = steps_summary(steps)
            total = len(steps)
            done = counts.get("done", 0)
            desc = state.get("description", "")[:60]
            status = flow.get("status", "?")

            # Find next actionable step (first non-done)
            pending = [s for s in steps if step_status(s) not in ("done",)]
            next_hint = ""
            if pending:
                next_step = pending[0]
                next_id = next_step.get("step_id", "?")
                next_task = (next_step.get("task", "") or "")[:40]
                next_hint = f' | next: {next_id} "{next_task}"'

            lines.append(
                f"- [{status}] {flow['flow_id']}: \"{desc}\" | {done}/{total} steps done{next_hint}"
            )

        lines.append(
            "Use taskflow_summary to inspect a flow and continue execution."
        )
        return "\n".join(lines)
    except Exception:
        return ""
```

#### build_system_prompt() 注入

在 `build_system_prompt()` 中，todo/boulder blocks 之后追加 taskflow block：

```python
def build_system_prompt(
    selected_file_names: list[str] | None = None,
    selected_skill_names: list[str] | None = None,
    session_id: str | None = None,
) -> str:
    # ... 现有逻辑 ...

    # --- Todo + boulder + taskflow blocks --------------------------------
    # Rebuilt from live state on every call, so they survive context
    # compression; skipped entirely when there is no session to scope them to.
    if session_id:
        blocks = [
            _build_todo_block(session_id),
            _build_boulder_block(session_id),
            _build_taskflow_block(session_id),  # ← 新增
        ]
    else:
        blocks = []

    # --- Assembling the final prompt ----------------------------------
    parts = [*file_paths, *blocks, skill_paths]
    return "\n\n".join(p for p in parts if p)
```

注入条件与 memory block 一致：仅当 `selected_file_names is None`（非筛选模式）且 `session_id` 存在时注入。taskflow block 已在 `if session_id:` 分支内，与 todo/boulder 一致。

### 4.6 注入格式示例

**有 2 个未完成 flow 时：**

```
## Pending TaskFlows
- [running] deploy-v2: "deploy to staging" | 2/5 steps done | next: step-3 "run smoke tests"
- [waiting] code-review: "review PR #42" | 1/1 steps done
Use taskflow_summary to inspect a flow and continue execution.
```

**无未完成 flow 时：** 不注入任何内容（返回 ""）。

**旧 flow（无 creator_session_key）：** 不匹配，不注入。

---

## 5. 实施顺序

```
Step 1: agent/tools/taskflow/tools/_shared.py — default_state() 增加 creator_session_key 参数
Step 2: agent/tools/taskflow/tools/taskflow_create.py — 注入 session_id + 传 creator_key
Step 3: agent/tools/taskflow/registry/store_sqlite.py — 新增 get_active_flows_sync()
Step 4: agent/tools/taskflow/registry/__init__.py — 导出
Step 5: workspace/prompt_builder.py — 新增 _build_taskflow_block() + 注入到 build_system_prompt()
Step 6: tests/agent/tools/taskflow/test_store_sqlite.py — sync 查询测试
Step 7: tests/workspace/test_prompt_builder_taskflow.py — prompt 注入测试
Step 8: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

### 6.1 单元测试 — store_sqlite (tests/agent/tools/taskflow/test_store_sqlite.py)

| 测试                                               | 说明                                              |
| -------------------------------------------------- | ------------------------------------------------- |
| `test_get_active_flows_sync_returns_running`       | 创建 running flow → sync 查询返回它               |
| `test_get_active_flows_sync_returns_waiting`       | 创建 waiting flow → sync 查询返回它               |
| `test_get_active_flows_sync_excludes_terminal`     | 创建 done/failed/cancelled flow → sync 查询不返回 |
| `test_get_active_flows_sync_empty`                 | 无活跃 flow → 返回空列表                          |
| `test_get_active_flows_sync_ordered_by_rev`        | 多个活跃 flow → 按 expected_revision DESC 排序    |
| `test_get_active_flows_sync_failure_returns_empty` | DB 不可用 → 返回空列表（fail-open）               |

### 6.2 单元测试 — taskflow_create (tests/agent/tools/taskflow/)

| 测试                                     | 说明                                                    |
| ---------------------------------------- | ------------------------------------------------------- |
| `test_create_stores_creator_session_key` | 创建 flow 时传入 session_id → state_json 含 creator_key |
| `test_create_without_session_id`         | session_id="" → state_json 不含 creator_key（向后兼容） |

### 6.3 集成测试 — prompt_builder (tests/workspace/test_prompt_builder_taskflow.py)

| 测试                                               | 说明                                                          |
| -------------------------------------------------- | ------------------------------------------------------------- |
| `test_taskflow_block_injected_when_active`         | 有匹配 session 的活跃 flow → prompt 含 "## Pending TaskFlows" |
| `test_taskflow_block_absent_when_no_flows`         | 无活跃 flow → prompt 不含 "Pending TaskFlows"                 |
| `test_taskflow_block_absent_when_session_mismatch` | 活跃 flow 的 creator_key 不匹配 → 不注入                      |
| `test_taskflow_block_absent_when_no_session`       | session_id=None → 不注入                                      |
| `test_taskflow_block_shows_step_progress`          | prompt 含 "2/5 steps done" 格式                               |
| `test_taskflow_block_max_three_flows`              | 5 个活跃 flow → 只注入前 3 个                                 |
| `test_taskflow_block_fail_open`                    | store_sqlite 异常 → prompt 不含 taskflow block，不崩溃        |
| `test_taskflow_block_absent_when_filtered_files`   | selected_file_names 非 None → 不注入（与 memory block 一致）  |
| `test_old_flow_without_creator_key_not_injected`   | 旧 flow 无 creator_session_key → 不注入                       |

### 6.4 运行验证

```bash
# 运行 taskflow store 测试
python -m pytest tests/agent/tools/taskflow/test_store_sqlite.py -v

# 运行 prompt builder 测试
python -m pytest tests/workspace/test_prompt_builder_taskflow.py -v

# 运行全部 taskflow 测试（确保不破坏现有功能）
python -m pytest tests/agent/tools/taskflow/ -v

# 运行全部 prompt builder 测试
python -m pytest tests/workspace/test_prompt_builder.py tests/workspace/test_prompt_builder_todos.py -v

# Lint + Typecheck
ruff check agent/tools/taskflow/registry/store_sqlite.py agent/tools/taskflow/tools/_shared.py agent/tools/taskflow/tools/taskflow_create.py workspace/prompt_builder.py
pyright agent/tools/taskflow/registry/store_sqlite.py agent/tools/taskflow/tools/_shared.py agent/tools/taskflow/tools/taskflow_create.py workspace/prompt_builder.py

# 端到端验证：启动 Agent，创建 TaskFlow，新开会话检查 prompt 包含未完成任务摘要
```

---

## 7. 数据流

### 7.1 创建 flow（记录创建者会话）

```
Agent 调用 taskflow_create(flow_id="deploy-v2", description="deploy to staging")
  → session_id 从 InjectedState 获取 = "abc"
  → creator_key = requester_session_key("abc") = "agent:main:session:abc"
  → default_state("deploy to staging", None, creator_session_key="agent:main:session:abc")
  → create_flow("deploy-v2", state)
  → state_json 写入 {"description":"deploy to staging","steps":[],"results":[],"creator_session_key":"agent:main:session:abc"}
  → 返回 "TaskFlow created: flow_id=deploy-v2, status=running, revision=1"
```

### 7.2 新会话启动（自动注入未完成任务）

```
新会话 session_id="abc" 启动
  → ContextEngineHook._get_and_reload_system_prompt("abc")
  → cache miss → build_system_prompt(session_id="abc")
  → _build_taskflow_block("abc")
    → creator_key = requester_session_key("abc") = "agent:main:session:abc"
    → get_active_flows_sync()
      → SELECT ... FROM task_flows WHERE status='running' OR status='waiting' ORDER BY expected_revision DESC
      → 返回 [{"flow_id":"deploy-v2","state":{"creator_session_key":"agent:main:session:abc",...},...}]
    → 过滤: state["creator_session_key"] == "agent:main:session:abc" ✓
    → 格式化:
      "## Pending TaskFlows\n
       - [running] deploy-v2: \"deploy to staging\" | 2/5 steps done | next: step-3 \"run smoke tests\"\n
       Use taskflow_summary to inspect a flow and continue execution."
  → 注入到系统 prompt（todo/boulder 之后、skills 之前）
  → 模型看到未完成任务摘要 → 主动调用 taskflow_summary → 续接执行
```

### 7.3 压缩后重建（刷新 flow 状态）

```
对话触发压缩（T1-T5 触发器）
  → summarization.py 重建系统 prompt
  → build_system_prompt(session_id="abc")
  → _build_taskflow_block("abc")  ← 重新查询，刷新 flow 状态
  → 如果 flow 已完成 → 不注入
  → 如果 flow 仍在进行 → 注入最新步骤进度
  → 压缩后的对话保留任务上下文
```

### 7.4 不匹配的场景（不注入）

```
新会话 session_id="xyz" 启动
  → creator_key = "agent:main:session:xyz"
  → get_active_flows_sync() 返回 [{"flow_id":"deploy-v2","state":{"creator_session_key":"agent:main:session:abc",...}}]
  → 过滤: "agent:main:session:abc" != "agent:main:session:xyz" ✗
  → 返回 ""
  → 不注入 taskflow block
```

---

## 8. 与其他方案的关系

| 方案                          | 关系                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------ |
| **LT-1 分层记忆存储**         | 独立，无依赖。两者可并行实施。                                                                         |
| **#5 检查点级任务恢复**       | 联动。LT-2 让模型知道有未完成任务（被动注入），#5 的 `taskflow_resume_from` 执行实际恢复（主动恢复）。 |
| **#6 进度报告工具**           | 联动。LT-2 是会话启动时的被动注入，#6 是会话中的主动查询（`taskflow_progress`）。两者互补。            |
| **#9 跨会话任务看板**         | 互补。LT-2 按创建者会话过滤注入，#9 是全局看板（`taskflow_list` 列出所有活跃 flow）。                  |
| **LT-3 摘要退化防护**         | 依赖 LT-2。压缩摘要基准线从 TaskFlow 状态（LT-2 注入的摘要）重建。                                     |
| **LT-7 摘要与 TaskFlow 协调** | 依赖 LT-2。压缩 prompt 中注入 TaskFlow 状态摘要，确保摘要准确。                                        |
| **LT-8 会话间意图连续性**     | 依赖 LT-2。"继续上次"场景需要 LT-2 先注入未完成任务摘要。                                              |

---

## 9. 风险与缓解

| 风险                              | 缓解                                                                                           |
| --------------------------------- | ---------------------------------------------------------------------------------------------- |
| 全表扫描性能                      | 活跃 flow 通常 1-5 个；后续可加 `creator_session_key` DB 列 + 索引优化（非阻塞）               |
| prompt 缓存导致 flow 状态过时     | 压缩后 `build_system_prompt()` 重新调用，自动刷新；会话内新建的 flow 由模型工作记忆跟踪        |
| 旧 flow 无 `creator_session_key`  | 向后兼容：缺失字段不匹配，不注入（可接受，旧 flow 在 LT-2 之前创建）                           |
| `get_active_flows_sync()` DB 异常 | try/except 包裹，返回空列表（fail-open），不影响系统 prompt 生成                               |
| prompt 注入失败                   | `_build_taskflow_block()` 整体 try/except，返回 ""（与 todo/boulder block 一致）               |
| `taskflow_create` 签名变更        | 新增 `session_id` 参数有默认值 `""`，向后兼容；`InjectedState` 在 LangGraph 工具调用时自动填充 |
| 注入过多 flow 撑大 prompt         | 最多 3 个 flow，每个 ~100 chars，总计 ~300-400 chars                                           |
| 并发写入与 sync 读竞争            | 复用 `get_flow_sync()` 的 `busy_timeout=5000ms` + WAL 模式                                     |
