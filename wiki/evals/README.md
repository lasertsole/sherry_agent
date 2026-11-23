# Evaluations

This tree records how the agent is measured against **external** benchmarks and
what each result does and does not prove. It is deliberately separate from the
product docs: a benchmark measures a harness, a model and an agent together, so
a score is not a capability percentage.

## Suites

| Suite | What it measures | Entry point |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | External coding tasks with hidden test patches and the benchmark's own grader; the default arm runs the benchmark's task images through docker or podman | `evals/deepswe/suite.py` |
| [DevBench (SWE-bench Verified)](../../evals/devbench/README.md) | Official F2P/P2P test lists judged from an in-image pytest pass; the agent edits a host copy of `/testbed` | `evals/devbench/suite.py` |
| [Multi-SWE-bench](../../evals/multi_swe_bench/README.md) | The devbench core over ByteDance's multi-language records (`--lang` filter; per-record images) | `evals/multi_swe_bench/suite.py` |
| [TAU-bench](../../evals/tau_bench/README.md) | Simulated business API wrapped as sherry tools; generic reward over recorded calls + outputs | `evals/tau_bench/suite.py` |
| [GAIA](../../evals/gaia/README.md) | General-assistant tasks, host mode, full tool surface; official answer normalization | `evals/gaia/suite.py` |
| [CyberSecEval-style](../../evals/cybersec/README.md) | Security MCQ / code analysis, host mode, choice-letter match | `evals/cybersec/suite.py` |
| [WebArena](../../evals/webarena/README.md) | The docker-compose web stack kept resident; browser tools kept; URL-shaped evaluators judged natively | `evals/webarena/suite.py` |
| [VisualWebArena](../../evals/visualwebarena/README.md) | WebArena + vision deltas (JS-evaluate on, vision-model evidence in the report) | `evals/visualwebarena/suite.py` |
| [OSWorld](../../evals/osworld/README.md) | Ubuntu task containers, TERMINAL-FIRST (VNC degraded per the plan); vm_* state getters judged natively | `evals/osworld/suite.py` |

Shared infrastructure lives in `evals/shared/`: dataset resolution, batch
slicing + resume, the uniform report (with the plan's token / context-hit-rate
/ code-intel summaries), the per-suite container lifecycle including compose
projects, and two report analyzers (`context_analyzer`, `code_intel_tracker`).
Those two are thin adapters over the product layer's `pub.func.session_report`
analyzers — shared algorithms, eval-only report shapes — and token folding
delegates to the product's `session_tokens()`; there is no second copy of the
tracking code. Every suite is dataset-optional by contract: a missing
dataset, container runtime or key SKIPS loudly (exit 3), never a silent pass.
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

Known gap: the container arm bounds each driven turn with `asyncio.wait_for`; it
has no hard kill for an in-flight provider call, which can outlive the deadline
(the retired `--local` mode's worker-process kill is the sketch for fixing this).

## Task results (tasks 1-14)

The first 14 tasks in sweep order were measured across the container runs of
2026-11-22 (`20261122_044351` … `20261122_191618`). "Best" is that task's
strongest run; `F2P` is the benchmark's fail-to-pass set, `P2P` the
pass-to-pass regression set that must stay green, and orchestration means both
`todowrite` and a TaskFlow flow were used.

| # | Task | Lang | Best F2P | P2P | Wall clock | Orchestration |
|---|---|---|---|---|---|---|
| 1 | abs-module-cache-flags | Go | **20/20 - resolved** | 3/3 | 3243 s | ok |
| 2 | abs-stepped-slices | Go | 5/6 | 6/6 | 1859 s | ok |
| 3 | actionlint-action-pinning-lint | Go | **55/55 - resolved** | 145/145 | 446 s | VIOLATED (no flow) |
| 4 | adaptix-name-mapping-aliases | Python | 38/44 | 2738/2738 | 1608 s | ok |
| 5 | aiomonitor-task-snapshots-diff | Python | 51/53 | 8/8 | 2241 s | ok |
| 6 | anko-default-function-arguments | Go | 0/2 | 119/119 | 1468 s | VIOLATED (no flow) |
| 7 | anko-typed-variable-bindings | Go | 7/9 | 94/94 | 787 s | ok |
| 8 | arcane-drift-detection-baselines | Go | **82/82 - resolved** | 2/2 | 2072 s | ok (todo 66) |
| 9 | arktype-json-schema-refs-dependencies | TypeScript | 23/25 | 1679/1679 | 2147 s | ok |
| 10 | awilix-async-container-initialization | TypeScript | 19/24 | 162/162 | 870 s | ok |
| 11 | bandit-incremental-cache-control | Python | 79/88 | 275/275 | 1989 s | ok |
| 12 | bandit-interprocedural-taint-checks | Python | 65/66 | 293/293 | 949 s | ok |
| 13 | bandit-structured-nosec-directives | Python | 65/69 | 281/282 | 1238 s | ok |
| 14 | boa-hierarchical-evaluation-cancellation | Rust | 0/17 | 7/7 | 2196 s | ok |

What these rows say:

- **3 of 14 resolved** (21%), with five more within a handful of tests
  (12: 65/66, 13: 65/69, 5: 51/53, 9: 23/25, 11: 79/88).
- **F2P is a gradient, not a binary.** Partial credit separates "implemented
  most of the feature" from "did nothing", which is what a single resolved
  bit would hide.
- **Budget matters more than any single fix.** Task 1 scored 9/20 under a
  1800 s turn budget and 20/20 under 7200 s with the same model — the run was
  still making progress when the clock ran out.
- **The two VIOLATED rows are real**: tasks 3 and 6 were worked through
  `terminal` alone, before the orchestration gate covered the shell tools.
- **One regression signal**: task 13 lost 1 P2P test (281/282) — a changed
  behaviour the hidden suite caught.
- Tasks 10-13's first attempts never ran: the provider returned HTTP 402
  (insufficient balance) and each task failed in about 20 s with zero tool
  calls. Those attempts are provider failures, not model results.
- Task 14's first attempt was a harness failure (16.9 s); the rerun completed
  normally. A transient task-image pull now retries instead of aborting the
  sweep.

## Results

- [DeepSWE](../../evals/deepswe/README.md) — run records and failure analysis
  in `evals/deepswe/RESULTS.md`.
