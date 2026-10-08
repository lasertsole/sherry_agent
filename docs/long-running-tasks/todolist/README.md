# Planning & Discipline Layer on top of TaskFlow

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> A session-scoped, lightweight planning and enforcement layer built on top of the existing TaskFlow system. It is **not a task engine**, **not a DAG**, and **does not implement a scheduler**. Session-level plans are managed by `todowrite` / `todoread` (compression-immune via system prompt injection). Cross-session, durable, subagent-dispatching execution is owned by **TaskFlow** (`depends_on`, `blocked/ready/dispatched/done`, `taskflow_dispatch`, `taskflow_wait_all`). The two layers are linked by `flow_id` / `step_id`; a single state has one authoritative source — no cross-layer duplication.

---

## Table of Contents

- [Positioning: Relationship with TaskFlow](#positioning-relationship-with-taskflow)
- [Design Philosophy & Core Principles](#design-philosophy--core-principles)
- [Architecture Overview](#architecture-overview)
- [Data Layer](data/README.md)
  - [Plan Files (workspace/sessions/<session_id>/plans/*.md)](data/README.md#plan-files-workspacesessionssession_idplansmd)
  - [Boulder State (src/data/boulder.json)](data/README.md#boulder-state-srcdataboulderjson)
  - [Evidence Ledger (src/data/evidence-ledger.jsonl)](data/README.md#evidence-ledger-srcdataevidence-ledgerjsonl)
  - [todos.db — Session-Level TODO Storage](data/README.md#todosdb--session-level-todo-storage)
- [Service Layer](#service-layer)
- [Tool Layer](#tool-layer)
- [Orchestration Execution Layer](#orchestration-execution-layer)
- [Compression Protection Layer](#compression-protection-layer)
- [Enforcement Layer E1–E7](enforcement/README.md)
  - [E1: System Prompt Enforcement — Orchestrator Doctrine](enforcement/README.md#e1-system-prompt-enforcement--orchestrator-doctrine)
  - [E2: Tool Description Enforcement — Format + Delegation Rules](enforcement/README.md#e2-tool-description-enforcement--format--delegation-rules)
  - [E3: Continuation Enforcer — Post-Turn Closure Core ★★](enforcement/README.md#e3-continuation-enforcer--post-turn-closure-core-)
  - [E4: Transition Barrier — Dual Insurance](enforcement/README.md#e4-transition-barrier--dual-insurance)
  - [E5: Sisyphus Completion Contract ★★](enforcement/README.md#e5-sisyphus-completion-contract-)
  - [E6: Delegation Routing — Todo & Subagent Linkage ★](enforcement/README.md#e6-delegation-routing--todo--subagent-linkage-)
  - [E7: Intent Recognition & Guidance — Pre-Trigger ★★](enforcement/README.md#e7-intent-recognition--guidance--pre-trigger-)
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
| Plan file progress (checkboxes)             | `workspace/sessions/<id>/plans/*.md` | Orchestrator edits `- [ ]` → `- [x]`                                |
| Execution evidence                          | `src/data/evidence-ledger.jsonl` | `EvidenceLedger` appends                                 |
| Active work state                           | `src/data/boulder.json` | Plan activation/recovery                                            |

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
Plan (workspace/sessions/<id>/plans/*.md)
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

`agent/tools/todolist/evidence_ledger.py` is an append-only JSONL ledger (`src/data/evidence-ledger.jsonl`, one JSON object per line). `append` + `read_all` remain the whole storage contract; session-scoped views read the same shared file:

```python
class EvidenceLedger:
    LEDGER_PATH = "src/data/evidence-ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None: ...          # one JSON line + UTC stamp
    @classmethod
    def read_all(cls) -> list[dict]: ...
    @classmethod
    def for_session(cls, session_key: str) -> SessionEvidenceLedger: ...
    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]: ...
    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int: ...
```

Staleness is derived, never stored: `mark_stale_for_path` appends a `{"event": "stale", "file_path": …}` row, and the read side treats an evidence row as stale when a later stale event names a path contained in its `command`. `agent/tools/todolist/evidence_recorder.py` wires this into the tools (fail-open, errors swallowed): `terminal` / `python_repl` always append a row for recognized verification commands (the taxonomy lives in `EVIDENCE_LEDGER["verify_commands"]`) and `write_file` / `patch_file` always append a stale event for the edited path. `agent/tools/taskflow/evidence_collector.py` renders the summary shown to the judges and to the `taskflow_finish` evidence gate.

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

### knowledge — Plan-Identity Isolation

The `knowledge` tool (`agent/tools/todolist/knowledge/`) is keyed by **plan identity**, not by plan name. Association comes from three sources (`ownership.association_plan_refs()`): the session's `plan_ref` state key, a `plan_ref` on one of the session's todos (SQL filtered by `session_id`), and a `src/data/boulder.json` work whose `plan_name` matches and whose `session_ids` contains the session. `identity.resolve_plan_identity()` maps that name to the canonical plan path and derives the storage directory `workspace/knowledge/plans/<plan_key>/`, where `plan_key = sha1(repo-relative plan path)[:12]`; each directory carries a `meta.json` with the readable `plan_name` / `plan_ref`. Consequences:

- Same-named plans held in different plan files are **physically isolated** — each session writes its own key directory, so neither can overwrite the other;
- One plan file shared through boulder `session_ids` resolves the same path for every listed session, so **multi-session collaboration stays intact** (one shared directory);
- A session whose plan file does not resolve writes under the fallback identity `session-<sha1(session_id)[:8]>` — hashing the full id keeps sessions whose ids share an 8-char prefix apart;
- `plan_ref` takes precedence when a name matches several paths; a name that stays ambiguous (and is not the session's own fallback) is refused with a diagnosable error, never silently written elsewhere.

`list` returns only the session's associated plans (readable name + key), and a foreign plan is refused on `read` / `write` (never a silent empty result). Legacy name-keyed `workspace/knowledge/plans/<plan-name>/` directories stay readable until the key directory exists; writes always land in the key directory. `clear_session` deletes the session's private identity directories and retains plans shared with another session through boulder `session_ids`; legacy directories are never deleted. A missing/corrupt boulder file, an empty `session_id`, or an unknown plan all deny safely — no exception, no cross-session read.

---

## Orchestration Execution Layer

### 5-Phase Flow

```
Phase 1: Select the plan → read src/data/boulder.json, list workspace/sessions/<session_id>/plans/*.md, match or restore
Phase 2: Create/update Boulder state → write boulder.json, register phases/tasks as todos
Phase 3: Execute next checkbox (scheduling delegated to TaskFlow)
  → Find first unchecked checkbox
  → Decompose into atomic sub-tasks
  → Register as TaskFlow steps: taskflow_run_task(flow_id, task, depends_on=[...])
  → Blocked steps not dispatched; ready steps via taskflow_dispatch
  → taskflow_wait_all → taskflow_resume (inject result, unlock successors)
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: Verify and record evidence → 5 gates → src/data/evidence-ledger.jsonl
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
| `_build_boulder_block()`   | src/data/boulder.json | ~5 lines     | Active work state                                           |
| `_build_knowledge_block()` | workspace/knowledge/plans/&lt;plan_key&gt;/ | ~20 lines | key_failures + key_successes + reusable_patterns (identity-resolved)         |

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
