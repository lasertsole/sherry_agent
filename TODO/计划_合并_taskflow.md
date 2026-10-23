# 计划：taskflow 工具合并（16→1，action 模式）

- 建档日期：2026-10-23
- 状态：**计划（未开工）**
- **文档范围**：把 16 个 `taskflow_*` 工具合并为 `taskflow(action=create/plan/resume/finish/fail/cancel/dispatch/replan/set_waiting/update_steps/wait_all/run_task/summary/progress/list/budget)`。中风险——4 个负载点 + 1 个 Python import + SKILL.md 重写 + 47 测试。
- 关联：`调研_工具合并影响面分析.md` §4、`计划_工具合并推荐方案.md` §1.3

---

## 1. 当前状态

### 1.1 十六个工具

| # | 工具名 | 主要参数 | 读/写 |
|---|---|---|---|
| 1 | `taskflow_create` | `flow_id`, `description`, `initial_state`, `deadline_hours`, `resumed_from`, `todo_content`, `todo_category` | 写 |
| 2 | `taskflow_plan` | `goal`, `flow_id`, `context`, `max_steps` | 写 |
| 3 | `taskflow_resume` | `flow_id`, `child_session_key`, `result`, `expected_revision`, `token_usage`, `validation_criteria`, `structured_result`, `step_outcome`, `auto_dispatch` | 写 |
| 4 | `taskflow_finish` | `flow_id`, `summary`, `expected_revision` | 写（终态） |
| 5 | `taskflow_fail` | `flow_id`, `reason`, `expected_revision` | 写（终态） |
| 6 | `taskflow_cancel` | `flow_id`, `reason`, `expected_revision` | 写（终态） |
| 7 | `taskflow_dispatch` | `flow_id`, `step_ids: list[str]`, `expected_revision` | 写 |
| 8 | `taskflow_replan` | `flow_id`, `reason`, `blocked_step_id`, `keep_done` | 写 |
| 9 | `taskflow_set_waiting` | `flow_id`, `wait_reason`, `expected_revision` | 写 |
| 10 | `taskflow_update_steps` | `flow_id`, `steps: list[dict]`, `expected_revision` | 写 |
| 11 | `taskflow_wait_all` | `flow_id`, `timeout_seconds`, `poll_interval_seconds` | 写（等待） |
| 12 | `taskflow_run_task` | `flow_id`, `task`, `label`, `expected_revision`, `depends_on`, `validation_criteria`, `retry_policy`, `aggregate_deps`, `response_schema`, `expected_params`, `input_bindings`, `judge_criteria`, `judge_model`, `functional_role`, `step_model`, `step_timeout_seconds`, `priority` | 写（17 参数——最大 schema） |
| 13 | `taskflow_summary` | `flow_id` | 读 |
| 14 | `taskflow_progress` | `flow_id` | 读 |
| 15 | `taskflow_list` | `status_filter` | 读（无 flow_id） |
| 16 | `taskflow_budget` | `flow_id`, `action="query"\|"set"`, `token_budget`, `expected_revision` | 读/写 |

全部 `@tool` 装饰器函数，`session_id` 通过 `InjectedState` 注入。metadata 在 `build_taskflow_tools()` 里统一打标。

### 1.2 引用点（全量）

| 文件 | 行 | 引用内容 | 合并后 | 影响 |
|---|---|---|---|---|
| `agent/tools/catalog.py` | 48-63 | `TOOL_GROUPS["tasks"]` 列 16 个 taskflow 名 | 改为 `("taskflow",)` | 注册 |
| `agent/tools/parallel_safety.py` | 53-55 | `READ_ONLY_TOOLS` 含 `taskflow_summary`/`progress`/`list` | 移除 3 个名 | parallel_safety |
| `agent/tools/taskflow/tools/__init__.py` | 35-61 | `_TASKFLOW_TOOLS` 16 个 + `_READ_ONLY_TASKFLOW` + `build_taskflow_tools` | 改为返回 1 个 merged tool | 注册 |
| `agent/tools/taskflow/__init__.py` | 9-37 | re-export 11 个工具 | 改为 re-export merged tool | 导出 |
| `pub/func/message/tool_output_prune.py` | 59-61 | dict 键 `taskflow_summary`/`run_task`/`resume` | 键改为 `"taskflow"` | 输出裁剪 |
| **`agent/tools/subagent/announce/delivery.py`** | **408, 417** | `from ...taskflow_resume import taskflow_resume; await taskflow_resume.coroutine(...)` — **直接 import Python 对象** | 保留 `taskflow_resume` 模块函数或改为调 merged tool | 🔴 **关键依赖** |
| `agent/tools/__init__.py` | 31, 69 | `build_taskflow_tools` 导入 + 注册 | 不变（函数名不变） | ✅ |
| `agent/tools/catalog.py` | 147 | `BULK_ONLY_GROUPS={"tasks","subagents"}` | 可能移除 `"tasks"`（3 个工具不需要 bulk-only） | UI |

