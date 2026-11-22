# GAIA

The general-assistant benchmark, host mode (no task container): the agent gets
the FULL tool surface — terminal/python_repl on the host, file tools in the
sandbox workspace, web_search/web_fetch/browser — the task's attachment is
staged into the workspace, and the final `FINAL ANSWER: …` line is scored by
the official normalization + exact match (implemented in
`evals/gaia/suite.py::score_answer`, faithful to the leaderboard's
`gaia_scorer.py`).

## Prerequisites

1. `GAIA_DATA_DIR` (or `--dataset`) with the metadata file — the official
   `2023_validation/metadata.jsonl` shape: `task_id` / `Level` / `Question` /
   `Final answer` / `file_name` (lowercase aliases accepted). The official
   files ship HF-GATED: download them manually once; the suite never
   downloads (missing dataset → skip, exit 3).
2. Attachment files live NEXT TO the metadata in that directory.
3. `MAIN_LLM_API_KEY`.

## Run

```bash
uv run python evals/evals.py gaia --dry-run
uv run python evals/evals.py gaia --all --batch 0-99
```

## Notes

- Level 2/3 tasks expect web access on the HOST (the agent's real web tools);
  an offline host will fail exactly those tasks — that is a measured result,
  not a harness error.
- Level 3 tasks may need a LONG clock: pass `--timeout 3600`.
