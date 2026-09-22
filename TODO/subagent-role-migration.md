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
- [Phase 2：taskflow DAG 质量门禁（增量）](#phase-2taskflow-dag-质量门禁增量)
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

### LLM 选择（当前代码，`_build_child_agent` 定义于 `core.py:807`，LLM 选择分支第 883-896 行）

| 角色                        | LLM                                                   |
| --------------------------- | ----------------------------------------------------- |
| MAIN                        | `build_main_llm()`                                    |
| ORCHESTRATOR                | `build_main_llm()`（较大模型）                        |
| LEAF                        | `build_auxiliary_llm()`（较小/较便宜模型）            |
| 任意角色 + `model_override` | `build_llm_by_name(model_override)`，失败回退角色默认 |

### 中间件链对比（当前代码）

Main agent 的中间件链由 `agent/core.py::_build_middlewares()`（定义于 `agent/core.py:140-234`，经 `create_agent()` 装配）构建；子代理的 `_build_child_agent()`（定义于 `agent/tools/subagent/spawn/core.py:807`，`create_agent()` 调用于 `spawn/core.py:908-939`）装配精简链——**11 个中间件被砍掉**（下表 "子代理" 列标 "无" 的行共 11 个）。

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

**问题（已修订）**：DAG 步骤无**类型维度**——所有 step 都是"dispatch 一个子代理执行任务"。但**质量门禁已存在**（不在 DAG 节点层面，而在 resume/finish 路径）：

- **StepJudge**（`step_judge.py`）在 `taskflow_resume` 里对带 `validation_criteria` 的步骤做 per-step 判定（PASS/RETRY/BLOCK）；
- **Finish Gate A–D**（`taskflow_finish.py`）在终局做 DAG 完整性/无 blocked/evidence/verifier 四道门；
- **Evidence 三件套** 提供 append-only 证据与 stale 检测。

因此**不需要**新增 `verify` 节点类型（那会与 StepJudge 平行且冲突）。本计划 Phase 2 只补一个缺口：**没有把上游步骤结果聚合进下游汇总任务的机制**（synthesize 聚合）。详见 Phase 2。

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
| AGENTS.md + YAML frontmatter 定义角色 | `agent/tools/subagent/roles/definitions/<name>/AGENTS.md`（可选 `workspace/subagent_roles/` 覆盖） |
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

#### 落盘位置决策（必读）

内置角色定义**必须入库、可随包分发**，因此**不能**放在 `workspace/subagent_roles/`：

- `.gitignore` 第 38 行是锚定的 `/workspace/`（只忽略仓库根 `workspace/`，让 `tests/workspace/` 仍可跟踪）。写入 `workspace/subagent_roles/` 的默认角色定义**永远不会入库、不可分发**——这是本计划早期草案的缺陷。
- `workspace/` 是**用户可变的人物/行为目录**；角色定义是**基础设施默认值**，应随代码走。

**选定位置：`agent/tools/subagent/roles/definitions/<name>/AGENTS.md`**（包内数据，随包跟踪/分发）。

理由：

| 判据 | `agent/tools/subagent/roles/definitions/`（选定） | `workspace/template/<lang>/subagent_roles/`（候选，否决） |
| ---- | ------------------------------------------------ | -------------------------------------------------------- |
| 可分发性 | ✅ 普通跟踪文件，随包分发 | ✅ 但每个语言目录要各放一份 |
| 语言无关 | ✅ 角色系统提示词是英文/语言中立，单份 | ❌ `template/<lang>/` 按语言分 4 份，内容重复 |
| 自动同步 | ✅ 无需同步，loader 直接读包路径 | ❌ `workspace/file_sync.py` 只按 `ALL_SYSTEM_FILE_NAMES` 平铺复制 5 个具名文件，**不会递归复制子目录**，需额外改造 |
| 项目先例 | ✅ 同 `skills/builtin/`（内置技能入库） | ⚠️ 模板仅用于人物文件 |

**用户覆盖（可选，不入库）**：loader 优先读 `workspace/subagent_roles/<name>/AGENTS.md`（若存在，用户可自定义），否则回退包内 `definitions/`。这样默认值随包分发，用户又能在运行时覆盖，且覆盖文件天然不入库（与人物文件同级语义）。

```
agent/tools/subagent/roles/
├── __init__.py
├── loader.py
└── definitions/            # 内置默认角色（跟踪，不可被 .gitignore 吞掉）
    ├── general/
    │   └── AGENTS.md
    ├── researcher/
    │   └── AGENTS.md
    ├── executor/
    │   └── AGENTS.md
    └── reviewer/
        └── AGENTS.md
```

- [ ] **编写 `agent/tools/subagent/roles/definitions/general/AGENTS.md`**

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

- [ ] **编写 `agent/tools/subagent/roles/definitions/researcher/AGENTS.md`**

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

- [ ] **编写 `agent/tools/subagent/roles/definitions/executor/AGENTS.md`**

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

- [ ] **编写 `agent/tools/subagent/roles/definitions/reviewer/AGENTS.md`**

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

Report findings as plain markdown. Do NOT emit a machine-parsed "Verdict"
block — quality gating is done by the LLM StepJudge (`step_judge.py`), which
reads the result text, not by a regex parser.

- Issues: `[Critical/Warning/Info] <file:line> <description>`
- Suggestions: `<improvement recommendation>`
- Test status: `[pass | fail | not-run]`
```

---

### Step 1.3：角色定义加载器

- [ ] **新建 `agent/tools/subagent/roles/loader.py`**

```python
"""Load functional role definitions from AGENTS.md files.

Built-in defaults ship INSIDE the package (tracked, distributable):
    agent/tools/subagent/roles/definitions/<name>/AGENTS.md

An optional per-user override may be placed (untracked) at:
    workspace/subagent_roles/<name>/AGENTS.md

Resolution order: workspace override → package default → None (caller falls
back to GENERAL defaults).

Parses YAML frontmatter (name, description, model_tier, tools) and the
markdown body as the role-specific system prompt supplement.
Mirrors deepagents' _load_local_subagents() pattern.
"""

from pathlib import Path
from dataclasses import dataclass

from config import WORKSPACE_DIR
from ..types.functional_role import FunctionalRole  # loader.py lives in roles/, types/ is a sibling


@dataclass(frozen=True)
class RoleDefinition:
    """Parsed role definition from an AGENTS.md file."""

    role: FunctionalRole
    description: str
    model_tier: str  # "main" | "auxiliary" | "inherit"
    tools: list[str] | None  # None = inherit all; empty list = no tools
    prompt_body: str  # markdown body after frontmatter


_DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"
_OVERRIDE_DIR_NAME = "subagent_roles"
_cache: dict[FunctionalRole, RoleDefinition] | None = None


def get_roles_dir() -> Path:
    """Return the tracked package directory holding the built-in definitions."""
    return _DEFINITIONS_DIR


def _role_file_candidates(role: FunctionalRole) -> tuple[Path, ...]:
    """Override first (untracked, user-editable), then the packaged default."""
    override = Path(WORKSPACE_DIR) / _OVERRIDE_DIR_NAME / role.value / "AGENTS.md"
    default = _DEFINITIONS_DIR / role.value / "AGENTS.md"
    return (override, default)


def load_role_definition(role: FunctionalRole) -> RoleDefinition | None:
    """Load a single role definition, preferring a workspace override over the default."""
    for path in _role_file_candidates(role):
        if path.is_file():
            # Implementation: parse YAML frontmatter + markdown body
            ...
            return parsed
    # Return None if no definition exists (caller falls back to GENERAL defaults)
    return None


def load_all_role_definitions() -> dict[FunctionalRole, RoleDefinition]:
    """Load every known role's definition (override → default; missing → skipped).

    Results are cached; call invalidate_role_cache() to force reload.
    """
    global _cache
    if _cache is not None:
        return _cache
    result: dict[FunctionalRole, RoleDefinition] = {}
    for role in FunctionalRole:
        definition = load_role_definition(role)
        if definition is not None:
            result[role] = definition
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

> 🔴 **必须同时改函数签名**：`agent/tools/subagent/spawn/plan.py::resolve_model_and_thinking_plan()`（定义于 `plan.py:60`）**当前形参只有** `model_override` / `thinking_override_raw` / `requester_thinking` / `target_agent_thinking`，**没有 `model_tier`**。若只改调用点不改签名，会在 spawn 时抛 `TypeError: unexpected keyword argument 'model_tier'`。因此本步必须**同时**把 `model_tier: str | None = None` 加进 `resolve_model_and_thinking_plan()` 的签名（并让 `ModelThinkingPlan`/调用链透传它），否则整个 spawn pipeline 立即不可用。`plan.py` 已列入下方"修改文件清单"。

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

当前逻辑（`_build_child_agent` 定义于 `core.py:807`，LLM 选择分支位于 `core.py:883-896`）：

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
    # Built-in definitions live in-package (agent/tools/subagent/roles/definitions/);
    # this names the OPTIONAL untracked workspace override dir (see Step 1.2).
    roles_override_dir_name: str = "subagent_roles"
```

---

### Step 1.9：工具注册 — 在 sessions_spawn 工具中暴露 functional_role 参数

- [ ] **修改 `agent/tools/subagent/tools/sessions_spawn.py::SessionsSpawnSchema`**

> **现状校正**：`sessions_spawn` 的 schema **不是 dict，而是 Pydantic 模型** `SessionsSpawnSchema(BaseModel)`（`sessions_spawn.py:21`），`SessionsSpawnTool.args_schema = SessionsSpawnSchema`。因此新增字段必须写成 `Field(...)`，不是 JSON-schema 字面量。
>
> 🔴 **必须保留**已落地的 `goal_loop: bool`（`:52`）与 `goal_max_turns: int | None`（`:58`）两个字段——它们驱动 CompletionJudge goal loop，改动时**不得**丢弃或改名。

在 `SessionsSpawnSchema` 中**追加**（不替换现有字段）：

```python
functional_role: str | None = Field(
    default=None,
    description="Functional specialization of the subagent worker. "
    "general=full access, researcher=read-only search, "
    "executor=write+run, reviewer=read-only audit. "
    "Omit to inherit the default role.",
)
extra_tools: list[str] | None = Field(
    default=None,
    description="Additional tool names to attach for this spawn only "
    "(deepagents per-task tool pattern).",
)
```

并在 `SessionsSpawnTool` 的调用路径（`_arun`/`_run` 组装 `spawn_subagent_direct(...)` 处）把这两个字段透传为 `functional_role_hint=` / `extra_tools=`，与既有的 `goal_loop` / `goal_max_turns` 透传并列（`sessions_spawn.py:128-129`）。

---

## Phase 2：taskflow DAG 质量门禁（增量）

> **本节已按"增量"重写（在既有门禁之上）。** 原草案把 hermes Kanban 的 verify/synthesize 作为新建 `StepType` 门禁引入；但仓库里这些职责**已经由 StepJudge / CompletionJudge / Evidence / Finish Gate A–D 落地**。平行重建既重复造轮子，原 Step 2.6 还会**绕过并破坏** StepJudge 的 BLOCK 语义。因此本节只保留**唯一无冲突增量**（synthesize 依赖结果聚合），其余逐条标注"取代 / 不执行 / 降级"。

### 既有质量门禁盘点（必须先读）

| 已落地机制 | 位置 | 覆盖的职责 |
| --- | --- | --- |
| **StepJudge**（LLM 判定 pass/retry/block；RETRY 复用 `_retry` 预算 + `judge_feedback` 注入重派；BLOCK 落 `block_reason`） | `agent/tools/taskflow/step_judge.py`；接入 `taskflow_resume.py:157-209`（仅当步骤带 `validation_criteria` 时调用）；`_retry.with_judge_feedback` | per-step 质量门禁（= 草案的 verify） |
| **CompletionJudge + Goal Loop**（done/continue，预算 `goal_max_turns`） | `agent/tools/subagent/spawn/completion_judge.py`；`spawn/core.py::complete_subagent_run` 前；`sessions_spawn` 的 `goal_loop`/`goal_max_turns` | 子代理内部完成度判定 |
| **Evidence 三件套**（append-only 账本 + stale 事件行 + 汇总） | `agent/tools/taskflow/evidence_collector.py`、`agent/tools/todolist/evidence_recorder.py`、`evidence_ledger.py` | 终局证据门禁 |
| **Finish Gate A–D** | `agent/tools/taskflow/tools/taskflow_finish.py`（A: DAG 完整；B: 无 blocked；C: evidence 无 FAIL/`[stale]`；D: SisyphusVerifier，仅传 todo+plan_path 时） | 终局门禁（比草案的原 finish 门禁更广） |
| **Drain 程序门控** | `SubagentCompletionDrainMiddleware.enforce_verification`（默认 False）+ `agent/core.py` 装配 | 完成度程序校验 |

**既有字段命名（务必对齐，不得新建平行字段）**：

- `validation_criteria`（步骤验收标准；`taskflow_run_task.py:108` 写入，`taskflow_resume.py:158-163` 读取）
- `judge_feedback`（StepJudge RETRY 的反馈；`_shared.py:208`、`taskflow_dispatch.py:118`、`taskflow_resume.py:189`）
- `block_reason`（StepJudge BLOCK/RETRY 失败的落因；`taskflow_resume.py:182/197/205`）

**不得新建的平行机制（硬约束）**：`class StepType`、`step_type` 字段、`verdict` 字段、`review_notes` 字段、`_parse_verify_verdict()` 文本 verdict 解析、以及任何独立"verify 步骤"门禁。这些职责已由 StepJudge 一端承担；引入它们会与既有语义冲突。

### 设计目标（修订）

DAG 步骤**无需新增类型维度**即可获得质量门禁：需要门禁的步骤在创建时带上 `validation_criteria`，`taskflow_resume` 会调用 StepJudge 判定 pass/retry/block。Phase 2 的**唯一新增能力**是 **synthesize（依赖结果聚合）**——让一个"汇总步骤"在被 dispatch 时自动把其 `depends_on` 步骤的已记录结果拼进子任务文本，产出最终交付。

```
[work steps] ──(validation_criteria → StepJudge pass/retry/block)──→ [aggregate step] ──→ finish
```

> 注意：**没有 verify 步骤**。质量判定是 StepJudge 在 `taskflow_resume` 内部完成的副作用，不是 DAG 的独立节点；`taskflow_finish` 的终局门禁是既有 Finish Gate A–D。

---

### Step 2.4（原 verify verdict 文本门禁）— **已取代，不执行**

**处置：取代（Superseded）。** 不对应任何新代码。

- 原设计（解析 reviewer 文本里的 `**Verdict**: pass|block`、`_parse_verify_verdict()` 正则、缺失则默认 block）**已由 `agent/tools/taskflow/step_judge.py` + `taskflow_resume.py:157-209` 完整覆盖**：StepJudge 是 LLM 判定器，输出 PASS/RETRY/BLOCK；RETRY 复用 `_retry` 预算并把 `judge_feedback` 注入重派；BLOCK 写 `block_reason` 并把步骤置 `BLOCKED`。
- 代码中**不存在** `class StepType`、`_parse_verify_verdict`（全仓 grep 为空），所以原 Step 2.1/2.4 的代码块**照抄会失败**。
- 历史设计留痕：`_parse_verify_verdict` 的正则 + 默认 block 方案**不执行**（对应 `REVIEW_FIXES` 的 **M5 丢弃**）。其安全意图（"无 verdict 不得静默通过"）由 StepJudge 的 fail-open + `block_reason` 语义承担。

---

### Step 2.6（`unlock_dependents` verdict 门禁）— **不执行（会破坏 StepJudge BLOCK）**

**处置：不执行（Do not execute）。** 原因：

现行 `_shared.py::unlock_dependents()`（`_shared.py:153-171`）有一条守卫：**`BLOCKED` 且 `depends_on` 为空的步骤永不自动解锁**（注释原文："A blocked step with no `depends_on` is not waiting on the DAG (for example it was blocked by the step judge), so it is never auto-unlocked."）。StepJudge 把步骤置 `BLOCKED` 时**不写 `depends_on`**，正是靠这条守卫让 BLOCK 不会被自动复活。

原 Step 2.6 的替换版会**丢掉这条守卫**（它只按 `deps_satisfied` 解锁，不再判 `depends_on` 是否为空），于是 **StepJudge 的 BLOCK 会被 `unlock_dependents` 自动改回 READY 而绕过**。且原 Step 2.6 依赖不存在的 `step_type`/`verdict` 字段。故**不执行**；现有守卫保持不变。

---

### Step 2.7（taskflow_finish 门禁）— **已由 Finish Gate A–D 覆盖，不执行**

**处置：不执行（Do not execute）。** 既有 `taskflow_finish.py` 已实现四道门（且更广）：

- Gate A（`_FINISHABLE_STATUSES`：DAG 完整，无 ready/dispatched 步骤）
- Gate B（无 `BLOCKED` 步骤；错误信息读既有 `block_reason`）
- Gate C（`_evidence_gate`：evidence 无 `FAIL` / `[stale]`）
- Gate D（`_verifier_gate`：`SisyphusVerifier`，仅当同时传 `todo` + `plan_path` 时）

原 Step 2.7（"存在 verdict=block 的 verify 步骤 → 拒绝 finish"）是其中 Gate B（blocked 即拒绝）的一个子集，且依赖不存在的 `step_type`/`verdict` 字段。**不执行**。

---

### Step 2.1 / 2.2 / 2.3 / 2.8 — **不执行或降级（不得引入平行字段）**

| 原步骤 | 处置 | 结论 |
| --- | --- | --- |
| **Step 2.1** `StepType` 枚举（`config.py`） | **不执行** | 引入 `step_type` 平行字段，与新约束冲突；verify 职责已由 StepJudge 承担，synthesize 不靠枚举区分（见 Step 2.5） |
| **Step 2.2** `new_step()` 加 `step_type`/`verdict`/`review_notes`；`validate_steps_list()` 校验 `step_type` | **不执行** | 平行字段；`verdict`/`review_notes` 与既有 `judge_feedback`/`block_reason` 重复。既有 `validate_steps_list()` 已校验 `step_id`/`task`/`status`/`depends_on`，无需扩展 |
| **Step 2.3** verify dispatch 前置检查 + 按 `step_type` 推导 `functional_role` | **不执行** | verify 检查已由 StepJudge 在 resume 端完成（dispatch 端无需重复）；`functional_role` 推导依赖 `step_type`。若未来确需 dispatch 端指派角色，用 **Phase 1 新增**的 `_dispatch.dispatch_child(..., functional_role=...)` 显式传参，**不经过 step_type** |
| **Step 2.8** `taskflow_progress` 展示 `verdict` | **不执行（降级）** | `verdict` 字段不存在。若确需进度可视，直接读既有字段 `block_reason` / `judge_feedback`（非空才展示），**不新增字段**；优先级低，可延后 |

> 原 Step 2.3 想新增的 `_dispatch.dispatch_child(functional_role=...)` 参数，其**有效部分**归属 Phase 1（让 taskflow 能按角色派子代理），放在 Phase 1 的 Step 2.3 增量段落中落定；此处不重复。

---

### Step 2.5：synthesize 依赖结果聚合 — 【保留：本阶段唯一无冲突增量】

**处置：保留（Retain）。** 核实判据（为什么它不与既有机制重叠）：

- `taskflow_dispatch.py` / `taskflow_run_task.py` / `_dispatch.py` 目前**只把步骤自身的 `task` 传给子代理**，从不读取上游步骤结果（全模块无 `dep_results` / `results_map` 之类的注入逻辑）。
- `taskflow_wait_all.py` 只做**等待 settle**，不做结果聚合。
- `taskflow_resume.py` 只做**记录结果 + StepJudge 判定**，不把上游结果喂给下游任务。
- Finish Gate A–D 只做终局校验，不产出交付。

因此"把 `depends_on` 步骤的已记录结果拼进汇总步骤的任务文本"是**仓库当前不存在**的能力，且不新增质量门禁语义、不与 StepJudge/Evidence/FinishGate 冲突。

**实现（明确、可执行、零平行门禁字段）**：

1. `taskflow_run_task()` 增加**一个布尔形参** `aggregate_deps: bool = False`（默认 False，向后兼容）。当为 True 时，把 `candidate["aggregate_deps"] = True` 写入步骤（持久化以便 `taskflow_dispatch` 批量路径与 StepJudge RETRY 重派都能复现——这是**行为开关**，不是类型/门禁字段，不违反上表"不得引入平行字段"约束）。
2. 在 `_shared.py` 新增**唯一** helper：

```python
def build_task_with_dep_results(step: dict, steps: list[dict], results: list[dict]) -> str:
    """Append dependency-step results to this step's task when ``aggregate_deps``.

    Results are looked up by each dependency step's ``child_session_key`` against
    ``state["results"]`` records (recorded shape: ``{child_session_key, result,
    result_hash}``). A dependency with no recorded result contributes a
    "no result recorded" placeholder. Returns the original task unchanged when
    ``aggregate_deps`` is falsy or there are no dependencies.
    """
```

3. **两处调用点**（`taskflow_run_task` 的立即 dispatch 路径 + `taskflow_dispatch` 的批量路径）在调用 `_dispatch.dispatch_child(...)` 前：

```python
task_text = build_task_with_dep_results(step, steps, results)
child_session_key = await _dispatch.dispatch_child(
    task=task_text, requester_session_key=requester_key, label=label
)
```

4. **重派复现**：StepJudge RETRY 走 `with_judge_feedback(step["task"], feedback)`（`taskflow_resume.py:135-136`）。让聚合在 dispatch helper 内**按 `aggregate_deps` 重新推导**（依赖结果在 dep 步骤 DONE 后稳定），**不**改写 `step["task"]`（避免污染原始任务与 evidence 指纹）。
5. **不做**：不新增 `step_type`/`verdict`/`review_notes`；不新增 verify 步骤；不新建 finish/unlock 门禁；不改 StepJudge 行为。

**可执行性检查**：`aggregate_deps` 只是 `taskflow_run_task` 的一个新参数 + `_shared.build_task_with_dep_results` + 两处调用；`results` 从 `state["results"]` 取（`taskflow_resume` 已在用），`dep["child_session_key"]` 是既有字段。无隐藏依赖。

---

### 与既有 StepJudge / CompletionJudge / Evidence / FinishGate 的关系（逐条三分）

| 原草案步骤 | 关系 | 处置 |
| --- | --- | --- |
| Step 2.1 `StepType` 枚举 | **不重叠（但是多余）** | 不执行——引入 `step_type` 平行字段 |
| Step 2.2 `new_step()` 扩展 | **取代** | 不执行——`verdict`/`review_notes` 与 `judge_feedback`/`block_reason` 重复 |
| Step 2.3 verify dispatch 检查 | **取代** | 不执行——StepJudge 在 resume 端已做 |
| Step 2.4 verdict 文本门禁 + `_parse_verify_verdict` | **取代** | 不执行——`step_judge.py` + `taskflow_resume.py:157-209`（M5 丢弃） |
| Step 2.5 synthesize 聚合 | **不重叠（唯一增量）** | **保留**——新增 `aggregate_deps` + 1 个 helper |
| Step 2.6 `unlock_dependents` verdict 门禁 | **冲突** | 不执行——会丢掉"无 `depends_on` 的 BLOCKED 永不自动解锁"守卫，绕过 StepJudge BLOCK |
| Step 2.7 `taskflow_finish` 门禁 | **取代** | 不执行——Finish Gate A–D 已覆盖且更广 |
| Step 2.8 `taskflow_progress` 展示 verdict | **取代（降级）** | 不执行——`verdict` 字段不存在；如需可视，读既有字段 |

**复用（不新建）**：StepJudge（per-step 判定）、`validation_criteria`（门禁触发字段）、`judge_feedback`/`block_reason`（结果字段）、Evidence 三件套、Finish Gate A–D、CompletionJudge + goal loop、`_dispatch.dispatch_child`。

**不得新建（平行机制清单）**：`StepType` / `step_type` / `verdict` / `review_notes` / `_parse_verify_verdict` / verify 步骤 dispatch 门禁 / unlock verdict 门禁 / finish verdict 门禁 / progress verdict 展示。凡与 StepJudge/Evidence/FinishGate 重叠者，一律复用既有实现，不平行重建。

**与 Phase 1 的独立关系**：Phase 2 的落地门禁**不依赖**功能角色——StepJudge / Evidence / FinishGate 都是功能角色无关的机制；Step 2.5 也不依赖角色。原草案"Phase 2 依赖 Phase 1 reviewer 角色"的阻塞关系**取消**，两阶段可独立实施（详见"实施顺序"）。

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

> **已存在的测试不得重建**：以下文件**已在仓库中**，覆盖既有门禁，Phase 2 只做回归/复用，**不得**新建同名或平行测试：
> `tests/agent/tools/taskflow/test_finish_gate.py`、`test_step_judge.py`、`test_resume_with_judge.py`、`test_evidence_collector.py`、`test_chain_smoke.py`。

- [ ] **T2.1（新增）** — `tests/agent/tools/taskflow/test_synthesize.py`
  - `_shared.build_task_with_dep_results()`：`aggregate_deps` 为假时返回原 task（不改文本）
  - `aggregate_deps` 为真且 deps 有结果时，按 `dep["child_session_key"]` 匹配 `state["results"]` 并拼进文本
  - deps 无记录结果时插入 "no result recorded" 占位，不抛异常
  - deps 为空时返回原 task

- [ ] **T2.2（新增）** — `tests/agent/tools/taskflow/test_synthesize_e2e.py`
  - `taskflow_run_task(aggregate_deps=True)` 在依赖已 done 的批量 dispatch 路径上把上游结果拼进子任务（断言 `_dispatch.dispatch_child` 收到的 `task` 含上游结果）
  - StepJudge RETRY 重派时聚合结果仍可复现（依赖结果未变则重派任务文本一致）
  - 回归：`aggregate_deps` 默认 False 时行为与当前完全一致

> **迁移说明**：原 T2.1（StepType）、T2.2（verify gate）、T2.4（finish gate）、T2.5（unlock verdict）、T2.6（verify 版 e2e）**全部删除**——它们测的是不存在/被取代的机制；对应能力已由上述既有测试覆盖。

---

## 回滚方案

### Phase 1 回滚

1. 在 `SubagentRunRecord` 中 `functional_role` 字段设默认值 `GENERAL` — 不传该参数时行为与当前完全一致
2. 删除包内默认 `agent/tools/subagent/roles/definitions/` 与可选覆盖目录 `workspace/subagent_roles/` 后，loader 返回 None，spawn pipeline 回退到 GENERAL 默认行为
3. `spawn_subagent_direct()` 的 `functional_role_hint` 和 `extra_tools` 参数均有默认值 None
4. 删除 `types/functional_role.py`、`roles/loader.py`、`roles/` 目录即可完全回退

### Phase 2 回滚

Phase 2 只新增一个**可选**能力，回滚代价极低，且**不触碰任何既有门禁**：

1. `taskflow_run_task()` 的 `aggregate_deps` 参数默认值为 `False` — 不传时与当前行为完全一致
2. `build_task_with_dep_results()` 是纯函数；删除它并从两处调用点移除即可完全回退
3. 既有步骤不会带 `aggregate_deps` 字段；读取方按缺失/假值处理，旧 flow state 无需迁移
4. StepJudge / Evidence / Finish Gate A–D / `unlock_dependents()` 守卫**均未被修改**，回滚不涉及它们

---

## 文件变更清单

### 新建文件

| 文件路径                                                               | 用途                |
| ---------------------------------------------------------------------- | ------------------- |
| `agent/tools/subagent/types/functional_role.py`                        | FunctionalRole 枚举                       |
| `agent/tools/subagent/roles/__init__.py`                               | roles 包                                  |
| `agent/tools/subagent/roles/loader.py`                                 | 角色定义加载器（包内默认 + workspace 覆盖） |
| `agent/tools/subagent/roles/definitions/general/AGENTS.md`             | general 角色定义（跟踪，随包分发）         |
| `agent/tools/subagent/roles/definitions/researcher/AGENTS.md`          | researcher 角色定义                       |
| `agent/tools/subagent/roles/definitions/executor/AGENTS.md`            | executor 角色定义                         |
| `agent/tools/subagent/roles/definitions/reviewer/AGENTS.md`            | reviewer 角色定义                         |
| `tests/agent/tools/subagent/types/test_functional_role.py`             | 枚举测试                                  |
| `tests/agent/tools/subagent/roles/test_loader.py`                      | 加载器测试                                |
| `tests/agent/tools/subagent/spawn/test_functional_role_integration.py` | 集成测试                                  |
| `tests/agent/tools/subagent/spawn/test_system_prompt_role.py`          | 提示词测试                                |
| `tests/agent/tools/subagent/test_backward_compatibility.py`            | 向后兼容测试                              |
| `tests/agent/tools/taskflow/test_synthesize.py`                        | 聚合单元测试                              |
| `tests/agent/tools/taskflow/test_synthesize_e2e.py`                    | 聚合 E2E 测试                             |

### 修改文件

| 文件路径                                          | 变更内容                                                                                                                                    |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent/tools/subagent/types/registry.py`          | SubagentRunRecord 增加 functional_role 字段                                                                                                 |
| `agent/tools/subagent/types/__init__.py`          | 导出 FunctionalRole                                                                                                                         |
| `agent/tools/subagent/spawn/core.py`              | spawn_subagent_direct() 增加 functional_role_hint/extra_tools 参数；Phase 4.5/8.5 插入角色逻辑；`_build_child_agent()`（`core.py:807`）增加角色驱动 LLM 选择 |
| `agent/tools/subagent/spawn/plan.py`              | **`resolve_model_and_thinking_plan()`（`plan.py:60`）增加 `model_tier` 形参并透传**（否则 Step 1.4 调用点 `TypeError`）                     |
| `agent/tools/subagent/spawn/system_prompt.py`     | build_subagent_system_prompt() 增加功能角色参数和角色提示词注入                                                                             |
| `agent/tools/subagent/tools/sessions_spawn.py`    | `SessionsSpawnSchema`（Pydantic，`:21`）增加 `functional_role`/`extra_tools` Field；**保留** `goal_loop`/`goal_max_turns`                   |
| `agent/tools/subagent/config.py`                  | SubagentConfig 增加 functional_roles_enabled/default_functional_role/roles_override_dir_name                                                |
| `agent/tools/taskflow/tools/_shared.py`           | 新增 `build_task_with_dep_results()`（唯一新增 helper）；`new_step()` 增加 `aggregate_deps`                                                 |
| `agent/tools/taskflow/tools/taskflow_run_task.py` | 增加 `aggregate_deps: bool = False` 参数并调用 `build_task_with_dep_results`                                                                |
| `agent/tools/taskflow/tools/taskflow_dispatch.py` | 批量 dispatch 路径调用 `build_task_with_dep_results`                                                                                        |
| `agent/tools/taskflow/tools/_dispatch.py`         | `dispatch_child()` 增加 `functional_role` 参数（Phase 1 增量，供 taskflow 按角色派子代理）                                                  |

> **已删除的草案变更（不再修改这些文件）**：`agent/tools/taskflow/config.py`（StepType）、`taskflow_resume.py`（verify verdict）、`taskflow_finish.py`（verify 门禁）、`taskflow_progress.py`（verdict 展示）。既有门禁保持不变。

---

## 实施顺序

```
Phase 1（subagent 功能角色）
  │
  ├─ Step 1.1  类型定义（FunctionalRole 枚举 + registry 字段）
  ├─ Step 1.2  角色定义文件（agent/tools/subagent/roles/definitions/*/AGENTS.md）
  ├─ Step 1.3  加载器（roles/loader.py）
  ├─ Step 1.4  spawn pipeline 集成（core.py Phase 4.5/8.5）+ plan.py 签名加 model_tier
  ├─ Step 1.5  LLM 选择（_build_child_agent 修改）
  ├─ Step 1.6  系统提示词（system_prompt.py 修改）
  ├─ Step 1.7  per-task 工具附加（extra_tools 参数）
  ├─ Step 1.8  配置集成（SubagentConfig）
  ├─ Step 1.9  工具 schema（sessions_spawn 增加 Pydantic Field，保留 goal_loop/goal_max_turns）
  └─ 测试 T1.1-T1.5

Phase 2（taskflow 质量门禁增量）— 独立于 Phase 1
  │
  ├─ Step 2.1  synthesize 聚合（_shared.build_task_with_dep_results）
  ├─ Step 2.2  run_task/dispatch 调用点接入 aggregate_deps
  └─ 测试 T2.1-T2.2
```

Phase 1 与 Phase 2 **无依赖关系，可独立并行实施**：Phase 2 的落地门禁（StepJudge / Evidence / Finish Gate A–D）与功能角色无关，Step 2.5 的结果聚合也不依赖角色。原草案"Phase 2 依赖 Phase 1 reviewer 角色"的阻塞关系**已取消**。

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
