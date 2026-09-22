# TaskFlow — Durable Multi-Step Task Flows with DAG Dependencies

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> TaskFlow provides durable, cross-turn task flows persisted in SQLite with optimistic locking, detached subagent step dispatch, DAG-based dependency management, parallel batch dispatch, bounded-poll waiting, and idempotent result injection. Fourteen tools form a complete lifecycle API mirroring the openclaw managedFlows surface: `taskflow_create` → `taskflow_run_task` → `taskflow_dispatch` / `taskflow_wait_all` → `taskflow_resume` → `taskflow_finish` / `taskflow_fail` / `taskflow_cancel`, with `taskflow_summary` for read-only re-read, `taskflow_set_waiting` for parking, `taskflow_progress` for progress reports, `taskflow_budget` for token/cost budgets, `taskflow_update_steps` for full steps replacement, and `taskflow_list` for the session-scoped board.

Source of truth: `agent/tools/taskflow/tools/*.py`, `agent/tools/taskflow/registry/store_sqlite.py`, `agent/tools/taskflow/config.py`. Skill reference: `skills/builtin/core/taskflow/SKILL.md`.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [State Machine](#state-machine)
- [Tool Family (14 Tools)](#tool-family-14-tools)
- [Optimistic Locking & Conflict Retry](#optimistic-locking--conflict-retry)
- [DAG Dependency System](#dag-dependency-system)
- [Parallel Step Execution](#parallel-step-execution)
- [Idempotent Resume](#idempotent-resume)
- [Conflict Retry with Spawned Children](#conflict-retry-with-spawned-children)
- [Persistence & Connection Lifecycle](#persistence--connection-lifecycle)
- [Known Limitations](#known-limitations)

---

## Overview

TaskFlow (`agent/tools/taskflow/`) is a durable task-flow system built on SQLite with WAL mode. It manages multi-step work that spans multiple conversation turns: each flow has a lifecycle (running → waiting → done/failed/cancelled), steps that can declare dependencies on each other, and results injected by detached child subagent sessions.

Key design choices:

- **Optimistic locking**: every mutation bumps `expected_revision` by 1; concurrent writers are detected via revision conflicts, not last-write-wins.
- **DAG in state_json**: step dependencies (`depends_on`), step status, and results all live inside the flow's `state_json` column — no DB schema migration needed for DAG features.
- **Detached dispatch**: steps are executed by spawning detached child subagent sessions via the existing spawn pipeline; results flow back through the announce/settle-wake pipeline.
- **Idempotent resume**: a `(child_session_key, result)` pair is fingerprinted; duplicate deliveries never inject twice.
- **Error-as-text contract**: tools never raise business errors to the LLM; they return human-readable strings prefixed with `Error:`.

---

## Architecture

```
agent/tools/taskflow/
├── __init__.py              # Package exports (11 tools re-exported)
├── config.py                # TaskFlowStatus, StepStatus enums, TERMINAL_STATUSES, TABLE_NAME
├── step_judge.py            # StepJudge — auxiliary-LLM pass/retry/block discriminator
├── evidence_collector.py    # Session/flow evidence summary rendered for judge prompts
├── registry/
│   ├── __init__.py
│   └── store_sqlite.py      # SQLite persistence: create/get/update, WAL, busy_timeout,
│                            #   FlowConflictError/FlowNotFoundError/FlowExistsError,
│                            #   sync path (get_flow_sync)
└── tools/
    ├── __init__.py           # build_taskflow_tools() → 14 tools, scope=main_only
    ├── _dispatch.py          # Monkeypatchable dispatch seam (spawn_subagent_direct)
    ├── _shared.py            # DAG helpers + conflict-retry persistence
    ├── taskflow_create.py    # Create flow at revision 1
    ├── taskflow_run_task.py  # Register step + dispatch (or block on unsatisfied deps)
    ├── taskflow_dispatch.py  # Batch dispatch multiple ready steps
    ├── taskflow_wait_all.py  # Bounded-poll wait for dispatched steps to settle
    ├── taskflow_resume.py    # Inject result, mark done, unlock dependents (idempotent)
    ├── taskflow_set_waiting.py # Park flow in waiting state
    ├── taskflow_summary.py   # Read-only re-read (also post-conflict re-read)
    ├── taskflow_progress.py  # Read-only progress report
    ├── taskflow_budget.py    # Token/cost budget query and set
    ├── taskflow_update_steps.py # Full replacement of the steps list
    ├── taskflow_list.py      # Session-scoped flow board
    ├── taskflow_finish.py    # Mark done (terminal)
    ├── taskflow_fail.py      # Mark failed (terminal)
    └── taskflow_cancel.py    # Cancel flow (terminal)
```

### Registration

Tools are registered via `build_taskflow_tools()` in `agent/tools/taskflow/tools/__init__.py`, which returns all 14 tools tagged with `metadata = {"scope": "main_only"}` and `handle_tool_error = True`. The subagent tool-policy drops them unconditionally — only the main agent manages shared flow state.

---

## State Machine

### Flow Status (`TaskFlowStatus`)

```
running ──→ waiting ──→ running (via taskflow_resume)
  │                       └──→ done     (taskflow_finish, terminal)
  │                       └──→ failed   (taskflow_fail, terminal)
  │                       └──→ cancelled(taskflow_cancel, terminal)
  └──→ done / failed / cancelled (directly from running)
```

Terminal statuses (`done`, `failed`, `cancelled`) are immutable: every mutating tool rejects them with `Error: TaskFlow '<id>' is terminal (status=...); no further mutations allowed`.

### Step Status (`StepStatus`)

```
blocked → ready → dispatched → done
```

| Status       | Meaning                                                                       | Transition trigger                            |
| ------------ | ----------------------------------------------------------------------------- | --------------------------------------------- |
| `blocked`    | Dependencies not yet all `done`; `taskflow_run_task` only registers, no spawn | `taskflow_resume` unlocks when deps satisfied |
| `ready`      | Dependencies satisfied, awaiting dispatch                                     | `taskflow_dispatch` dispatches it             |
| `dispatched` | Detached child session spawned, `child_session_key` persisted                 | `taskflow_resume` injects result              |
| `done`       | Result injected via `taskflow_resume`                                         | (terminal for this step)                      |

> `done` means "a result was injected", **not** "the child succeeded". There are no `failed`/`skipped` step statuses; failure-aware handling lives in the step's opt-in `retry_policy` and in the step judge (`blocked` on `block` or on an exhausted retry budget).

### Legacy Step Compatibility

Steps without a `status` field are handled by `step_status()`: a step with a `child_session_key` is treated as `dispatched`; otherwise `ready`. This keeps flows without the field backward-compatible.

---

## Tool Family (14 Tools)

### taskflow_create

```python
async def taskflow_create(flow_id: str, description: str = "", initial_state: dict | None = None) -> str
```

Creates a durable flow at `INITIAL_REVISION = 1` with status `running`. Returns the flow id, status, and revision. `FlowExistsError` if the id is already taken (returns an error string with the current revision for re-read).

### taskflow_run_task

```python
async def taskflow_run_task(
    flow_id: str, task: str, label: str | None = None,
    depends_on: list[str] | None = None,
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

Registers a step on the flow and dispatches it to a detached child subagent. Step ids are auto-assigned as `step-1`, `step-2`, etc.

- **No dependencies** (or all satisfied) → step is `dispatched` immediately (child spawned).
- **Unsatisfied dependencies** → step is registered as `blocked`, no child spawned. Unknown dependency ids are rejected before any spawn or state change.
- Returns `step_id`, `child_session_key`, and `revision` (or `pending=[...]` listing unsatisfied deps when blocked).

### taskflow_dispatch

```python
async def taskflow_dispatch(
    flow_id: str, step_ids: list[str],
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

Batch-dispatches one or more currently-ready steps. A step is dispatchable when `ready`, or `blocked` but its dependencies are now satisfied.

- **All-or-nothing validation**: every id is validated before any spawn. An unknown id, an already-dispatched/done step, or a blocked step with unsatisfied deps rejects the whole call with zero spawns.
- **Mid-batch failure**: if a spawn fails mid-batch, already-spawned steps are persisted in one `update_flow` so no child is left unrecorded. The error names the failed and dispatched step ids.
- The flow-level `child_session_key` is deliberately NOT touched — per-step child keys are authoritative.

### taskflow_wait_all

```python
async def taskflow_wait_all(
    flow_id: str, timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 0.5,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

Waits until this flow's dispatched child sessions settle (or a timeout). Polls ONLY the child sessions recorded on THIS flow's dispatched steps — unrelated background children never block the return.

- An unknown/absent run is treated as settled (never hangs).
- On timeout, returns a partial report with instructions to `taskflow_resume` the settled children and call `wait_all` again.
- The registry seam (`get_run_by_child_session_key` / `is_live_unended_run`) is lazily imported and injectable for tests.

### taskflow_resume

```python
async def taskflow_resume(
    flow_id: str, child_session_key: str = "", result: str = "",
    expected_revision: int | None = None, token_usage: dict | None = None,
    validation_criteria: str | None = None,
) -> str
```

Injects a completed child session result into the flow state (idempotent). Appends `{child_session_key, result, result_hash, injected_at}` to results. If the flow was `waiting`, returns it to `running`.

**DAG bookkeeping**: marks the matching step `done` and calls `unlock_dependents()` to move newly-satisfied `blocked` steps to `ready`. Returns the unlocked step ids. **Resume never spawns** — the caller dispatches newly-ready steps explicitly via `taskflow_dispatch`.

**Step judge**: when the step carries `validation_criteria` (set by `taskflow_run_task` or passed here), an auxiliary-LLM judge (`agent/tools/taskflow/step_judge.py`, temperature 0) reviews the result against the criteria and returns `pass` / `retry` / `block`. `retry` re-dispatches the step through the shared `_retry` seam, reusing the step's own `retry_count` budget (`STEP_JUDGE["max_retries"]`, default 2) and appending the judge's guidance to the replacement task via `with_judge_feedback`; an exhausted budget or a `block` verdict marks the step `blocked` with the judge's reason. The judge is shown the flow's evidence summary and is fail-open — a disabled judge, a model error, or an unparseable response degrades to `pass`.

### taskflow_set_waiting

```python
async def taskflow_set_waiting(
    flow_id: str, wait_reason: str = "",
    expected_revision: int | None = None,
) -> str
```

Parks the flow in `waiting` state, recording what it is waiting for. The wait payload is cleared by `taskflow_resume`.

### taskflow_summary

```python
async def taskflow_summary(flow_id: str) -> str
```

Read-only re-read of the full flow state: status, revision, child_session_key, description, all steps (with status, depends_on, child_session_key), step status counts, results, wait payload, summary, failure_reason, cancel_reason. Also the designated re-read step after a revision conflict.

### taskflow_progress

```python
async def taskflow_progress(flow_id: str) -> str
```

Read-only completion report: completion percentage, status breakdown, next steps, and an estimated remaining time (when at least two `done` steps carry a `dispatched_at` timestamp). Never mutates the flow.

### taskflow_budget

```python
async def taskflow_budget(
    flow_id: str, action: str = "query", token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

Queries (`query`) or sets (`set`) the flow's token/cost budget. `query` reports `total_tokens`, `total_cost`, the budget, tokens remaining, and a status (`ok` / `WARNING` at 80% / `EXCEEDED`); `set` requires a positive `token_budget` and writes through the optimistic lock.

### taskflow_update_steps

```python
async def taskflow_update_steps(
    flow_id: str, steps: list[dict],
    expected_revision: int | None = None,
) -> str
```

Full replacement of the flow's steps list (like `todowrite` for TaskFlow): steps can be added, removed, reordered, or have their `task`/`depends_on` rewritten. Safety rules keep a `dispatched` step bound to its `child_session_key` and refuse to rewrite a `done` step; deleting a `dispatched` step whose child still runs succeeds with a non-blocking `Warning:` naming the child key.

### taskflow_list

```python
async def taskflow_list(status_filter: str = "active") -> str
```

Read-only board of this session's flows: `"active"` (running + waiting), `"all"` (terminal statuses included), or an exact status name. Every read filters the owning `session_id` in SQL; the rendered table caps the description at 40 chars and derives `updated_at` from the flow's activity stamps.

### taskflow_finish / taskflow_fail / taskflow_cancel

```python
async def taskflow_finish(flow_id: str, summary: str = "", expected_revision: int | None = None,
                          todo: dict | None = None, plan_path: str | None = None,
                          checkbox_label: str | None = None) -> str
async def taskflow_fail(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
async def taskflow_cancel(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
```

Terminal transitions. `finish` records a `summary`, `fail` records a `failure_reason`, `cancel` records a `cancel_reason` in the flow state. Already-dispatched child sessions keep running; their results are still deliverable via `taskflow_resume` until the flow was cancelled.

`finish` gates the DONE transition on four checks, in order: **A** every step is `done` or `blocked`; **B** no step is `blocked`; **C** the flow's evidence (`agent/tools/taskflow/evidence_collector.py`) carries no `FAIL` and no `[stale]` row; **D** `SisyphusVerifier` passes — only when the caller explicitly supplies both `todo` and `plan_path`, so no flow/step schema migration is needed. Every gate is fail-open: an unavailable collector or a verifier error passes rather than blocking a finish.

---

## Optimistic Locking & Conflict Retry

All mutations go through `UPDATE ... WHERE flow_id = ? AND expected_revision = ?`. When the update matches zero rows, the write conflicted (or the flow vanished):

- **`FlowConflictError`**: carries the latest revision so the caller can re-read and retry. The error text embeds it: `expected_revision=2 but latest revision=3; re-read with taskflow_summary and retry with expected_revision=3`.
- **`FlowNotFoundError`**: the flow was deleted.

**Retry flow** (from the LLM's perspective):

1. Call `taskflow_summary(flow_id)` to re-read, getting the latest `revision`.
2. Re-apply the mutation with `expected_revision=<latest revision>`.
3. The conflict error text contains the directly-usable retry value.

Terminal flows are immutable: every mutating tool checks `is_terminal(status)` before any state change.

---

## DAG Dependency System

DAG fields live entirely inside `state_json` (no DB migration). The `StepStatus` enum defines four states, and three pure functions in `_shared.py` manage transitions:

### deps_satisfied(step, steps) → bool

True iff every `depends_on` id exists in `steps` and is `done`. Missing/empty `depends_on` is trivially satisfied. An unknown dep id is never satisfied. A **self-dependency is never satisfied**, which makes a self-referential step safe to leave blocked without an infinite unlock loop.

### mark_step_done(steps, child_session_key) → str | None

Marks the step matching `child_session_key` as `done`. Idempotent: a repeated call for the same key returns the same step id. Returns `None` when no step carries that child key.

### unlock_dependents(steps) → list[str]

A **single pass** through `steps`: moves `blocked` steps with now-satisfied dependencies to `ready`. Returns the newly-ready step ids. Single-pass ensures a dependency cycle can never loop.

---

## Parallel Step Execution

Two tools enable parallel execution of independent steps:

### Batch Dispatch (`taskflow_dispatch`)

Validates the entire batch before any spawn (all-or-nothing). Sequential spawn through the shared `_dispatch.dispatch_child` seam. A mid-batch spawn failure stops the batch and persists the successful spawns in ONE `update_flow` call. The `apply_dispatched_steps()` helper re-applys recorded dispatched-step payloads onto a freshly-read step list, matching by `step_id` — a spawned child is never lost to a concurrent writer that rewrote the step list.

### Bounded-Poll Wait (`taskflow_wait_all`)

Flow-scoped: only polls child sessions recorded on THIS flow's dispatched steps. Never blocks behind unrelated background children. Clamped poll interval (minimum `0.05s`). Unknown/absent runs are settled (never hangs). Timeout returns a partial report.

---

## Idempotent Resume

`taskflow_resume` fingerprints the `(child_session_key, result)` pair using SHA-256 truncated to 16 hex chars. Before injecting, it checks whether a result with the same `result_hash` already exists in the flow's `results` list. If so, the call is a no-op: it neither re-injects nor bumps the revision. This makes announce-pipeline redeliveries safe — duplicate deliveries cannot corrupt the state.

---

## Conflict Retry with Spawned Children

When a child session has ALREADY spawned but the optimistic-lock write loses the race, the child must never be silently dropped. `update_flow_with_conflict_retry()` in `_shared.py` handles this:

1. After a successful spawn, the caller invokes this with a `build_state` callback.
2. On `FlowConflictError`: re-read the flow, re-invoke `build_state(fresh_flow, attempt)` against the freshly-read state, and retry up to `PERSIST_MAX_ATTEMPTS = 3`.
3. On `FlowNotFoundError` or a terminal fresh flow: return an error naming every spawned `child_session_key` — the caller must recover them by key, NOT re-dispatch.
4. On exhaustion: same error, naming all child keys.

The `build_state` callback receives a fresh flow and an attempt number, so it can rebuild state against the latest data (e.g., re-assigning `step_id` based on the current step count).

---

## Persistence & Connection Lifecycle

`store_sqlite.py` mirrors the subagent registry blueprint:

- **Database**: `agent/tools/taskflow/data/taskflow_registry.db`
- **Table**: `task_flows(flow_id TEXT PK, state_json TEXT NOT NULL, wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL, child_session_key TEXT)`
- **WAL mode**: switched once per process via `_switch_to_wal_if_needed()`; the pragma is skipped once the file is already WAL.
- **Busy timeout**: `5000ms` on EVERY connection (first statement). The journal-mode switch does not reliably honor this, handled separately with try/except tolerance.
- **Async init**: once-per-process under an `asyncio.Lock` owned by the calling loop; other loops poll `_initialized` (never touch a foreign loop's lock — avoids non-threadsafe `call_soon` wakeups).
- **Sync path**: `get_flow_sync()` uses stdlib `sqlite3` with `threading.Lock`-guarded one-time table creation. Failures are logged and swallowed with a `None` return (for system prompt injection where no event loop exists).

---

## Known Limitations

- **`done` ≠ success**: step `done` only means "a result was injected", not "the child succeeded". No step-level `failed`/`skipped` statuses exist, and a step with neither `validation_criteria` nor a matching `retry_policy` still lands `done` and unlocks successors even after a failed child. For failure-aware recovery, attach a `retry_policy` to the step: `taskflow_wait_all` re-dispatches a settled child while retry budget remains, and marks the step `done` with a failure-note once the budget is exhausted; with `validation_criteria`, the step judge's `block` verdict or exhausted retry budget marks the step `blocked` instead.
- **`taskflow_wait_all` timeout is bounded polling**: a never-settling child session does not automatically fail the flow. The timeout returns a partial report.
- **Step ids are sequential**: `step-{len(steps)+1}` assigned at registration time. If steps are appended concurrently, the id is re-computed inside `build_state` on conflict retry.
