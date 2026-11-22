# TAU-bench

The tool-use agent benchmark: a simulated business API (airline / retail), a
user simulator (another model), and the package's own reward over the final
state + expected actions. The adapter wraps the environment's API tools as
real Sherry tools (`evals/tau_bench/api_wrapper.py`) and runs the episode loop.

## Prerequisites

1. `pip install tau-bench` (plus its `litellm` dependency) into this venv.
   A host without the package SKIPS (exit 3) before any model call.
2. `MAIN_LLM_API_KEY` — it also drives the USER SIMULATOR through the
   package's own interface (the adapter passes `user_model` through).

## Run

```bash
uv run python evals/evals.py tau_bench --dry-run
uv run python evals/evals.py tau_bench --scene retail --all --batch 0-49
uv run python evals/evals.py tau_bench --scene airline --all
```

A local tasks JSON may override the package's list
(`TAU_BENCH_DATA_DIR/tasks.json`; records carry `scene` / `instructions` /
`expected_actions` / `outputs` — the README of the dataset documents the
schema; unknown shapes refuse loudly).

## Grading fidelity

The reward is the adapter's generic checker (expected actions must appear in
the recorded call log; expected outputs must appear in the final response) —
the report's `benchmark.grading` field SAYS so. It is not the package's
`task_eval` unless that surface is reachable through
`evals.tau_bench.api_wrapper`'s pinned interface; the unit tests pin exactly
that interface against a fake, so a package refactor fails a test instead of
mis-scoring.
