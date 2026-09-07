# Nudge 知识提取重构方案 — Plan-Aware Knowledge Extraction

> 配套文件: TODOLIST_PLAN.md, TODOLIST_ENFORCEMENT.md
> 参考来源: oh-my-openagent-dev (D:\selfProj\oh-my-openagent-dev)
> 日期: 2026-09-07

## 设计哲学

**废弃 10-turn threshold 的 skill 提取触发**，改为 **plan-aware 知识提取 + skill 库更新**：

```
旧方案 (10-turn threshold):
  每 10 次 tool call → 启动 nudge agent → _SKILL_REVIEW_PROMPT → 全对话审查
  问题: 时机不对（可能在工作中间触发）、上下文不聚焦、提取质量低

新方案 (plan-aware):
  todo 列表全部完成时 → 启动 nudge agent → _PLAN_EXTRACTION_PROMPT (Part 1 + Part 2)
  Part 1: 结构化知识 JSON 提取 (knowledge action="write")
  Part 2: skill 库更新 (skill_manage) — 原 _SKILL_REVIEW_PROMPT 指引完整保留
  优势: 时机精确（工作刚完成）、上下文结构化（plan + todos + ledger + subagent runs）、提取质量高
```

**核心原则**:

- Memory nudge（10-turn）**保留不变** — `_nudge_memory()` + `_MEMORY_REVIEW_PROMPT` 继续使用
- Skill nudge（10-turn）**触发机制废弃** — 删除 `_nudge_skill()`, `_nudge_combined()`, `_COMBINED_REVIEW_PROMPT`
- Skill 更新指引**合并进 plan extraction** — `_SKILL_REVIEW_PROMPT` 的内容融入 `_PLAN_EXTRACTION_PROMPT`，不单独删除
- Plan extraction（新增）— 替代 skill nudge 的触发机制，在 todo 全部完成时触发，同时做知识 JSON 提取 + skill 库更新

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Nudge 触发条件                                    │
├───────────────────────┬──────────────────────────────────────────────┤
│ Memory Nudge (保留)   │ Plan Extraction + Skill Update (新增)        │
│ 10-turn threshold     │ todo-list all-complete trigger                │
│ _nudge_memory()       │ _nudge_plan_extraction()                      │
│ _MEMORY_REVIEW_PROMPT │ _PLAN_EXTRACTION_PROMPT (含 skill 更新指引)   │
│ 保存到 memory store   │ 写入两处:                                      │
│                       │   ① .omo/knowledge/<plan-name>/*.json (JSON) │
│                       │   ② skills/ 目录 (SKILL.md + references/)     │
└───────────────────────┴──────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                     触发流程                                          │
├──────────────────────────────────────────────────────────────────────┤
│ 1. ContextEngineHook._after_agent_impl                               │
│    → 检查 memory counter (10-turn) → _nudge_memory()  (保留)        │
│    → 检查 todo-all-complete flag    → _nudge_plan_extraction() (新增) │
│ 2. _nudge_plan_extraction()                                          │
│    → _build_plan_context(session_id)                                  │
│      → 读取 plan 文件 (.omo/plans/*.md)                              │
│      → 读取 todos (todos.db)                                         │
│      → 读取 ledger (.omo/ledger.jsonl)                               │
│      → 读取 subagent runs (registry queries)                         │
│    → 启动 nudge agent (with _NudgeLimitTool)                         │
│    → nudge agent 同时执行两种操作:                                   │
│      ① 调用 knowledge(action="write") 写入 JSON 知识文件             │
│      ② 调用 skill_manage 更新 skill 库 (SKILL.md + references/)      │
│ 3. knowledge tool (action="write")                                    │
│    → knowledge_store.py 持久化到 .omo/knowledge/<plan-name>/*.json  │
│                                                                      │
│ ── 知识查询 (两层设计) ───────────────────────────────────────────── │
│ Tier 1: 系统提示词注入 (压缩免疫)                                    │
│   prompt_builder._build_knowledge_block()                            │
│   → 读 plan-summary.json → 注入 key_failures + key_successes         │
│   → ~20 行，每次重建系统提示词时从文件实时读取                       │
│ Tier 2: 工具按需查询 (详细)                                          │
│   knowledge(action="read", plan_name="...", layer="task", position=0)│
│   → 返回完整 failure_set/success_path/method                         │
│   → 主 agent 需要具体信息时主动调用，不占系统提示词                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 知识文件数据模型

### 存储结构

```
.omo/
  knowledge/
    <plan-name>/
      task-<position>.json      # 每个 todo item 一份
      wave-<index>.json          # 每个波次汇总一份
      plan-summary.json          # 整个计划的总结
```

### task-<position>.json — 单任务知识

```json
{
  "schema_version": 1,
  "plan_name": "implement-auth",
  "plan_path": ".omo/plans/implement-auth.md",
  "task": {
    "position": 0,
    "content": "[auth/login.py] [implement] to [user login] - expect [working login endpoint]",
    "category": "deep",
    "delegation": "subagent",
    "wave_index": 0,
    "status": "completed"
  },
  "subagent_runs": [
    {
      "task_name": "implement-login",
      "task_prompt": "implement login endpoint in auth/login.py...",
      "result_text": "Created auth/login.py with POST /login endpoint...",
      "outcome": "ok"
    }
  ],
  "failure_set": [
    "pytest tests/test_login.py::test_invalid_password failed: bcrypt hash mismatch",
    "initial approach used synchronous bcrypt — switched to passlib for async support"
  ],
  "success_path": [
    "1. Used passlib.context.CryptContext for async-compatible password hashing",
    "2. JWT token generation with python-jose, 30min expiry",
    "3. FastAPI dependency injection for token validation"
  ],
  "method": "async-password-hashing-with-passlib",
  "extracted_at": "2026-09-07T12:00:00Z"
}
```

### wave-<index>.json — 波次汇总知识

```json
{
  "schema_version": 1,
  "plan_name": "implement-auth",
  "wave_index": 0,
  "task_count": 3,
  "tasks_summary": [
    { "position": 0, "method": "async-password-hashing-with-passlib" },
    { "position": 1, "method": "jwt-token-with-python-jose" },
    { "position": 2, "method": "fastapi-dependency-injection" }
  ],
  "wave_failure_patterns": [
    "All tasks initially tried synchronous bcrypt — async approach needed passlib"
  ],
  "wave_success_patterns": [
    "Wave 0 completed successfully: auth foundation (hashing + JWT + dependency injection)"
  ],
  "extracted_at": "2026-09-07T12:05:00Z"
}
```

### plan-summary.json — 计划总结知识

```json
{
  "schema_version": 1,
  "plan_name": "implement-auth",
  "plan_path": ".omo/plans/implement-auth.md",
  "total_waves": 3,
  "total_tasks": 8,
  "overall_method": "fastapi-auth-stack-with-async-hashing",
  "key_failures": [
    "Synchronous bcrypt caused test failures — resolved with passlib async context",
    "JWT secret not loaded from env initially — fixed with pydantic-settings"
  ],
  "key_successes": [
    "Modular auth stack: passlib (hashing) + python-jose (JWT) + FastAPI deps",
    "All tests passing, typecheck clean, lint clean"
  ],
  "reusable_patterns": [
    "Async password hashing: passlib.context.CryptContext(schemes=['bcrypt'])",
    "JWT validation as FastAPI dependency: Depends(verify_token)",
    "Environment-based secret loading: pydantic-settings BaseSettings"
  ],
  "extracted_at": "2026-09-07T12:10:00Z"
}
```

### 字段语义

| 字段                | 说明                                                              |
| ------------------- | ----------------------------------------------------------------- |
| `failure_set`       | 失败模式集合：遇到的错误、错误的根本原因、尝试过的失败方案        |
| `success_path`      | 成功路径：有序步骤，描述最终成功的实现方式                        |
| `method`            | 提取的方法名（简短标识符），用于跨计划检索                        |
| `subagent_runs`     | 关联的 subagent 执行记录（仅 `result_text` + `outcome` + `task`） |
| `reusable_patterns` | 可复用模式：从整个计划中提取的通用化模式                          |

---

## 触发机制

### 当前机制 (core.py)

```python
# 现有: _after_agent_impl 中
nudge_review_memory_count = state_register_db.get_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0) + 1
nudge_review_skill_count = state_register_db.get_state(session_id, _NUDGE_SKILL_COUNT_KEY, 0)

need_nudge_review_memory = nudge_review_memory_count >= _NUDGE_MEMORY_THRESHOLD  # 10
need_nudge_skill_memory = nudge_review_skill_count >= _NUDGE_SKILL_THRESHOLD     # 10

# 触发:
if need_memory and need_skill:
    _nudge_combined(...)
else:
    if need_memory: _nudge_memory(...)
    if need_skill: _nudge_skill(...)
```

### 新机制

```python
# _after_agent_impl 中
# 1. Memory counter (保留不变)
nudge_review_memory_count = state_register_db.get_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0) + 1
need_nudge_review_memory = nudge_review_memory_count >= _NUDGE_MEMORY_THRESHOLD

# 2. Skill counter (废弃) → 替换为 todo-complete 检测
need_plan_extraction = _detect_todo_all_complete(session_id)

# 触发:
if need_nudge_review_memory:
    _nudge_memory(...)

if need_plan_extraction:
    _nudge_plan_extraction(...)
```

### todo-all-complete 检测

```python
_PLAN_EXTRACTION_FIRED_KEY = "nudge_plan_extraction_fired"

def _detect_todo_all_complete(session_id: str) -> bool:
    """检测 todo 列表是否刚刚全部完成（且本次未触发过提取）。

    条件:
    1. todos 存在（非空）
    2. 所有 todo 状态为 completed 或 cancelled
    3. 本次 plan extraction 尚未触发过（防重复）

    返回 True 后设置 _PLAN_EXTRACTION_FIRED_KEY = True，
    下次 todos 变为非全完成状态时重置为 False。
    """
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return False

    all_done = all(t["status"] in ("completed", "cancelled") for t in todos)
    if not all_done:
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
        return False

    already_fired = state_register_db.get_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
    if already_fired:
        return False

    state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, True)
    return True
```

### 与 skill counter 的关系

**废弃的计数器**:

- `_NUDGE_SKILL_COUNT_KEY` — 不再使用
- `_NUDGE_SKILL_THRESHOLD` — 不再使用
- `_wrap_tool_call_impl()` 中的 skill counter 递增 — 删除

**保留的计数器**:

- `_NUDGE_MEMORY_COUNT_KEY` — 保留
- `_NUDGE_MEMORY_THRESHOLD` — 保留 (10)

**新增的状态键**:

- `_PLAN_EXTRACTION_FIRED_KEY` — per-session，标记本次 plan extraction 是否已触发

---

## 计划上下文构建器

### _build_plan_context()

```python
def _build_plan_context(session_id: str) -> dict:
    """构建 plan extraction 的上下文数据。

    读取:
    1. Plan 文件 (.omo/plans/*.md 或 todos 中的 plan_ref)
    2. Todos (todos.db)
    3. Ledger (.omo/ledger.jsonl)
    4. Subagent runs (registry queries)

    返回:
    {
        "plan_name": str,
        "plan_path": str,
        "plan_content": str,
        "todos": list[dict],
        "ledger_entries": list[dict],
        "subagent_runs": list[dict],
    }
    """
    import json, os

    # 1. 读取 todos
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return {}

    # 2. 读取 plan 文件
    plan_path = ""
    plan_name = ""
    plan_content = ""
    # 从 todos 的 plan_ref 字段获取计划路径
    for t in todos:
        if t.get("plan_ref"):
            plan_path = t["plan_ref"]
            break

    if plan_path and os.path.exists(plan_path):
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_content = f.read()
        plan_name = os.path.splitext(os.path.basename(plan_path))[0]
    else:
        # 无 plan 文件，用 session_id 作为 plan_name
        plan_name = f"session-{session_id[:8]}"

    # 3. 读取 ledger
    ledger_entries = []
    ledger_path = ".omo/ledger.jsonl"
    if os.path.exists(ledger_path):
        with open(ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        entry = json.loads(line)
                        if entry.get("plan") == plan_path or entry.get("plan") == plan_name:
                            ledger_entries.append(entry)
                    except json.JSONDecodeError:
                        continue

    # 4. 读取 subagent runs
    # 只取 result_text + outcome + task，不取完整对话
    session_key = f"agent:main:session:{session_id}"
    subagent_runs = []
    try:
        from agent.tools.subagent.registry import list_runs_for_requester
        runs = list_runs_for_requester(session_key)
        for run in runs:
            outcome = run.execution.outcome
            subagent_runs.append({
                "task_name": run.task_name,
                "task": run.task,
                "result_text": run.completion.result_text,
                "outcome": outcome.status.value if outcome else "unknown",
                "error": outcome.error if outcome else None,
            })
    except Exception:
        pass

    return {
        "plan_name": plan_name,
        "plan_path": plan_path,
        "plan_content": plan_content,
        "todos": todos,
        "ledger_entries": ledger_entries,
        "subagent_runs": subagent_runs,
    }
```

### Subagent 数据限制

从 `SubagentRunRecord` 中只提取以下字段（不获取完整对话）:

| 提取字段         | 来源                                          |
| ---------------- | --------------------------------------------- |
| `task_name`      | `run.task_name`                               |
| `task` (prompt)  | `run.task`                                    |
| `result_text`    | `run.completion.result_text` (24KB 截断)      |
| `outcome.status` | `run.execution.outcome.status` (ok/error/...) |
| `outcome.error`  | `run.execution.outcome.error`                 |

**不提取**: 完整对话历史、tool calls、中间消息

### Registry 查询 API

| 方法                           | 文件           | 说明                           |
| ------------------------------ | -------------- | ------------------------------ |
| `list_runs_for_requester(key)` | `queries.py:7` | 返回该 session 发起的所有 runs |

---

## Extraction Prompt

`_PLAN_EXTRACTION_PROMPT` 由两部分组成：Part 1 知识 JSON 提取 + Part 2 skill 库更新（从原 `_SKILL_REVIEW_PROMPT` 移植）。

```python
_PLAN_EXTRACTION_PROMPT = """You are a knowledge extraction and skill maintenance specialist.
A plan has just been completed. Do TWO things:

## Part 1: Structured Knowledge Extraction

Extract knowledge from the completed plan below into JSON knowledge files.

### Context

{plan_context}

### Writing Style (CRITICAL)

ALL text fields must be CONCISE and DENSE:
- failure_set entries: one sentence per failure — root cause + what failed, no stack traces
- success_path entries: one sentence per step — verb + library/function name + why it works
- method: 3-5 words, kebab-case (e.g., "async-password-hashing-with-passlib")
- key_failures/key_successes (plan layer): one sentence each, max 150 chars
- reusable_patterns: one-liner code or config snippet, max 100 chars
- Strip error messages to the essential signal: "bcrypt hash mismatch" not the full traceback
- No narrative, no explanations of context, no "we decided to" — just the fact

BAD:  "We initially tried using synchronous bcrypt but it caused test failures because
       the hash function was blocking the event loop, so we switched to passlib"
GOOD: "Synchronous bcrypt blocks event loop — use passlib CryptContext for async"

### For EACH task, extract:

1. **failure_set**: What went wrong? Root cause + what failed, one sentence each.
   - Include file paths, command names, error strings (stripped to essential signal)
   - If no failures, use empty array []

2. **success_path**: Final successful approach. Ordered one-liner steps.
   - Verb + library/function name + why it works
   - If trivial, use ["straightforward implementation"]

3. **method**: 3-5 word kebab-case identifier (e.g., "async-password-hashing-with-passlib")

### For EACH WAVE, summarize (one-liner each):
- Common failure patterns across tasks
- Common success patterns across tasks

### For the PLAN as a whole (one-liner each, max 150 chars):
- Overall method/approach (3-5 words)
- Key failures (most impactful, max 5)
- Key successes (most reusable, max 5)
- Reusable patterns (one-liner code/config, max 8)

### Output (Part 1)

Write knowledge files using the knowledge tool with action='write':
- knowledge(action="write", plan_name="...", layer="task", position=0, data={...})
- knowledge(action="write", plan_name="...", layer="wave", wave_index=0, data={...})
- knowledge(action="write", plan_name="...", layer="plan", data={...})

Extract ALL layers. Be thorough — this knowledge will be queried in future sessions
to avoid repeating the same mistakes and to replicate successful approaches.

If the plan had no subagent runs and no notable patterns, still write the files
with empty failure_set and minimal success_path.

## Part 2: Skill Library Update

Be ACTIVE — most completed plans produce at least one skill update, even if
small. A pass that does nothing is a missed learning opportunity, not a
neutral outcome.

Target shape of the library: CLASS-LEVEL skills, each with a rich SKILL.md
and a `references/` directory for session-specific detail. Not a long flat
list of narrow one-session-one-skill entries. This shapes HOW you update,
not WHETHER you update.

### Signals to look for (any one warrants action):
  - User corrected your style, tone, format, legibility, or verbosity during
    the plan execution. Frustration signals like 'stop doing X', 'this is too
    verbose', 'don't format like this', 'why are you explaining', 'just give
    me the answer', or an explicit 'remember this' are FIRST-CLASS skill
    signals. Update the relevant skill(s) to embed the preference so the next
    session starts already knowing.
  - User corrected your workflow, approach, or sequence of steps. Encode the
    correction as a pitfall or explicit step in the skill that governs that
    class of task.
  - Non-trivial technique, fix, workaround, debugging path, or tool-usage
    pattern emerged from the subagent runs. Capture it.
  - A skill that got loaded or consulted during the plan turned out to be
    wrong, missing a step, or outdated. Patch it NOW.

### Preference order — prefer the earliest action that fits:
  1. UPDATE A CURRENTLY-LOADED SKILL. Look through the plan context for
     skills the user loaded or you read. If any covers the territory of
     the new learning, PATCH that one first.
  2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view). If no
     loaded skill fits but an existing class-level skill does, patch it.
     Add a subsection, a pitfall, or broaden a trigger.
  3. ADD A SUPPORT FILE under an existing umbrella. Three kinds:
       - `references/<topic>.md` — session-specific detail (error
         transcripts, reproduction recipes, provider quirks) AND condensed
         knowledge banks: quoted research, API docs, external authoritative
         excerpts, or domain notes. Write it concise and for the value of
         the task, not as a full mirror of upstream docs.
       - `templates/<name>.<ext>` — starter files meant to be copied and
         modified (boilerplate configs, scaffolding, a known-good example).
       - `scripts/<name>.<ext>` — statically re-runnable actions the skill
         can invoke directly (verification scripts, fixture generators,
         deterministic probes).
     Add support files via skill_manage action=write_file with file_path
     starting 'references/', 'templates/', or 'scripts/'. The umbrella's
     SKILL.md should gain a one-line pointer to any new support file.
  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing skill covers
     the class. The name MUST be at the class level — NOT a specific PR
     number, error string, feature codename, library-alone name, or
     'fix-X / debug-Y / audit-Z-today' session artifact. If the proposed
     name only makes sense for today's task, it's wrong — fall back to
     (1), (2), or (3).

### Do NOT capture as skills:
  - Environment-dependent failures: missing binaries, fresh-install errors,
    post-migration path mismatches, 'command not found', unconfigured
    credentials, uninstalled packages. The user can fix these — they are
    not durable rules.
  - Negative claims about tools or features ('browser tools do not work',
    'X tool is broken', 'cannot use Y from execute_code'). These harden
    into refusals the agent cites against itself for months after the
    actual problem was fixed.
  - Session-specific transient errors that resolved before the plan ended.
    If retrying worked, the lesson is the retry pattern, not the original
    failure.
  - One-off task narratives.

### User-preference embedding:
When the user expressed a style/format/workflow preference during the plan,
the update belongs in the SKILL.md body, not just in memory. Memory
captures 'who the user is and what the current situation and state of your
operations are'; skills capture 'how to do this class of task for this
user'. When they complain about how you handled a task, the skill that
governs that task needs to carry the lesson.

### Output (Part 2)

Use the skill_manage tool to update or create skills as described above.
Act on Part 2 only if there is real signal from the plan execution. If
genuinely nothing stands out, skip Part 2 and say 'No skill updates needed.'

## Important

The knowledge you write via the knowledge tool (action='write') is stored
as JSON files in .omo/knowledge/. In future sessions, you can query this
knowledge using the SAME tool with action='read' to look up failure_set,
success_path, and method from previously completed plans before starting
similar work. A condensed summary is also auto-injected into the system
prompt so you always know what previously failed/succeeded.
"""
```

---

## 知识查询策略 — 两层设计

```
Tier 1: 系统提示词注入 (压缩免疫)
  prompt_builder._build_knowledge_block()
  → 读 plan-summary.json
  → 注入 key_failures top 3-5 + key_successes top 3-5 + reusable_patterns
  → ~20 行，每次重建系统提示词时从文件实时读取
  → 压缩后 LLM 知道"之前什么方法失败了、什么成功了"

Tier 2: 工具按需查询 (详细)
  knowledge(action="read", plan_name="...", layer="task", position=0)
  → 返回完整 failure_set / success_path / method
  → 主 agent 在需要具体信息时主动调用
  → 不占系统提示词空间
```

**提取时告知 agent**：`_PLAN_EXTRACTION_PROMPT` 末尾告知 agent "知识已写入文件，未来可用 `knowledge(action='read')` 查询"。

---

## 工具层

### knowledge — 统一知识工具 (write + read + list)

- **新建文件**: `agent/tools/todolist/knowledge/knowledge_tool.py`

```python
from langchain_core.tools import tool
from typing import Literal

@tool("knowledge")
async def knowledge(
    action: Literal["write", "read", "list"],
    plan_name: str | None = None,
    layer: Literal["task", "wave", "plan"] | None = None,
    data: dict | None = None,
    position: int | None = None,
    wave_index: int | None = None,
) -> str:
    """Plan-aware knowledge store. Write during extraction, read before similar work.

    Actions:
      - write: Write knowledge data (nudge agent during plan extraction)
      - read:  Read knowledge for a plan (main agent, on-demand)
      - list:  List all plans that have knowledge files

    Read examples:
      knowledge(action="read", plan_name="implement-auth")
        → Returns plan summary + task index
      knowledge(action="read", plan_name="implement-auth", layer="task", position=0)
        → Returns full failure_set/success_path/method for task 0
      knowledge(action="read", plan_name="implement-auth", layer="wave", wave_index=0)
        → Returns wave-level failure/success patterns
      knowledge(action="read", plan_name="implement-auth", layer="plan")
        → Returns full plan summary

    Write examples:
      knowledge(action="write", plan_name="...", layer="task", position=0, data={...})
      knowledge(action="write", plan_name="...", layer="wave", wave_index=0, data={...})
      knowledge(action="write", plan_name="...", layer="plan", data={...})
    """
    from agent.tools.todolist.knowledge.knowledge_store import KnowledgeStore

    if action == "write":
        if not plan_name or not data or not layer:
            return "Error: write requires plan_name, layer, and data"
        path = await KnowledgeStore.write(
            layer=layer, plan_name=plan_name, data=data,
            position=position, wave_index=wave_index,
        )
        return f"Knowledge written to {path}"

    elif action == "read":
        if not plan_name:
            return "Error: read requires plan_name"
        return KnowledgeStore.read_formatted(
            plan_name=plan_name, layer=layer,
            position=position, wave_index=wave_index,
        )

    elif action == "list":
        plans = KnowledgeStore.list_plans()
        if not plans:
            return "No knowledge files found."
        lines = ["Available plans with knowledge:"]
        for p in plans:
            summary = KnowledgeStore.read_summary(p)
            if summary:
                method = summary.get("method", "unknown")
                lines.append(f"  - {p} (method: {method})")
            else:
                lines.append(f"  - {p}")
        return "\n".join(lines)

    return f"Unknown action: {action}"
```

### knowledge_store.py — 持久化 + 格式化读取

- **新建文件**: `agent/tools/todolist/knowledge/knowledge_store.py`

```python
"""Plan-aware knowledge storage.

Knowledge files are sidecar JSON files in .omo/knowledge/<plan-name>/.
Plan file and todos.db are read-only — never modified by nudge.
"""

import json
import os
from datetime import datetime
from typing import Literal

_KNOWLEDGE_ROOT = ".omo/knowledge"


class KnowledgeStore:

    @staticmethod
    async def write(
        layer: Literal["task", "wave", "plan"],
        plan_name: str,
        data: dict,
        position: int | None = None,
        wave_index: int | None = None,
    ) -> str:
        dir_path = os.path.join(_KNOWLEDGE_ROOT, plan_name)
        os.makedirs(dir_path, exist_ok=True)

        data["schema_version"] = 1
        data["plan_name"] = plan_name
        data["extracted_at"] = datetime.utcnow().isoformat()

        if layer == "task":
            assert position is not None, "position required for task layer"
            filename = f"task-{position}.json"
        elif layer == "wave":
            assert wave_index is not None, "wave_index required for wave layer"
            filename = f"wave-{wave_index}.json"
        elif layer == "plan":
            filename = "plan-summary.json"
        else:
            raise ValueError(f"Unknown layer: {layer}")

        file_path = os.path.join(dir_path, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return file_path

    @staticmethod
    def read_all(plan_name: str) -> dict:
        """Read all knowledge files for a plan (raw dict)."""
        dir_path = os.path.join(_KNOWLEDGE_ROOT, plan_name)
        if not os.path.exists(dir_path):
            return {}
        result = {"tasks": {}, "waves": {}, "plan": None}
        for filename in os.listdir(dir_path):
            file_path = os.path.join(dir_path, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if filename.startswith("task-"):
                pos = int(filename.replace("task-", "").replace(".json", ""))
                result["tasks"][pos] = data
            elif filename.startswith("wave-"):
                idx = int(filename.replace("wave-", "").replace(".json", ""))
                result["waves"][idx] = data
            elif filename == "plan-summary.json":
                result["plan"] = data
        return result

    @staticmethod
    def read_summary(plan_name: str) -> dict | None:
        """Read only plan-summary.json."""
        path = os.path.join(_KNOWLEDGE_ROOT, plan_name, "plan-summary.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def read_formatted(
        plan_name: str,
        layer: str | None = None,
        position: int | None = None,
        wave_index: int | None = None,
    ) -> str:
        """Read knowledge as formatted text for tool output.

        - No layer: return plan summary + task index (overview)
        - layer="plan": return full plan summary
        - layer="task" + position: return full task detail
        - layer="wave" + wave_index: return full wave detail
        """
        all_data = KnowledgeStore.read_all(plan_name)
        if not all_data:
            return f"No knowledge found for plan: {plan_name}"

        # Full plan summary
        if layer == "plan" or layer is None:
            p = all_data.get("plan")
            if not p:
                return f"No plan summary found for: {plan_name}"
            lines = [f"# Knowledge: {plan_name}"]
            lines.append(f"Method: {p.get('method', 'unknown')}")
            lines.append(f"Total waves: {p.get('total_waves', '?')}")
            lines.append(f"Total tasks: {p.get('total_tasks', '?')}")

            kfs = p.get("key_failures", [])
            if kfs:
                lines.append("\n## Key Failures")
                for i, f in enumerate(kfs[:5], 1):
                    lines.append(f"  {i}. {f}")

            kss = p.get("key_successes", [])
            if kss:
                lines.append("\n## Key Successes")
                for i, s in enumerate(kss[:5], 1):
                    lines.append(f"  {i}. {s}")

            rps = p.get("reusable_patterns", [])
            if rps:
                lines.append("\n## Reusable Patterns")
                for rp in rps:
                    lines.append(f"  - {rp}")

            if layer == "plan":
                return "\n".join(lines)

            # Overview: append task index
            tasks = all_data.get("tasks", {})
            if tasks:
                lines.append("\n## Task Index")
                for pos in sorted(tasks.keys()):
                    t = tasks[pos]
                    m = t.get("method", "unknown")
                    fs = len(t.get("failure_set", []))
                    sp = len(t.get("success_path", []))
                    lines.append(f"  Task {pos}: method={m} (failures={fs}, steps={sp})")
                lines.append("\nUse knowledge(action='read', plan_name='{plan_name}', "
                             "layer='task', position=N) for task detail.".format(
                                 plan_name=plan_name))
            return "\n".join(lines)

        # Task detail
        if layer == "task":
            if position is None:
                return "Error: layer='task' requires position"
            t = all_data.get("tasks", {}).get(position)
            if not t:
                return f"No knowledge found for task {position} in {plan_name}"
            lines = [f"# Task {position} Knowledge: {plan_name}"]
            lines.append(f"Method: {t.get('method', 'unknown')}")

            fs = t.get("failure_set", [])
            if fs:
                lines.append("\n## Failure Set")
                for i, f in enumerate(fs, 1):
                    lines.append(f"  {i}. {f}")
            else:
                lines.append("\n## Failure Set: (none)")

            sp = t.get("success_path", [])
            if sp:
                lines.append("\n## Success Path")
                for i, s in enumerate(sp, 1):
                    lines.append(f"  {i}. {s}")
            else:
                lines.append("\n## Success Path: (none)")

            runs = t.get("subagent_runs", [])
            if runs:
                lines.append("\n## Subagent Runs")
                for r in runs:
                    lines.append(f"  - {r.get('task_name', '?')}: "
                                 f"outcome={r.get('outcome', '?')}")

            return "\n".join(lines)

        # Wave detail
        if layer == "wave":
            if wave_index is None:
                return "Error: layer='wave' requires wave_index"
            w = all_data.get("waves", {}).get(wave_index)
            if not w:
                return f"No knowledge found for wave {wave_index} in {plan_name}"
            lines = [f"# Wave {wave_index} Knowledge: {plan_name}"]
            lines.append(f"Task count: {w.get('task_count', '?')}")

            wfp = w.get("wave_failure_patterns", [])
            if wfp:
                lines.append("\n## Failure Patterns")
                for i, f in enumerate(wfp, 1):
                    lines.append(f"  {i}. {f}")

            wsp = w.get("wave_success_patterns", [])
            if wsp:
                lines.append("\n## Success Patterns")
                for i, s in enumerate(wsp, 1):
                    lines.append(f"  {i}. {s}")

            ts = w.get("tasks_summary", [])
            if ts:
                lines.append("\n## Tasks Summary")
                for t in ts:
                    lines.append(f"  - Task {t.get('position')}: "
                                 f"method={t.get('method', '?')}")

            return "\n".join(lines)

        return f"Unknown layer: {layer}"

    @staticmethod
    def list_plans() -> list[str]:
        """List all plan names that have knowledge files."""
        if not os.path.exists(_KNOWLEDGE_ROOT):
            return []
        return sorted(os.listdir(_KNOWLEDGE_ROOT))
```

### **init**.py — 工具注册

- **新建文件**: `agent/tools/todolist/knowledge/__init__.py`

```python
from .knowledge_tool import knowledge
from .knowledge_store import KnowledgeStore

_KNOWLEDGE_TOOLS = [knowledge]

def build_knowledge_tools() -> list:
    tools = list(_KNOWLEDGE_TOOLS)
    for t in tools:
        t.metadata = {"nudge": True, "scope": "main_only"}
    return tools
```

> `nudge: True` metadata 的作用：
>
> - Nudge agent（有 `_NudgeLimitTool`）：`action="write"` 通过检查 ✅
> - Main agent（无 `_NudgeLimitTool`）：`action="read"` 随时可用，不受限制
> - Main agent 也可以 `action="write"`（无硬阻断），但提取时机由 `_detect_todo_all_complete()` 控制

---

## nudge.py 修改

### 删除

```python
# 删除以下常量和函数:
_COMBINED_REVIEW_PROMPT = ...     # ~75 行，删除（不再有 combined 场景）
_nudge_skill()                    # ~12 行，删除（功能合并进 _nudge_plan_extraction）
_nudge_combined()                 # ~15 行，删除（不再有 combined 场景）
```

### 内容合并（不删除，移入新 prompt）

```python
# _SKILL_REVIEW_PROMPT 的内容（~95 行 skill 更新指引）不删除，
# 而是融入 _PLAN_EXTRACTION_PROMPT 的 Part 2。
# 原 _SKILL_REVIEW_PROMPT 常量本身删除（内容已在 _PLAN_EXTRACTION_PROMPT 中），
# 但其指引逻辑完整保留在新 prompt 的 Part 2 章节中。
```

### 保留

```python
# 保留:
_MEMORY_REVIEW_PROMPT            # 不变
_nudge_memory()                  # 不变
_NudgeLimitTool                  # 不变（knowledge 和 skill_manage 都有 nudge:True metadata）
_create_nudge_agent()            # 不变
```

### 新增

```python
_PLAN_EXTRACTION_PROMPT = """..."""  # 见上方 Extraction Prompt 章节

_PLAN_EXTRACTION_LOCK_KEY = "nudge_plan_extraction_lock"


async def _nudge_plan_extraction(
    session_id: str,
    system_prompt: str,
    messages: list[BaseMessage],
) -> None:
    """Plan-aware knowledge extraction + skill library update.

    Triggered when all todos are complete. Builds plan context
    (plan file + todos + ledger + subagent runs), then launches nudge
    agent with _PLAN_EXTRACTION_PROMPT (Part 1: JSON knowledge extraction
    via knowledge(action="write"); Part 2: skill library update via skill_manage).
    Both tools have nudge:True metadata and pass _NudgeLimitTool.
    """
    state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, True)
    try:
        context = _build_plan_context(session_id)
        if not context:
            logger.debug("Plan extraction: no plan context for session {}", session_id)
            return

        context_str = json.dumps(context, ensure_ascii=False, indent=2)
        prompt = _PLAN_EXTRACTION_PROMPT.format(plan_context=context_str)

        _agent = await _create_nudge_agent(system_prompt)
        res = await _agent.ainvoke(
            input={
                "session_id": session_id,
                "messages": [*messages, HumanMessage(content=prompt)],
            }
        )
        logger.debug("plan extraction res is {}", res["messages"][-1])
    finally:
        state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False)
```

### _build_plan_context() 位置

`_build_plan_context()` 放在 `nudge.py` 中（作为模块级函数），因为它需要：

1. 读取 plan 文件 + todos.db + ledger.jsonl（文件系统操作）
2. 调用 subagent registry queries（需要 import）
3. 构建 JSON 字符串传给 nudge prompt

---

## core.py 修改

### 删除

```python
# 删除:
_NUDGE_SKILL_COUNT_KEY = "nudge_review_skill_count"      # 不再使用
_NUDGE_SKILL_THRESHOLD = 10                                # 不再使用
_NUDGE_SKILL_LOCK_KEY = "nudge_review_skill_lock"         # 不再使用

# 删除 import:
from .nudge import _nudge_memory, _nudge_skill, _nudge_combined
# 改为:
from .nudge import _nudge_memory, _nudge_plan_extraction

# 删除 _wrap_tool_call_impl 中的 skill counter 逻辑:
def _wrap_tool_call_impl(self, request):
    # 整个方法删除（只用于 skill counter 递增）
    # wrap_tool_call / awrap_tool_call 也删除（如果只有 skill counter 用途）
```

### 修改

```python
# 新增:
_PLAN_EXTRACTION_FIRED_KEY = "nudge_plan_extraction_fired"
_PLAN_EXTRACTION_LOCK_KEY = "nudge_plan_extraction_lock"

# _is_lock 修改:
@staticmethod
def _is_lock(session_id: str) -> bool:
    return state_register_mem.get_state(
        session_id, _NUDGE_MEMORY_LOCK_KEY, False
    ) or state_register_mem.get_state(
        session_id, _PLAN_EXTRACTION_LOCK_KEY, False
    )

# _after_agent_impl 修改:
def _after_agent_impl(self, state):
    session_id = state.get("session_id", "")
    if session_id.strip() == "":
        raise RuntimeError("Not pass session_id")

    messages = cast("list[BaseMessage]", state["messages"])
    system_prompt = self._get_and_reload_system_prompt(session_id)

    # Memory counter (保留不变)
    nudge_review_memory_count = (
        state_register_db.get_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0) + 1
    )

    if self._is_lock(session_id):
        state_register_db.set_state(
            session_id, _NUDGE_MEMORY_COUNT_KEY, nudge_review_memory_count
        )
        return None

    need_nudge_review_memory = nudge_review_memory_count >= _NUDGE_MEMORY_THRESHOLD
    need_plan_extraction = _detect_todo_all_complete(session_id)

    if need_nudge_review_memory:
        state_register_db.set_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0)
    else:
        state_register_db.set_state(
            session_id, _NUDGE_MEMORY_COUNT_KEY, nudge_review_memory_count
        )

    return (
        session_id,
        system_prompt,
        messages,
        need_nudge_review_memory,
        need_plan_extraction,  # 替换 need_skill
    )

# after_agent / aafter_agent 修改:
async def aafter_agent(self, state, runtime):
    result = self._after_agent_impl(state)
    if result is None:
        return None

    session_id, system_prompt, messages, need_memory, need_plan_extraction = result

    # Persist (保留不变)
    last_turn_messages = slice_last_turn(all_messages)["messages"]
    last_turn_messages = _reconcile_denials_for_persistence(last_turn_messages)
    format_last_turn_messages = sanitize_tool_use_result_pairing(last_turn_messages)
    nudge_messages = sanitize_tool_use_result_pairing(messages)

    async def _persist():
        await add_messages(session_id=session_id, messages=format_last_turn_messages)

    async def _nudge():
        if need_memory:
            await _nudge_memory(session_id, system_prompt, nudge_messages)
        if need_plan_extraction:
            await _nudge_plan_extraction(session_id, system_prompt, nudge_messages)

    await asyncio.gather(_persist(), _nudge())
    return None
```

### _detect_todo_all_complete()

放在 `core.py` 中（模块级函数），或放在 `nudge.py` 中由 `core.py` import:

```python
def _detect_todo_all_complete(session_id: str) -> bool:
    """检测 todo 列表是否刚刚全部完成。"""
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync
        todos = get_todos_sync(session_id)
    except Exception:
        return False

    if not todos:
        return False

    all_done = all(t["status"] in ("completed", "cancelled") for t in todos)
    if not all_done:
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
        return False

    already_fired = state_register_db.get_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
    if already_fired:
        return False

    state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, True)
    return True
```

---

## service.py 修改 (TODOLIST_PLAN.md 中的 TodoService)

### 全部完成检测

TodoService 已在 TODOLIST_PLAN.md 中定义。`_detect_todo_all_complete()` 直接读取 `store_sqlite.get_todos_sync()`，不需要 service 层额外修改。

### 非计划对话

非计划对话（没有 plan 文件、没有 todos）不会触发 plan extraction：

- `_detect_todo_all_complete()` 返回 False（todos 为空）
- 即使有 todos 但无 `plan_ref`，`_build_plan_context()` 仍能工作（用 session_id 作为 plan_name）

**用户确认**: 非计划对话不做 skill 提取是可接受的。

---

## Tier 1: 系统提示词注入 — _build_knowledge_block()

- **修改文件**: `workspace/prompt_builder.py`
- **代码量**: ~25 行
- **阶段**: Phase C

从 `plan-summary.json` 读取精简摘要注入系统提示词。每次重建系统提示词（含压缩后）时从文件实时读取，天然免疫压缩。

```python
def _build_knowledge_block(session_id: str) -> str:
    """注入当前 plan 的知识摘要（压缩免疫）。

    从 .omo/knowledge/<plan-name>/plan-summary.json 读取:
    - key_failures top 3-5
    - key_successes top 3-5
    - reusable_patterns

    完整明细通过 knowledge(action="read") 工具按需查询。
    """
    import json, os

    # 从 todos 的 plan_ref 获取当前 plan_name
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync
        todos = get_todos_sync(session_id)
        plan_ref = ""
        for t in todos:
            if t.get("plan_ref"):
                plan_ref = t["plan_ref"]
                break
        if not plan_ref:
            return ""
        plan_name = os.path.splitext(os.path.basename(plan_ref))[0]
    except Exception:
        return ""

    summary_path = os.path.join(".omo", "knowledge", plan_name, "plan-summary.json")
    if not os.path.exists(summary_path):
        return ""

    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
    except Exception:
        return ""

    lines = [f"## Knowledge Summary: {plan_name}"]
    lines.append(f"Method: {summary.get('method', 'unknown')}")

    kfs = summary.get("key_failures", [])[:5]
    if kfs:
        lines.append("Key Failures (avoid repeating):")
        for i, f in enumerate(kfs, 1):
            lines.append(f"  {i}. {_trunc(f, 200)}")

    kss = summary.get("key_successes", [])[:5]
    if kss:
        lines.append("Key Successes:")
        for i, s in enumerate(kss, 1):
            lines.append(f"  {i}. {_trunc(s, 200)}")

    rps = summary.get("reusable_patterns", [])[:8]
    if rps:
        lines.append("Reusable Patterns:")
        for rp in rps:
            lines.append(f"  - {_trunc(rp, 150)}")

    lines.append("Use knowledge(action='read') tool for detailed failure_set/success_path.")
    return "\n".join(lines)


def _trunc(s: str, max_len: int = 200) -> str:
    """截断字符串，超长追加省略号。"""
    s = s.replace("\n", " ").strip()
    return s[:max_len] + "..." if len(s) > max_len else s
```

### 在 prompt_builder.py 中调用

```python
def build_system_prompt(session_id: str, ...) -> str:
    ...
    parts = [
        _read_static_files(...),     # AGENTS.md
        _build_skills_block(...),     # <available_skills>
        _build_todo_block(session_id),        # 当前 todo 列表
        _build_boulder_block(session_id),    # boulder 工作状态
        _build_knowledge_block(session_id),  # ← 新增: 知识摘要
    ]
    return "\n\n".join(p for p in parts if p)
```

### 长度限制

`_build_knowledge_block()` 的上下文开销有硬上限：

| 字段              | 数量上限 | 单条截断  | 最大行数                             |
| ----------------- | -------- | --------- | ------------------------------------ |
| method            | 1        | 200 chars | 1                                    |
| key_failures      | 5        | 200 chars | 5                                    |
| key_successes     | 5        | 200 chars | 5                                    |
| reusable_patterns | 8        | 150 chars | 8                                    |
| footer            | 1        | —         | 1                                    |
| **总计**          |          |           | **~20 行, ~3000 chars, ~700 tokens** |

- 不跨计划累积：只读当前 plan_ref 对应的 plan-summary.json
- 每条字符串经 `_trunc()` 截断（换行转空格 + 超长省略）
- 完整明细（未截断的 failure_set/success_path）通过 Tier 2 工具查询

### 为什么不全量注入

| 方案                              | 上下文开销          | 压缩免疫 | 信息完整度        |
| --------------------------------- | ------------------- | -------- | ----------------- |
| 全量注入所有 task/wave/plan JSON  | ~500+ 行            | ✅       | 100%              |
| 仅注入 plan-summary 摘要 (Tier 1) | ~20 行 (硬上限)     | ✅       | 60% (关键信息)    |
| 工具按需查询 (Tier 2)             | 0 行（不占提示词）  | ✅       | 100% (需主动调用) |
| **两层组合**                      | **~20 行 (硬上限)** | **✅**   | **100%**          |

---

## NudgeLimitTool 与 plan extraction

`_NudgeLimitTool` 已存在于 `nudge.py`，在 plan extraction 期间同样生效：

- 只允许 `metadata.nudge == True` 的工具执行
- `knowledge` 设置 `metadata = {"nudge": True, "scope": "main_only"}` → nudge agent 可 write ✅
- `skill_manage` 已有 `metadata = {"nudge": True}` → 可用（Part 2 skill 库更新）
- 其他工具（如 `read_file`, `terminal` 等）若无 `nudge: True` → 被拦截

**主 agent vs nudge agent 对 `knowledge` 工具的访问**:

| Agent       | `_NudgeLimitTool` | `action="write"`                                      | `action="read"` | `action="list"` |
| ----------- | ----------------- | ----------------------------------------------------- | --------------- | --------------- |
| Main agent  | ❌ 未注册         | 可用（但提取时机由 `_detect_todo_all_complete` 控制） | ✅ 随时可用     | ✅ 随时可用     |
| Nudge agent | ✅ 已注册         | ✅ 通过检查                                           | ✅ 通过检查     | ✅ 通过检查     |

**nudge agent 可用的工具**:

| 工具           | nudge:True | 用途                                      |
| -------------- | ---------- | ----------------------------------------- |
| `knowledge`    | ✅         | 写 JSON 知识文件 (Part 1, action="write") |
| `skill_manage` | ✅         | 更新 SKILL.md + references/ (Part 2)      |
| `skill_list`   | ✅         | 查找现有 skill (Part 2 步骤 2)            |
| `skill_view`   | ✅         | 查看现有 skill 内容 (Part 2)              |
| `memory`       | ✅         | 如有 memory 信号也可写                    |

**memory nudge 和 plan extraction 并发**:

- 如果 memory nudge 和 plan extraction 同时触发，它们各自启动独立的 nudge agent
- 两者都受 `_NudgeLimitTool` 限制
- 两者都通过 `_is_lock()` 检查避免重复（不同 lock key）

---

## 完整文件清单

| #   | 操作 | 文件路径                                            | 代码量                   | 说明                                                                                                                                                                                                               |
| --- | ---- | --------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | 新建 | `agent/tools/todolist/knowledge/__init__.py`        | ~10 行                   | 工具注册 + builder                                                                                                                                                                                                 |
| 2   | 新建 | `agent/tools/todolist/knowledge/knowledge_store.py` | ~130 行                  | JSON 读写 + 格式化读取 + 目录管理 + read_summary()                                                                                                                                                                 |
| 3   | 新建 | `agent/tools/todolist/knowledge/knowledge_tool.py`  | ~60 行                   | 统一 @tool 定义 (action="write"/"read"/"list")                                                                                                                                                                     |
| 4   | 修改 | `agent/tools/__init__.py`                           | ~5 行                    | 注册 build_knowledge_tools                                                                                                                                                                                         |
| 5   | 修改 | `agent/middlewares/context_engine/nudge.py`         | ~160 行新增, ~100 行删除 | 新增 _PLAN_EXTRACTION_PROMPT (含 skill 更新指引 + read 提示) + _nudge_plan_extraction + _build_plan_context; 删除 _COMBINED_REVIEW_PROMPT + _nudge_skill + _nudge_combined; _SKILL_REVIEW_PROMPT 内容移入新 prompt |
| 6   | 修改 | `agent/middlewares/context_engine/core.py`          | ~40 行修改, ~30 行删除   | 替换 skill counter 为 todo-complete 检测; 删除 _NUDGE_SKILL_* 常量; 修改 _after_agent_impl; 修改 aafter_agent; 新增 _detect_todo_all_complete                                                                      |
| 7   | 修改 | `workspace/prompt_builder.py`                       | ~25 行新增               | 新增 _build_knowledge_block() — Tier 1 系统提示词注入 plan-summary.json 精简摘要                                                                                                                                   |

---

## 实现顺序

```
Phase A: 知识存储 + 工具 (可与 TODOLIST_PLAN Phase 1 并行)
  1. knowledge_store.py — KnowledgeStore 类 (write + read_all + read_summary + read_formatted + list_plans)
  2. knowledge_tool.py — 统一 @tool 定义 (action="write"/"read"/"list")
  3. knowledge/__init__.py — builder
  4. agent/tools/__init__.py — 注册

Phase B: Nudge 重构 (依赖 TODOLIST_PLAN Phase 1 的 store_sqlite.py)
  5. nudge.py — 新增 _PLAN_EXTRACTION_PROMPT (Part 1 知识提取 + Part 2 skill 更新指引 + read 提示) + _build_plan_context + _nudge_plan_extraction
  6. nudge.py — 删除 _COMBINED_REVIEW_PROMPT + _nudge_skill + _nudge_combined (_SKILL_REVIEW_PROMPT 内容已在步骤 5 移入新 prompt)
  7. core.py — 删除 _NUDGE_SKILL_* 常量 + _wrap_tool_call_impl
  8. core.py — 新增 _PLAN_EXTRACTION_FIRED_KEY + _detect_todo_all_complete
  9. core.py — 修改 _after_agent_impl + after_agent + aafter_agent

Phase C: 系统提示词注入 (Tier 1, 依赖 Phase A 的 knowledge_store.py)
  10. prompt_builder.py — 新增 _build_knowledge_block() — 读 plan-summary.json 注入精简摘要
```

**依赖关系**: Phase B 步骤 5-9 依赖 `store_sqlite.get_todos_sync()` 存在（TODOLIST_PLAN Phase 1 步骤 1）。

---

## 关键设计决策

| 决策               | 选择                                                    | 理由                                            |
| ------------------ | ------------------------------------------------------- | ----------------------------------------------- |
| 触发时机           | todo-list all-complete                                  | 时机精确：工作刚完成，上下文完整                |
| 存储格式           | JSON (知识) + Markdown (skill)                          | JSON 程序化查询；SKILL.md 保持人类可读          |
| 存储位置           | ① .omo/knowledge/<plan-name>/*.json ② skills/           | 知识 sidecar 只读；skill 库可加载进系统提示词   |
| 知识层级           | 3 层: task / wave / plan                                | 对应 HTN 的三层：原子任务 / 并行波次 / 整体计划 |
| failure_set 字段   | 错误 + 根因 + 失败方案                                  | 未来避免重复同样的错误                          |
| success_path 字段  | 有序步骤 + 具体库名/函数名                              | 未来可复制成功路径                              |
| method 字段        | 短标识符                                                | 跨计划检索                                      |
| subagent 数据限制  | 仅 result_text + outcome + task                         | 避免上下文爆炸，聚焦结果而非过程                |
| 非计划对话         | 不做 skill 提取                                         | 无结构化上下文可提取，质量不可控                |
| memory nudge       | 保留不变 (10-turn threshold)                            | memory 提取与 plan 无关，继续按 turn 触发       |
| skill 更新指引     | 合并进 _PLAN_EXTRACTION_PROMPT Part 2                   | 原 _SKILL_REVIEW_PROMPT 内容完整保留，不丢弃    |
| _NudgeLimitTool    | 保留不变                                                | plan extraction 受限于 nudge-only tools         |
| skill counter 废弃 | 删除 _NUDGE_SKILL_COUNT_KEY + _wrap_tool_call_impl      | 不再按 tool call 计数触发 skill 提取            |
| 工具统一           | 单一 `knowledge` 工具 (action 区分 write/read/list)     | 提取和查询用同一工具，传参区分，减少工具数量    |
| 知识查询策略       | 两层: Tier 1 系统提示词注入摘要 + Tier 2 工具按需查详细 | 摘要压缩免疫(~20行)；完整明细不占提示词空间     |

---

## 与旧方案的对比

| 维度           | 旧方案 (10-turn skill nudge)          | 新方案 (plan-aware extraction)                 |
| -------------- | ------------------------------------- | ---------------------------------------------- |
| 触发时机       | 每 10 次 tool call                    | todo 列表全部完成时                            |
| 上下文         | 全对话历史                            | plan + todos + ledger + subagent runs (结构化) |
| 提取目标       | skill 库更新 (SKILL.md + references/) | ① JSON 知识文件 + ② skill 库更新 (两部分合并)  |
| 存储格式       | Markdown (SKILL.md)                   | ① JSON 程序化查询 + ② Markdown (SKILL.md)      |
| 存储位置       | skills/ 目录                          | ① .omo/knowledge/<plan-name>/ ② skills/ 目录   |
| skill 更新指引 | 独立 _SKILL_REVIEW_PROMPT             | 合并进 _PLAN_EXTRACTION_PROMPT Part 2          |
| 提取质量       | 低（时机不对、上下文不聚焦）          | 高（时机精确、上下文结构化）                   |
| 非计划对话     | 仍然触发（每 10 turn）                | 不触发（无 todos = 无提取）                    |
| memory nudge   | 每 10 turn 触发                       | 不变（每 10 turn 触发）                        |
| skill nudge    | 每 10 turn 触发                       | 废弃（合并进 plan extraction）                 |
| combined nudge | memory + skill 同时触发               | 废弃（memory 和 plan extraction 独立触发）     |
| 工具           | skill_manage (独立)                   | 统一 knowledge 工具 (action 区分 write/read)   |
| 压缩后知识可见 | ❌ 无注入                             | ✅ Tier 1 系统提示词注入摘要 (~20行)           |
| 压缩后知识查询 | 需手动 read_file                      | ✅ knowledge(action="read") 工具按需查询       |

---

## 边界情况

| 场景                                    | 行为                                                                          |
| --------------------------------------- | ----------------------------------------------------------------------------- |
| 无 plan 文件但有 todos                  | `_build_plan_context()` 用 `session-<id>` 作为 plan_name，仍提取              |
| todos 为空                              | `_detect_todo_all_complete()` 返回 False，不触发                              |
| todos 全部 completed 但无 subagent runs | 仍提取（subagent_runs 为空数组，提取 failure_set/success_path 从 todos 推断） |
| todos 部分 cancelled 部分 completed     | 视为 all-complete（cancelled 也是终态），触发提取                             |
| plan extraction 已触发 + 新的 todos     | `_PLAN_EXTRACTION_FIRED_KEY` 重置：todos 变为非全完成时重置为 False           |
| memory + plan extraction 同时触发       | 各自启动独立 nudge agent，各自有独立 lock key                                 |
| nudge agent 失败                        | 异常被 catch，不影响主流程（与现有 _nudge_memory 行为一致）                   |
| plan 文件不存在但 todos 有 plan_ref     | `plan_content` 为空字符串，仍提取（从 todos + subagent runs 推断）            |
