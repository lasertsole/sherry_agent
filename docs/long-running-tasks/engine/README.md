# 🧩 TaskFlow Engine — DAG, Retry, Budget, Deadline & Board

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Long-Running Tasks](../README.md): the durable DAG engine, step retry policy, token/cost budget, deadlines, result validation, progress reports, idle detection, and the session-scoped board.

---

## 🧩 TaskFlow DAG Engine

### Status enums (`agent/tools/taskflow/config.py`)

```python
TABLE_NAME = "task_flows"          # config.py:5
INITIAL_REVISION = 1               # config.py:8

class TaskFlowStatus(StrEnum):     # config.py:11
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class StepStatus(StrEnum):         # config.py:21
    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})   # config.py:35
```

### Table schema (`agent/tools/taskflow/registry/store_sqlite.py`)

```sql
CREATE TABLE IF NOT EXISTS task_flows (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL,
    session_id TEXT NOT NULL DEFAULT ''
);
```

The DAG itself (`steps[]`, `results[]`, `depends_on`, `creator_session_key`) lives entirely inside `state_json` — no schema migration is needed to add DAG fields. The token/cost/deadline columns come from additive DDL (`_TOKEN_COLUMN_DDL`, `_DEADLINE_COLUMN_DDL`, `store_sqlite.py:83-129`). `session_id` is the isolation column (additive `_SESSION_ID_COLUMN_DDL`, indexed by `idx_taskflow_session_status`): it is stamped at creation and every read/mutation filters on it (`WHERE flow_id = ? AND session_id = ?`, `WHERE session_id = ? AND status IN (…)`). Legacy rows created before the column carry `session_id = ''`, are invisible to every session-scoped read, and stay reachable only through the system-level sweeper queries. The WAL pragma is applied once per process and `PRAGMA busy_timeout = 5000` precedes every statement (`store_sqlite.py:234-271`).

### Step status machine

```
blocked ──(deps satisfied)──▶ ready ──(dispatch)──▶ dispatched ──(resume)──▶ done
```

| Status | Meaning | Set by |
| :--- | :--- | :--- |
| `blocked` | at least one `depends_on` id is not `done`; no child spawned | `taskflow_run_task` |
| `ready` | all dependencies satisfied, awaiting dispatch | registration, or `unlock_dependents` after a resume |
| `dispatched` | a detached child session was spawned; `child_session_key` + `dispatched_at` recorded | `taskflow_run_task` / `taskflow_dispatch` |
| `done` | a result was injected by `taskflow_resume` | `taskflow_resume` |