### 1.3 模型面文本（需更新）

| 文件 | 范围 | 内容 |
|---|---|---|
| **`skills/builtin/core/taskflow/SKILL.md`** | 行 27-156 + 多处 | 全部 16 个工具的签名 + 用法——**最大文档重写** |
| `AGENTS.md` | 23 处 | `taskflow_resume`×6, `taskflow_plan`×5 等 |
| `README.md` + 四语言 | ~行 78 | `taskflow_create(resumed_from=…)` |
| `workspace/prompt_builder.py` | 324 | `"Use taskflow_summary to inspect a flow"` |
| `agent/middlewares/todo_continuation/core.py` | 多处 | 提到 `taskflow_finish`/`cancel`/`wait_all`/`resume`/`run_task`/`dispatch` |
| `agent/middlewares/task_intent/core.py` | 233-236 | 提到 `taskflow_run_task`/`dispatch`/`wait_all`/`resume` |
| `agent/tools/todolist/service.py` | 162-163, 168-169 | 错误消息提到 `taskflow_wait_all`/`taskflow_resume` |

### 1.4 测试

47+ 个测试文件引用 taskflow 工具名——大部分通过 `tools["taskflow_XXX"].coroutine(...)` 字典键调用。

---

## 2. 合并方案

### 2.1 合并后的工具

```python
# agent/tools/taskflow/tools/taskflow_merged.py

class TaskflowActionSchema(BaseModel):
    action: Literal[
        "create", "plan", "resume", "finish", "fail", "cancel",
        "dispatch", "replan", "set_waiting", "update_steps",
        "wait_all", "run_task", "summary", "progress", "list", "budget"
    ] = Field(description="The taskflow action to perform.")

    # 所有可选参数铺平（全部 optional，按 action 填需要的）
    flow_id: str | None = None
    goal: str | None = None                    # plan
    description: str | None = None             # create
    initial_state: dict | None = None          # create
    deadline_hours: float | None = None        # create
    resumed_from: str | None = None            # create
    todo_content: str | None = None            # create
    todo_category: str = "quick"               # create
    context: dict | None = None               # plan
    max_steps: int = 8                         # plan
    child_session_key: str | None = None       # resume
    result: str | None = None                  # resume
    token_usage: int | None = None             # resume
    validation_criteria: str | None = None     # resume / run_task
    structured_result: dict | None = None      # resume
    step_outcome: str | None = None            # resume
    auto_dispatch: bool = True                 # resume
    summary: str | None = None                # finish
    reason: str | None = None                 # fail / cancel / replan
    wait_reason: str | None = None            # set_waiting
    steps: list[dict] | None = None           # update_steps
    step_ids: list[str] | None = None        # dispatch
    blocked_step_id: str | None = None        # replan
    keep_done: bool = True                    # replan
    timeout_seconds: float = 300.0            # wait_all
    poll_interval_seconds: float = 2.0        # wait_all
    task: str | None = None                   # run_task
    label: str | None = None                 # run_task
    depends_on: list[str] | None = None      # run_task
    retry_policy: dict | None = None         # run_task
    aggregate_deps: bool = False             # run_task
    response_schema: dict | None = None      # run_task
    expected_params: dict | None = None      # run_task
    input_bindings: dict | None = None       # run_task
    judge_criteria: str | None = None        # run_task
    judge_model: str | None = None           # run_task
    functional_role: str | None = None        # run_task
    step_model: str | None = None            # run_task
    step_timeout_seconds: int | None = None  # run_task
    priority: int | None = None              # run_task
    expected_revision: int | None = None      # 大多数写操作
    status_filter: str = "active"            # list
    budget_action: str = "query"             # budget（避免与顶层 action 冲突）
    token_budget: int | None = None          # budget
```

### 2.2 Handler dict（同 `skill_manage` 模式）

