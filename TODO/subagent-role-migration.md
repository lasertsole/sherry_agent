# 迁移计划：功能角色分工 + 任务流质量门禁

> 参考来源：deepagents（功能角色分工） + hermes-agent Kanban Swarm（质量门禁）
>
> 目标项目：sherry_agent-main
>
> 创建日期：2026-09-21

---

## 目录

- [现状分析（含当前工具/中间件限制矩阵）](#现状分析)
- [Phase 1：subagent 功能角色分工（参考 deepagents）](#phase-1subagent-功能角色分工参考-deepagents)
- [Phase 2：taskflow DAG 质量门禁（参考 hermes Kanban）](#phase-2taskflow-dag-质量门禁参考-hermes-kanban)
- [测试计划](#测试计划)
- [回滚方案](#回滚方案)

---

## 现状分析

### subagent 现状

```
agent/tools/subagent/
├── types/
│   ├── capability.py          # SubagentSessionRole: MAIN/ORCHESTRATOR/LEAF（仅深度）
│   ├── spawn.py               # SpawnMode / ContextMode
│   └── registry.py            # SubagentRunRecord（无 functional_role 字段）
├── capabilities/core.py       # resolve_subagent_capabilities(depth) → 按深度推导角色
├── config.py                  # SubagentConfig（无角色定义配置）
├── spawn/
│   ├── core.py                # spawn_subagent_direct() → 12 phase pipeline
│   │                           #   _build_child_agent(): ORCHESTRATOR→main_llm, LEAF→auxiliary_llm
│   ├── system_prompt.py       # build_subagent_system_prompt(): 按深度角色生成提示词
│   ├── inherited_tool_policy.py  # apply_tool_policy(): deny/allow + main_only metadata
│   ├── gateway_dispatch.py    # resolve_least_privilege_scopes(): 按深度角色分配 scopes
│   └── plan.py                # resolve_model_and_thinking_plan(): 仅 model_override
└── ...
```

**问题**：所有 LEAF worker 完全同质——相同 LLM（auxiliary_llm）、相同工具集（见下方矩阵）、相同提示词。无 researcher/coder/reviewer 等功能角色。`agent_id` 仅用于 session key 命名，不改变 LLM/工具/提示词。当前限制**仅按深度角色**（MAIN/ORCHESTRATOR/LEAF）区分，所有 LEAF 的工具白名单、LLM 选择、系统提示词完全相同。

### 深度角色工具限制现状（三重机制）

当前代码**仅按深度角色**（MAIN / ORCHESTRATOR / LEAF）限制工具，无功能角色维度。限制通过三重机制叠加生效：

#### 机制 1：`metadata["scope"] == "main_only"` — 不可覆盖，最先丢弃

在 `inherited_tool_policy.py::apply_tool_policy()` 中，任何 `metadata["scope"] == "main_only"` 的工具**在 deny/allow 逻辑之前**被无条件移除。所有子代理（无论 ORCHESTRATOR 还是 LEAF）一律丢失，无法通过 allow 列表重新授予。

| 工具                               | 标记位置                                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------- |
| `memory`                           | `agent/tools/memory.py`                                                       |
| `skill_manage`                     | `agent/tools/skill_tools/skill_manage.py`                                     |
| `sessions_kill` / `sessions_steer` | `agent/tools/subagent/tools/runtime_tools.py`                                 |
| 14 个 taskflow 工具                | `agent/tools/taskflow/tools/__init__.py`（`build_taskflow_tools()` 统一标记） |
| `todowrite` / `todoread`           | `agent/tools/todolist/tools/__init__.py`（`build_todolist_tools()` 统一标记） |
| `knowledge`                        | `agent/tools/todolist/knowledge/__init__.py`                                  |

#### 机制 2：`DEFAULT_SUBAGENT_BLOCKED_TOOLS` — 名称 deny 列表（ORCHESTRATOR 可解锁）

```python
# inherited_tool_policy.py
DEFAULT_SUBAGENT_BLOCKED_TOOLS = ["sessions_spawn", "sessions_yield"]
```

这是回退安全网，仅当调用方未提供 deny 策略（`tool_deny is None`）时生效。在 `spawn/core.py` Phase 8 中：

```python
tool_deny = list(DEFAULT_SUBAGENT_BLOCKED_TOOLS)  # ["sessions_spawn", "sessions_yield"]
if role == SubagentSessionRole.ORCHESTRATOR:
    # ORCHESTRATOR 从 deny 中移除 → 可 spawn + yield
    tool_deny = [t for t in tool_deny if t not in ("sessions_spawn", "sessions_yield")]
# LEAF: 保留 deny → 不可 spawn + yield
```

#### 机制 3：scope-gap deny — 缺权限 scope 的工具被补充拒绝

在 `spawn/core.py::_execute_subagent()` 中，如果某 scope 不在 `run.scopes` 中，对应工具被加入 deny：

```python
scope_tool_map = {
    "subagent:spawn": "sessions_spawn",
    "subagent:kill":  "sessions_kill",
    "subagent:yield": "sessions_yield",
    "subagent:send":  "sessions_send",
}
for scope, tool_name in scope_tool_map.items():
    if scope not in run.scopes and tool_name not in effective_deny:
        effective_deny.append(tool_name)
```

### 各深度角色的 scope 分配

`gateway_dispatch.py::resolve_least_privilege_scopes()` 按角色分配最小权限 scope：

| 角色         | scopes                                                |
| ------------ | ----------------------------------------------------- |
| MAIN         | 不适用（主代理不经 scope 过滤）                       |
| ORCHESTRATOR | `subagent:read` + `spawn` + `kill` + `yield` + `send` |
| LEAF         | `subagent:read` + `yield`                             |

> 注：LEAF 获得 `subagent:yield` scope，但 `sessions_yield` 仍在机制 2 的 deny 列表中，deny 优先级高于 scope 授权。

### 有效工具可用性矩阵（当前代码）

| 工具 / 工具组                                               | MAIN | ORCHESTRATOR      | LEAF | 限制机制                            |
| ----------------------------------------------------------- | ---- | ----------------- | ---- | ----------------------------------- |
| `sessions_spawn`                                            | 有   | 有（机制 2 解锁） | 无   | 机制 2 deny + 机制 3 scope 缺失     |
| `sessions_yield`                                            | 有   | 有（机制 2 解锁） | 无   | 机制 2 deny（scope 有但 deny 优先） |
| `sessions_send`                                             | 有   | 有（scope 有）    | 无   | 机制 3 scope 缺失                   |
| `sessions_kill` / `sessions_steer`                          | 有   | 无                | 无   | 机制 1 main_only                    |
| `memory`                                                    | 有   | 无                | 无   | 机制 1 main_only                    |
| `skill_manage`                                              | 有   | 无                | 无   | 机制 1 main_only                    |
| 14 个 taskflow 工具                                         | 有   | 无                | 无   | 机制 1 main_only                    |
| `todowrite` / `todoread`                                    | 有   | 无                | 无   | 机制 1 main_only                    |
| `knowledge`                                                 | 有   | 无                | 无   | 机制 1 main_only                    |
| `agents_list` / `subagents_list`                            | 有   | 有                | 有   | —                                   |
| `python_repl` / `read_file` / `write_file` / `patch_file`   | 有   | 有                | 有   | —                                   |
| `web_search` / `terminal` / MCP 工具                        | 有   | 有                | 有   | —                                   |
| `skill_list` / `skill_view` / `message_search` / `question` | 有   | 有                | 有   | —                                   |

### LLM 选择（当前代码，`_build_child_agent` 第 821-834 行）

| 角色                        | LLM                                                   |
| --------------------------- | ----------------------------------------------------- |
| MAIN                        | `build_main_llm()`                                    |
| ORCHESTRATOR                | `build_main_llm()`（较大模型）                        |
| LEAF                        | `build_auxiliary_llm()`（较小/较便宜模型）            |
| 任意角色 + `model_override` | `build_llm_by_name(model_override)`，失败回退角色默认 |

### 中间件链对比（当前代码）

Main agent 的 `built_agent()`（`agent/core.py`）装配完整中间件链；子代理的 `_build_child_agent()`（`agent/tools/subagent/spawn/core.py`）装配精简链——**12 个中间件被砍掉**。

| 中间件                                      | MAIN                             | 子代理 (ORCHESTRATOR/LEAF)   | 说明                                           |
| ------------------------------------------- | -------------------------------- | ---------------------------- | ---------------------------------------------- |
| `system_prompt_injection` (@dynamic_prompt) | 有                               | 无                           | 子代理用 `build_subagent_system_prompt()` 替代 |
| `MultimodalProcessor`                       | 有                               | 无                           | 子代理不处理多模态输入                         |
| `IterationBudget`                           | 有 (`main_agent_max_iterations`) | 有 (`worker_max_iterations`) | 迭代上限不同                                   |
| `ToolGuardrails`                            | 有                               | 有                           | —                                              |
| `ContextEvictionMiddleware` (P0-2/P2-4)     | 有                               | **无**                       | 子代理不做上下文逐出                           |
| `ToolCallNormalize`                         | 有                               | 有                           | —                                              |
| `PathGuard`                                 | 有                               | **无**                       | 子代理不做路径安全检查                         |
| `SubagentCompletionDrainMiddleware`         | 有                               | **无**                       | 子代理不排空子代理完成队列                     |
| `TaskIntentMiddleware` (E7)                 | 有                               | **无**                       | 子代理不做任务意图分类                         |
| `OutputRepetitionGuard`                     | 有                               | 有                           | —                                              |
| `MaxTokensBoostMiddleware`                  | 有                               | 有                           | —                                              |
| `HeartbeatStaleness`                        | 有                               | 有                           | —                                              |
| `HumanInTheLoop` (HITL)                     | 有                               | **无**                       | 子代理不触发人机交互                           |
| `MessagePersistenceMiddleware`              | 有                               | **无**                       | 子代理不持久化消息到 MesMemory                 |
| `LLMRetryMiddleware`                        | 有                               | **无**                       | 子代理不做 LLM 重试                            |
| `Summarization`                             | 有                               | 有                           | —                                              |
| `TodoContinuationEnforcer` (E3)             | 有                               | **无**                       | 子代理不做 Todo 连续性强制                     |
| Graph wrapper: `RepetitionGuardWrapper`     | 有                               | 有                           | —                                              |
| Graph wrapper: `ContextLimitGuardWrapper`   | 有                               | **无**                       | 子代理不做上下文限制守卫                       |

**子代理被砍掉的中间件涉及的职责**：上下文管理（Eviction / ContextLimitGuard）、人机交互（HITL）、消息持久化（MessagePersistence）、LLM 重试（LLMRetry）、任务意图分类（TaskIntent）、路径安全（PathGuard）、子代理排空（SubagentCompletionDrain）、Todo 连续性（TodoContinuationEnforcer）、多模态处理（MultimodalProcessor）。

### taskflow 现状

```
agent/tools/taskflow/
├── config.py                  # TaskFlowStatus / StepStatus (BLOCKED→READY→DISPATCHED→DONE)
├── tools/
│   ├── _shared.py             # new_step(): step_id/task/depends_on/status/retry_count
│   ├── _dispatch.py           # dispatch_child(): monkeypatchable → spawn_subagent_direct()
│   ├── taskflow_create.py     # 创建 flow
│   ├── taskflow_dispatch.py   # 批量 dispatch ready 步骤
│   ├── taskflow_resume.py     # 子代理完成后 resume
│   ├── taskflow_finish.py     # 标记 flow done
│   └── ...
└── registry/
    └── store_sqlite.py        # state_json 存储 steps/results
```

**问题**：DAG 步骤无类型区分——所有 step 都是"dispatch 一个子代理执行任务"。没有 verify/synthesize 等质量门禁节点。`taskflow_finish` 直接标记完成，无审查环节。

---

## Phase 1：subagent 功能角色分工（参考 deepagents）

### 设计目标

引入与深度角色正交的**功能角色**维度：

```
功能角色: general / researcher / executor / reviewer / [用户自定义]  ← 决定"做什么"
深度角色: main / orchestrator / leaf                              ← 决定"能 spawn 多深"
```

两个维度独立运作：功能角色驱动 LLM 选择、工具策略和系统提示词；深度角色驱动 spawn 权限和 scopes。

### 参考的 deepagents 模式

| deepagents 模式                       | sherry 适配方式                                    |
| ------------------------------------- | -------------------------------------------------- |
| AGENTS.md + YAML frontmatter 定义角色 | `workspace/subagent_roles/<name>/AGENTS.md`        |
| `tools: []` 精确工具白名单            | 复用 `inherited_tool_policy.py` 的 allow/deny 机制 |
| `web: true` 附加 web 工具             | sherry 无独立 web 工具集，跳过                     |
| `mode: fresh` 隔离上下文              | 已有 `ContextMode.ISOLATED`，复用                  |
| Per-task 工具动态附加（TaskTools）    | `spawn_subagent_direct()` 增加 `extra_tools` 参数  |
| `general-purpose` 自动注入            | sherry 默认无角色时回退到 `general`                |

---

### Step 1.1：功能角色枚举与类型定义

- [ ] **新建 `agent/tools/subagent/types/functional_role.py`**

```python
"""Functional role enum for sub-agents — orthogonal to depth-based SubagentSessionRole.

Functional roles drive LLM selection, tool policy, and system prompt content.
Depth roles (MAIN/ORCHESTRATOR/LEAF) drive spawn permissions and scopes.
"""

from enum import StrEnum


class FunctionalRole(StrEnum):
    """Functional specialization of a sub-agent worker.

    GENERAL: full tool access, can modify files (default fallback).
    RESEARCHER: read-only, codebase/web search, cheaper model.
    EXECUTOR: write-capable, code execution, no subagent spawn.
    REVIEWER: read-only diff audit, quality gate enforcement.
    """

    GENERAL = "general"
    RESEARCHER = "researcher"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
```

- [ ] **修改 `agent/tools/subagent/types/registry.py`**

在 `SubagentRunRecord` 中增加字段：

```python
# 在现有字段之后添加（depth/role 之后）
functional_role: FunctionalRole = FunctionalRole.GENERAL
```

- [ ] **修改 `agent/tools/subagent/types/__init__.py`**，导出 `FunctionalRole`

---

### Step 1.2：角色定义文件系统（参考 deepagents AGENTS.md + frontmatter）

- [ ] **创建目录结构**

```
workspace/
└── subagent_roles/
    ├── general/
    │   └── AGENTS.md
    ├── researcher/
    │   └── AGENTS.md
    ├── executor/
    │   └── AGENTS.md
    └── reviewer/
        └── AGENTS.md
```

- [ ] **编写 `workspace/subagent_roles/general/AGENTS.md`**

```markdown
---
name: general
description: "General-purpose worker for multi-step tasks with full file access"
model_tier: auxiliary
tools: inherit
---

You are a GENERAL-purpose subagent worker.

## Capabilities

- Full tool access (inherited from parent)
- Can read, write, and modify files
- Can execute terminal commands
- Can use web search

## When to use

- Multi-step tasks that require file modifications
- Tasks that don't fit a specific specialist role
- General coding, debugging, and implementation work
```

- [ ] **编写 `workspace/subagent_roles/researcher/AGENTS.md`**

```markdown
---
name: researcher
description: "Read-only research worker for codebase exploration and web search"
model_tier: auxiliary
tools:
  - read_file
  - terminal
  - web_search
---

You are a RESEARCHER subagent worker.

## Capabilities

- Read-only: you CANNOT modify, create, or delete files
- Codebase search via terminal commands (grep, find, rg, ls)
- Web search
- Terminal for read-only inspection (cat, ls, find, git log, git diff)

## When to use

- "Find where X is defined"
- "Search for usage of Y"
- "Research how Z works in this codebase"
- "Find all files matching pattern P"

## Output

Report findings concisely with file paths and line numbers.
Do NOT attempt to fix or modify anything you find.
```

- [ ] **编写 `workspace/subagent_roles/executor/AGENTS.md`**

```markdown
---
name: executor
description: "Code execution worker for running scripts and terminal commands"
model_tier: auxiliary
tools:
  - read_file
  - write_file
  - patch_file
  - terminal
  - python_repl
---

You are an EXECUTOR subagent worker.

## Capabilities

- Full file read/write/patch access
- Terminal command execution
- Python REPL
- Cannot dispatch taskflow tasks (taskflow tools are main-only;
  request task dispatch from the parent agent instead)

## When NOT to use

- Do NOT use for research-only tasks (use researcher)
- Do NOT use for code review (use reviewer)
- Do NOT spawn further subagents (you are a LEAF executor)

## Output

Report what was executed, results, and any errors encountered.
```

- [ ] **编写 `workspace/subagent_roles/reviewer/AGENTS.md`**

```markdown
---
name: reviewer
description: "Read-only code reviewer for quality assurance and diff audit"
model_tier: auxiliary
tools:
  - read_file
  - terminal
---

You are a REVIEWER subagent worker.

## Capabilities

- Read-only: you CANNOT modify, create, or delete files
- Code reading via read_file
- Terminal for git diff, git log, test running

## Responsibilities

- Audit code changes for correctness, security, and style
- Verify tests pass
- Check for edge cases and error handling
- Validate against project conventions (AGENTS.md)

## Output Format

## Review Result

**Verdict**: [pass | block]
**Issues Found**:

- [Critical/Warning/Info] <file:line> <description>
  **Suggestions**:
- <improvement recommendation>

**Test Status**: [pass | fail | not-run]
```

---

### Step 1.3：角色定义加载器

- [ ] **新建 `agent/tools/subagent/roles/loader.py`**

```python
"""Load functional role definitions from workspace/subagent_roles/<name>/AGENTS.md.

Parses YAML frontmatter (name, description, model_tier, tools) and the
markdown body as the role-specific system prompt supplement.

Mirrors deepagents' _load_local_subagents() pattern.
"""

from pathlib import Path
from dataclasses import dataclass

from config import WORKSPACE_DIR
from .types.functional_role import FunctionalRole


@dataclass(frozen=True)
class RoleDefinition:
    """Parsed role definition from an AGENTS.md file."""

    role: FunctionalRole
    description: str
    model_tier: str  # "main" | "auxiliary" | "inherit"
    tools: list[str] | None  # None = inherit all; empty list = no tools
    prompt_body: str  # markdown body after frontmatter


_ROLES_DIR_NAME = "subagent_roles"
_cache: dict[FunctionalRole, RoleDefinition] | None = None


def get_roles_dir() -> Path:
    """Return the workspace subagent_roles directory path."""
    return Path(WORKSPACE_DIR) / _ROLES_DIR_NAME


def load_role_definition(role: FunctionalRole) -> RoleDefinition | None:
    """Load a single role definition from its AGENTS.md file."""
    # Implementation: parse YAML frontmatter + markdown body
    # Return None if file not found (caller falls back to GENERAL defaults)
    ...


def load_all_role_definitions() -> dict[FunctionalRole, RoleDefinition]:
    """Scan subagent_roles/ and load all role definitions.

    Results are cached; call invalidate_role_cache() to force reload.
    """
    global _cache
    if _cache is not None:
        return _cache
    # Scan directory, parse each AGENTS.md
    ...
    _cache = result
    return result


def invalidate_role_cache() -> None:
    """Clear the role definition cache (e.g. after workspace file edit)."""
    global _cache
    _cache = None
```

- [ ] **新建 `agent/tools/subagent/roles/__init__.py`**，导出 `RoleDefinition`、`load_role_definition`、`load_all_role_definitions`

---

### Step 1.4：功能角色解析（在 spawn pipeline 中集成）

- [ ] **修改 `agent/tools/subagent/spawn/core.py::spawn_subagent_direct()`**

在 Phase 4（Ownership & capability resolution）之后、Phase 5（Model & thinking plan）之前插入新 Phase：

```python
# --- Phase 4.5: Functional role resolution ---
# Resolve functional role from spawn parameter or agent_id mapping
functional_role = _resolve_functional_role(functional_role_hint, agent_id)
role_def = load_role_definition(functional_role)

# --- Phase 5: Model & thinking plan (modified) ---
# Pass role_def.model_tier to model resolution
model_plan = resolve_model_and_thinking_plan(
    model_override=model,
    thinking_override_raw=thinking,
    requester_thinking=None,
    target_agent_thinking=None,
    model_tier=role_def.model_tier if role_def else "auxiliary",
)
```

在 Phase 8（Tool policy）中，用 role_def 覆盖工具策略：

```python
# --- Phase 8: Tool policy & registry registration (modified) ---
tool_allow = []
tool_deny = list(DEFAULT_SUBAGENT_BLOCKED_TOOLS)

# Functional role drives tool policy (deepagents pattern)
if role_def and role_def.tools is not None:
    # Explicit tool whitelist — only these tools are available
    tool_allow = list(role_def.tools)
    # Clear deny list; the whitelist already restricts
    tool_deny = []

# Depth role still controls spawn/yield
if role == SubagentSessionRole.ORCHESTRATOR:
    if tool_allow:
        # Explicit whitelist mode: ADD spawn/yield to allow list
        # (clearing deny was already done above; deny is empty, so
        # removing from it would be a no-op — must add to allow instead)
        for t in ("sessions_spawn", "sessions_yield"):
            if t not in tool_allow:
                tool_allow.append(t)
    else:
        # Inherit mode: remove spawn/yield from deny list
        tool_deny = [t for t in tool_deny if t not in ("sessions_spawn", "sessions_yield")]
```

在 Phase 10（System prompt）中，注入角色提示词：

```python
# --- Phase 10: System prompt & context assembly (modified) ---
system_prompt = build_subagent_system_prompt(
    role=role,
    functional_role=functional_role,
    role_description=role_def.description if role_def else "",
    role_prompt_body=role_def.prompt_body if role_def else "",
    task=task,
    ...
)
```

在 `register_run()` 调用中增加 `functional_role` 字段。

- [ ] **修改 `spawn_subagent_direct()` 签名**，增加参数：

```python
async def spawn_subagent_direct(
    ...,
    functional_role_hint: str | None = None,  # NEW
    extra_tools: list[str] | None = None,  # NEW: per-task tool attachment
) -> SpawnResult:
```

- [ ] **新增辅助函数 `_resolve_functional_role()`**

```python
def _resolve_functional_role(
    hint: str | None,
    agent_id: str,
) -> FunctionalRole:
    """Resolve functional role from hint or agent_id mapping.

    Priority: explicit hint → agent_id name match → GENERAL default.
    """
    if hint:
        try:
            return FunctionalRole(hint)
        except ValueError:
            logger.warning("Unknown functional_role hint '{}', falling back to GENERAL", hint)
    # Try to match agent_id to a known role name
    try:
        return FunctionalRole(agent_id)
    except ValueError:
        pass
    return FunctionalRole.GENERAL
```

---

### Step 1.5：角色驱动的 LLM 选择（修改 `_build_child_agent`）

- [ ] **修改 `agent/tools/subagent/spawn/core.py::_build_child_agent()`**

当前逻辑（`core.py:821-834`）：

```python
# 现状：仅按深度角色选 LLM
if model_override:
    child_llm = build_llm_by_name(model_override)
elif role == SubagentSessionRole.ORCHESTRATOR:
    child_llm = build_main_llm()
else:
    child_llm = build_auxiliary_llm()
```

改为：

```python
# 新：model_override > role_def.model_tier > 深度角色
if model_override:
    child_llm = build_llm_by_name(model_override)
elif role_def and role_def.model_tier == "main":
    child_llm = build_main_llm()
elif role_def and role_def.model_tier == "auxiliary":
    child_llm = build_auxiliary_llm()
elif role == SubagentSessionRole.ORCHESTRATOR:
    child_llm = build_main_llm()
else:
    child_llm = build_auxiliary_llm()
```

需要在 `_build_child_agent` 签名中增加 `functional_role` 和 `role_def` 参数，从 `_execute_subagent` 传入。

---

### Step 1.6：角色专属系统提示词（修改 `system_prompt.py`）

- [ ] **修改 `agent/tools/subagent/spawn/system_prompt.py::build_subagent_system_prompt()`**

增加参数：

```python
def build_subagent_system_prompt(
    role: SubagentSessionRole,
    task: str,
    functional_role: FunctionalRole = FunctionalRole.GENERAL,  # NEW
    role_description: str = "",  # NEW
    role_prompt_body: str = "",  # NEW
    requester_label: str = "parent agent",
    depth: int = 1,
    max_depth: int = 3,
    child_session_key: str = "",
    requester_session_key: str = "",
    can_spawn: bool = False,
    is_persistent_session: bool = False,
) -> str:
```

在 Section 1（Role）中注入功能角色描述：

```python
# Section 1: Role (modified)
if role == SubagentSessionRole.LEAF:
    role_desc = (
        f"You are a LEAF worker subagent with the {functional_role.value.upper()} specialization.\n"
        f"You CANNOT spawn further subagents.\n"
    )
    if role_description:
        role_desc += f"{role_description}\n"
    role_desc += "Execute your assigned task directly and report your results."
elif role == SubagentSessionRole.ORCHESTRATOR:
    role_desc = (
        f"You are an ORCHESTRATOR subagent with the {functional_role.value.upper()} specialization.\n"
        f"You MAY spawn further subagents using the `sessions_spawn` tool.\n"
    )
    if role_description:
        role_desc += f"{role_description}\n"
    role_desc += "Keep your children's tasks brief and focused."

# Append role-specific prompt body (from AGENTS.md)
if role_prompt_body:
    sections.append(f"## Role Instructions\n{role_prompt_body}")
```

---

### Step 1.7：Per-task 工具动态附加（参考 deepagents TaskTools）

- [ ] **修改 `agent/tools/subagent/spawn/core.py::spawn_subagent_direct()`**

在 Phase 8 之后、Phase 11 之前，合并 extra_tools：

```python
# --- Phase 8.5: Per-task tool attachment (deepagents TaskTools pattern) ---
if extra_tools:
    # extra_tools is a list of tool NAMES; resolve to actual tool objects
    # and merge into the tool list for THIS spawn only
    from agent.tools import build_main_tools
    all_tools = build_main_tools()
    tool_name_map = {getattr(t, "name", str(t)): t for t in all_tools}
    additional = [tool_name_map[name] for name in extra_tools if name in tool_name_map]
    # These tools are ADDED to whatever the role allows, not replacing
```

在 Phase 11 中将 `additional` 工具传入 `_execute_subagent_with_lane` → `_build_child_agent`。

---

### Step 1.8：配置集成

- [ ] **修改 `agent/tools/subagent/config.py`**，增加角色相关配置：

```python
class SubagentConfig(BaseModel):
    ...
    # Functional roles
    functional_roles_enabled: bool = True
    default_functional_role: str = "general"
    roles_dir_name: str = "subagent_roles"
```

---

### Step 1.9：工具注册 — 在 sessions_spawn 工具中暴露 functional_role 参数

- [ ] **查找并修改 `sessions_spawn` 工具 schema**

在 `agent/tools/subagent/` 下找到 `sessions_spawn` 工具定义文件，在 schema 中增加：

```python
"functional_role": {
    "type": "string",
    "enum": ["general", "researcher", "executor", "reviewer"],
    "description": "Functional specialization of the subagent worker. "
                   "general=full access, researcher=read-only search, "
                   "executor=write+run, reviewer=read-only audit.",
    "default": "general",
},
"extra_tools": {
    "type": "array",
    "items": {"type": "string"},
    "description": "Additional tool names to attach for this spawn only "
                   "(deepagents per-task tool pattern).",
},
```

---

## Phase 2：taskflow DAG 质量门禁（参考 hermes Kanban）

### 设计目标

在 taskflow DAG 中引入 **StepType** 维度，让步骤可以声明自己是 `work`、`verify` 或 `synthesize` 类型。`verify` 步骤作为质量门禁，必须通过才能继续后续步骤；`synthesize` 步骤在所有前置步骤完成后综合产出最终结果。

### 参考的 hermes Kanban Swarm 模式

```
planning root (立即完成, 共享黑板)
    ├─ parallel specialist workers (并行执行)
    └─ verifier (等待所有 worker, 门槛 pass/block)
         └─ synthesizer (等待 verifier, 产出最终交付)
```

sherry 适配为 taskflow DAG 节点类型：

```
[work steps] ──→ [verify step] ──→ [synthesize step] ──→ finish
                     │
                     ├─ pass → unlock dependents
                     └─ block → flow stays running, needs rework
```

---

### Step 2.1：StepType 枚举

- [ ] **修改 `agent/tools/taskflow/config.py`**

```python
class StepType(StrEnum):
    """Functional type of a DAG step.

    WORK: standard task dispatched to a subagent (default, backward-compatible).
    VERIFY: quality gate — waits for all deps to be done, then audits results.
        Emits pass/block verdict. Block prevents downstream steps from unblocking.
    SYNTHESIZE: final aggregation — waits for all deps, produces deliverable.
        Typically the last step before taskflow_finish.
    """

    WORK = "work"
    VERIFY = "verify"
    SYNTHESIZE = "synthesize"
```

---

### Step 2.2：步骤定义扩展（修改 `_shared.py`）

- [ ] **修改 `agent/tools/taskflow/tools/_shared.py::new_step()`**

```python
def new_step(
    step_id: str,
    task: str,
    depends_on: list[str] | None = None,
    status: StepStatus | str = StepStatus.READY,
    step_type: StepType | str = StepType.WORK,  # NEW
    verdict: str | None = None,  # NEW: "pass" | "block" | None
    review_notes: str | None = None,  # NEW
) -> dict:
    return {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": str(status),
        "step_type": str(step_type),  # NEW
        "verdict": verdict,  # NEW
        "review_notes": review_notes,  # NEW
        "retry_count": 0,
    }
```

- [ ] **修改 `validate_steps_list()`**，验证 `step_type` 字段：

```python
valid_step_types = {member.value for member in StepType}
# 在循环中添加
step_type = step.get("step_type", StepType.WORK.value)
if step_type not in valid_step_types:
    return f"Error: step '{step_id}' has invalid step_type {step_type!r}"
```

---

### Step 2.3：verify 步骤的 dispatch 逻辑

- [ ] **修改 `agent/tools/taskflow/tools/taskflow_dispatch.py`**

在 dispatch 前检查步骤类型：

```python
for sid in requested:
    step = by_id[sid]
    step_type = step.get("step_type", StepType.WORK.value)

    if step_type == StepType.VERIFY.value:
        # Verify steps require ALL dependencies to be done (not just satisfied)
        # This is stricter than BLOCKED→READY: every dep must have verdict=pass
        deps = step.get("depends_on") or []
        for dep_id in deps:
            dep = by_id.get(dep_id)
            if dep is None or step_status(dep) != StepStatus.DONE:
                return f"Error: verify step '{sid}' requires all dependencies done"
            # Check dependency verdicts — blocked deps prevent verification
            dep_verdict = dep.get("verdict")
            if dep_verdict == "block":
                return f"Error: verify step '{sid}' blocked by dependency '{dep_id}' verdict=block"
```

- [ ] **修改 `_dispatch.py::dispatch_child()`**，支持按 step_type 传递 functional_role：

```python
async def dispatch_child(
    task: str,
    requester_session_key: str,
    label: str | None = None,
    functional_role: str | None = None,  # NEW
) -> str:
    from agent.tools.subagent import spawn_subagent_direct

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        label=label,
        expects_completion_message=True,
        functional_role_hint=functional_role,  # NEW
    )
    ...
```

在 `taskflow_dispatch.py` 中，根据 step_type 自动推导 functional_role：

```python
# Map step_type to functional_role for the dispatched subagent
step_type_to_role = {
    StepType.WORK.value: None,  # let caller decide or default to general
    StepType.VERIFY.value: "reviewer",
    StepType.SYNTHESIZE.value: "general",  # synthesizer uses general with special prompt
}

functional_role = step_type_to_role.get(step_type)
```

---

### Step 2.4：verify 步骤的 resume 逻辑 — 门禁判定

- [ ] **修改 `agent/tools/taskflow/tools/taskflow_resume.py`**

在 resume 时，如果步骤是 verify 类型，解析子代理返回的 verdict：

```python
# After extracting result text from subagent completion
step_type = step.get("step_type", StepType.WORK.value)

if step_type == StepType.VERIFY.value:
    # Parse verdict from result text (reviewer outputs structured format)
    verdict, review_notes = _parse_verify_verdict(result_text)
    step["verdict"] = verdict
    step["review_notes"] = review_notes

    if verdict == "block":
        # Do NOT mark step as done — keep it dispatched
        # The flow stays running; the agent must address the review notes
        # and re-dispatch the failing dependency steps
        # Instead, mark as "blocked" with review notes visible
        step["status"] = str(StepStatus.READY)  # ready for re-dispatch
        # Do NOT unlock dependents — they stay blocked
        return f"Verify step '{step_id}' BLOCKED: {review_notes}"

# For synthesize steps, just mark done normally
```

- [ ] **新增 `_parse_verify_verdict()` 辅助函数**

```python
def _parse_verify_verdict(result_text: str) -> tuple[str, str]:
    """Extract verdict and notes from a reviewer subagent's structured output.

    Expected format (from reviewer AGENTS.md):
        ## Review Result
        **Verdict**: [pass | block]
        **Issues Found**:
          - ...
    """
    import re

    # Match the verdict line specifically: **Verdict**: pass  or  **Verdict**: block
    # Case-insensitive, allows optional whitespace.  Anchored to the bold marker
    # to avoid matching "pass" / "block" elsewhere in the review notes.
    verdict_match = re.search(
        r"\*\*\s*verdict\s*\*\*\s*[:：]\s*(pass|block)",
        result_text,
        re.IGNORECASE,
    )

    if verdict_match:
        verdict = verdict_match.group(1).lower()
    else:
        # No verdict line found — default to block for safety.
        # A malformed or missing verdict should never silently pass.
        verdict = "block"

    # Notes = full output (reviewer subagent's structured text)
    notes = result_text
    return verdict, notes
```

---

### Step 2.5：synthesize 步骤 — 最终聚合

- [ ] **修改 `taskflow_resume.py`**，synthesize 步骤的特殊处理：

```python
if step_type == StepType.SYNTHESIZE.value:
    # Synthesize step: collect all dependency results and pass to subagent
    deps = step.get("depends_on") or []
    dep_results = []
    for dep_id in deps:
        dep = by_id.get(dep_id)
        if dep:
            dep_result = results_map.get(dep_id, "")
            dep_results.append(f"### Step {dep_id}\n{dep_result}")

    # Enrich the task with dependency results
    enriched_task = f"{task}\n\n## Input from dependency steps\n\n" + "\n\n".join(dep_results)
    # Use enriched_task when dispatching
```

---

### Step 2.6：unlock 逻辑修改 — 受 verdict 门禁控制

- [ ] **修改 `agent/tools/taskflow/tools/_shared.py::unlock_dependents()`**

```python
def unlock_dependents(steps: list[dict]) -> list[str]:
    """Move blocked steps with satisfied deps to ready; return their ids.

    A verify step with verdict=block does NOT unlock its dependents —
    the block propagates downstream until the issue is resolved.
    """
    newly_ready: list[str] = []
    for step in steps:
        if step_status(step) != StepStatus.BLOCKED:
            continue
        if not deps_satisfied(step, steps):
            continue
        # NEW: check if any dependency is a verify step with verdict=block
        blocked_by_verify = False
        for dep_id in step.get("depends_on") or []:
            dep = next((s for s in steps if s.get("step_id") == dep_id), None)
            if dep and dep.get("step_type") == StepType.VERIFY.value:
                if dep.get("verdict") == "block":
                    blocked_by_verify = True
                    break
        if blocked_by_verify:
            continue
        step["status"] = str(StepStatus.READY)
        newly_ready.append(step.get("step_id"))
    return newly_ready
```

---

### Step 2.7：taskflow_finish 增加门禁检查

- [ ] **修改 `agent/tools/taskflow/tools/taskflow_finish.py`**

在标记 done 前，检查是否有未通过的 verify 步骤：

```python
# Check for blocked verify steps before allowing finish
steps = state.get("steps") or []
blocked_verifies = [
    s for s in steps
    if s.get("step_type") == StepType.VERIFY.value
    and s.get("verdict") == "block"
]
if blocked_verifies:
    step_ids = [s.get("step_id") for s in blocked_verifies]
    return (
        f"Error: Cannot finish flow — verify step(s) {step_ids} "
        f"have verdict=block. Address review notes and re-dispatch."
    )
```

---

### Step 2.8：taskflow_progress 展示 verdict 信息

- [ ] **修改 `agent/tools/taskflow/tools/taskflow_progress.py`**

在进度报告中展示 verify/synthesize 步骤的 verdict：

```python
# For verify steps, show verdict
step_type = step.get("step_type", "work")
if step_type == "verify":
    verdict = step.get("verdict", "pending")
    line += f" | verdict={verdict}"
    if step.get("review_notes"):
        line += f" | notes={step['review_notes'][:80]}..."
```

---

## 测试计划

### Phase 1 测试

- [ ] **T1.1** — `tests/agent/tools/subagent/types/test_functional_role.py`
  - FunctionalRole 枚举值正确
  - SubagentRunRecord.functional_role 默认值为 GENERAL

- [ ] **T1.2** — `tests/agent/tools/subagent/roles/test_loader.py`
  - 加载 general/researcher/executor/reviewer 角色定义
  - frontmatter 解析正确（name/description/model_tier/tools）
  - markdown body 作为 prompt_body 返回
  - 缺失文件返回 None，不抛异常
  - 缓存失效后重新加载

- [ ] **T1.3** — `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`
  - spawn 时传入 `functional_role_hint="researcher"` → run record 中 functional_role=RESEARCHER
  - researcher 角色的工具白名单生效（无 write_file/edit）
  - researcher 角色使用 auxiliary LLM
  - executor 角色的工具白名单生效（有 write_file/bash，无 sessions_spawn）
  - reviewer 角色的工具白名单生效（只读）
  - `extra_tools` 参数附加的工具出现在子代理工具列表中
  - 未知 functional_role_hint 回退到 GENERAL

- [ ] **T1.4** — `tests/agent/tools/subagent/spawn/test_system_prompt_role.py`
  - LEAF + RESEARCHER 的提示词包含 "RESEARCHER specialization"
  - ORCHESTRATOR + GENERAL 的提示词包含 "ORCHESTRATOR" + "GENERAL"
  - role_prompt_body 被注入到 "Role Instructions" section

- [ ] **T1.5** — `tests/agent/tools/subagent/test_backward_compatibility.py`
  - 不传 functional_role_hint 时，行为与当前完全一致
  - 现有 `agent_id` 参数仍仅用于 session key 命名
  - SubagentRunRecord 无 functional_role 字段时反序列化默认为 GENERAL

### Phase 2 测试

- [ ] **T2.1** — `tests/agent/tools/taskflow/test_step_type.py`
  - StepType 枚举值正确
  - new_step() 默认 step_type=WORK
  - validate_steps_list() 拒绝非法 step_type

- [ ] **T2.2** — `tests/agent/tools/taskflow/test_verify_gate.py`
  - verify 步骤在依赖未全 done 时不可 dispatch
  - verify 步骤在依赖有 verdict=block 时不可 dispatch
  - verify 步骤 resume 后 verdict=pass → 依赖解锁
  - verify 步骤 resume 后 verdict=block → 依赖保持锁定
  - _parse_verify_verdict() 正确解析 "**Verdict**: pass" 和 "**Verdict**: block"
  - _parse_verify_verdict() 缺少 verdict 行时默认 block（安全失败）

- [ ] **T2.3** — `tests/agent/tools/taskflow/test_synthesize.py`
  - synthesize 步骤 dispatch 时 task 被依赖结果充实
  - synthesize 步骤完成后结果写入 flow state

- [ ] **T2.4** — `tests/agent/tools/taskflow/test_finish_gate.py`
  - 存在 verdict=block 的 verify 步骤时 taskflow_finish 返回 Error
  - 所有 verify 步骤 verdict=pass 时 taskflow_finish 成功

- [ ] **T2.5** — `tests/agent/tools/taskflow/test_unlock_with_verdict.py`
  - unlock_dependents() 在依赖 verify 步骤 verdict=block 时不解锁
  - unlock_dependents() 在依赖 verify 步骤 verdict=pass 时正常解锁

- [ ] **T2.6** — `tests/agent/tools/taskflow/test_e2e_quality_gate.py`
  - 完整流程：create → dispatch work steps → resume → dispatch verify → resume (pass) → dispatch synthesize → resume → finish
  - 完整流程（block）：create → dispatch work steps → resume → dispatch verify → resume (block) → finish 被拒绝

---

## 回滚方案

### Phase 1 回滚

1. 在 `SubagentRunRecord` 中 `functional_role` 字段设默认值 `GENERAL` — 不传该参数时行为与当前完全一致
2. `workspace/subagent_roles/` 目录可删除，loader 返回 None 时 spawn pipeline 回退到 GENERAL 默认行为
3. `spawn_subagent_direct()` 的 `functional_role_hint` 和 `extra_tools` 参数均有默认值 None
4. 删除 `types/functional_role.py`、`roles/loader.py`、`roles/` 目录即可完全回退

### Phase 2 回滚

1. `new_step()` 的 `step_type` 参数默认值为 `WORK` — 不传时与当前行为完全一致
2. `validate_steps_list()` 对无 `step_type` 字段的旧 step 默认为 WORK
3. `unlock_dependents()` 的 verdict 检查仅对 `step_type=verify` 的步骤生效，WORK 步骤不受影响
4. `taskflow_finish()` 的门禁检查仅在有 verify 步骤时触发，纯 WORK 流程不受影响

---

## 文件变更清单

### 新建文件

| 文件路径                                                               | 用途                |
| ---------------------------------------------------------------------- | ------------------- |
| `agent/tools/subagent/types/functional_role.py`                        | FunctionalRole 枚举 |
| `agent/tools/subagent/roles/__init__.py`                               | roles 包            |
| `agent/tools/subagent/roles/loader.py`                                 | 角色定义加载器      |
| `workspace/subagent_roles/general/AGENTS.md`                           | general 角色定义    |
| `workspace/subagent_roles/researcher/AGENTS.md`                        | researcher 角色定义 |
| `workspace/subagent_roles/executor/AGENTS.md`                          | executor 角色定义   |
| `workspace/subagent_roles/reviewer/AGENTS.md`                          | reviewer 角色定义   |
| `tests/agent/tools/subagent/types/test_functional_role.py`             | 枚举测试            |
| `tests/agent/tools/subagent/roles/test_loader.py`                      | 加载器测试          |
| `tests/agent/tools/subagent/spawn/test_functional_role_integration.py` | 集成测试            |
| `tests/agent/tools/subagent/spawn/test_system_prompt_role.py`          | 提示词测试          |
| `tests/agent/tools/subagent/test_backward_compatibility.py`            | 向后兼容测试        |
| `tests/agent/tools/taskflow/test_step_type.py`                         | StepType 测试       |
| `tests/agent/tools/taskflow/test_verify_gate.py`                       | 门禁测试            |
| `tests/agent/tools/taskflow/test_synthesize.py`                        | 聚合测试            |
| `tests/agent/tools/taskflow/test_finish_gate.py`                       | finish 门禁测试     |
| `tests/agent/tools/taskflow/test_unlock_with_verdict.py`               | 解锁测试            |
| `tests/agent/tools/taskflow/test_e2e_quality_gate.py`                  | E2E 测试            |

### 修改文件

| 文件路径                                          | 变更内容                                                                                                                                   |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `agent/tools/subagent/types/registry.py`          | SubagentRunRecord 增加 functional_role 字段                                                                                                |
| `agent/tools/subagent/types/__init__.py`          | 导出 FunctionalRole                                                                                                                        |
| `agent/tools/subagent/spawn/core.py`              | spawn_subagent_direct() 增加 functional_role_hint/extra_tools 参数；Phase 4.5/8.5 插入角色逻辑；_build_child_agent() 增加角色驱动 LLM 选择 |
| `agent/tools/subagent/spawn/system_prompt.py`     | build_subagent_system_prompt() 增加功能角色参数和角色提示词注入                                                                            |
| `agent/tools/subagent/config.py`                  | SubagentConfig 增加 functional_roles_enabled/default_functional_role                                                                       |
| `agent/tools/taskflow/config.py`                  | 增加 StepType 枚举                                                                                                                         |
| `agent/tools/taskflow/tools/_shared.py`           | new_step() 增加 step_type/verdict/review_notes；unlock_dependents() 增加 verdict 门禁；validate_steps_list() 增加 step_type 验证           |
| `agent/tools/taskflow/tools/taskflow_dispatch.py` | dispatch 前检查 verify 步骤依赖；按 step_type 推导 functional_role                                                                         |
| `agent/tools/taskflow/tools/taskflow_resume.py`   | resume 时解析 verify verdict；synthesize 步骤充实 task                                                                                     |
| `agent/tools/taskflow/tools/taskflow_finish.py`   | finish 前检查 blocked verify 步骤                                                                                                          |
| `agent/tools/taskflow/tools/taskflow_progress.py` | 进度报告展示 verdict                                                                                                                       |
| `agent/tools/taskflow/tools/_dispatch.py`         | dispatch_child() 增加 functional_role 参数                                                                                                 |

---

## 实施顺序

```
Phase 1（subagent 功能角色）
  │
  ├─ Step 1.1  类型定义（FunctionalRole 枚举 + registry 字段）
  ├─ Step 1.2  角色定义文件（workspace/subagent_roles/*/AGENTS.md）
  ├─ Step 1.3  加载器（roles/loader.py）
  ├─ Step 1.4  spawn pipeline 集成（core.py Phase 4.5/8.5）
  ├─ Step 1.5  LLM 选择（_build_child_agent 修改）
  ├─ Step 1.6  系统提示词（system_prompt.py 修改）
  ├─ Step 1.7  per-task 工具附加（extra_tools 参数）
  ├─ Step 1.8  配置集成（SubagentConfig）
  ├─ Step 1.9  工具 schema（sessions_spawn 暴露参数）
  └─ 测试 T1.1-T1.5

Phase 2（taskflow 质量门禁）— 依赖 Phase 1 的 reviewer 角色
  │
  ├─ Step 2.1  StepType 枚举（config.py）
  ├─ Step 2.2  步骤定义扩展（_shared.py: new_step + validate + unlock）
  ├─ Step 2.3  dispatch 逻辑（taskflow_dispatch.py + _dispatch.py）
  ├─ Step 2.4  resume 门禁判定（taskflow_resume.py + _parse_verify_verdict）
  ├─ Step 2.5  synthesize 聚合（taskflow_resume.py）
  ├─ Step 2.6  unlock 门禁（_shared.py: unlock_dependents）
  ├─ Step 2.7  finish 门禁（taskflow_finish.py）
  ├─ Step 2.8  进度展示（taskflow_progress.py）
  └─ 测试 T2.1-T2.6
```

Phase 2 的 Step 2.3 依赖 Phase 1 的 reviewer 角色定义（verify 步骤自动 dispatch 为 reviewer functional_role），因此 Phase 2 必须在 Phase 1 完成后实施。

---

## 下游依赖

本文件（`subagent-role-migration.md`）是功能角色分工的基础设施层。以下计划依赖本文件 Phase 1 完成后方可实施：

### PTC_SUBAGENT_PLAN.md

- **依赖项**: Phase 1 全部（FunctionalRole 枚举、角色加载器、spawn pipeline 集成、LLM 选择、系统提示词）
- **角色**: EXECUTOR — PTC 子代理仅注入给 `functional_role == FunctionalRole.EXECUTOR` 的子代理
- **使用方式**:
  - `spawn_subagent_direct()` 传入 `functional_role_hint="executor"`
  - PTC 工具（`ptc_create` / `ptc_execute` / `ptc_status` / `ptc_cancel`）通过 `extra_tools` 参数附加
  - PTC 系统提示词通过 `build_subagent_system_prompt()` 的 `role_prompt_body` 注入
- **阻塞关系**: 如果 Phase 1 未完成，PTC 计划无法按角色条件注入，会退化为所有 LEAF 子代理均获得 PTC 工具

### CODE_INTEL_SUBAGENT_PLAN.md

- **依赖项**: Phase 1 全部（同 PTC）
- **角色**: RESEARCHER — 代码检索框架仅注入给 `functional_role == FunctionalRole.RESEARCHER` 的子代理
- **使用方式**:
  - `spawn_subagent_direct()` 传入 `functional_role_hint="researcher"`
  - 检索工具（`code_index_search` / `code_index_query` / `semantic_search`）通过 `extra_tools` 参数附加
  - 检索框架系统提示词通过 `role_prompt_body` 注入
- **阻塞关系**: 同 PTC，Phase 1 未完成时退化为所有 LEAF 子代理均获得检索工具

### DEEPAGENTS_BORROWING_PLAN.md P1-6（中间件脚手架保护）

- **依赖项**: 无硬依赖（P1-6 可独立于 Phase 1 实施）
- **关联**: P1-6 的 `_SUBAGENT_REQUIRED` 当前是单一份集合；未来当不同功能角色需要不同中间件链时（如 reviewer 需要 `HumanInTheLoop`），P1-6 的 entry 选择逻辑可从 `entries=_SUBAGENT_REQUIRED` 演进为 `entries=_SUBAGENT_REQUIRED_BY_ROLE.get(functional_role, _SUBAGENT_REQUIRED)`
- **非阻塞**: P1-6 可在 Phase 1 之前或之后实施，两者独立