`done` means **"a result was injected"**, not "the child succeeded" (see [Known Limitations](../README.md#-known-limitations)). Legacy steps without a `status` field are derived: a step carrying `child_session_key` is treated as `dispatched`, otherwise `ready` (`_shared.py:85`, `step_status`).

### `depends_on` semantics

`deps_satisfied(step, steps)` (`_shared.py:99`) is true iff every `depends_on` id exists in the current step list **and** is `done`. Missing/empty `depends_on` is trivially satisfied. An unknown dependency id is never satisfied, and a **self-dependency is never satisfied** — so a self-referential step stays safely blocked instead of entering an unlock loop. `unlock_dependents(steps)` (`_shared.py:137`) is a **single pass** over the list, which structurally prevents a dependency cycle from looping. `taskflow_run_task` rejects unknown dependency ids *before* any spawn or state change.

### Tool family (14 tools)

All tools are `async`, decorated `@tool("taskflow_…")`, tagged `metadata={"scope": "main_only"}` and `handle_tool_error=True` by `build_taskflow_tools()` (`tools/__init__.py:58-76`). Only the main agent may manage shared flow state; the subagent tool policy drops the family unconditionally.

| Tool | Purpose |
| :--- | :--- |
| `taskflow_create` | Create a flow at revision 1; optionally set `deadline_hours` |
| `taskflow_run_task` | Register a step (optional `validation_criteria` / `retry_policy`) and dispatch it (or record it `blocked`) |
| `taskflow_dispatch` | Batch-dispatch several ready steps all-or-nothing |
| `taskflow_update_steps` | Full-replace the steps list (add/remove/reorder/rewrite task or depends_on; dispatched/done safety rules; orphan-child warning) |
| `taskflow_wait_all` | Flow-scoped bounded poll for dispatched steps to settle (auto-retries settled steps with a policy) |
| `taskflow_resume` | Idempotently inject a child result, unlock dependents, aggregate tokens (failure-aware retry + criteria echo) |
| `taskflow_set_waiting` | Park the flow in `waiting` with a reason |
| `taskflow_summary` | Read-only re-read (also the post-conflict re-read) |
| `taskflow_progress` | Human-readable progress/completion report |
| `taskflow_budget` | Query or set the token/cost budget |
| `taskflow_list` | This session's board (`active` / `all` / status name) |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | Terminal transitions |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:40
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    session_id: SessionId = "",
) -> str
```

### Dispatch — `taskflow_dispatch` batch semantics

`taskflow_dispatch(flow_id, step_ids, expected_revision=None, session_id="")` (`taskflow_dispatch.py:36`) validates **every** id before anything spawns. An id is dispatchable when its status is `ready`, or `blocked` with dependencies already satisfied. An unknown id, a duplicate, or a step already `dispatched`/`done` rejects the whole call with zero spawns. On success the steps are spawned sequentially through the shared `_dispatch.dispatch_child` seam (`_dispatch.py:10`) and persisted in **one** `update_flow` call. If a spawn fails mid-batch the loop stops, already-spawned children are persisted so none is silently dropped, and the error names both the failed and dispatched step ids. The flow-level `child_session_key` is deliberately left untouched — per-step child keys are authoritative.

### Update — `taskflow_update_steps` full replacement

`taskflow_update_steps(flow_id, steps, expected_revision=None, session_id="")` (`taskflow_update_steps.py:191`) full-replaces the flow's steps list (like `todowrite` for TaskFlow): the stored DAG becomes exactly the list you pass, so steps can be added, removed, reordered, or have their `task`/`depends_on` rewritten. Safety rules: `step_id` must be unique and every `depends_on` must reference an id present in the new list (no self-dependency); a `dispatched` step keeps its `child_session_key` and cannot be downgraded to `ready`/`blocked`; a `done` step cannot change its task/depends_on/status; new steps must be `ready`/`blocked` (dispatch through `taskflow_dispatch`); terminal flows reject the call. Deleting a `dispatched` step whose child is still running succeeds but returns a non-blocking `Warning:` naming the child key — kill the child or settle it via `taskflow_wait_all`/`taskflow_resume`. Reconciliation uses the same optimistic lock: a mismatched `expected_revision` is rejected with the latest revision for a re-read + retry.

### Wait — `taskflow_wait_all` flow scope

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")` (`taskflow_wait_all.py:110`) polls **only** the child sessions recorded on *this* flow's `dispatched` steps, so unrelated background children can never block the return. An unknown or already-cleaned run is treated as settled (it never hangs). The interval is clamped to `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]` (0.05 s). On timeout it returns a partial report plus the instruction to `taskflow_resume` settled children and call `wait_all` again. It deliberately does **not** reuse `sessions_yield`, which fires only when all of a requester session's children settle.

### Optimistic locking

Every mutation goes through `UPDATE … WHERE flow_id = ? AND expected_revision = ?` and bumps the revision by exactly 1 (`store_sqlite.py:460-481`). Zero matched rows means a conflict:

```python
# FlowConflictError message (store_sqlite.py:173)
"TaskFlow '<id>' revision conflict: expected_revision=2 but latest revision=3;
 re-read with taskflow_summary and retry with expected_revision=3"
```

When a child has **already spawned** but the write loses the race, `update_flow_with_conflict_retry()` (`_shared.py:191`) re-reads the fresh flow, rebuilds the state via a `build_state` callback, and retries up to `PERSIST_MAX_ATTEMPTS = 3`. If the flow vanished or turned terminal, or retries are exhausted, it returns an error naming every spawned `child_session_key` and instructs the caller to recover them **by key, never re-dispatch**.

## 🔁 Step Retry Policy

A step may carry a declarative `retry_policy` so a failed child is re-dispatched automatically instead of being accepted as the step's final result. The policy, the counter and the replacement child key all live inside `state_json` (no schema migration), and the retry helpers are `agent/tools/taskflow/tools/_retry.py`.

```python
# retry_policy on a step (stored by taskflow_run_task)
{"max_retries": 2, "retry_delay_seconds": 30.0, "retry_on": ["timeout", "rate_limit"]}
```

| Field | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `max_retries` | non-negative int | `0` | Maximum **re**-dispatch attempts |
| `retry_delay_seconds` | non-negative number | `60.0` (`DEFAULT_RETRY_DELAY_SECONDS`) | Backoff slept before each re-dispatch |
| `retry_on` | `list[str]` | `[]` | Failure types that trigger a retry; empty = every classified failure |

`validate_policy()` (`_retry.py:120`) rejects a malformed policy at `taskflow_run_task` time — a non-dict policy, a negative/non-int `max_retries`, a negative/non-numeric `retry_delay_seconds`, or a `retry_on` that is not a list of strings — with an `Error:` string before any spawn or write. At resume time a missing/malformed stored policy degrades to `None` (`normalize_policy`), which falls back to no-retry behavior rather than failing the call.

`retry_count` (stored on the step, default `0`) counts **re-dispatches**, never the original dispatch: `0` while the first child runs, `1` once the first replacement is spawned. A retry is allowed while `retry_count < max_retries` (`retries_remaining`, `_retry.py:95`). `apply_redispatch()` (`_retry.py:170`) mutates the step in place — new `child_session_key`, `dispatched_at`, `status = dispatched`, bumped `retry_count`.

### Failure classification

`classify_failure(result)` (`_retry.py:56`) is a **result-text heuristic**. It lowercases the text, strips success phrasings that literally contain a failure word (`_NEGATED_FAILURE_PHRASES`, e.g. `"no error"`, `"error-free"`), then returns the first matching ordered bucket:

| Classified type | Trigger substrings |
| :--- | :--- |
| `timeout` | `timeout`, `timed out`, `time limit` |
| `rate_limit` | `rate limit`, `rate_limit`, `429`, `too many requests` |
| `error` | `error`, `failed`, `failure`, `exception`, `traceback`, `aborted`, `crashed` |

A clean result never retries. With a non-empty `retry_on`, only a matching classified type retries; with an empty list every classified failure does (`should_retry_failure`, `_retry.py:103`).

### Two automatic re-dispatch paths

| Entry point | Trigger | Behavior |
| :--- | :--- | :--- |
| `taskflow_wait_all` | a polled child settled **dead** with no result | `plan_settled_retries()` re-dispatches once per settled step while budget remains, else marks it `done` with a failure-note result |
| `taskflow_resume` | the injected result text **classifies as a failure** allowed by `retry_on` | spawns a replacement, records the failure result, and keeps the step `dispatched` on the new child |

- **`taskflow_wait_all`** calls `_retry_settled_steps()` (`taskflow_wait_all.py:117`) after every target settles. Steps without a policy produce no action (legacy flows keep the byte-identical output); a child whose result was already injected is left alone. At most **one** retry decision per settled step is executed per call — the replacement is spawned and recorded, and the orchestrator calls `wait_all` again to wait for it. No background retry loop lives in the tool. Replacement children are persisted through `persist_retry_actions()` (`_retry.py:254`), which re-applies the plan onto a freshly-read step list via `update_flow_with_conflict_retry()` so a concurrent writer cannot drop a spawned replacement.
- **`taskflow_resume`** checks the step's policy before marking it done (`taskflow_resume.py:122-144`). On a retryable failure it sleeps `retry_delay_seconds` and spawns a replacement; the step stays `dispatched` on the new child. If the replacement spawn itself raises, the step is left `done` with the failure result and the response carries a `retry:` note naming the exception.

### Exhaustion and failure notes

When `retry_count` has reached `max_retries`, `plan_settled_retries()` emits an `exhausted` action instead of a redispatch. The step is marked `done` and a failure-note result is appended with `retry_exhausted: True`, produced by `exhausted_note()` + `failure_result_record()` (`_retry.py:178-196`):

```
retry budget exhausted: step step-4 child agent:main:session:... settled without a
result after 2 retry/retries (max_retries=2); marked done by taskflow_wait_all
```

Exhausted steps need an explicit decision — resume the failure note or fail the flow. Both the `wait_all` and `resume` paths keep the resume idempotency contract: the failure note carries a `result_hash`, so a redelivered settle is never recorded twice.

## 🪙 Token / Cost Budget

`taskflow_budget` (`taskflow_budget.py:10`) is both the query and the setter:

```python
@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",           # "query" | "set"
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

- **`query`** reports `total_tokens`, `total_cost`, the budget, tokens remaining, and a status: `EXCEEDED` when `total_tokens >= budget`, `WARNING` when usage ≥ `MODEL_PRICING["budget_warn_threshold"]` (0.80), else `ok`. With no budget set the percentage is reported as not set.
- **`set`** requires a positive integer `token_budget`, rejects terminal flows, and writes through the optimistic lock (pass `expected_revision` to fail fast).

Spend is aggregated in `taskflow_resume` when the caller passes `token_usage` (`taskflow_resume.py:108-122`):

```python
token_usage={"input_tokens": 1200, "output_tokens": 800, "model_name": "deepseek-chat"}
```

The tool lazily imports `from config.features import MODEL_PRICING`, looks up the model in `MODEL_PRICING["model_pricing_per_m_tokens"]` (falling back to `_default`), and computes:

```
total_tokens = existing_total_tokens + input_tokens + output_tokens
cost_delta   = input_tokens  * pricing["input"]  / 1_000_000
             + output_tokens * pricing["output"] / 1_000_000
total_cost   = round(existing_total_cost + cost_delta, 6)
```

`MODEL_PRICING` lives in `config/features/infra_side/model_pricing.py`:

| Model | input (USD / 1M) | output (USD / 1M) |
| :--- | :--- | :--- |
| `glm-5` | 0.5 | 1.5 |
| `deepseek-chat` | 0.14 | 0.28 |
| `kimi-latest` | 0.55 | 2.19 |
| `_default` | 1.0 | 3.0 |

`budget_warn_threshold` = `0.80`.

## ⏰ Task Deadline

`taskflow_create` accepts an optional `deadline_hours` (`taskflow_create.py:17`):

```python
@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
    deadline_hours: float | None = None,
) -> str
```

When `deadline_hours` is positive the tool stores `deadline_ts = time.time() + deadline_hours * 3600` in the `deadline_ts` column. `taskflow_summary` renders the deadline and marks it `EXCEEDED` once passed. The actual enforcement is the background **sweeper** (`agent/tools/subagent/registry/sweeper.py`), which runs `_expire_overdue_taskflows()` on every sweep cycle (`sweeper.py:123`):

```python
overdue = await taskflow_store.get_overdue_flows(time.time())
for flow in overdue:
    state["failure_reason"] = "Deadline exceeded: " + time.strftime("%Y-%m-%d %H:%M", ...)
    update_flow(flow_id, flow["expected_revision"], state=state, status=TaskFlowStatus.FAILED.value)