```python
# 保留 16 个原模块函数本体不改——handler 只做 dispatch
_TASKFLOW_ACTION_HANDLERS: dict[str, Callable] = {
    "create": _handle_create,
    "plan": _handle_plan,
    "resume": _handle_resume,
    "finish": _handle_finish,
    "fail": _handle_fail,
    "cancel": _handle_cancel,
    "dispatch": _handle_dispatch,
    "replan": _handle_replan,
    "set_waiting": _handle_set_waiting,
    "update_steps": _handle_update_steps,
    "wait_all": _handle_wait_all,
    "run_task": _handle_run_task,
    "summary": _handle_summary,
    "progress": _handle_progress,
    "list": _handle_list,
    "budget": _handle_budget,
}

async def _run_taskflow(action, session_id="", **kwargs):
    """Merged tool 的入口——先校验再 dispatch。"""
    # 1. action-aware 校验
    err = _validate_taskflow_action(action, kwargs)
    if err:
        return err
    # 2. dispatch 到 handler
    handler = _TASKFLOW_ACTION_HANDLERS.get(action)
    if handler is None:
        return _param_error(action, unknown=True)
    return await handler(session_id=session_id, **kwargs)
```

### 2.2a action-aware 校验层

合并后所有参数都是 optional——Pydantic 不拦"缺必填参数"。dispatch 前加**校验表 + 参数规则反馈**，让模型看到错误后知道该 action 的完整调用规则：

