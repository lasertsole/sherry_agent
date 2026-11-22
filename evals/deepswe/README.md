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
  until they do, and per-tool usage is reported (`orchestration_compliant`).
- The task text's decidable clauses become a numbered acceptance checklist in
  the prompt, and the gate appends it to every reviewer spawn
  (`sessions_spawn(functional_role="reviewer")`), so the verdict is per clause.
- The production auto-turn trigger is a server hook; the harness drives plan
  continuation itself (up to 3 turns) and stops as soon as a continuation
  changes nothing (`no_progress_stop`).

## Verified locally (2026-11-01)

- GOLD self-check, both default tasks: `httpx-streaming-json-iteration`
  1404/1404 P2P + 108/108 F2P; `cattrs-partial-structuring-recovery` 7/7 P2P +
  69/69 F2P.

Failure records are kept in [RESULTS.md](RESULTS.md) (the retired local mode's
runs and the container runs are all there).
