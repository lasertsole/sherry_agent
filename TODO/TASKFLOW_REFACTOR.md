# TaskFlow Session 隔离方案

## 目标

每个 session 只能看到自己的 TaskFlow / TodoList / Knowledge 数据，绝不串。子 agent 不能操作这三类工具。

## 问题根因

| 问题                          | 现状                                                                                     |
| ----------------------------- | ---------------------------------------------------------------------------------------- |
| 无 DB 级隔离                  | `task_flows` 表没有 `session_id` 列，`creator_session_key` 藏在 `state_json` JSON 里     |
| SQL 查询全表                  | `get_active_flows_sync()` / `get_all_flows_sync()` / `get_flow()` 全部不过滤 session     |
| Python 层软过滤               | summarization 中间件读全表再 Python filter；`taskflow_list` 完全不过滤                   |
| 子 agent 有 taskflow          | `spawn/core.py:447` 和 `steer.py:136` 调 `build_main_tools()`，含 `build_taskflow_tools` |
| `taskflow_summary` 无归属检查 | 任何 session 凭 `flow_id` 就能读任何 flow                                                |

---

## Phase 1: DB Schema + Store 层

**文件**: `agent/tools/taskflow/registry/store_sqlite.py`

### Schema 变更

```sql
-- 新增列（additive migration，同 token 列模式）
ALTER TABLE task_flows ADD COLUMN session_id TEXT NOT NULL DEFAULT '';

-- 新增索引
CREATE INDEX IF NOT EXISTS idx_taskflows_session ON task_flows(session_id);
```

### 函数签名变更

| 函数                      | Before                                                                   | After                                                                            |
| ------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `_CREATE_TABLE_SQL`       | 无 session_id                                                            | 加 `session_id TEXT NOT NULL DEFAULT ''`                                         |
| `_SELECT_COLUMNS_SQL`     | 无 session_id                                                            | 加 `session_id`                                                                  |
| `_row_to_flow()`          | 不解包 session_id                                                        | 解包 `session_id`                                                                |
| `create_flow()`           | `create_flow(flow_id, state, *, status, child_session_key, deadline_ts)` | 加 `session_id: str` 必填参数，INSERT 含 `session_id`                            |
| `get_flow()`              | `get_flow(flow_id)`                                                      | `get_flow(flow_id, session_id)` → `WHERE flow_id = ? AND session_id = ?`         |
| `get_flow_sync()`         | `get_flow_sync(flow_id)`                                                 | `get_flow_sync(flow_id, session_id)` → 同上                                      |
| `get_active_flows_sync()` | `get_active_flows_sync()`                                                | `get_active_flows_sync(session_id)` → `WHERE session_id = ? AND status IN (...)` |
| `get_all_flows_sync()`    | `get_all_flows_sync(status_filter)`                                      | `get_all_flows_sync(session_id, status_filter)` → `WHERE session_id = ?`         |
| `update_flow()`           | `WHERE flow_id = ? AND expected_revision = ?`                            | `WHERE flow_id = ? AND expected_revision = ? AND session_id = ?`                 |

### 不改的函数

| 函数                               | 原因                                      |
| ---------------------------------- | ----------------------------------------- |
| `get_overdue_flows()`              | sweeper 系统级扫描，跨 session 是设计意图 |
| `get_waiting_flows()`              | 同上                                      |
| `get_flow` (async) 无 session 重载 | 删掉，所有调用方必须传 session_id         |

### Migration DDL

```python
_SESSION_ID_COLUMN_DDL: list[str] = [
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN session_id TEXT NOT NULL DEFAULT ''",
]

_SESSION_ID_INDEX_DDL: list[str] = [
    f"CREATE INDEX IF NOT EXISTS idx_taskflows_session ON {TABLE_NAME}(session_id)",
]
```

在 `_ensure_tables_sync()` 和 `_init_db()` 中执行 migration（同 `_ensure_token_columns_sync` 模式）。

---

## Phase 2: 工具层

所有 taskflow 工具已有 `SessionId = Annotated[str, InjectedState("session_id")]` 模式，只需加注入 + 透传。

### 需要加 `SessionId` 注入的工具（目前没有）