```python
_TASKFLOW_ACTION_RULES: dict[str, dict] = {
    "create": {
        "required": {"flow_id": "str — flow identifier"},
        "optional": {
            "description": "str — what the flow accomplishes",
            "initial_state": "dict — pre-built state (rare)",
            "deadline_hours": "float — time budget",
            "resumed_from": "str — resume a previously failed flow",
            "todo_content": "str — GC-root todo text",
            "todo_category": "str — 'quick' or 'complex' (default 'quick')",
            "expected_revision": "int — optimistic lock",
        },
    },
    "plan": {
        "required": {"goal": "str — the objective to decompose"},
        "optional": {
            "flow_id": "str — explicit ID (auto-derived if omitted)",
            "context": "dict — planning context",
            "max_steps": "int — max steps (default 8)",
            "expected_revision": "int — optimistic lock",
        },
    },
    "resume": {
        "required": {
            "flow_id": "str — flow to inject the result into",
            "child_session_key": "str — completed child session key",
            "result": "str — the child's result text",
        },
        "optional": {
            "expected_revision": "int — optimistic lock",
            "token_usage": "int — child token consumption",
            "validation_criteria": "str — pass/fail criteria",
            "structured_result": "dict — structured output",
            "step_outcome": "str — override step outcome",
            "auto_dispatch": "bool — auto-dispatch next ready steps (default true)",
        },
    },
    "finish": {
        "required": {"flow_id": "str — flow to mark done"},
        "optional": {
            "summary": "str — completion summary",
            "expected_revision": "int — optimistic lock",
        },
    },
    "fail": {
        "required": {"flow_id": "str — flow to mark failed"},
        "optional": {
            "reason": "str — why the flow failed",
            "expected_revision": "int — optimistic lock",
        },
    },
    "cancel": {
        "required": {"flow_id": "str — flow to cancel"},
        "optional": {
            "reason": "str — why cancelled",
            "expected_revision": "int — optimistic lock",
        },
    },
    "dispatch": {
        "required": {
            "flow_id": "str — flow to dispatch steps on",
            "step_ids": "list[str] — step IDs to dispatch",
        },
        "optional": {"expected_revision": "int — optimistic lock"},
    },
    "replan": {
        "required": {"flow_id": "str — flow to re-plan"},
        "optional": {
            "reason": "str — why re-planning",
            "blocked_step_id": "str — the step that blocked",
            "keep_done": "bool — keep completed steps (default true)",
        },
    },
    "set_waiting": {
        "required": {"flow_id": "str — flow to park"},
        "optional": {
            "wait_reason": "str — what it's waiting for",
            "expected_revision": "int — optimistic lock",
        },
    },
    "update_steps": {
        "required": {
            "flow_id": "str — flow to update",
            "steps": "list[dict] — full replacement step list",
        },
        "optional": {"expected_revision": "int — optimistic lock"},
    },
    "wait_all": {
        "required": {"flow_id": "str — flow to wait on"},
        "optional": {
            "timeout_seconds": "float — max wait (default 300)",
            "poll_interval_seconds": "float — poll interval (default 2)",
        },
    },
    "run_task": {
        "required": {
            "flow_id": "str — flow to register the step on",
            "task": "str — task description for the child subagent",
        },
        "optional": {
            "label": "str — short step label",
            "depends_on": "list[str] — prerequisite step IDs",
            "expected_revision": "int — optimistic lock",
            "validation_criteria": "str — pass/fail criteria for the judge",
            "retry_policy": "dict — retry configuration",
            "aggregate_deps": "bool — aggregate dependency results (default false)",
            "response_schema": "dict — JSON schema for structured output",
            "expected_params": "dict — expected parameters",
            "input_bindings": "dict — input mapping",
            "judge_criteria": "str — criteria for step judge",
            "judge_model": "str — model for step judge",
            "functional_role": "str — 'researcher'/'executor'/'reviewer'/'librarian'",
            "step_model": "str — model override for this step",
            "step_timeout_seconds": "int — step timeout",
            "priority": "int — step priority",
        },
    },
    "summary": {
        "required": {"flow_id": "str — flow to read"},
        "optional": {},
    },
    "progress": {
        "required": {"flow_id": "str — flow to report"},
        "optional": {},
    },
    "list": {
        "required": {},
        "optional": {"status_filter": "str — 'active'/'completed'/'all' (default 'active')"},
    },
    "budget": {
        "required": {"flow_id": "str — flow to set/query budget"},
        "optional": {
            "budget_action": "str — 'query' or 'set' (default 'query')",
            "token_budget": "int — budget in tokens (for 'set')",
            "expected_revision": "int — optimistic lock",
        },
    },
}

def _validate_taskflow_action(action: str, kwargs: dict) -> str | None:
    """校验必填参数，缺失时返回结构化错误 + 该 action 的完整参数规则。"""
    rules = _TASKFLOW_ACTION_RULES.get(action)
    if rules is None:
        return (
            f"Unknown action '{action}'. "
            f"Available: {', '.join(sorted(_TASKFLOW_ACTION_RULES.keys()))}"
        )
    required = rules["required"]
    missing = [p for p in required if not kwargs.get(p)]
    if not missing:
        return None  # 校验通过
    # 构建错误反馈——包含完整参数规则让模型自我纠正
    parts = [f"Error: action='{action}' has incorrect parameters."]
    parts.append(f"Missing required: {', '.join(missing)}")
    if required:
        parts.append("Required: " + ", ".join(f"{k} ({v})" for k, v in required.items()))
    if rules["optional"]:
        opts = rules["optional"]
        parts.append("Optional: " + ", ".join(f"{k} ({v})" for k, v in opts.items()))
    # 示例调用
    req_names = [p for p in missing if p in required] or list(required)
    example_params = ", ".join(f'{p}=<{required[p].split(" — ")[0]}>' for p in req_names)
    parts.append(f'Example: taskflow(action="{action}", {example_params})')
    return " ".join(parts)
```

**模型看到的错误反馈示例**：

```
模型调用: taskflow(action="run_task", flow_id="flow-1")  # 缺 task

返回: Error: action='run_task' has incorrect parameters.
      Missing required: task
      Required: flow_id (str — flow to register the step on),
                task (str — task description for the child subagent)
      Optional: label (str — short step label),
                depends_on (list[str] — prerequisite step IDs),
                expected_revision (int — optimistic lock),
                validation_criteria (str — pass/fail criteria for the judge),
                retry_policy (dict — retry configuration),
                aggregate_deps (bool — aggregate dependency results (default false)),
                response_schema (dict — JSON schema for structured output),
                expected_params (dict — expected parameters),
                input_bindings (dict — input mapping),
                judge_criteria (str — criteria for step judge),
                judge_model (str — model for step judge),
                functional_role (str — 'researcher'/'executor'/'reviewer'/'librarian'),
                step_model (str — model override for this step),
                step_timeout_seconds (int — step timeout),
                priority (int — step priority)
      Example: taskflow(action="run_task", flow_id=<str>, task=<str>)

模型下一轮: taskflow(action="run_task", flow_id="flow-1", task="Write migration script")  ✅
```

