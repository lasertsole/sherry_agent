# 计划：taskflow step 挂载 skill（可插拔行为）

- 建档日期：2026-10-12
- 状态：**计划（未开工）**
- 父文档：[对标计划_Zcode可借鉴能力.md](./对标计划_Zcode可借鉴能力.md) §2.5
- 涉及文件：`agent/tools/taskflow/tools/_shared.py`、`taskflow_dispatch.py`、`taskflow_run_task.py`、`taskflow_resume.py`、`taskflow_plan.py`、`config.py`

## 是什么

允许 taskflow step 声明一个或多个 skill 名称。dispatch 该 step 时，系统自动把 SKILL.md 内容 prepend 到子代理的 task 文本前——子代理直接拿到 skill 的完整指令，不需要主代理先 `skill_view` 读一遍再手动嵌入 task 文本。

机制完全复用 cron job 的 `_assemble_skill_prompt()`（`skills/builtin/core/cron/scripts/base.py:90-153`）：`_skill_view(name, caller_scope="background")` 加载 → `[IMPORTANT: ...]` header 包装 → `bump_use()` 计数 → 注入扫描（warning only）。

## 例子

```
flow: "build user avatar upload feature"

  step-1: "设计 API schema"           delegation=self     ← 不挂 skill
  step-2: "实现后端上传接口"           delegation=subagent  ← 不挂 skill
  step-3: "实现前端头像组件"           delegation=subagent  ← 不挂 skill
  step-4: "写 E2E 测试"               delegation=subagent, skills=["browser"]
  step-5: "写 API 文档"               delegation=subagent, skills=["code-wiki"]
```

step-4 dispatch 时，子代理收到的 task 文本：

```
[IMPORTANT: The user has invoked the "browser" skill. Follow the skill's instructions below.]

<browser SKILL.md 全文>

写 E2E 测试

## Upstream Results           ← 现有 build_task_with_dep_results 追加
...
## Input parameters           ← 现有 build_task_with_bindings 追加
...
```

主代理不需要调 `skill_view("browser")` 读 ~2000 tokens 的 skill 内容再手动嵌入——`taskflow_dispatch` 时系统自动 prepend。

## 为什么做

**主代理是编排者，不该当 skill 内容中继。** 现在没有挂载机制时，主代理要给 step 附 skill 指令，必须：

1. 调 `skill_view("browser")` → ~2000 tokens 进主代理上下文
2. 把 skill 要点摘抄进 step 的 task 文本 → 经过一轮 LLM 转述，精度可能丢
3. 再调 `taskflow_create` / `taskflow_update_steps` 创建 step

每挂一个 skill 多一个 turn + ~2000 tokens 过主代理上下文。挂载机制把这条路从"主代理手动中继"变成"系统自动注入"——主代理只声明 skill 名，系统在 dispatch 时直接把 SKILL.md prepend 给子代理。

## 设计

### 1. step 加 `skills` 字段

`_OPTIONAL_STEP_FIELDS`（`_shared.py:85-99`）加一条：

```python
_OPTIONAL_STEP_FIELDS: tuple[str, ...] = (
    "expected_params",
    "response_schema",
    "judge_criteria",
    "judge_model",
    "validation_criteria",
    "functional_role",
    "step_model",
    "step_timeout_seconds",
    "priority",
    "input_bindings",
    "retry_policy",
    "skills",              # ← 新增：list[str]，dispatch 时 prepend SKILL.md
)
```

字段语义：`list[str]` | `None`。`None` 或空列表 = 不挂 skill（当前行为，向后兼容）。非空 = dispatch 时按顺序 prepend 每个 skill 的 SKILL.md。

`new_step`（`_shared.py:102-133`）已经对 `_OPTIONAL_STEP_FIELDS` 做白名单写入，加进去即可，不需要改 `new_step` 本身。

### 2. `prepend_skills()` 函数

新建 `agent/tools/taskflow/tools/_skill_prepend.py`（~40 行）：

