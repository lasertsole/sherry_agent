# DeepSWE

DeepSWE is an external coding benchmark: each task pins a repository commit,
ships a hidden test patch, and grades the agent's diff with its own
`tests/grader.py`. The suite uses the benchmark's Docker images and a separate
verifier image.

> The Docker-free host mode (`--local`, `evals/deepswe/local.py`) was removed on
> 2026-11-22 once the container path was proven on podman (GOLD 113/113, scored
> runs). Its results remain in [RESULTS.md](RESULTS.md); the worker-process
> hard-timeout scheme it used (`agent_worker.py`) lives in git history and is
> the sketch for the container mode's own hard kill (record bug #10).

## Commands

```bash
# Container-backed (official harness shape; docker or podman + MAIN_LLM_API_KEY)
uv run python evals/deepswe/suite.py --dry-run                 # dataset/task validation only
uv run python evals/deepswe/suite.py --preflight-only          # pull + verifier build + GOLD check, no model
SHERRY_EVAL_CONTAINER_BIN=podman uv run python evals/deepswe/suite.py --timeout 1800
uv run python evals/deepswe/suite.py --all --n-tasks 5 --keep-workdirs
```

## Layered regression

The 117-task benchmark is split into a 12-task **L1** regression set and the
remaining 105 tasks (**L2**). L1 covers all five languages, mixes baselined
with previously untested tasks, and is the only set that can opt into
experience extraction:

```bash
# L1 (~5h): rerun after each change; --experience-extraction is L1-only
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py \
  --experience-extraction --timeout 3600

# L2 (105 tasks, ~53h): run only after L1 passes; no extraction override
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py \
  --timeout 3600
```

The L1 IDs are defined as `L1_TASK_IDS` in
[`suite.py`](suite.py) (`actionlint-action-pinning-lint`,
`bandit-interprocedural-taint-checks`, `arktype-json-schema-refs-dependencies`,
`aiomonitor-task-snapshots-diff`, `abs-module-cache-flags`,
`bandit-incremental-cache-control`, `koota-entity-snapshot-rollback`,
`returns-validated-error-accumulation`, `langchain-request-coalescing`,
`happy-dom-abort-pending-body-reads`, `csstree-shorthand-expansion-compression`,
`pest-character-class-coalescing`). Selecting `L1_TASK_IDS` explicitly with the
flag is equivalent to the short command above. Passing the flag with no L1 task
selected is a usage error; non-L1 tasks in a mixed selection are left on the
historical arm.

Each result records `experience_extraction` (`enabled`, `forced`, `fired`,
`snapshot_hash`) and the report has an `experience_extraction` summary. For L1
tasks with `--experience-extraction`, the eval harness forces the product's
skill-review pass after the run instead of relying on the plan-completion hook,
so the same L1 run can measure both regression and whether extraction actually
produced output.

Task checkouts are staged under the OS temp dir
(`<temp>/sherry_evals/deepswe-work/<run_id>`, override with
`SHERRY_EVAL_WORK_ROOT`) — never inside the product checkout, whose self-path
guard refuses the agent's writes.

`SHERRY_EVAL_CONTAINER_BIN` pins the engine (`docker` / `podman`; the default
prefers docker). The container arm creates the task and verifier containers with
`--http-proxy=false` on podman — podman propagates the host proxy into every
container, and a stale proxy breaks the benchmark's own localhost tests — and
builds verifiers with `--network none` (WSL2 has no nftables).

## What the harness enforces

The agent runs for real, so the harness states its rules and then verifies them:

- `todowrite` plus one TaskFlow flow (`taskflow_flow(action="plan")`) must exist
  before any file edit or child spawn; the mutating tools answer with the rule
  until they do, and per-tool usage is reported (`orchestration_compliant`). The
  counters are bucketed PER SESSION — one suite drives every task in one
  process, so an earlier task's planning never unlocks a later task — and a
  read-only `todoread` is not planning. A child agent is counted but never
  gated (it has no `todowrite`/`taskflow_flow`). Each task's own numbers land in
  `orchestration_gate`; `orchestration.gate` is the run-wide total.
- The task text's decidable clauses become a numbered acceptance checklist in
  the prompt, and the gate appends it to every reviewer spawn
  (`sessions_spawn(functional_role="reviewer")`), so the verdict is per clause.
- The production auto-turn trigger is a server hook; the harness drives plan
  continuation itself (up to 3 turns) and stops as soon as a continuation
  changes nothing (`no_progress_stop`). The reader is scoped to the task's OWN
  session: the plan registry is shared by every task in a run, so an unscoped
  read let one task's unfinished plan drive the next one.
- `--timeout` is the budget of the WHOLE drive: every driven turn (first or
  continuation) draws from one deadline, a turn that would start with nothing
  left does not start, and the report carries `budget_s`/`deadline_hit`.
- A terminal provider error (`insufficient_balance`, `invalid_api_key`,
  401/402/403) stops the suite after TWO consecutive tasks: the remaining ones
  are never started and are reported as `not_run` (a 429 or a timeout never
  trips this). `aggregate` then carries `attempted`/`not_run` and
  `resolve_rate_all` beside the valid-run `resolve_rate`.

## Verified locally (2026-11-01)

- GOLD self-check, both default tasks: `httpx-streaming-json-iteration`
  1404/1404 P2P + 108/108 F2P; `cattrs-partial-structuring-recovery` 7/7 P2P +
  69/69 F2P.

Failure records are kept in [RESULTS.md](RESULTS.md) (the retired local mode's
runs and the container runs are all there).