```
模型调用: taskflow(action="create")  # 缺 flow_id

返回: Error: action='create' has incorrect parameters.
      Missing required: flow_id
      Required: flow_id (str — flow identifier)
      Optional: description (str — what the flow accomplishes), ...
      Example: taskflow(action="create", flow_id=<str>)

模型下一轮: taskflow(action="create", flow_id="flow-1", description="Migrate user table")  ✅
```

```
模型调用: taskflow(action="plan")  # 缺 goal

返回: Error: action='plan' has incorrect parameters.
      Missing required: goal
      Required: goal (str — the objective to decompose)
      Optional: flow_id (str — explicit ID (auto-derived if omitted)),
                context (dict — planning context), max_steps (int — max steps (default 8))
      Example: taskflow(action="plan", goal=<str>)

模型下一轮: taskflow(action="plan", goal="Add user authentication with JWT")  ✅
```

```
模型调用: taskflow(action="start")  # 未知 action

返回: Unknown action 'start'.
      Available: budget, cancel, create, dispatch, fail, finish, list, plan,
                 progress, replan, resume, run_task, set_waiting, summary,
                 update_steps, wait_all

模型下一轮: taskflow(action="create", ...)  ✅
```

### 2.3 delivery.py 的 Python import 处理

`agent/tools/subagent/announce/delivery.py:408/417` 直接 import `taskflow_resume` 模块函数并调 `.coroutine()`。

**方案**：保留 `taskflow/tools/taskflow_resume.py` 模块文件不删——模块函数 `taskflow_resume` 仍是可调用的 `@tool` 对象。delivery.py 的 import 不变。merged tool 的 `resume` handler 也调这个模块函数。

```python
# delivery.py 不变——继续直接 import + .coroutine()
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume
await taskflow_resume.coroutine(flow_id=..., child_session_key=..., result=..., session_id=...)
```

### 2.4 metadata

```python
# merged tool 整体 metadata
metadata = {
    "scope": "main_only",
    "idempotent": False,         # 含写 action
    "side_effect_scope": "session",
    "concurrent_safe": False,    # 含写 action，是 batch barrier
}
```

原来 3 个读工具（summary/progress/list）是 `idempotent=True`/`side_effect_scope="none"`——合并后整体标 unsafe。读 action 也变成 barrier，轻微效率损失。

### 2.5 BULK_ONLY_GROUPS

`BULK_ONLY_GROUPS = frozenset({"tasks", "subagents"})`——`tasks` 组从 19 个工具变为 3 个（taskflow + todo + knowledge，假设 todo 也在但 todo 不合并）。可以移除 `"tasks"` 改为逐个开关。

### 2.6 改动文件清单

| 文件 | 改动 |
|---|---|
| `agent/tools/taskflow/tools/taskflow_merged.py` | **新建**——merged tool + schema + handler dict |
| `agent/tools/taskflow/tools/__init__.py` | `build_taskflow_tools()` 返回 `[merged_tool]`；`_TASKFLOW_TOOLS`/`_READ_ONLY_TASKFLOW` 清理 |
| `agent/tools/taskflow/__init__.py` | re-export 改为 merged tool |
| `agent/tools/catalog.py` | `TOOL_GROUPS["tasks"]` 16 个名 → `("taskflow",)`；`BULK_ONLY_GROUPS` 移除 `"tasks"` |
| `agent/tools/parallel_safety.py` | `READ_ONLY_TOOLS` 移除 3 个 taskflow 读名 |
| `pub/func/message/tool_output_prune.py` | dict 键 `taskflow_summary`/`run_task`/`resume` → `"taskflow"` |
| `skills/builtin/core/taskflow/SKILL.md` | **全文重写**——16 个工具签名 → 1 个 action 工具 |
| `AGENTS.md` | 23 处更新 |
| `README.md` + 四语言 | `taskflow_create(resumed_from=…)` → `taskflow(action=create, resumed_from=…)` |
| `workspace/prompt_builder.py:324` | `"Use taskflow_summary"` → `"Use taskflow(action=summary)"` |
| `agent/middlewares/todo_continuation/core.py` | 多处 prompt 文本更新 |
| `agent/middlewares/task_intent/core.py:233-236` | prompt 文本更新 |
| `agent/tools/todolist/service.py:162-163,168-169` | 错误消息更新 |
| 原 16 个 `taskflow_*.py` 模块文件 | **保留**——作为 handler 实现，不删除 |
| `agent/tools/subagent/announce/delivery.py` | **不变**——继续 import `taskflow_resume` 模块函数 |

