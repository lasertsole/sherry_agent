---
name: ulw-execute
description: Orchestrator execution doctrine. Run a plan file to completion by delegating every implementation unit to subagents, verifying with the Sisyphus contract, and using TaskFlow for dependency-ordered DAG execution.
scope: main_only
---

# ulw-execute: Orchestrated Execution

The execution side of the planning and discipline layer. It drives a plan file (session-scoped `workspace/sessions/<session_id>/plans/*.md`; legacy `.omo/plans/*.md` is still accepted) from start to completion: select the plan, write boulder state, execute the next checkbox, verify and record evidence, mark progress. It does not implement DAG scheduling — that part belongs to TaskFlow.

## Doctrine (MANDATORY)

> YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.
> YOU DO NOT WRITE CODE. YOU DO NOT EDIT PRODUCT FILES.
> EVERY unit of implementation MUST be delegated to a spawned subagent.

The main session is responsible for: building the plan, splitting tasks, delegating, and verifying completion. Any line of product code, test, or fix is delegated to a subagent. Violating this doctrine means the work is considered incomplete.

## The 5-Phase flow

```
Phase 1: Select the plan
  -> Read .omo/boulder.json
  -> List the session plan dir workspace/sessions/<session_id>/plans/*.md
     (legacy .omo/plans/*.md is still accepted; deleting a session deletes its plans)
  -> Match by plan-name, or resume the active work with status="active"

Phase 2: Create or update Boulder state
  -> Write .omo/boulder.json (session_id prefix sherry:)
  -> Register the plan's Phases and Tasks as todos

Phase 3: Execute the next checkbox (all scheduling is delegated to TaskFlow)
  -> Read the plan, find the first unchecked column-0 checkbox
  -> Split it into atomic sub-tasks (completable in one worker run)
  -> Register checkboxes with dependencies as TaskFlow steps:
       taskflow_run_task(flow_id, task, depends_on=[...])
  -> Steps with unmet dependencies are recorded as blocked by TaskFlow and not dispatched
  -> Call taskflow_dispatch(flow_id, step_ids) to dispatch ready steps in parallel
  -> taskflow_wait_all(flow_id) waits for the child sessions dispatched by this flow to settle
  -> taskflow_resume(flow_id, child_session_key, result) injects the result and unlocks successors
  -> DELEGATE EVERYTHING, routing each sub-task through the routing table below

Phase 4: Verify and record evidence
  -> 5 gates (see the Sisyphus contract)
  -> Evidence is written to .omo/ledger.jsonl

Phase 5: Mark progress
  -> Edit the plan checkbox: - [ ] -> - [x]
  -> Re-read the plan and confirm the remaining count decreased
  -> Append a task-completed ledger entry
  -> Continue to the next checkbox without asking whether to continue
```

## Delegation routing table

Every sub-task is routed by category. The categories match the todo `category` field:

| category | Fits |
| -------- | -------- |
| `quick` | Single-file changes of about 10 lines or fewer, or pure lookups |
| `deep` | Multi-file, complex logic, requiring cross-module understanding and reasoning |
| `ultrabrain` | Hard architectural decisions, difficult debugging, problems needing prolonged thought |
| `visual` | UI/frontend/styling/screenshot-verification work |
| `git` | Commits, rebase, history search, branch operations |
| `writing` | Docs, READMEs, comments, human-facing prose output |

The routing decision is written into the todo's `category` field; when `delegation: subagent`, once the subagent is spawned, put the returned `child_session_key` into `subagent_id`.

## Parallel topology

- **Independent lanes -> parallel workers**: tasks with separate files and no shared contract are dispatched in parallel in one batch (`taskflow_dispatch` batch dispatch).
- **Ordered dependency lanes -> serial waves**: when C depends on A and B finishing first, wait for the prerequisite steps to be unlocked via `done` (`depends_on` + the unlock from `taskflow_resume`), then dispatch.
- **Overlapping lanes -> serial or manual coordination**: when concurrent changes to the same module or contract are more likely to break, run them serially.

Principle: parallelize when possible, never race ahead of dependencies, never force concurrency on overlapping work. The basis for judgment is always TaskFlow's `depends_on` and step `status` — do not recompute them at the todo layer.

## The Sisyphus completion contract

A checkbox is truly complete only after passing through three stages:

```
DoneClaim          -> You believe it is done; declare it first, but do not mark completed
AdversarialVerify  -> Run the acceptance criteria, actively look for counterexamples
FullyDone          -> Only after verification passes, mark the checkbox completed
```

5 gates (all must pass, one by one, for FullyDone):

1. **plan reread**: re-read the plan text and confirm the goal and acceptance criteria were not misunderstood.
2. **automated verification**: run automated verification (tests, build, static checks).
3. **manual QA**: actually execute once and observe end-to-end behavior, not just the code.
4. **adversarial QA**: actively probe, covering at least a dirty worktree and misleading success output; for stale-state classes, judge applicability based on the actual caching situation.
5. **cleanup**: clean up temporary resources (sessions, processes, temp files) and confirm no leftovers.

If verification fails, the task is not complete: re-dispatch, or fix and restart from DoneClaim. Unverified completion claims are always rejected.

## DAG execution is delegated to TaskFlow

The orchestration layer only calls TaskFlow; it never rebuilds it:

- `taskflow_run_task(flow_id, task, depends_on=[...])`: register a step and decide whether to dispatch based on dependencies.
- `taskflow_dispatch(flow_id, step_ids)`: batch-dispatch ready steps.
- `taskflow_wait_all(flow_id, ...)`: wait for the child sessions dispatched by this flow to settle.
- `taskflow_resume(flow_id, child_session_key, result)`: inject the result, mark done, unlock successors (idempotent).
- `taskflow_summary(flow_id)`: read-only readback of each step's `status` / `depends_on` and the per-status counts.

The full step state machine (`blocked -> ready -> dispatched -> done`) and the optimistic-lock retry rules are in the `taskflow` skill. Do not implement `depends_on`, waves, or frontier scheduling at the todolist layer.

## Porting notes

This skill was ported from oh-my-openagent (omo)'s `ulw-execute/SKILL.md` and adapted to sherry_agent infrastructure (subagent spawn, auto_turn, SQLite). The upstream repository is unreachable in this environment; the omo excerpts the port was based on have been adapted to this repository's infrastructure (DAG execution delegated to the TaskFlow tools).
