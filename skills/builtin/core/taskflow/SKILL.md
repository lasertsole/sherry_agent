---
name: taskflow
description: Durable multi-step task flows with optimistic locking, detached subagent step dispatch, waiting and idempotent resume via the taskflow_* tool family
scope: main_only
---

# TaskFlow

Manage long-running task flows across turns with the `taskflow_*` tool family:
state is persisted to SQLite and supports optimistic locking, step dispatch,
waiting, and resume. Mirrors the API surface of openclaw managedFlows
(createManaged / runTask / setWaiting / resume / finish / fail / requestCancel /
getTaskSummary).

## When to use

- A multi-step job needs its progress tracked across turns (steps, results, status).
- A step should be dispatched to a subagent session and its result written back
  into the flow state once it completes.
- Multiple writers (main session, concurrent sub-tasks) modify the same flow at
  once, requiring conflict detection instead of silent overwrite.

## Tool overview

- `taskflow_create(flow_id, description, initial_state)`: create a flow with
  initial revision=1 and status running.
- `taskflow_run_task(flow_id, task, label, depends_on, expected_revision,
  validation_criteria, retry_policy, response_schema, expected_params,
  input_bindings, judge_criteria, judge_model, functional_role, step_model,
  step_timeout_seconds, priority)`:
  register a step and dispatch a detached subagent session (through the existing
  spawn entry); `child_session_key` is persisted. `validation_criteria` records
  natural-language acceptance criteria on the step (the step judge uses them at
  resume); `retry_policy` opts the step into failure-aware re-dispatch by
  `taskflow_wait_all`. The expectation side of a step is declared here:
  `response_schema` (JSON Schema the child's structured output must satisfy — it
  is handed to the child as its output contract and validated again on resume),
  `judge_criteria` + `judge_model` (semantic acceptance criteria and an optional
  judge model), `expected_params` (structured inputs, recorded for the report),
  and `input_bindings` (e.g. `{"target_files": "step-1.structured_result.files"}`
  — upstream structured values appended to this step's task as an
  `## Input parameters` block). Execution metadata: `functional_role`
  (researcher / executor / reviewer / librarian / general), `step_model`,
  `step_timeout_seconds`, `priority`. `depends_on` is a list of
  prerequisite step ids (e.g. `["step-1"]`): while dependencies are unmet the
  step is recorded as `blocked` and is not dispatched; once every dependency is
  `done` it is dispatched immediately and recorded as `dispatched`. When the
  child session completes, its result flows back automatically through the
  existing announce/settle-wake pipeline — do not poll; once the result arrives,
  inject it with `taskflow_resume`.
- `taskflow_dispatch(flow_id, step_ids, expected_revision)`: batch-dispatch one
  or more steps that are currently `ready` (dependencies satisfied but not yet
  dispatched). Use it to dispatch independent steps in parallel, or to dispatch
  steps unlocked by `taskflow_resume`. If any id is unknown, is already
  `dispatched`/`done`, or still has unmet dependencies, the whole batch is
  rejected — nothing is dispatched and no state changes; if a dispatch fails
  mid-batch, the steps already dispatched successfully are persisted first and
  the failures are reported.
- `taskflow_update_steps(flow_id, steps, expected_revision)`: full-replace the
  flow's steps list (like `todowrite` for TaskFlow). Pass the COMPLETE list;
  each step is an object with `step_id`, `task`, `depends_on`, `status`
  (`ready | blocked | dispatched | done | failed | skipped | cancelled`). Use
  it to add, remove, reorder, or
  rewrite steps. New steps must be `ready`/`blocked` — dispatch still goes
  through `taskflow_dispatch`. A `dispatched` step keeps its
  `child_session_key` and cannot be downgraded to `ready`/`blocked` (its task
  may change, but the running child keeps the old task). A `done` step cannot
  change its task/`depends_on`/status. Removing a `dispatched` step whose
  child is still running succeeds with a non-blocking `Warning:` — kill the
  child or settle it first. Terminal flows reject the call.
- `taskflow_wait_all(flow_id, timeout_seconds, poll_interval_seconds)`: bounded
  polling that waits for the child sessions of all `dispatched` steps in this
  flow to settle. It only checks child sessions dispatched by this flow and does
  not wait on unrelated sessions, so it never gets stuck behind other background
  tasks. Returns the settle status of each step and prompts you to call
  `taskflow_resume` for each completed child session.
- `taskflow_set_waiting(flow_id, wait_reason, expected_revision)`: set the flow
  to waiting and record the wait reason.