```

Overdue flows are marked `failed`; already-terminal flows are excluded by the query, and per-flow exceptions are logged and swallowed so one bad row cannot abort the sweep.

## ✅ Result Validation

A step can carry natural-language **acceptance criteria** so the orchestrator has something concrete to judge a child's result against. Both tools accept `validation_criteria`:

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:46
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,     # stored on the step
    retry_policy: dict | None = None,           # (see above)
    session_id: SessionId = "",
) -> str
```

`taskflow_run_task` stores a stripped non-empty `validation_criteria` on the step (both the `blocked` and the `dispatched` write paths). At resume time the criteria are **echoed** back to the orchestrator rather than enforced:

```python
# taskflow_resume.py:146-157
if step is not None and redispatched_key is None:
    step["status"] = str(StepStatus.DONE)
    criteria = (validation_criteria or "").strip()
    if criteria:
        step["validation_criteria"] = criteria
    stored_criteria = str(step.get("validation_criteria") or "").strip()
    if stored_criteria:
        validation_text = (
            f"\n  validation_criteria: {stored_criteria}"
            f"\n  ⚠ Result needs validation against criteria"
        )
```

The tool never evaluates the criteria — it only surfaces them alongside the injected result, and the response gains a trailing `validation_criteria: …` + `⚠ Result needs validation against criteria` block. Two guardrails matter:

- Passing `validation_criteria` to `taskflow_resume` **overrides** the stored value (for example to tighten or correct it based on what the child actually did).
- The echo is emitted only when the step is actually marked `done` (`redispatched_key is None`); a step re-dispatched under the retry policy keeps its criteria stored and gets the echo on the eventual successful resume.

The orchestrator (the main-agent model) is the judge: compare the child result against the criteria, then decide to accept the step, re-dispatch, or fail the flow. There is no automatic pass/fail gate.

## 📊 Progress Report

`taskflow_progress` (`taskflow_progress.py:22`) is a read-only report with completion percentage, breakdown, next steps and an estimated remaining time:

```python
@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str
```

Output shape:

```
Progress Report: <flow_id>
  Status: running
  Description: <first 80 chars>
  Completion: 3/5 steps (60%)
  Breakdown: done=3 · dispatched=1 · ready=0 · blocked=1
  Next steps:
    → [step-4] <task, first 60 chars>
    ⊘ [step-5] <task, first 60 chars>
  Est. remaining: ~12.5 minutes (based on 3 completed steps)
  Waiting on: <wait reason>            # only when the flow is waiting
  Results injected: 3                  # only when results exist
```

Status icons are `done=✓`, `dispatched=→`, `ready=○`, `blocked=⊘` (`taskflow_progress.py:14`). The estimate is produced only when at least **two** `done` steps carry a `dispatched_at` timestamp; it averages the spacing between dispatch timestamps and multiplies by the remaining step count. A flow with no steps returns `Progress: flow_id=…, status=…` plus `No steps registered yet.`

