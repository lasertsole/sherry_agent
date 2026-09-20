# 🛡️ TodoList Enforcement Layer — E1–E7 & Orchestrator Doctrine

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [TodoList](../README.md): the seven enforcement layers (E1–E7) that keep the orchestrator planning, delegating, verifying, and honest.

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

**Files**: `stagnation_tracker.py` (~90 lines) + `todo_continuation/core.py` (~110 lines) + `agent/core.py` (~2 lines registration).

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

Implementation: `verifier.py` (~80 lines) + `subagent_completion_drain/core.py` extension (~15 lines, appends Sisyphus verification reminder after subagent completion).

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
4. `_is_system_directive()` filters E3/E7 injected messages (no false trigger) — E7 injections carry `metadata={"origin": "task_intent", "internal": true}`, so the store and the filter positively identify them as internal (never a real user request; excluded from session titles and from user-request extraction).
5. E3 has backoff cooldown (limits frequency even if mis-triggered).
6. Post-compression re-arm (clears armed flag; E1 system prompt simultaneously rebuilt).

**Files**: `agent/middlewares/task_intent/core.py` (~160 lines) + `agent/core.py` (~2 lines registration).