- `taskflow_resume(flow_id, child_session_key, result, expected_revision,
  token_usage, validation_criteria, structured_result, step_outcome)`:
  inject the child-session result into the flow state and return to running.
  Idempotent: resuming again with the same (child_session_key, result) does not
  inject a second time, and the revision does not change.

  Two-tier quality gate, both tiers opt-in per step and chained:

  * **Tier 1 — structure.** When the step declares `response_schema`, the result
    (`structured_result` when you pass it, else parsed from `result`) is
    validated against that schema. The verdict is recorded on the result entry
    (`structured_result`, `schema_validated`, and the pairing key `step_id`). A
    failure is a failure signal like any other: it re-dispatches under the
    step's `retry_policy` (`schema_error` is the retry type) and **never** spends
    a judge call; with no retry budget left the step is marked `failed`.
  * **Tier 2 — semantics.** When the step declares `judge_criteria` (or the
    legacy `validation_criteria`), an auxiliary-LLM step judge reviews the
    result — the validated `structured_result` when there is one, else the
    result text — and returns pass / retry / block: `retry` re-dispatches within
    the step's retry budget (`STEP_JUDGE["max_retries"]`, default 2) with the
    judge's guidance appended to the replacement task, and `block` (or an
    exhausted budget) marks the step `blocked`. A Tier 1 pass does **not** skip
    Tier 2: a structurally valid result can still be semantically wrong.

  `step_outcome` (`success` / `failure` / `partial` / `skipped`) lets the caller
  state how the child run ended; it outranks both tiers. `failure` marks the step
  `failed` without calling the judge, `skipped` marks it `skipped`, and
  `success` / `partial` are recorded while the gates still run. The judge is
  fail-open — when it is disabled or unavailable the step is marked `done`.
- `taskflow_finish(flow_id, summary, expected_revision, todo, plan_path,
  checkbox_label)`: mark done (terminal). Gated, fail-open: every step must be
  `done` or `blocked`, no step may be `blocked`, the flow's evidence must have
  no failing/stale row, and — only when you pass the linked `todo` +
  `plan_path` — `SisyphusVerifier` must pass.
- `taskflow_fail(flow_id, reason, expected_revision)`: mark failed (terminal).
- `taskflow_cancel(flow_id, reason, expected_revision)`: cancel the flow (terminal).
- `taskflow_summary(flow_id)`: read-only readback of status, revision,
  child_session_key, steps, and results; also the re-read entry point after a conflict.
- `taskflow_progress(flow_id)`: read-only progress report (user-facing):
  completion percentage, per-status counts, the first 3 pending steps, estimated
  remaining time based on the `dispatched_at` intervals of completed steps, plus
  the wait reason and the number of injected results. Changes no state; use
  `taskflow_summary` when you need raw steps/results or the latest revision.
- `taskflow_budget(flow_id, action, token_budget, expected_revision)`: query or
  set the flow's token/cost budget. `action="query"` reports tokens used,
  estimated cost, remaining quota, and status
  (`ok` / `WARNING` (≥80%) / `EXCEEDED` (≥100%)); `action="set"` sets a
  positive-integer `token_budget`, rejected for terminal flows. When
  `taskflow_resume` receives `token_usage`, the child session's token usage and
  estimated cost accumulate onto the flow.
- `taskflow_list(status_filter)`: read-only board of **this session's**
  TaskFlows (every read filters the owning `session_id` in SQL).
  `status_filter` takes `"active"` (default, running+waiting), `"all"`
  (including terminal), or a concrete status name
  (`running`/`waiting`/`done`/`failed`/`cancelled`). Each row: flow_id, status,
  description (truncated to 40 characters), steps done/total, creator session key
  (truncated to 16 characters), last activity time (the flow's newest timestamp,
  or `-` when none). Ordered by expected_revision descending (most recent
  activity first).

## Optimistic locking and conflict retry

Every mutation goes through `UPDATE ... WHERE flow_id = ? AND expected_revision = ?`;
under concurrent double-writes exactly one succeeds and the other receives a
conflict error carrying the latest revision (e.g. `latest revision=3`). Retry
procedure:

1. Re-read with `taskflow_summary(flow_id)` to get the latest `revision`.
2. Replay your change against the latest state, retrying with
   `expected_revision=<latest revision>`.
3. The expected_revision in the conflict error text is directly usable as the retry value.

Flows in a terminal state (done / failed / cancelled) can no longer be changed;
any mutation is rejected.

## State machine

running -> waiting (set waiting) -> running (resume) -> done / failed / cancelled (terminal).
`taskflow_resume` returns a waiting flow to running; a running flow stays running.

## Step dependencies and status

Besides `task` / `label` / `child_session_key`, a step carries the DAG fields
and, optionally, its expectations:

- `depends_on`: list of prerequisite step ids (e.g. `["step-1"]`). Step ids look
  like `step-1`, `step-2` and are assigned sequentially by `taskflow_run_task`;
  dependencies reference ids, not list positions.