## 💤 Idle Detection

A flow parked with `taskflow_set_waiting` may have its child crash without ever resuming. The sweeper detects this with `_scan_stale_waiting_taskflows()` (`sweeper.py:154`):

1. Read all `waiting` flows.
2. `timeout_secs = TASKFLOW_INFRA["waiting_timeout_hours"] * 3600` (24 h by default).
3. For a wait payload with `set_at`, skip while `now - set_at <= timeout_secs`.
4. Check child liveness via `get_run_by_child_session_key(flow["child_session_key"])` + `is_live_unended_run(run)`. If the child is still live, skip; if the liveness import itself fails, the child is treated as dead.
5. Otherwise stamp a **non-destructive marker** into `wait_json`: `stale_detected_at` and `stale_child_session_key`.

The flow is **never auto-failed** by idle detection — the marker is advisory and refreshed each cycle. Independently, `taskflow_summary` renders the wait status and prints `wait_status: STALE (waiting X.Xh, timeout=24h) — child session may have crashed; consider taskflow_resume with a failure result or re-dispatch` when the wait exceeds `TASKFLOW_INFRA["waiting_timeout_hours"]` (`taskflow_summary.py:67-90`).

## 📋 Session-Scoped Board & Isolation

Every session sees only its own data. `taskflow_summary`, the auto-resume readers **and** `taskflow_list` are all scoped by `session_id`, which the store filters in SQL — a flow owned by another session is indistinguishable from a missing one (`FlowNotFoundError` on mutation, `None` on read):

```python
# agent/tools/taskflow/tools/taskflow_list.py:85
@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active", session_id: SessionId = "") -> str
```

