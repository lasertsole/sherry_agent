# Planning & Discipline Layer on top of TaskFlow

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> A session-scoped, lightweight planning and enforcement layer built on top of the existing TaskFlow system. It is **not a task engine**, **not a DAG**, and **does not implement a scheduler**. Session-level plans are managed by `todowrite` / `todoread` (compression-immune via system prompt injection). Cross-session, durable, subagent-dispatching execution is owned by **TaskFlow** (`depends_on`, `blocked/ready/dispatched/done`, `taskflow_dispatch`, `taskflow_wait_all`). The two layers are linked by `flow_id` / `step_id`; a single state has one authoritative source — no cross-layer duplication.

---

## Table of Contents

- [Positioning: Relationship with TaskFlow](#positioning-relationship-with-taskflow)
- [Design Philosophy & Core Principles](#design-philosophy--core-principles)
- [Architecture Overview](#architecture-overview)
- [Data Layer](#data-layer)
- [Service Layer](#service-layer)
- [Tool Layer](#tool-layer)
- [Orchestration Execution Layer](#orchestration-execution-layer)
- [Compression Protection Layer](#compression-protection-layer)
- [Enforcement Layer E1–E7](#enforcement-layer-e1e7)
- [Frontend](#frontend)

---

## Positioning: Relationship with TaskFlow

### Two-Layer Division of Labor

| Dimension   | Session Plan / Checklist                                                | Execution Flow (DAG)                                                                      |
| ----------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Owner       | Planning & Discipline Layer (this doc)                                  | TaskFlow (existing system)                                                                |
| Scope       | Single session, lightweight                                             | Cross-session, durable                                                                    |
| State       | todo `content/status/priority/category/delegation`                      | step `depends_on/status/child_session_key/results`                                        |
| Persistence | `todos.db` (session-level)                                              | `taskflow_registry.db` + flow `state_json`                                                |
| Injection   | System prompt block (rebuilt after every compression, naturally immune) | Tool return text (`taskflow_summary` re-read)                                             |
| Main duty   | Planning, visibility, discipline enforcement (E1–E7)                    | Dependency satisfaction, block/unlock, parallel dispatch, bounded wait, result injection  |
| Scheduling? | No                                                                      | Yes (`taskflow_run_task` / `taskflow_dispatch` / `taskflow_wait_all` / `taskflow_resume`) |

One-liner: **the planning & discipline layer is responsible for "think clearly, keep track, force completion"; TaskFlow is responsible for "actually run, cross-session, manage dependencies".**

### Boundary: What Lives Where

| Concern                                     | Authority             | Notes                                                               |
| ------------------------------------------- | --------------------- | ------------------------------------------------------------------- |
| Whether a todo is complete                  | `todos.db`            | Only `todowrite` can change todo status                             |
| Whether a step is complete / deps satisfied | TaskFlow `state_json` | Only `taskflow_resume` can mark a step `done` and unlock successors |
| Whether a child session is still running    | subagent registry     | `get_run_by_child_session_key` + `is_live_unended_run`              |
| Plan file progress (checkboxes)             | `.omo/plans/*.md`     | Orchestrator edits `- [ ]` → `- [x]`                                |
| Execution evidence                          | `.omo/ledger.jsonl`   | `EvidenceLedger` appends                                            |
| Active work state                           | `.omo/boulder.json`   | Plan activation/recovery                                            |

### Association (Single Source of Truth)

A todo may carry two optional fields pointing to TaskFlow:

- `flow_id`: the linked TaskFlow flow id.
- `step_id`: the linked TaskFlow step id (e.g. `step-2`), for re-reading that step's DAG status.

The **no-mirroring** principle:

- The todo layer does **not** copy `depends_on`, does **not** store waves, does **not** cache step status.
- When DAG status is needed, call the read-only `taskflow_summary(flow_id)` and map `blocked/ready/dispatched/done` back to the UI/prompt.
- When advancing the DAG, call TaskFlow tools; the todo layer only updates the corresponding todo status.

---

## Design Philosophy & Core Principles

Adopts the HTN (Hierarchical Task Network) paradigm: plan file → checkbox → atomic sub-task → delegate to subagent workers → adversarial verification.

```
Plan (.omo/plans/*.md)
  └─ Wave 0: [Checkbox A] [Checkbox B]          ← parallel, no dependencies
  └─ Wave 1: [Checkbox C] (depends on A, B)      ← wait for Wave 0
  └─ Wave 2: [Final Verification Wave]            ← global wrap-up

Each Checkbox → decomposed into atomic sub-tasks → delegated to subagent workers
  └─ Worker returns DoneClaim
       └─ AdversarialVerify (independent verification)
            └─ FullyDone → mark checkbox complete
```

The "Wave" here is a **narrative unit in the plan file**, not a todolist scheduling data structure. "When can the next step be dispatched" is determined by TaskFlow's `depends_on` + `blocked/ready` status.

**Core principle** (`ulw-execute/SKILL.md:6-8`):

> YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.
> YOU DO NOT WRITE CODE. YOU DO NOT EDIT PRODUCT FILES.
> EVERY unit of implementation MUST be delegated to a spawned subagent.

Does not rely on model self-discipline. A 7-layer enforcement forms a closed loop:

```
Before (message arrives)    During                     After
┌──────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ E7 Intent │  │ E6 Delegation ★    │  │ E3 Continuation ★★  │
│ Recognizer│  │ #a fan-out         │  │ idle + incomplete    │
│ ★★       │  │ #b category route  │  │ todo + backoff +     │
│ before_   │  │ #c delegation cmd  │  │ stagnation + abort   │
│ model     │  │ #d barrier         │  │ + recovery mode      │
│ inject    │  └────────────────────┘  └──────────────────────┘
└──────────┘  ┌────────────────────┐  ┌──────────────────────┐
┌──────────┐  │ E1 System prompt   │  │ E5 Sisyphus verify ★★│
│ E2 Tool  │  │ orchestrator       │  │ DoneClaim →          │
│ desc      │  │ doctrine           │  │ AdversarialVerify →  │
│ MANDATORY │  │ + hook notice      │  │ FullyDone             │
│ format    │  └────────────────────┘  └──────────────────────┘
└──────────┘                            ┌──────────────────────┐
                                          │ E4 Transition barrier│
                                          │ TaskFlow step status+│
                                          │ subagent alive → block│
                                          └──────────────────────┘
```

One-liner: model won't plan → E7 injects guidance; model stops lazily → E3 pulls back; model fakes completion → E5 blocks; model writes code itself → E1 doctrine deters; model marks done early → E4 hard-blocks.

---

## Architecture Overview

```
Layer 9  │ UI Component      │ TodoDock.vue + TodoItem.vue (PrimeVue), grouped by TaskFlow flow
Layer 8  │ Frontend State    │ useTodoList.ts (module singleton) + grouped by flow
Layer 7  │ Realtime Comm     │ WS: todo_updated push + todo_refresh reconnect
Layer 6  │ Compression Guard │ build_system_prompt injects current todos + boulder state
Layer 5  │ DAG Execution     │ Provided by TaskFlow: depends_on + blocked/ready/dispatched/done
Layer 4  │ Orchestration     │ ulw-execute: plan→checkbox→sub-task→worker→verify, calls TaskFlow
Layer 3  │ Tools             │ todowrite + todoread (DAG executed by taskflow_* tool family)
Layer 2  │ Service           │ TodoService + EvidenceLedger (DAG queries delegated to TaskFlow)
Layer 1  │ Data Storage      │ todos.db (WAL) + boulder.json + ledger.jsonl + plans/*.md + taskflow_registry.db
─────────│──────────────────│
E1       │ System Prompt     │ AGENTS.md: MANDATORY orchestrator doctrine
E2       │ Tool Description  │ todowrite docstring format rules
E3       │ Continuation ★★  │ after_agent middleware: idle+incomplete→auto-continue
E4       │ Transition Barrier│ TaskFlow step not done / subagent running → block completed
E5       │ Sisyphus Verify  │ DoneClaim → AdversarialVerify → FullyDone (5 gates)
E6       │ Delegation Route  │ #a fan-out + #b category route + #c delegation cmd + #d barrier
E7       │ Intent Recognizer│ before_model: arming (no plan + task intent) + plan-active reminder
```

**Layer 5's DAG capability is NOT implemented by this layer** — it is provided by TaskFlow (`agent/tools/taskflow/tools/*`). This layer only calls, never rebuilds.

---

## Data Layer

### Plan Files (.omo/plans/*.md)

Checkbox-format Markdown defining the complete HTN decomposition:

```markdown
# <Plan Name>

## Goal

<detailed goal: plan name, path, terminal state, delivery mode, verification>

## Context

<project background, constraints, known info>

## TODOs

### Wave 0: <Wave description>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>

### Wave 1: <Wave description> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder State (.omo/boulder.json)

Persistent work state:

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": ".omo/plans/<plan-name>.md",
      "plan_name": "<plan-name>",
      "session_ids": ["sherry:<session_id>"],
      "status": "active",
      "worktree_path": null,
      "created_at": "2026-09-07T00:00:00Z"
    }
  }
}
```

### Evidence Ledger (.omo/ledger.jsonl)

One JSON object per line, recording execution evidence per checkbox:

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "artifact": ".omo/evidence/xxx.txt", "adversarial_classes": {"stale_state": "not-applicable", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
```

### todos.db — Session-Level TODO Storage

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    plan_ref     TEXT    DEFAULT NULL,
    flow_id      TEXT    DEFAULT NULL,
    step_id      TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
```

| Field      | Description                                                        |
| ---------- | ------------------------------------------------------------------ |
| `plan_ref` | Linked `.omo/plans/*.md` file path                                 |
| `flow_id`  | Linked TaskFlow flow id (DAG owned by TaskFlow)                    |
| `step_id`  | Linked TaskFlow step id (e.g. `step-2`), for re-reading DAG status |

> **Design boundary**: `todos.db` does **not** hold `depends_on` / waves / step status. The dependency graph, `blocked/ready/dispatched/done` and unlock logic are all provided by TaskFlow. The todo only points to the corresponding flow step via `flow_id`/`step_id`; DAG status is re-read via `taskflow_summary(flow_id)`.

CRUD interface:

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """Full replacement: DELETE + INSERT (transactional)"""

async def get_todos(session_id: str) -> list[dict]:
    """Read sorted by position"""

def get_todos_sync(session_id: str) -> list[dict]:
    """Sync path for system prompt injection (no event loop)"""

async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """Read todos linked to a TaskFlow flow (map DAG status back to UI)"""
```

---

## Service Layer

### TodoService

```python
class TodoService:
    @staticmethod
    async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
        validated = _validate_todos(todos)
        # E4: Transition barrier — block completed when TaskFlow step not done / subagent running
        for todo in validated:
            if todo["status"] == "completed":
                _assert_transition_allowed(todo)
        await store.replace_all(session_id, validated)
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def get_flow_progress(session_id: str, flow_id: str) -> dict:
        """Map TaskFlow's DAG status back to a todo view (does not recompute frontier).
        DAG scheduling lives in TaskFlow; this method only calls read-only taskflow_summary."""
        from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary
        linked = await store.get_todos_by_flow(session_id, flow_id)
        summary_text = await taskflow_summary.ainvoke({"flow_id": flow_id})
        return {"todos": linked, "taskflow_summary": summary_text}
```

### EvidenceLedger

```python
class EvidenceLedger:
    LEDGER_PATH = ".omo/ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None:
        entry["timestamp"] = datetime.utcnow().isoformat()
        with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @classmethod
    def read_all(cls) -> list[dict]:
        if not os.path.exists(cls.LEDGER_PATH):
            return []
        with open(cls.LEDGER_PATH, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
```

### WS Push

```python
async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    from runtime import relation_register
    ws = relation_register.get_websocket_by_session_id(session_id)
    if ws:
        await ws.send_text(json.dumps({
            "event": "todo_updated", "session_id": session_id,
            "content": {"todos": todos}
        }))
```

---

## Tool Layer

### todowrite — Full Replacement, Write-Is-Read

```python
@tool("todowrite")
async def todowrite(
    todos: list[dict],
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Update the todo list for the current session (full replacement).
    Pass the COMPLETE list every time.
    Status: pending|in_progress|completed|cancelled.
    Priority: high|medium|low.
    Category (optional): quick|deep|ultrabrain|visual|git|writing.
    Delegation (optional): self|subagent.
    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_run_task(flow_id, task, depends_on=[...])."""
    result = await service.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER
    return output
```

### todoread — Explicit Read

```python
@tool("todoread")
async def todoread(
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### Tool Registration (E2 Injection Point)

```python
def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES  # E2 format rules
    return list(_TODOLIST_TOOLS)
```

### SKILL.md

The skill file defines when to use todolist (3+ step work), available tools, status/priority/delegation fields, DAG fields (delegated to TaskFlow), and rules (full replacement each call, one in_progress at a time, no marking completed before subagent returns).

---

## Orchestration Execution Layer

### 5-Phase Flow

```
Phase 1: Select the plan → read .omo/boulder.json, list .omo/plans/*.md, match or restore
Phase 2: Create/update Boulder state → write boulder.json, register phases/tasks as todos
Phase 3: Execute next checkbox (scheduling delegated to TaskFlow)
  → Find first unchecked checkbox
  → Decompose into atomic sub-tasks
  → Register as TaskFlow steps: taskflow_run_task(flow_id, task, depends_on=[...])
  → Blocked steps not dispatched; ready steps via taskflow_dispatch
  → taskflow_wait_all → taskflow_resume (inject result, unlock successors)
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: Verify and record evidence → 5 gates → .omo/ledger.jsonl
Phase 5: Mark progress → edit checkbox - [ ] → - [x], continue without asking
```

### Plan → TaskFlow Mapping

1. `taskflow_create(flow_id, description, initial_state)` — create the flow.
2. `taskflow_run_task(flow_id, task, depends_on=[...])` — register steps:
   - No deps → immediately `dispatched` (child session spawned).
   - Deps unsatisfied → `blocked`, not dispatched. Unknown dep id → error, no state change.
3. `taskflow_resume(flow_id, child_session_key, result)` — inject result:
   - Step becomes `done`; dependent `blocked` steps unlock to `ready` (returned).
   - **Resume does NOT auto-dispatch.**
4. `taskflow_dispatch(flow_id, step_ids)` — batch parallel dispatch ready steps.
5. `taskflow_wait_all(flow_id, timeout, poll_interval)` — wait for dispatched children to settle.
6. `taskflow_finish(flow_id, summary)` — wrap up; `taskflow_summary` anytime for re-read.

### Step State Machine (TaskFlow Authority)

```
blocked (deps not all done; run_task only registers, no spawn)
  → ready (deps satisfied, awaiting dispatch; unlocked by taskflow_resume)
  → dispatched (detached child session spawned, child_session_key persisted)
  → done (result injected via taskflow_resume)
```

### HTN → TaskFlow Capability Mapping

| Plan-layer concept (HTN)        | TaskFlow capability                                                      |
| ------------------------------- | ------------------------------------------------------------------------ |
| Inter-checkbox dependencies     | step `depends_on` (step-id list, e.g. `["step-1"]`)                      |
| Wave / wave index               | No explicit wave; `ready` status = "current wave"                        |
| Node status                     | `status ∈ {blocked, ready, dispatched, done}` in `state_json`            |
| Frontier (executable items)     | `ready` status + `taskflow_dispatch` validates `deps_satisfied`          |
| Block when deps unsatisfied     | `taskflow_run_task(..., depends_on=[...])` registers `blocked`, no spawn |
| Unlock successors on completion | `taskflow_resume` marks step `done` + `unlock_dependents`                |
| Parallel dispatch               | `taskflow_dispatch(flow_id, step_ids)` batch dispatch ready steps        |
| Wait for parallel children      | `taskflow_wait_all(...)` flow-scoped bounded poll                        |
| Re-read DAG status              | `taskflow_summary(flow_id)` renders per-step status + counts             |

### Parallel Delivery Channel Decision

| Topology                                 | Condition                               | Strategy                                                                  |
| ---------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------- |
| Independent channels → parallel workers  | Separate files, no shared contracts     | One parallel spawn burst (`taskflow_dispatch`)                            |
| Ordered dependency channel → wave serial | C needs A and B first                   | Wait for predecessor `done` then execute (`depends_on` + `resume` unlock) |
| Overlapping channels → team              | Same module/contract, concurrent faster | Serialize or manually coordinate                                          |

---

## Compression Protection Layer

The `build_system_prompt()` is re-called by the `Summarization` middleware after every context compression. Injecting current todos + boulder state here is naturally immune to compression.

### Injection Content

| Block                      | Data source       | Context cost | Description                                                 |
| -------------------------- | ----------------- | ------------ | ----------------------------------------------------------- |
| `_build_todo_block()`      | todos.db          | ~10 lines    | Current todo list + status + TaskFlow flow/step association |
| `_build_boulder_block()`   | .omo/boulder.json | ~5 lines     | Active work state                                           |
| `_build_knowledge_block()` | .omo/knowledge/   | ~20 lines    | key_failures + key_successes + reusable_patterns            |

### Implementation

```python
def _build_todo_block(session_id: str) -> str:
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        tag_parts = [t.get("category", "quick")]
        if t.get("delegation", "self") != "self":
            tag_parts.append(t["delegation"])
        if t.get("flow_id"):
            tag_parts.append(f"flow:{t['flow_id']}")
        if t.get("step_id"):
            tag_parts.append(t["step_id"])
        lines.append(f"- [{icon.get(t['status'], '○')}] ({', '.join(tag_parts)}) {t['content']} ({t['priority']})")
    lines.append("Update todos via todowrite. Pass the COMPLETE list each time.")
    lines.append("Dependency scheduling is owned by TaskFlow; declare depends_on via taskflow_run_task.")
    lines.append("Your todo list is tracked by the continuation system. Incomplete todos will trigger automatic continuation.")
    lines.append("Completion is verified by the Sisyphus contract — unverified claims will be rejected.")
    return "\n".join(lines)
```

### Why This Is Lighter Than omo's 5-Layer Defense

| omo's 5 layers                                      | This approach                                                           |
| --------------------------------------------------- | ----------------------------------------------------------------------- |
| Prune protection list                               | Not needed (todos in system prompt)                                     |
| Pre-compression snapshot + post-compression restore | Not needed (system prompt rebuilt from DB each time)                    |
| 8-segment compression context injection             | Not needed (todos not in conversation history)                          |
| 60s compression protection window                   | Not needed (no continuation injector to protect)                        |
| Continuation enforcer                               | Needed, simplified to after_agent middleware (E3)                       |
| Knowledge summary injection                         | New `_build_knowledge_block()` ~25 lines                                |
| **Total ~600+ lines**                               | **~65 lines (prompt injection) + ~130 lines (continuation middleware)** |

---

## Enforcement Layer E1–E7

### E1: System Prompt Enforcement — Orchestrator Doctrine

Added to `AGENTS.md` (loaded into system prompt by `prompt_builder.py`). Zero code — pure text.

```markdown
## Task Management & Orchestration (CRITICAL)

### Orchestrator Doctrine (MANDATORY)

YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.

- You DO NOT write code. You DO NOT edit product files.
- EVERY unit of implementation MUST be delegated to a spawned subagent.

### When to Create Todos (MANDATORY)

- Multi-step task (2+ steps) → ALWAYS create todos first
- Uncertain scope → ALWAYS (todos clarify thinking)

### Workflow (NON-NEGOTIABLE)

1. IMMEDIATELY on receiving request: todowrite to plan atomic steps.
2. For each checkbox: decompose into atomic sub-tasks for ONE worker.
3. DELEGATE every sub-task via task tool — route by category.
4. Before starting each step: Mark in_progress (only ONE at a time)
5. After subagent returns: verify via acceptance criteria, then mark completed.

### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Do NOT mark a TaskFlow-linked todo completed while its step is not `done`

### Completion Contract (Sisyphus)

- DoneClaim → AdversarialVerify → FullyDone
- If verification fails, the task is NOT done — re-dispatch or fix.

**FAILURE TO FOLLOW ORCHESTRATOR DOCTRINE = INCOMPLETE WORK.**
```

### E2: Tool Description Enforcement — Format + Delegation Rules

Overrides todowrite description in `build_todolist_tools()` with MANDATORY format rules:

- Each todo title must encode: WHERE, WHY, HOW, EXPECTED RESULT.
- Format: `[WHERE] [HOW] to [WHY] - expect [RESULT]`
- Granularity: single atomic action completable in 1-3 tool calls.
- One in_progress at a time.
- No marking completed while subagent running or without verification.

### E3: Continuation Enforcer — Post-Turn Closure Core ★★

The most critical enforcement layer: **does not rely on model self-discipline**. After a turn ends, if incomplete todos exist, the system auto-injects a continuation message to pull the LLM back.

**Key constants** (from omo todo-continuation-enforcer):

- `_MAX_STAGNATION = 3` — consecutive no-change → stop
- `_BASE_COOLDOWN_S = 2.0` — base backoff
- `_MAX_COOLDOWN_S = 60.0` — max backoff
- `_FAILURE_RESET_WINDOW_S = 300` — 5min no failures → reset
- `_MAX_RECOVERY_ATTEMPTS = 2` — recovery mode cap

**Workflow**:

```
turn ends (no tool_call, agent loop exits)
  → Summarization.aafter_agent (rebuild system prompt with latest todos)
  → TodoContinuationEnforcer.aafter_agent
      → is_abort_error? → skip
      → get_todos_sync(session_id) → filter incomplete
      → check_stagnation: snapshot compare, N consecutive no-change?
          → should_enter_recovery? → RECOVERY_PROMPT
          → else: stop continuation
      → is_in_cooldown? → skip
      → build continuation prompt (full todo status + flow/step)
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _watch_user_takeover() 0.5s poll
      → user sends message → detect_state() busy → cancel → reset()
```

**Files**: `stagnation_tracker.py` (~90 lines) + `todo_continuation.py` (~110 lines) + `agent/core.py` (~2 lines registration).

### E4: Transition Barrier — Dual Insurance

**Prompt level** (E1 AGENTS.md): Do NOT mark completed while subagent running or TaskFlow step not `done`.

**Code level hard block** (`service.py`): Two sources, neither builds a local DAG scheduler:

1. **TaskFlow step status** (DAG authority in TaskFlow): todo linked with `flow_id`/`step_id` → only `done` allows `completed`. `blocked`/`ready`/`dispatched` all block.
2. **Subagent registry liveness**: todo linked with `subagent_id` → `get_run_by_child_session_key` + `is_live_unended_run` checks if child still running.

```python
for todo in validated:
    if todo["status"] != "completed":
        continue
    # Source 1: TaskFlow step status
    flow_id, step_id = todo.get("flow_id"), todo.get("step_id")
    if flow_id and step_id:
        step_status = _read_taskflow_step_status(flow_id, step_id)
        if step_status is not None and step_status != "done":
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} is '{step_status}'. "
                "Call taskflow_wait_all then taskflow_resume to inject the result first."
            )
    # Source 2: subagent liveness
    if todo.get("subagent_id") and _is_subagent_running(todo["subagent_id"]):
        raise TodoStoreError(
            f"Cannot mark todo completed: subagent {todo['subagent_id']} is still running."
        )
```

### E5: Sisyphus Completion Contract ★★

Three-stage completion verification against "fake completion":

```
Worker returns result
  → DoneClaim: orchestrator receives completion claim
  → AdversarialVerify: independent verification (not trusting worker's self-report)
      Gate 1: Plan reread — re-read plan, confirm acceptance criteria
      Gate 2: Automated verification — run specified verification commands
      Gate 3: Manual QA — human or agent checks observable behavior
      Gate 4: Adversarial QA — probe stale state / dirty worktree / leftover resources
      Gate 5: Cleanup — clean up temporary resources
  → FullyDone: all gates pass → mark checkbox completed
  → NOT done: any gate fails → re-dispatch or fix
```

Implementation: `verifier.py` (~80 lines) + `subagent_completion_drain.py` extension (~15 lines, appends Sisyphus verification reminder after subagent completion).

### E6: Delegation Routing — Todo & Subagent Linkage ★

**Lifecycle**:

```
LLM creates todo (with category + delegation fields)
  → #a fan-out reminder (first time per session)
  → #b category tells LLM which subagent type to spawn
  → #c AGENTS.md guides how to delegate
  → Dependent steps: taskflow_run_task(depends_on=[...]) → blocked or dispatched
  → Ready steps: taskflow_dispatch(flow_id, step_ids) → get child_session_key
  → LLM updates todo's subagent_id + flow_id/step_id
  → #d-prompt: "Do NOT mark done before step done / subagent returned"
  → #d-code: update_todos() checks step status + is_live_unended_run() → hard block
  → taskflow_wait_all → taskflow_resume → E5 Sisyphus verify → LLM marks completed
```

**Category routing table** (from `ulw-execute/SKILL.md:134-148`):

| Category            | Routes to                        | Description                           |
| ------------------- | -------------------------------- | ------------------------------------- |
| `quick` (low)       | subagent spawn (default model)   | Mechanical, single-file, boilerplate  |
| `deep` (high)       | subagent spawn (reasoning model) | Complex debugging, research-intensive |
| `ultrabrain` (high) | subagent spawn (strongest model) | Genuinely hard logic problem          |
| `visual` (medium)   | subagent spawn                   | Frontend, UI/UX, styling              |
| `git` (low)         | subagent spawn                   | Git operations                        |
| `writing` (low)     | subagent spawn                   | Documentation and prose               |

### E7: Intent Recognition & Guidance — Pre-Trigger ★★

Solves "model won't proactively call planning tools". Dual-mode design:

| Scenario                             | Mode              | Behavior                                  |
| ------------------------------------ | ----------------- | ----------------------------------------- |
| User says "implement login", no plan | E7a (first arm)   | Inject full orchestrator guidance prompt  |
| Already armed + new task request     | E7a (lightweight) | Inject short reminder                     |
| Active plan + user sends any message | E7b (plan-active) | Append plan-active reminder               |
| Active plan + turn ends              | (E3)              | E7 does not intervene (E3 covers)         |
| User asks "what is REST API?"        | (skip)            | Non-task intent                           |
| After compression + new task         | E7a (re-arm)      | Clear armed flag, re-inject full guidance |

**Intent detection** — lightweight heuristic, no LLM call (zero latency, zero cost):

1. If matches question patterns → non-task
2. If matches chat patterns → non-task
3. If contains task keywords → task
4. Long message (>100 chars) non-question → likely task

**Anti-loop design**:

1. E7b takes priority over E7a (active plan → E7b, not E7a).
2. E7a once-per-session (`_armed_sessions` Set; already armed → short reminder only).
3. E7b only runs in `before_model` (E3's `after_agent` continuation won't trigger E7b).
4. `_is_system_directive()` filters E3/E7 injected messages (no false trigger).
5. E3 has backoff cooldown (limits frequency even if mis-triggered).
6. Post-compression re-arm (clears armed flag; E1 system prompt simultaneously rebuilt).

**Files**: `agent/middlewares/task_intent.py` (~160 lines) + `agent/core.py` (~2 lines registration).

---

## Frontend

### Frontend State Layer (useTodoList.ts)

Module-level singleton, WebSocket-driven, grouped by TaskFlow flow:

```typescript
interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  category?: "quick" | "deep" | "ultrabrain" | "visual" | "git" | "writing";
  delegation?: "self" | "subagent";
  subagent_id?: string | null;
  flow_id?: string | null;
  step_id?: string | null;
  taskflow_status?: "blocked" | "ready" | "dispatched" | "done" | null;
}
```

WS events: `todo_updated` (push on change) + `todo_refresh` (re-send on reconnect).

### UI Components (TodoDock.vue + TodoItem.vue)

- **TodoDock.vue**: Groups todos by `flow_id`; shows progress count; collapsible.
- **TodoItem.vue**: Checkbox (PrimeVue), category badge, delegation icon, in-progress pulse dot.

### WS Message Format

```json
{
  "event": "todo_updated",
  "session_id": "xxx",
  "content": {
    "todos": [
      {
        "content": "Implement login",
        "status": "completed",
        "priority": "high",
        "flow_id": "login-flow",
        "step_id": "step-1",
        "taskflow_status": "done"
      }
    ]
  }
}
```

`taskflow_status` is the step status re-read from `taskflow_summary(flow_id)`; the frontend does not recompute waves/dependencies.
