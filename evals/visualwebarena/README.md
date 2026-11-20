# VisualWebArena

WebArena's vision sibling — the webarena adapter with the plan's three deltas:

- `SHERRY_BROWSER_ALLOW_EVALUATE=1` is set by the suite (the browser's
  JS-evaluate permission for the visual checks);
- the report records whether the MAIN model is vision-capable
  (`benchmark.vision_model`) — screenshots must REACH the model for the tasks
  to be answerable; a non-vision model turns every visual task into a
  text-only guess, which is a measured result, not an error;
- the dataset comes from `VISUALWEBARENA_DATA_DIR` (same record shape as
  WebArena's).

## Run

```bash
uv run python evals/evals.py visualwebarena --dry-run
uv run python evals/evals.py visualwebarena --all --batch 0-99
```

See `evals/webarena/README.md` for the environment prerequisites (the same
compose stack plus the VWA-specific images the fork ships).