| 工具                   | 文件                      | 改动                                                                                                                            |
| ---------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `taskflow_summary`     | `taskflow_summary.py`     | 加 `SessionId` 参数，传给 `get_flow(flow_id, session_id)`                                                                       |
| `taskflow_list`        | `taskflow_list.py`        | 加 `SessionId` 参数，传给 `get_all_flows_sync(session_id, ...)`；docstring 从 "cross-session board" 改为 "current session only" |
| `taskflow_budget`      | `taskflow_budget.py`      | 加 `SessionId`，传给 `get_flow` / `update_flow`                                                                                 |
| `taskflow_cancel`      | `taskflow_cancel.py`      | 同上                                                                                                                            |
| `taskflow_fail`        | `taskflow_fail.py`        | 同上                                                                                                                            |
| `taskflow_finish`      | `taskflow_finish.py`      | 同上                                                                                                                            |
| `taskflow_progress`    | `taskflow_progress.py`    | 加 `SessionId`，传给 `get_flow`                                                                                                 |
| `taskflow_set_waiting` | `taskflow_set_waiting.py` | 加 `SessionId`，传给 `get_flow` / `update_flow`                                                                                 |

### 已有 `SessionId` 的工具（只需透传到 store 调用）

| 工具                | 文件                   | 改动                                                                        |
| ------------------- | ---------------------- | --------------------------------------------------------------------------- |
| `taskflow_create`   | `taskflow_create.py`   | `create_flow(..., session_id=session_id)`                                   |
| `taskflow_dispatch` | `taskflow_dispatch.py` | `get_flow(flow_id, session_id)` + `update_flow(..., session_id=session_id)` |
| `taskflow_resume`   | `taskflow_resume.py`   | 同上                                                                        |
| `taskflow_run_task` | `taskflow_run_task.py` | 同上                                                                        |
| `taskflow_wait_all` | `taskflow_wait_all.py` | 同上                                                                        |

### `_shared.py` 改动

| 函数                                | 改动                                                                                                     |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `update_flow_with_conflict_retry()` | 加 `session_id` 参数，透传给 `get_flow` / `update_flow`                                                  |
| `default_state()`                   | 保留 `creator_session_key` 在 state_json（子 agent dispatch 仍需要）；`session_id` 是 DB 列不在 state 里 |
| `requester_session_key()`           | 不改，仍用于 child dispatch                                                                              |

---

## Phase 3: 子 agent 访问控制

**文件**: `agent/tools/__init__.py`

### 拆分 tool builders

```python
_MAIN_ONLY_BUILDERS = [
    build_taskflow_tools,       # DAG 编排 — main agent 独占
    build_todolist_tools,       # 计划管理 — main agent 独占
    build_knowledge_tools,      # 知识库 — main agent 独占
]

_SHARED_BUILDERS = [
    build_python_repl_tool,
    build_read_file_tool,
    build_write_file_tool,
    build_patch_file_tool,
    build_memory_tool,
    build_web_search_tool,
    build_terminal_tool,
    build_mcp_tools,
    build_skill_manage_tool,
    build_skill_list_tool,
    build_skill_view_tool,
    build_message_search_tool,
    build_subagent_runtime_tools,
    build_question_tool,
]

def build_main_tools() -> list[BaseTool]:
    return tool_flatten(_SHARED_BUILDERS + _MAIN_ONLY_BUILDERS)

def build_subagent_tools() -> list[BaseTool]:
    """Tools available to spawned subagents — no taskflow/todolist/knowledge."""
    return tool_flatten(_SHARED_BUILDERS)
```

### 调用方改动

| 文件                                    | 行      | Before                                                                  | After                                                                           |
| --------------------------------------- | ------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `agent/tools/subagent/spawn/core.py`    | 447     | `from agent.tools import build_main_tools` → `tools=build_main_tools()` | `from agent.tools import build_subagent_tools` → `tools=build_subagent_tools()` |
| `agent/tools/subagent/spawn/core.py`    | 807-818 | `build_main_tools()` fallback                                           | `build_subagent_tools()`                                                        |
| `agent/tools/subagent/control/steer.py` | 136     | `from agent.tools import build_main_tools` → `tools=build_main_tools()` | `from agent.tools import build_subagent_tools` → `tools=build_subagent_tools()` |

---

## Phase 4: 中间件 + Data Provider

### `summarization/core.py` (line ~296-302)

```python
# Before:
creator_key = requester_session_key(session_id)
active_flows = taskflow_store.get_active_flows_sync()
mine = [f for f in active_flows
        if (f.get("state") or {}).get("creator_session_key") == creator_key]

# After:
active_flows = taskflow_store.get_active_flows_sync(session_id)
# SQL 已过滤，不需要 Python 层 filter
```

