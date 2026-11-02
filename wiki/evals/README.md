# Evaluations

This tree records how the agent is measured against **external** benchmarks and
what each result does and does not prove. It is deliberately separate from the
product docs: a benchmark measures a harness, a model and an agent together, so
a score is not a capability percentage.

## Suites

| Suite | What it measures | Entry point |
| :--- | :--- | :--- |
| [SWE-Bench](SWE-Bench/README.md) | Real repository issues judged by their own FAIL_TO_PASS / PASS_TO_PASS sets | `evals/swebench/suite.py` |
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
# SWE-Bench HARD slice, with the current eval arm selected by environment:
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

Each run writes `evals/results/swebench/<run_id>/` with `eval_report.json`, the
per-instance diffs and a `sandbox/` tree. The directory is gitignored; this wiki
records the stable conclusions.

## Results

- [SWE-Bench](SWE-Bench/README.md) — failure history, the no-edit gate
  post-mortem, why the read-before-write license did not stop the first soft-gate
  patch loop, and the 8/16 `DelegationPressureGate` result.