```python
"""Prepend skill SKILL.md content to a taskflow step's task text.

Reuses the exact mechanism from cron's _assemble_skill_prompt
(skills/builtin/core/cron/scripts/base.py:90-153):
_skill_view(name, caller_scope="background") → [IMPORTANT: ...] header
→ bump_use() → injection scan (warning only).
"""
from __future__ import annotations
import json
from loguru import logger


def prepend_skills(task: str, skills: list[str] | None) -> str:
    """Prepend each skill's SKILL.md content before the task text.

    Returns the original task unchanged when skills is None/empty or
    every skill fails to load (fail-open: a bad binding never blocks dispatch).
    """
    if not skills:
        return task

    from agent.tools.skill_tools.skill_view import _INJECTION_PATTERNS, _skill_view
    from agent.tools.pub_base.skill_usage import bump_use

    blocks: list[str] = []
    for name in skills:
        try:
            parsed = json.loads(_skill_view(name, caller_scope="background"))
        except Exception as e:
            logger.warning("taskflow: failed to load skill '{}': {}", name, e)
            continue
        if not parsed.get("success"):
            logger.warning(
                "taskflow: skill '{}' not loadable ({}); step runs without it",
                name, parsed.get("error", "unknown"),
            )
            continue
        content = parsed.get("content", "")
        if not content:
            continue
        blocks.append(
            f'[IMPORTANT: The user has invoked the "{name}" skill. '
            f"Follow the skill's instructions below.]\n\n{content}"
        )
        try:
            bump_use(name)
        except Exception as e:
            logger.debug("taskflow: bump_use failed for '{}': {}", name, e)

    if not blocks:
        return task

    assembled = "\n\n".join(blocks) + "\n\n" + task

    # Warning-only injection scan (same as cron)
    lowered = assembled.lower()
    if any(pattern in lowered for pattern in _INJECTION_PATTERNS):
        logger.warning(
            "taskflow: assembled task+skill prompt contains patterns "
            "that may indicate prompt injection"
        )
    return assembled
```

**和 cron 的 `_assemble_skill_prompt` 逐行对齐**——同样的 header 格式、同样的 fail-open（坏 binding 跳过不阻断）、同样的 `bump_use` best-effort、同样的注入扫描。未来可以抽成共享函数（见"不做的事"）。

### 3. 接入 dispatch 的 task-text 组装链

现有组装链（`taskflow_dispatch.py:118-124`、`taskflow_run_task.py:177-187`、`_auto_dispatch_ready` in `taskflow_resume.py:462-467`）：

```python
# 现有：
task_text = with_judge_feedback(
    build_task_with_bindings(
        build_task_with_dep_results(step, steps, results),  # 读 step["task"]
        step, steps, results,
    ),
    feedback,
)
```

`build_task_with_dep_results` 内部读 `step["task"]` 作为基文本。skill prepend 要在基文本之后、upstream results 之前。

**改动**：`build_task_with_dep_results` 加一个 `task_override: str | None = None` 参数——传入时用它替代 `step["task"]` 作为基文本：

```python
# _shared.py — build_task_with_dep_results
def build_task_with_dep_results(
    step, steps, results, *, task_override: str | None = None
) -> str:
    task = task_override if task_override is not None else step["task"]
    ...
```

三处调用点改为：

```python
# taskflow_dispatch / taskflow_run_task / _auto_dispatch_ready：
skill_task = prepend_skills(step["task"], step.get("skills"))
task_text = with_judge_feedback(
    build_task_with_bindings(
        build_task_with_dep_results(step, steps, results, task_override=skill_task),
        step, steps, results,
    ),
    feedback,
)
```

**组装顺序**（skill 在最前，task 文本紧跟，现有 builder 追加在后面）：

```
[IMPORTANT: skill 内容]          ← prepend_skills
<SKILL.md 全文>

<step["task"] 原文>              ← 基文本

## Upstream Results              ← build_task_with_dep_results
...
## Input parameters              ← build_task_with_bindings
...
## Previous Attempt Feedback     ← with_judge_feedback（仅 re-dispatch）
...
```

### 4. `taskflow_plan` 支持 skills 字段

`taskflow_plan`（`taskflow_plan.py`）的 planner prompt 和 `_parse_plan` 加 `skills`：