### `runtime/data_provider.py` (line 85-87)

```python
# Before:
def get_active_flows(self) -> list[dict]:
    """Return every non-terminal taskflow row (caller filters by session)."""
    ...

# After:
def get_active_flows(self, session_id: str) -> list[dict]:
    """Return non-terminal taskflow rows for this session only."""
    ...
```

### `agent/prompt_data_provider.py` (line 37-41)

```python
# Before:
def get_active_flows(self) -> list[dict]:
    return store_sqlite.get_active_flows_sync()

# After:
def get_active_flows(self, session_id: str) -> list[dict]:
    return store_sqlite.get_active_flows_sync(session_id)
```

---

## Phase 5: 前端

前端不直接查 taskflow API — 数据通过 session-scoped WS 推送获得。改动最小：

| 文件                                      | 改动                                                         |
| ----------------------------------------- | ------------------------------------------------------------ |
| `client/app/composables/use-todo-list.ts` | `Todo` 类型加 `session_id?: string` 字段（defense-in-depth） |
| `client/app/components/chat/TodoDock.vue` | 无改动                                                       |
| `client/app/components/chat/TodoItem.vue` | 无改动                                                       |

实际隔离在后端 SQL 层强制，前端只是防御性标注。

---

## Phase 6: 测试

| 测试文件                                              | 改动                                                                                  |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `tests/agent/tools/taskflow/test_store_sqlite.py`     | 所有调用点加 `session_id` 参数；新增隔离测试：session A 创建的 flow，session B 查不到 |
| `tests/agent/tools/taskflow/test_dag_e2e.py`          | 所有 `create_flow` / `get_flow` / `update_flow` 调用加 `session_id`                   |
| `tests/agent/tools/taskflow/test_token_budget.py`     | 同上                                                                                  |
| `tests/agent/tools/taskflow/test_deadline.py`         | 同上                                                                                  |
| `tests/agent/tools/subagent/test_spawn_direct_e2e.py` | 验证子 agent 工具列表不含 taskflow/todolist/knowledge                                 |
| `tests/agent/middlewares/test_summarization.py`       | `get_active_flows_sync` 调用加 `session_id`                                           |

### 隔离测试用例

```python
async def test_session_isolation():
    """Session B cannot read Session A's flows."""
    await store_sqlite.create_flow("flow-A", state, session_id="session-A")

    # B can't read A's flow
    flow = await store_sqlite.get_flow("flow-A", "session-B")
    assert flow is None

    # B can't list A's flow
    flows = store_sqlite.get_active_flows_sync("session-B")
    assert len(flows) == 0

    # A can read own flow
    flow = await store_sqlite.get_flow("flow-A", "session-A")
    assert flow is not None
```

---

## 不改的部分

| 组件                                                      | 原因                                           |
| --------------------------------------------------------- | ---------------------------------------------- |
| `sweeper.py` 的 `get_overdue_flows` / `get_waiting_flows` | 系统级扫描，跨 session 是设计意图              |
| `creator_session_key` in `state_json`                     | 子 agent dispatch 仍需要构建 child session key |
| `requester_session_key()`                                 | 仍用于 child dispatch，不再用于过滤            |
| `TodoDock.vue` / `TodoItem.vue`                           | 前端组件已按 session 渲染，无直接 API 调用     |

---

## 实施顺序

1. Phase 1: DB schema + store 层（最底层，其他都依赖它）
2. Phase 2: 工具层（依赖 Phase 1 的 store 签名）
3. Phase 3: 子 agent 访问控制（独立于 1/2，可并行）
4. Phase 4: 中间件 + provider（依赖 Phase 1）
5. Phase 5: 前端（依赖 Phase 4 的 provider 签名）
6. Phase 6: 测试（全量验证）

---

## Phase 7: TaskFlow DAG 动态改动

### 问题

TaskFlow 只支持追加 step（`taskflow_run_task` 自增 `step-{N+1}`），不支持删除、改描述、改依赖、改顺序、合并/拆分 step。agent 实施过程中发现原计划要改时无法修改已有 DAG。

TodoList 侧用 `todowrite` 全量替换没有这个问题，但 TodoList 不管调度，改了 `flow_id`/`step_id` 关联后 TaskFlow 侧的幽灵 step 仍然在。