---

## 3. 测试集（验证功能无影响）

### 3.1 现有测试（策略：模块函数本体不改 → 集成测试不变）

**关键设计决策**：保留 16 个模块函数（`taskflow_create`/`taskflow_plan`/...）本体不改。现有测试通过 `tools["taskflow_create"].coroutine(...)` 调用——合并后 `tools` 字典里没有 `"taskflow_create"` 键了，但有 `"taskflow"` 键。

| 测试策略 | 改动量 | 验证 |
|---|---|---|
| **策略 A：改调用方式**——所有 `tools["taskflow_create"].coroutine(flow_id=X, ...)` → `tools["taskflow"].coroutine(action="create", flow_id=X, ...)` | 47+ 文件，每处改 1-2 行 | 验证 merged tool 的 action 路由正确 |
| **策略 B：直接调模块函数**——现有测试改为 `from ...taskflow_create import taskflow_create; await taskflow_create.coroutine(...)` | 47+ 文件，改 import 路径 | 验证模块函数本体不变（但绕过 merged tool） |

**推荐策略 A**——改调用方式，直接验证 merged tool 的 dispatch 正确性。

### 3.2 现有测试清单（需改调用方式的）

| 测试文件 | 调用的工具 | 改动 |
|---|---|---|
| `tests/agent/tools/taskflow/test_taskflow_tools.py` | 全部 16 个 | `tools["taskflow_XXX"]` → `tools["taskflow"]` + `action=XXX` |
| `tests/agent/tools/taskflow/test_dag_e2e.py` | create, run_task, dispatch, resume, finish | 同上 |
| `tests/agent/tools/taskflow/test_resume_dag.py` | resume | 同上 |
| `tests/agent/tools/taskflow/test_update_steps.py` | update_steps | 同上 |
| `tests/agent/tools/taskflow/test_taskflow_list.py` | list | 同上 |
| `tests/agent/tools/taskflow/test_taskflow_progress.py` | progress | 同上 |
| `tests/agent/tools/taskflow/test_taskflow_dispatch.py` | dispatch | 同上 |
| `tests/agent/tools/taskflow/test_taskflow_wait_all.py` | wait_all | 同上 |
| `tests/agent/tools/taskflow/test_token_budget.py` | budget | 同上 |
| `tests/agent/tools/taskflow/test_plan_replan_loop.py` | plan, replan, fail | 同上 |
| `tests/agent/tools/taskflow/test_conflict_persistence.py` | create, resume, finish | 同上 |
| `tests/agent/tools/taskflow/conftest.py` | 基础设施 | 工具列表改为 merged |
| `tests/agent/tools/taskflow/test_auto_resume_binding.py` | resume | 同上 |
| `tests/agent/tools/taskflow/test_chain_smoke.py` | create, run_task, dispatch, resume, finish | 同上 |
| `tests/agent/tools/taskflow/test_concurrent_dispatch.py` | dispatch | 同上 |
| `tests/agent/tools/taskflow/test_concurrent_resume.py` | resume | 同上 |
| `tests/agent/tools/taskflow/test_deadline.py` | create | 同上 |
| `tests/agent/tools/taskflow/test_finish_gate.py` | finish | 同上 |
| `tests/agent/tools/taskflow/test_gate_retry_e2e.py` | resume, run_task | 同上 |
| `tests/agent/tools/taskflow/test_idle_detection.py` | summary（断言文本含 `"taskflow_resume"`） | 改断言文本 |
| `tests/agent/tools/taskflow/test_lineage.py` | create, resume | 同上 |
| `tests/agent/tools/taskflow/test_no_double_dispatch.py` | dispatch | 同上 |
| `tests/agent/tools/taskflow/test_parallel_dispatch_failure.py` | dispatch, resume | 同上 |
| `tests/agent/tools/taskflow/test_role_synthesize_smoke.py` | create, run_task | 同上 |
| `tests/agent/tools/taskflow/test_run_task_dag.py` | run_task | 同上 |
| `tests/agent/tools/taskflow/test_schema_retry_feedback.py` | resume | 同上 |
| `tests/agent/tools/taskflow/test_summary_dag.py` | summary | 同上 |
| `tests/agent/tools/taskflow/test_synthesize.py` | create, dispatch | 同上 |
| `tests/agent/tools/taskflow/test_synthesize_e2e.py` | 多个 | 同上 |
| `tests/agent/tools/taskflow/test_tri_color_gc.py` | create, finish, fail | 同上 |
| `tests/agent/tools/taskflow/test_validation.py` | resume | 同上 |
| `tests/agent/tools/taskflow/test_expectation_closure.py` | resume, finish | 同上 |
| `tests/agent/tools/subagent/test_auto_resume.py` | resume（import） | 不变（直接 import 模块函数） |
| `tests/agent/middlewares/test_plan_continuation.py` | 断言文本含 `taskflow_wait_all`/`finish` | 改断言文本 |
| `tests/agent/middlewares/test_summarization_comprehensive.py` | 断言文本 | 改 |
| `tests/agent/tools/subagent/test_max_tokens_boost_wiring.py:107` | `_StubTool("taskflow_create")` | 改为 `_StubTool("taskflow")` |
| `tests/agent/tools/todolist/test_tools_wiring.py` | 列 10 个 taskflow 名 | 改为 `["taskflow"]` |
| `tests/agent/tools/todolist/test_todolist_e2e.py` | create | 同上 |
| `tests/full/test_session_isolation_e2e.py` | create, run_task, dispatch, resume | 同上 |
| `tests/full/test_taskflow_closure_e2e.py` | create, resume | 同上 |
| `tests/full/test_taskflow_update_steps_e2e.py` | update_steps | 同上 |
| `tests/perf/test_perf_hot_paths.py:46` | `"name": "taskflow_run_task"` fixture | 改为 `"taskflow"` |
| `tests/workspace/test_prompt_builder_taskflow.py:147` | 断言 `"Use taskflow_summary"` | 改断言 |
| `tests/agent/tools/test_parallel_safety.py` | taskflow 名在 `READ_ONLY_TOOLS` | 移除旧名，加 `"taskflow"` 或不加（unsafe） |

