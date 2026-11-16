# Evaluations

This tree records how the agent is measured against **external** benchmarks and
what each result does and does not prove. It is deliberately separate from the
product docs: a benchmark measures a harness, a model and an agent together, so
a score is not a capability percentage.

## Suites

| Suite | What it measures | Entry point |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | External coding tasks with hidden test patches and the benchmark's own grader; `--local` is the Docker-free host mode | `evals/deepswe/suite.py` |
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
# DeepSWE default slice in the Docker-free host mode:
uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200
```

Each run writes `evals/results/deepswe-local/<run_id>/` with `eval_report.json`
and the per-task diffs. The directory is gitignored; this wiki records the
stable conclusions. Docker-backed runs use the benchmark's images and are a
different path from local mode.

## Results

- [DeepSWE](../../evals/deepswe/README.md) — local-mode results and failure
  analysis in `evals/deepswe/RESULTS.md`.