planner prompt 的 JSON shape（`_SYSTEM_PROMPT`）加一个可选字段：

```json
[{"step_id": "step-1", "task": "...", "depends_on": [], "skills": ["browser"]}]
```

`_parse_plan`（`taskflow_plan.py:54-93`）的 normalize 加一行：

```python
plan.append({
    "step_id": step_id,
    "task": task,
    "depends_on": deps,
    "judge_criteria": entry.get("judge_criteria"),
    "response_schema": entry.get("response_schema"),
    "skills": entry.get("skills"),    # ← 新增；None = 不挂
})
```

`new_step` 的 `_OPTIONAL_STEP_FIELDS` 白名单已经包含 `"skills"`（步骤 1 加了），所以 `skills` 会被正确写入。

### 5. `taskflow_update_steps` 支持 skills 字段

`taskflow_update_steps` 已有的字段更新逻辑通过 `_OPTIONAL_STEP_FIELDS` 白名单校验——加了 `"skills"` 后，模型可以通过 `taskflow_update_steps` 给已有 step 追加/修改 skill 绑定。

### 6. skill 名称校验

和 cron 一样：`_skill_view(name)` 会做全名匹配 + 目录匹配 + frontmatter `name` 匹配。不存在的 skill → `parsed.get("success") == False` → log + skip（fail-open）。不需要额外校验。

### 7. curator 集成（可选，低优先）

cron 的 `skill_refs.py::referenced_skill_names()` 让 curator 在归档 skill 前检查"有没有 cron job 在用"。taskflow 也应做等价物——扫描活跃 flow 的 step 的 `skills` 字段，让 curator 的 90 天归档拒绝归档某个活跃 flow 还在用的 skill。

```python
# agent/tools/taskflow/registry/store_sqlite.py 或 _shared.py
def referenced_skill_names(session_id: str | None = None) -> set[str]:
    """All skill names bound by steps of non-terminal flows."""
    flows = get_active_flows_sync(session_id) if session_id else get_all_flows_sync()
    names: set[str] = set()
    for flow in flows:
        for step in (flow.get("state") or {}).get("steps") or []:
            skills = step.get("skills")
            if isinstance(skills, list):
                names.update(s for s in skills if isinstance(s, str) and s.strip())
    return names
```