### 3.3 新增测试

| 测试 | 验证内容 |
|---|---|
| `test_taskflow_action_create` | `action="create"` → flow 创建，返回 revision；与原 `taskflow_create` 结果相同 |
| `test_taskflow_action_plan` | `action="plan", goal=X` → 分解出 step DAG |
| `test_taskflow_action_resume` | `action="resume"` → 子代理结果注入 |
| `test_taskflow_action_finish` | `action="finish"` → 终态 + Gate A-C |
| `test_taskflow_action_fail` | `action="fail"` → 终态 |
| `test_taskflow_action_cancel` | `action="cancel"` → 终态 |
| `test_taskflow_action_dispatch` | `action="dispatch", step_ids=[...]` → 批量派发 |
| `test_taskflow_action_replan` | `action="replan"` → 重新规划 |
| `test_taskflow_action_set_waiting` | `action="set_waiting"` → WAITING 状态 |
| `test_taskflow_action_update_steps` | `action="update_steps", steps=[...]` → 全量替换 |
| `test_taskflow_action_wait_all` | `action="wait_all"` → 等待子代理完成 |
| `test_taskflow_action_run_task` | `action="run_task", task=X` → 注册 + 派发 step |
| `test_taskflow_action_summary` | `action="summary"` → 返回 flow 状态 |
| `test_taskflow_action_progress` | `action="progress"` → 返回进度 |
| `test_taskflow_action_list` | `action="list"` → 返回 session 的 flow 列表 |
| `test_taskflow_action_budget` | `action="budget", budget_action="set", token_budget=X` → 设置预算 |
| `test_taskflow_action_unknown` | `action="invalid"` → 返回 `"Unknown action"` |
| `test_taskflow_handler_keys_match_schema` | `set(_TASKFLOW_ACTION_HANDLERS.keys()) == set(get_args(Literal[...]))` — pin handler dict 与 schema 一致（复制 `test_skill_manage_dispatch.py` 模式） |
| `test_taskflow_metadata` | `metadata["scope"]=="main_only"` + `metadata["idempotent"]==False` |
| `test_taskflow_in_catalog` | catalog 返回 `taskflow`，组 `tasks`，`required=False` |
| `test_taskflow_old_names_absent` | 16 个旧名不在 catalog / `build_main_tools()` / `READ_ONLY_TOOLS` 中 |
| `test_taskflow_delivery_py_still_works` | `delivery.py` 的 `import taskflow_resume` + `.coroutine()` 仍可调用（模块函数保留） |
| `test_taskflow_tool_output_prune` | `tool_output_prune` 用 `"taskflow"` 键格式化输出（不再用旧名） |