- `status`: step status, one of
  `blocked | ready | dispatched | done | failed | skipped | cancelled` (see
  "Step statuses" below).
- expectations (all optional, all defaulting to "no gate"): `response_schema`,
  `judge_criteria` / `judge_model`, `expected_params`, `input_bindings`,
  `validation_criteria`, `retry_policy`, and the execution metadata
  `functional_role` / `step_model` / `step_timeout_seconds` / `priority`.

Status progression: `blocked -> ready -> dispatched -> done`, with `failed` /
`skipped` / `cancelled` ending a step that did not succeed.

- `blocked`: dependencies are not all `done`; `taskflow_run_task` only registers
  the step and does not dispatch it.
- `ready`: dependencies satisfied, waiting to be dispatched. **Unlocked by
  `taskflow_resume`**: after a prerequisite step is marked `done`, the `blocked`
  steps depending on it become `ready`; `taskflow_resume` only reports the newly
  unlocked step ids and does not dispatch automatically. Unlocking is
  failure-aware: a prerequisite that ended `failed` leaves its dependents
  `blocked`, and one that was `skipped`/`cancelled` cascades `skipped` into them.
- `dispatched`: a detached subagent session was dispatched and
  `child_session_key` was persisted.
- `done`: the child-session result was injected into the flow state via
  `taskflow_resume`.

`taskflow_summary` renders each step's status, depends_on, its configured
expectations (schema properties, judge criteria, bindings, metadata) and the
per-status counts; `taskflow_progress` reports `failed=` / `skipped=` counts and
a "Needs a decision" list naming each unresolved step with its recorded reason.

## Dynamic step editing (`taskflow_update_steps`)

`taskflow_update_steps(flow_id, steps, expected_revision)` full-replaces the DAG:
pass the complete list and it becomes the stored list, so steps can be added,
removed, reordered, or rewritten in one call. Safety rules: `step_id` must be
unique; every `depends_on` entry must reference a `step_id` present in the new
list (no self-dependency); a `dispatched` step keeps its `child_session_key`
and cannot be downgraded to `ready`/`blocked`; a `done` step cannot change its
task, `depends_on`, or status; new steps must be `ready`/`blocked` (dispatch
through `taskflow_dispatch`); terminal flows reject the call. Deleting a
dispatched step whose child is still running succeeds but returns a
non-blocking warning — kill the child or settle it (`taskflow_wait_all` /
`taskflow_resume`) before relying on the new DAG.

## Recommended flow (dependencies + parallelism)

1. Register steps with `taskflow_run_task`: independent steps are dispatched
   immediately; steps with unmet `depends_on` are recorded as `blocked` (not dispatched).
2. Once a prerequisite step's result arrives, inject it with `taskflow_resume`:
   that step becomes `done` and unlocks the `blocked` steps depending on it to
   `ready` (the newly ready step ids are returned).
3. Dispatch those `ready` steps in one `taskflow_dispatch` call (they can be
   dispatched in parallel together with other independent steps).
4. `taskflow_wait_all` waits for the child sessions of the steps dispatched by
   this flow to settle.
5. Call `taskflow_resume` for each completed child session, injecting the results
   one by one; each injection may unlock the next layer of `ready` steps — return
   to step 3 until every step is `done`.

## Step statuses

`blocked → ready → dispatched → done` is the happy path. Three further statuses
describe how a step ended without succeeding:

- `failed` — a definitive failure: the caller declared `step_outcome="failure"`,
  or a `response_schema` result stayed invalid after its retry budget. Its
  dependents stay `blocked` (a failure never unlocks anything); `fail_reason`
  names the cause.
- `skipped` — the step was declared `step_outcome="skipped"`, or a dependency was
  skipped/cancelled (the skip cascades down the branch, so nothing waits forever
  on a result that will not come); `skip_reason` names the cause.
- `cancelled` — `taskflow_cancel` cancelled the flow; every step that had not
  finished is marked `cancelled`.

`taskflow_finish` refuses to complete a flow with any of those pending: they are
a decision for the caller (resume / retry / cancel), not a completion record.

## Known limitations

- A step being `done` means "the result was injected and its configured gates
  passed". With **no** expectation configured (`response_schema`,
  `judge_criteria`/`validation_criteria`, `retry_policy`) a failed child still
  lands `done` and unlocks its successors — declare an expectation (or report
  `step_outcome="failure"`) to make the closure catch it.
- The gate is fail-open by design: an unavailable judge marks the step `done`
  rather than wedging the flow, so a judge outage degrades to the old behaviour.
- `input_bindings` resolve against `structured_result` only; a binding that does
  not resolve contributes `null` to the task text instead of failing dispatch.
- `taskflow_wait_all` only performs bounded polling; a timeout returns a partial
  report. A child session that never settles will not automatically fail the flow.