### 现状

| 操作                      | 支持?  | 工具                                                          |
| ------------------------- | ------ | ------------------------------------------------------------- |
| 创建 flow                 | 是     | `taskflow_create`                                             |
| 追加 step                 | 是     | `taskflow_run_task` — step_id 自动 `step-{N+1}`               |
| dispatch ready steps      | 是     | `taskflow_dispatch`                                           |
| resume 注入结果           | 是     | `taskflow_resume`                                             |
| 标记 step done + 解锁     | 是     | `taskflow_resume` 内部 `mark_step_done` + `unlock_dependents` |
| **删除 step**             | **否** | 无工具                                                        |
| **改 step 的 task 描述**  | **否** | 无工具                                                        |
| **改 step 的 depends_on** | **否** | 无工具                                                        |
| **改 step 顺序**          | **否** | 无工具                                                        |
| **合并/拆分 step**        | **否** | 无工具                                                        |
| 取消整个 flow             | 是     | `taskflow_cancel`（终态，不可逆）                             |

根因：step 的 `step_id` 是 `step-{N+1}` 自增的，DAG 依赖靠 `depends_on` 字符串引用。没有工具能修改已存在 step 的任何字段，也没有删除 step 的工具。

### 方案：新增 `taskflow_update_steps` 工具

**文件**: `agent/tools/taskflow/tools/taskflow_update_steps.py`（新建）

```python
@tool("taskflow_update_steps")
async def taskflow_update_steps(
    flow_id: str,
    steps: list[dict],
    expected_revision: int | None = None,
    session_id: SessionId = "",
) -> str:
    """Full-replace the steps list of a flow (like todowrite for TaskFlow).

    Pass the COMPLETE steps list. Each step must have: step_id, task, depends_on, status.
    Can add/remove/reorder/modify steps. Already-dispatched steps should keep
    their child_session_key; changing a dispatched step's task is allowed but
    the running child won't pick up the change.

    Constraints:
    - step_id must be unique within the list
    - depends_on must reference existing step_ids in the new list
    - No self-dependency (rejected like taskflow_run_task)
    - status must be one of: ready, blocked, dispatched, done
    - A step with status=dispatched must retain its child_session_key
    - A step with status=done must retain its child_session_key
    - Terminal flows (done/failed/cancelled) reject the call
    """
```

### 安全规则

1. **dispatched step 不可降级**: status=dispatched 的 step 不能被改为 ready/blocked（子 agent 已在跑）
2. **done step 不可改**: status=done 的 step 的 task/depends_on 不可改（结果已注入）
3. **child_session_key 保留**: dispatched/done step 必须保留原 child_session_key
4. **depends_on 引用校验**: 新 steps 列表内的引用必须自洽（引用的 step_id 必须在列表内）
5. **孤儿 child 检测**: 如果删除了一个 dispatched step，其 child_session_key 对应的子 agent 仍在跑 → 返回 warning（不阻塞，agent 应先 kill 或 wait_all）

### 改动文件

| 文件                                                  | 改动                                       |
| ----------------------------------------------------- | ------------------------------------------ |
| `agent/tools/taskflow/tools/taskflow_update_steps.py` | 新建 — 工具实现                            |
| `agent/tools/taskflow/tools/__init__.py`              | 导出 `taskflow_update_steps`               |
| `agent/tools/taskflow/config.py`                      | 如需新常量                                 |
| `agent/tools/taskflow/tools/_shared.py`               | 可能加校验辅助函数 `validate_steps_list()` |
| `agent/tools/taskflow/registry/store_sqlite.py`       | 无改动（`update_flow` 已支持替换 state）   |

### 测试

| 测试                           | 场景                                   |
| ------------------------------ | -------------------------------------- |
| `test_update_steps_replace.py` | 全量替换 steps 列表                    |
| 同上                           | 删除中间 step + 重新排序               |
| 同上                           | 改 task 描述（ready/blocked step）     |
| 同上                           | 改 depends_on（ready step）            |
| 同上                           | dispatched step 不可降级 → 拒绝        |
| 同上                           | done step 不可改 task → 拒绝           |
| 同上                           | 删除 dispatched step → warning         |
| 同上                           | depends_on 引用不存在的 step_id → 拒绝 |
| 同上                           | 终态 flow → 拒绝                       |