---

## 4. 评判标准（功能无影响）

### 4.1 功能行为不变

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S1：flow 创建结果不变** | 创建 flow → `action="create"` | 返回的 `flow_id` + `revision` 与原 `taskflow_create` 相同 |
| **S2：plan 分解结果不变** | `action="plan", goal=X` | 分解出的 step DAG（step_id/task/depends_on）与原 `taskflow_plan` 相同 |
| **S3：resume 结果不变** | `action="resume"` + 完整参数 | step 状态推进 + evidence 记录 + auto_dispatch 行为与原 `taskflow_resume` 相同 |
| **S4：finish gate 不变** | `action="finish"` | Gate A（DAG 完整性）+ Gate B（无未解决 step）+ Gate C（evidence 无 FAIL）+ Gate E（goal judge）与原 `taskflow_finish` 相同 |
| **S5：dispatch 结果不变** | `action="dispatch", step_ids=[...]` | step 状态从 ready → dispatched，子代理派发行为与原 `taskflow_dispatch` 相同 |
| **S6：run_task 结果不变** | `action="run_task"` + 17 参数 | step 注册 + 派发 + response_schema + judge_criteria + functional_role 与原 `taskflow_run_task` 相同 |
| **S7：summary/progress/list 结果不变** | `action="summary/progress/list"` | 返回的 JSON 结构与原工具相同 |
| **S8：budget 结果不变** | `action="budget"` | 预算设置/查询与原 `taskflow_budget` 相同 |
| **S9：expected_revision 乐观锁不变** | 所有写 action 传 `expected_revision` | 冲突时返回 `FlowConflictError` + retry，与原行为相同 |
| **S10：delivery.py auto-resume 不变** | 子代理完成 → `delivery.py` 调 `taskflow_resume.coroutine()` | auto-resume 仍工作（模块函数保留） |

### 4.2 并行安全 + 作用域

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S11：作用域不变** | `metadata["scope"]` | `== "main_only"` |
| **S12：并行安全** | `tool_is_parallel_safe("taskflow", tool)` | `False`（含写 action，是 barrier——与原写工具一致；原 3 个读工具变为 barrier 是轻微效率损失） |

### 4.3 catalog + 注册

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S13：catalog 正确** | `tool_catalog()` | 含 `taskflow`，组 `tasks`，`required=False`；不含 16 个旧名 |
| **S14：READ_ONLY_TOOLS 更新** | `READ_ONLY_TOOLS` | 不含 `taskflow_summary`/`progress`/`list` |
| **S15：BULK_ONLY_GROUPS 更新** | `BULK_ONLY_GROUPS` | 不含 `"tasks"`（或保留——取决于是否需要 bulk-only UI） |
| **S16：agent_config_service 验证** | `PUT /sessions/agent_config` 带 `tools: ["taskflow"]` | 200 OK；带 `tools: ["taskflow_create"]` → 400 |

### 4.4 模型面文本

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S17：SKILL.md 更新** | `skills/builtin/core/taskflow/SKILL.md` | 文档化为 1 个 `taskflow(action=...)` 工具，16 个 action 的签名和用法 |
| **S18：prompt 文本更新** | `prompt_builder.py` + `todo_continuation` + `task_intent` | 所有 `taskflow_XXX` 引用改为 `taskflow(action=XXX)` |
| **S19：AGENTS.md 更新** | 23 处引用 | 全部更新为 `taskflow(action=...)` |

### 4.5 测试

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S20：现有测试全过** | `pytest tests/agent/tools/taskflow/ tests/agent/tools/todolist/ tests/agent/middlewares/test_plan_continuation.py tests/full/ -q -k "not llm_e2e"` | 全 pass（改调用方式后） |
| **S21：新增测试全过** | `pytest tests/agent/tools/taskflow/test_taskflow_merged_dispatch.py -q` | 全 pass |
| **S22：handler keys pin** | `test_taskflow_handler_keys_match_schema` | `set(_TASKFLOW_ACTION_HANDLERS.keys()) == set(Literal args)` |
| **S23：delivery.py 测试** | `pytest tests/agent/tools/subagent/test_auto_resume.py -q` | 全 pass（模块函数保留） |
| **S24：perf 测试** | `pytest tests/perf/test_perf_hot_paths.py -q -s` | 无回归（工具调用路径只多一层 dispatch） |
