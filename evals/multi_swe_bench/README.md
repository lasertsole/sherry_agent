# Multi-SWE-bench

ByteDance's multi-language SWE-bench, reusing the DevBench adapter (the record
schema matches: repo + base commit + problem statement + held-out test patch).

Differences from DevBench:

- dataset: `MULTI_SWE_BENCH_DATA_DIR` (or `--dataset`), a JSON array of
  instances that ALSO carries a `language` field (both `language` and `lang`
  spellings accepted);
- `--lang java|go|rust|...` narrows the run; an unknown value refuses with the
  languages the dataset actually carries;
- the task image comes from the RECORD's `image` field (per-language builds
  are the operator's dataset responsibility) — a record without one is a
  per-task harness skip, never a silent pass.

## Run

```bash
uv run python evals/evals.py multi_swe_bench --dry-run
uv run python evals/evals.py multi_swe_bench --lang java --all --batch 0-49
```

## Verification note

The plan itself flagged the dataset's exact field names as "verify once the
network allowed". The adapter accepts the documented superset
(`language`/`lang`, `FAIL_TO_PASS`/`fail_to_pass`, `image`) and refuses loudly
on anything it cannot grade — verify a small `--dry-run` slice against a real
dataset download before a long run.