curator 的 archive 过渡检查改为 `cron_refs ∪ taskflow_refs`。这条可以后续做，不阻塞核心功能。

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/tools/taskflow/tools/_shared.py` | `_OPTIONAL_STEP_FIELDS` 加 `"skills"`；`build_task_with_dep_results` 加 `task_override` 参数 | ~10 改动 |
| `agent/tools/taskflow/tools/_skill_prepend.py` | 新建：`prepend_skills()` 函数 | ~50 |
| `agent/tools/taskflow/tools/taskflow_dispatch.py` | 组装链加 `prepend_skills` 调用 | ~5 改动 |
| `agent/tools/taskflow/tools/taskflow_run_task.py` | 同上 | ~5 改动 |
| `agent/tools/taskflow/tools/taskflow_resume.py` | `_auto_dispatch_ready` 同上 | ~5 改动 |
| `agent/tools/taskflow/tools/taskflow_plan.py` | planner prompt + `_parse_plan` 加 `skills` | ~10 改动 |
| `tests/agent/tools/taskflow/test_skill_prepend.py` | 新建：prepend + fail-open + 组装顺序测试 | ~80 |
| `tests/agent/tools/taskflow/test_taskflow_plan_skills.py` | 新建：planner 产出 skills 字段 | ~40 |

**合计 ~205 行**

## 验收

- step 不带 `skills` → `prepend_skills` 返回原 task 文本，行为不变
- step 带 `skills=["browser"]` → 子代理收到的 task 文本开头是 `[IMPORTANT: ... browser ...]` + SKILL.md 全文
- step 带 `skills=["browser", "code-wiki"]` → 两个 skill block 按顺序 prepend，task 文本在最后
- step 带 `skills=["nonexistent"]` → log warning + 跳过 + 正常 dispatch（fail-open）
- step 带一个存在的 + 一个不存在的 skill → 存在的 prepend，不存在的跳过
- skill prepend 在 upstream results / input parameters / judge feedback 之前
- `bump_use` 对 agent-created skill 计数 +1（对 bundled skill 是 no-op，和 cron 一致）
- 注入扫描发现可疑 pattern → log warning，不阻断 dispatch
- `taskflow_plan` 的 planner 能产出带 `skills` 字段的 step
- `taskflow_update_steps` 能给已有 step 追加/修改 `skills`
- 压缩后 skill 绑定不丢——`skills` 在 step dict 里（SQLite 持久化），重建时从 flow state 读

## 风险

- **token 膨胀**：每个 skill 的 SKILL.md 可能 ~2000-5000 tokens。如果一个 step 挂 3 个 skill，子代理的 task 文本膨胀 ~15000 tokens。缓解：step 的 `skills` 默认为 `None`（不挂），只有主代理主动声明才挂；skill 内容不经过主代理上下文（只在 dispatch 时注入子代理）。
- **skill 内容过时**：step 持久化了 `skills=["browser"]`，但 browser skill 后来被删了 → dispatch 时 `_skill_view` 返回失败 → log + skip（fail-open）。不会阻断 flow。
- **`build_task_with_dep_results` 签名变更**：加 `task_override` 参数是向后兼容的（默认 `None` = 读 `step["task"]`）。现有测试不受影响。
- **跨包依赖**：`_skill_prepend.py` 在 `agent/tools/taskflow/` 里 import `agent.tools.skill_tools.skill_view` 和 `agent.tools.pub_base.skill_usage`——同在 `agent/` 包内，不违反 import 规则。

## 不做的事

- **不抽共享函数**——`prepend_skills` 和 cron 的 `_assemble_skill_prompt` 逻辑几乎一样，理想情况应抽到 `agent/tools/pub_base/skill_prompt.py` 让两者共用。但 cron 的函数在 `skills/builtin/core/cron/scripts/base.py`（`skills/` 包内），`agent/` 不能 import `skills/`（单向依赖）。抽共享函数需要先把 cron 的 `_assemble_skill_prompt` 也迁到 `agent/`，是独立重构。本计划先在 taskflow 里复制 ~40 行，后续统一抽。
- **不做 skill 热重载**——step 的 `skills` 字段持久化在 flow state 里，改要调 `taskflow_update_steps`。不读运行时 skill 目录变化。
- **不做 skill 参数传递**——不支持给 skill 传参数（如 `skills=[{"name": "browser", "args": {...}}]`）。skill 名是纯字符串，和 cron 一致。
- **不做 curator 集成（首期）**——`referenced_skill_names` 等价物可以后续做，不阻塞核心功能。首期 skill 被删只影响 dispatch 时 fail-open skip。

## 后续（不阻塞首期）

首期（上面的改动清单）先在 taskflow 里复制 ~40 行 `prepend_skills`，和 cron 各自独立。以下两项在首期落地后再做，不阻塞核心功能。

### 后续 1：抽共享函数 `assemble_skill_prompt`

**问题**：`prepend_skills`（taskflow 的 `_skill_prepend.py`）和 `_assemble_skill_prompt`（cron 的 `scripts/base.py:90-153`）逻辑几乎逐行相同——`_skill_view` 加载 → `[IMPORTANT: ...]` header → `bump_use` → 注入扫描。两份代码各自维护，改一处忘改另一处会不一致。

**障碍**：cron 的 `_assemble_skill_prompt` 在 `skills/builtin/core/cron/scripts/base.py`（`skills/` 包内）。`agent/` 不能 import `skills/`（AGENTS.md 的 import 规则：`skills/**` MUST NOT import `server/**`，但 `agent/**` import `skills/**` 也没有先例且违反分层意图——`skills/` 是可插拔内容层，`agent/` 是核心引擎层，引擎不应依赖内容层）。所以不能直接让 cron 调 taskflow 的 `prepend_skills`，也不能让 taskflow 调 cron 的 `_assemble_skill_prompt`。

**方案**：把共享逻辑抽到 `agent/tools/pub_base/skill_prompt.py`（`agent/` 包内，两边都能 import）：

```python
# agent/tools/pub_base/skill_prompt.py

"""Shared skill-prompt assembly: load SKILL.md by name, wrap in
[IMPORTANT: ...] header, bump_use, injection scan.

Used by:
- cron: skills/builtin/core/cron/scripts/base.py::_assemble_skill_prompt
- taskflow: agent/tools/taskflow/tools/_skill_prepend.py::prepend_skills

Both callers are thin wrappers that pass their message + skill list
and get back the assembled prompt.
"""
from __future__ import annotations
import json
from loguru import logger


def assemble_skill_prompt(message: str, skill_names: list[str]) -> str:
    """Prepend each skill's SKILL.md content before the message.

    Fail-open: a bad/unloadable binding is logged and skipped, never blocks.
    Returns the original message unchanged when skill_names is empty or
    every skill fails to load.
    """
    if not skill_names:
        return message

    from agent.tools.skill_tools.skill_view import _INJECTION_PATTERNS, _skill_view
    from agent.tools.pub_base.skill_usage import bump_use

    blocks: list[str] = []
    for name in skill_names:
        try:
            parsed = json.loads(_skill_view(name, caller_scope="background"))
        except Exception as e:
            logger.warning("skill_prompt: failed to load skill '{}': {}", name, e)
            continue
        if not parsed.get("success"):
            logger.warning(
                "skill_prompt: skill '{}' not loadable ({}); running without it",
                name, parsed.get("error", "unknown"),
            )
            continue
        content = parsed.get("content", "")
        if not content:
            continue
        blocks.append(
            f'[IMPORTANT: The user has invoked the "{name}" skill. '
            f"Follow the skill's instructions below.]\n\n{content}"
        )
        try:
            bump_use(name)
        except Exception as e:
            logger.debug("skill_prompt: bump_use failed for '{}': {}", name, e)

    if not blocks:
        return message

    assembled = "\n\n".join(blocks) + "\n\n" + message
    lowered = assembled.lower()
    if any(pattern in lowered for pattern in _INJECTION_PATTERNS):
        logger.warning(
            "skill_prompt: assembled prompt contains patterns that "
            "may indicate prompt injection"
        )
    return assembled
```

**迁移步骤**：

1. 新建 `agent/tools/pub_base/skill_prompt.py`，放入上面的 `assemble_skill_prompt`。
2. taskflow 的 `_skill_prepend.py::prepend_skills` 改为薄封装：
   ```python
   from agent.tools.pub_base.skill_prompt import assemble_skill_prompt

   def prepend_skills(task: str, skills: list[str] | None) -> str:
       if not skills:
           return task
       return assemble_skill_prompt(task, skills)
   ```
3. cron 的 `scripts/base.py::_assemble_skill_prompt` 改为薄封装：
   ```python
   from agent.tools.pub_base.skill_prompt import assemble_skill_prompt

   def _assemble_skill_prompt(message: str, skill_names: list[str]) -> str:
       return assemble_skill_prompt(message, skill_names)
   ```
   注意：cron 的 `scripts/base.py` 在 `skills/` 包内，它 import `agent.tools.pub_base` 是允许的（`skills/builtin/**` user scripts may import `models`/`bus`/`channels`/`workspace`/`runtime`，`agent.tools.pub_base` 也在允许方向上——AGENTS.md 的 ban 只覆盖 `server/**`）。
4. 删掉两边的重复逻辑（`_skill_view` import、`bump_use` import、block 构建、注入扫描）。
5. 测试：cron 的 `test_cron_skill_binding.py` 和 taskflow 的 `test_skill_prepend.py` 都不需要改——它们测的是行为（header 格式、fail-open、message 在最后），不是实现。

**改动量**：新建 1 个文件 ~50 行，改 2 个文件各 ~-30 行（删重复逻辑 + 加一行 import）。净减 ~10 行。

**时机**：首期 taskflow 的 `prepend_skills` 落地、测试通过后再做。如果首期后发现两边的 header 格式或 fail-open 行为有分歧，这一步也会顺带统一。

### 后续 2：curator 集成 `referenced_skill_names`

**问题**：curator 的 90 天归档过渡会拒绝归档"有 cron job 在用"的 skill（通过 `skill_refs.py::referenced_skill_names()` 检查 `cron_jobs.json`）。但 taskflow step 的 `skills` 字段没纳入这个检查——一个活跃 flow 的 step 挂了 `skills=["browser"]`，curator 可能把 browser skill 归档掉，下次 dispatch 时 fail-open skip。

**方案**：taskflow 侧加 `referenced_skill_names()`，curator 的 archive 检查改为 `cron_refs ∪ taskflow_refs`。

```python
# agent/tools/taskflow/registry/store_sqlite.py（或 _shared.py）

def referenced_skill_names() -> set[str]:
    """All skill names bound by steps of non-terminal flows.

    Scans every running/waiting flow's step list for non-empty `skills`
    fields. Used by the curator's archive transition to refuse archiving
    a skill some active flow still references.
    """
    names: set[str] = set()
    try:
        flows = get_all_flows_sync()  # 非 terminal 的 flow
    except Exception:
        return names  # fail-open: DB 读失败 → 不阻止归档
    for flow in flows:
        steps = (flow.get("state") or {}).get("steps") or []
        for step in steps:
            skills = step.get("skills")
            if isinstance(skills, list):
                names.update(s for s in skills if isinstance(s, str) and s.strip())
    return names
```

**curator 接入**（`context_engine/curator/` 的 archive 过渡检查）：

```python
# 现在：
from skills.builtin.core.cron.scripts.skill_refs import referenced_skill_names as cron_refs
active_refs = cron_refs()

# 改后：
from skills.builtin.core.cron.scripts.skill_refs import referenced_skill_names as cron_refs
from agent.tools.taskflow.registry.store_sqlite import referenced_skill_names as taskflow_refs
active_refs = cron_refs() | taskflow_refs()
```

**注意**：curator 在 `context_engine/` 包内。`context_engine/**` MUST NOT import `agent/**`（AGENTS.md import 规则）。所以不能直接 import `agent.tools.taskflow.registry.store_sqlite`。

**解法**：通过 `runtime/hooks.py` 或 `runtime/data_provider.py` 做依赖倒置——curator 在 `runtime` 层注册一个 `taskflow_skill_refs` provider，taskflow 在 server 启动时填充它：

```python
# runtime/data_provider.py 加一个 provider slot
class TaskflowSkillRefsProvider(Protocol):
    def __call__(self) -> set[str]: ...

_taskflow_skill_refs: TaskflowSkillRefsProvider | None = None

def register_taskflow_skill_refs(fn: TaskflowSkillRefsProvider) -> None:
    global _taskflow_skill_refs
    _taskflow_skill_refs = fn

def get_taskflow_skill_refs() -> set[str]:
    if _taskflow_skill_refs is None:
        return set()
    try:
        return _taskflow_skill_refs()
    except Exception:
        return set()  # fail-open
```

```python
# server 启动时（agent/core.py 或 server/trigger/channels/core.py）
from runtime.data_provider import register_taskflow_skill_refs
from agent.tools.taskflow.registry.store_sqlite import referenced_skill_names as tf_refs

register_taskflow_skill_refs(tf_refs)
```

```python
# curator 的 archive 检查
from runtime.data_provider import get_taskflow_skill_refs
active_refs = cron_refs() | get_taskflow_skill_refs()
```

和现有的 `PromptDataProvider` / `SkillWriteProvider` 完全同一个模式——`runtime/data_provider.py` 是跨层 seam，`agent/` 注册、`context_engine/` 消费，不违反 import 规则。

**改动量**：`runtime/data_provider.py` ~15 行（provider slot），`store_sqlite.py` ~15 行（函数），curator ~5 行（改检查），server 启动 ~3 行（注册）。合计 ~38 行。

**时机**：首期 skill 挂载落地后再做。首期 skill 被删只影响 dispatch 时 fail-open skip（不阻断 flow），风险低。
