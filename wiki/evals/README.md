# Evaluations

This tree records how the agent is measured against **external** benchmarks and
what each result does and does not prove. It is deliberately separate from the
product docs: a benchmark measures a harness, a model and an agent together, so
a score is not a capability percentage.

## Suites

| Suite | What it measures | Entry point |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | External coding tasks with hidden test patches and the benchmark's own grader; the default arm runs the benchmark's task images through docker or podman, `--local` is the Docker-free host mode | `evals/deepswe/suite.py` |
| Coding capability | Self-designed coding tasks and the shipped prompt arms | `evals/coding_capability/` |
| External benches | Bridged coding benchmarks and harness adapters | `evals/` |

## Measurement rules

- Report **N of M** on the selected slice; never present a percentage as a
  leaderboard position.
- Separate the **harness** result from the **model** result: a provider error, a
  missing test patch or a timeout is not a failed fix.
- Record the **arm** (neutral prompt, coding template, delegation-pressure),
  the model, the git revision and the exact command.
- A clean diff is part of the verdict: a change under `tests/` or a broken
  PASS_TO_PASS is not a success, even when the target test passes.
- Token and cost figures that cover only the main session must say so; child
  session usage is currently not aggregated by every report.

## Running

```bash
# DeepSWE, container-backed (the benchmark's task images; docker or podman):
uv run --no-sync python evals/deepswe/suite.py --preflight-only  # pull + verifier build + GOLD check, no model
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py --timeout 1800

# DeepSWE default slice in the Docker-free host mode:
uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200
```

`SHERRY_EVAL_CONTAINER_BIN` pins the engine (`docker` / `podman`; the default
prefers docker and falls back to podman). `--preflight-only` proves the whole
container path — image pull, verifier build, GOLD self-check — without spending
a model call; a scored run still needs `MAIN_LLM_API_KEY`. On podman the task
and verifier containers are created with `--http-proxy=false` (podman propagates
the host proxy into every container, and a dead proxy makes the benchmark's own
localhost tests fail) and verifiers are built with `--network none` (WSL2 has no
nftables, so a build-time bridge cannot be set up).

Each run writes `evals/results/deepswe/<run_id>/` with `eval_report.json` and the
per-task diffs. The directory is gitignored; this wiki records the stable
conclusions.

## What the harness enforces

DeepSWE drives the real main agent, so the harness states the rules it needs and
then verifies them instead of trusting the prompt:

- **Orchestration gate** — before any file edit or child spawn the agent must
  have called `todowrite` and opened one TaskFlow flow (`taskflow_flow` with
  `action="plan"`); otherwise the mutating tool answers with the rule. Usage is
  counted per tool and reported (`orchestration_compliant`).
- **Acceptance checklist** — the task text's decidable clauses (normative
  keywords such as *must* / *raise* / *reject* / *only* / *charset*) become a
  numbered checklist in the prompt, and the gate appends it to every
  `sessions_spawn(functional_role="reviewer")` task, so the verifier answers
  PASS/FAIL per clause instead of agreeing with the plan.
- **Plan continuation** — the production auto-turn trigger is a server hook, so
  the harness drives the continuation itself: after each turn it reads the
  sandbox plan registry, and while items are open it sends a bounded
  `[PLAN CONTINUATION]` turn (at most 3). A continuation that changes nothing in
  the diff stops the loop (`no_progress_stop`).
- **Report fields** — `orchestration` (policy, per-task counts, gate counters,
  `acceptance_checklist`, `reviewer_checklist_injections`), `continuations`,
  `no_progress_stop`, plus the verifier verdict and the patch.

Known gap: the container arm bounds each driven turn with `asyncio.wait_for`;
unlike `--local`'s worker process it has no hard kill, so an in-flight provider
call can outlive the deadline.

## Results

- [DeepSWE](../../evals/deepswe/README.md) — local-mode results and failure
  analysis in `evals/deepswe/RESULTS.md`.