| `status_filter` | Rows |
| :--- | :--- |
| `"active"` (default) | this session's `running` + `waiting` flows |
| `"all"` | this session's flows, terminal statuses included |
| any other value | exact status match (`running`, `waiting`, `done`, `failed`, `cancelled`) |

It is read-only (no `expected_revision`) and backed by `store_sqlite.get_all_flows_sync(session_id, status_filter)`. The sync reader uses the stdlib `sqlite3` path that works without an event loop, orders rows by `expected_revision DESC` (most recently active first), and is fail-open — an init/read failure returns `[]`. `"active"` delegates to `get_active_flows_sync(session_id)`.

The rendered board is a padded text table with fixed columns, capped at 40 chars for the description and 16 for the creator key:

```
TaskFlow board (active): 2 flow(s)
flow_id | status  | description                              | steps | creator          | updated_at
--------+-..----+-..----------------------------------------+-..---+----------------+-..----------
flow-1  | running | Build the parser                         | 2/5   | agent:main:sess  | 2026-09-12 14:03:21
flow-2  | waiting | Wait for the upstream review              | 1/3   | agent:main:sess  | 2026-09-12 13:58:07
```

The schema has **no `updated_at` column** (migration-free). `_last_activity_ts()` (`taskflow_list.py:28`) therefore derives "last updated" as the maximum of the activity stamps persisted anywhere on the flow — `wait.set_at`, every `step.dispatched_at`, and every `result.injected_at` — rendered as a UTC timestamp (`-` when the flow has no stamp at all). An empty registry returns `No task flows found`.

### The three session-owned planning families

| Family | Session linkage | Store |
| :--- | :--- | :--- |
| **TaskFlow** | `task_flows.session_id` column (this plan) | `agent/tools/taskflow/registry/store_sqlite.py` |
| **TodoList** | `session_id` is the table's primary-key prefix — the family was already session-scoped | `agent/tools/todolist/registry/store_sqlite.py` |
| **Knowledge** | per plan identity, enforced in the tool: association comes from `ownership.association_plan_refs()` (the session's `plan_ref` state key, its todos' `plan_ref`s SQL filtered by `session_id`, and `src/data/boulder.json` works whose `plan_name` matches and whose `session_ids` contain the session); `identity.resolve_plan_identity()` then maps the name to the canonical plan path and derives the storage key `sha1(repo-relative path)[:12]`. Same-named plans in different files are physically isolated; one plan file shared through boulder `session_ids` resolves one `<plan_key>` for every listed session. `read` / `write` on a foreign or ambiguous plan are refused with a diagnosable error and `list` returns only associated plans (readable name + key); `build_knowledge_block(session_id)` resolves the session's primary identity for the prompt block; `clear_session` purges the session's private identity directories and keeps plans shared with another session. | `agent/tools/todolist/knowledge/identity.py` |

**Subagent boundary.** All three families are tagged `metadata["scope"] = "main_only"` by their builders (`build_taskflow_tools`, `build_todolist_tools`, `build_knowledge_tools`). `apply_tool_policy` (`agent/tools/subagent/spawn/inherited_tool_policy.py`) drops `main_only` tools **first and unconditionally** — before any allow/deny list and non-overridably by ORCHESTRATOR unblocking — so a spawned child never receives a `taskflow_*`, `todowrite`/`todoread`, or `knowledge` tool. The same tag pattern already covered `memory`, `skill_manage`, `sessions_kill` and `sessions_steer`. `tests/agent/tools/taskflow/test_taskflow_tools.py` locks the real-toolset assertion, and `tests/agent/tools/subagent/test_max_tokens_boost_wiring.py` locks it at the `_build_child_agent` boundary.

**Cross-session refusal.** A foreign `flow_id` collision on `taskflow_create` reports existence without leaking the revision, and a mutation attempt against another session's flow returns the same "not found" text as an unknown id. `tests/agent/tools/taskflow/test_store_sqlite.py`, `test_taskflow_tools.py`, `test_dag_e2e.py` and `tests/server/DAO/test_clear_session.py` cover the read/list/update/purge paths.

