# DevBench — SWE-bench Verified

Sherry's main agent on SWE-bench Verified (500 Python instances), following the
DeepSWE suite's architecture: the agent edits a host copy of `/testbed`; the
same tree is bind-mounted into the task's pre-built `sweb.eval.x86_64.<instance>`
image where its `terminal` commands run; grading applies the model patch + the
held-out test patch in that image and judges the official FAIL_TO_PASS /
PASS_TO_PASS lists from parsed pytest output.

## Prerequisites

1. Docker (or Podman, `SHERRY_EVAL_CONTAINER_BIN=podman`) with the pre-built
   task images. Build them ONCE with the official tooling (hours, not this
   suite's job):
   ```bash
   pip install swebench
   python -m swebench.harness.prepare_images --dataset_name princeton-nlp/SWE-bench_Verified
   ```
2. The dataset file (one JSON array of instances — the official
   `test.json`): set `SWEBENCH_DATA_DIR` or pass `--dataset <file>`.
3. `MAIN_LLM_API_KEY` (+ the rest of the MAIN model env).

## Run

```bash
uv run python evals/evals.py devbench --dry-run          # dataset validation only
uv run python evals/evals.py devbench --all --batch 0-49  # a 50-task slice
uv run python evals/evals.py devbench --resume --all      # finish an interrupted run
```

A missing dataset / Docker / key SKIPS loudly (exit 3) — never a silent pass.
A harness bug exits 2; the score gate exits 1 against `baseline.json`.

## Grading fidelity

The in-image pytest pass is a compact reimplementation of the official
`run_evaluation` log judging: the test files are reset to the base commit, the
held-out `test_patch` applies first, then the model patch; `FAIL_TO_PASS` must
all pass and `PASS_TO_PASS` must not regress
(`external_bench.parse_pytest_output` supplies the parsed ids). The report's
`benchmark.grading` field records this. Full-protocol runs (conda env
activation per instance, per-instance env vars) belong to the official
harness; treat this suite's numbers as sherry-harness numbers, not leaderboard
numbers.
