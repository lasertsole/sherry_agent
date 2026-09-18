# TaskFlow Session 隔离方案

> **执行状态（2026-09-19）**：Phase 1–6 已全部落地并通过门禁（`tests/run_tests_split.py` → PASS、`basedpyright agent/ server/` 0 errors、`ruff check`、`lint-imports` → 7 kept / 0 broken）。
>
> **计划断言 vs 实测（偏差逐条记录，以现状为准）**：
> 1. **Phase 3 无需拆分 builders**：`apply_tool_policy` 早已实现 `metadata["scope"] == "main_only"` 机制，且 `build_taskflow_tools` / `build_todolist_tools` / `build_knowledge_tools` **已经**为三个工具族打上该标签——子 agent 本来就拿不到这三类工具（`_build_child_agent` 必经 `apply_tool_policy` 无条件剔除）。按任务要求优先复用既有标签机制，**未**新建 `build_subagent_tools`（避免两套白名单）；改为补两组工具列表断言测试（真实工具集 + `_build_child_agent` 边界）。
> 2. **TodoList 无需加列**：`todos` 表早已以 `session_id` 作为主键前缀，全部 store 函数按会话过滤（`get_todos(session_id)` / `replace_all(session_id, …)`），未重复造。
> 3. **Knowledge 未加列**：按计划采用 `plan_ref → 计划名` 间接隔离——`knowledge` 工具以计划名为键，`build_knowledge_block(session_id)` 先解析该会话自己的 `plan_ref`；已在四语文档写明该语义与子 agent 绝对边界（`knowledge` 为 `main_only`），未加会话列。
> 4. **Phase 4 额外改了 2 个消费者**（计划未列出）：`workspace/prompt_builder.py::_build_taskflow_block` 与 `context_engine/session_continuity.py::_get_active_taskflow_ids_sync` 也调用 `provider.get_active_flows()`，同步改为传 `session_id` 并删除 Python 层 creator 过滤。
> 5. **store 签名为必填**：`create_flow(..., session_id=)`（空值 `ValueError`）、`get_flow(flow_id, session_id)`、`get_flow_sync(flow_id, session_id)`、`update_flow(..., session_id=)` 均无默认值；`taskflow_create` 在缺会话时返回 `Error: session_id is required`（原 `test_create_without_session_id` 的旧断言按新契约改写为"拒绝且不落库"）。
> 6. **索引取复合**：实现为 `idx_taskflow_session_status(session_id, status)`（计划写的是单列 `idx_taskflows_session`），按 `session_id + status IN (…)` 查询形态取复合索引；`idx_taskflow_status` 保留给 sweeper 的无会话状态扫描。
> 7. **`clear_session` 语义选择**：照 `server/DAO/messages.py` 的 "purge every trace" 语义，清会话时**同时删除**该会话的 todo 行与 task_flow 行（best-effort：失败只告警、不阻塞其余清理；`session_id=''` 的隔离前行永不被匹配）。计划未提，此处定案并写入四语文档。
> 8. **Phase 7 未执行**：本次任务范围明确为 6 个 Phase（会话隔离 + 子 agent 边界）；Phase 7（TaskFlow DAG 动态改动 / `taskflow_update_steps`）是独立特性，保持原样未动。
> 9. **计划中的单进程组合门禁命令不具备可行性**：`pytest tests/agent/tools/taskflow tests/agent/tools/todolist tests/agent/middlewares tests/agent/tools/subagent tests/server -q -k "not llm_e2e"` 在**基线 commit `336cfce`（未含本次任何改动）上同样失败**——40 failed + 1 error，失败集合与改动后逐条一致（已实测对比）。这是仓库已知的 `tests/agent/tools/subagent/conftest.py` `sys.modules` stub 污染（README §Testing 明确说明该场景必须分进程）。权威门禁为 `tests/run_tests_split.py`（3 进程分组），已通过。

---

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

> ✅ 已完成：`task_flows` 增量加 `session_id TEXT NOT NULL DEFAULT ''`（`_SESSION_ID_COLUMN_DDL`，旧库自动补列）+ 复合索引；`create_flow` / `get_flow` / `update_flow` / `get_flow_sync` / `get_active_flows_sync` / `get_all_flows_sync` 全部按会话过滤，`update_flow` 的冲突探测也只在本会话内查 revision（跨会话 flow 视同不存在）；`get_overdue_flows` / `get_waiting_flows` 保留跨会话（sweeper 系统扫描）；新增 `delete_flows_by_session`。

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

> ✅ 已完成：`taskflow_summary` / `taskflow_list` / `taskflow_budget` / `taskflow_cancel` / `taskflow_fail` / `taskflow_finish` / `taskflow_progress` / `taskflow_set_waiting` 补齐 `SessionId` 注入；`taskflow_create` / `dispatch` / `resume` / `run_task` / `wait_all` 透传 `session_id` 到 store 与冲突重试助手；`taskflow_list` docstring 由 cross-session board 改为 current session only。

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

> ✅ 由现状满足（标签机制，未拆 builders）：核实确认三个构建器均已打 `scope=main_only`，`apply_tool_policy` 无条件剔除；新增 `test_subagent_policy_drops_main_only_planning_families`（真实工具集）与 `test_child_agent_drops_main_only_planning_tools`（`_build_child_agent` 边界）锁定。

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

> ✅ 已完成：`_get_taskflow_context_sync` 改为 SQL 过滤（删除 Python creator 过滤）；`PromptDataProvider.get_active_flows(session_id)` 协议 + `AgentPromptDataProvider` 实现同步；`workspace/prompt_builder.py` 与 `context_engine/session_continuity.py` 两个消费者同步（计划外补充）；`clear_session` 增补 todos/taskflows 清理。

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

> ✅ 已完成：计划所指文件已迁移为 Pinia store——改动落在 `client/app/stores/todo.ts` 的 `Todo` 类型（`session_id?: string`）；`pnpm test:unit`（356 passed）/ `typecheck` / `dpdm` 全绿。

前端不直接查 taskflow API — 数据通过 session-scoped WS 推送获得。改动最小：

| 文件                                      | 改动                                                         |
| ----------------------------------------- | ------------------------------------------------------------ |
| `client/app/composables/use-todo-list.ts` | `Todo` 类型加 `session_id?: string` 字段（defense-in-depth） |
| `client/app/components/chat/TodoDock.vue` | 无改动                                                       |
| `client/app/components/chat/TodoItem.vue` | 无改动                                                       |

实际隔离在后端 SQL 层强制，前端只是防御性标注。

---

## Phase 6: 测试

> ✅ 已完成：既有 store/工具/E2E/中间件/前端测试按新签名更新（无弱化）；新增跨会话读取拒绝、跨会话变更拒绝、列表隔离、重复 id 不泄露 revision、子 agent 工具列表断言、`clear_session` 规划存储清理、E4 屏障按会话读取、Knowledge 边界等测试。

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

> ⏸ 本次未执行（任务范围明确为 6 个 Phase；该特性独立于会话隔离）。

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
