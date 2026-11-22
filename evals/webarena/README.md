# WebArena

The web-agent benchmark on sherry's browser tooling (CDP Chromium, kept by
skipping `trim_tools`). The official docker-compose stack (GitLab / Reddit /
shopping / CMS) runs RESIDENTLY: started once per run through
`evals/shared/container_manager.compose_up`, torn down at the end.

## Prerequisites

1. The official stack: clone `web-arena-g/webarena` (or `web-arena-x` — the
   maintained fork) and point the suite at its compose file:
   `WEBARENA_COMPOSE_FILE=<repo>/docker-compose.yml` or `--compose-file`.
   The images pull GBs; do it once.
2. `WEBARENA_BASE_URL` (default `http://localhost:8080`) — the apps' entry
   point the agent is told about.
3. The task configs: `WEBARENA_DATA_DIR` (or `--dataset`) — the benchmark's
   `config files/*.json` flattened into one JSON/JSONL of
   `{task_id, intent, start_url, evaluator}`.
4. `MAIN_LLM_API_KEY`; a Chromium the browser tool can drive.

## Run

```bash
uv run python evals/evals.py webarena --dry-run
uv run python evals/evals.py webarena --all --batch 0-99
```

## Grading fidelity

Native judging covers the URL-shaped evaluators (`url_last`, `url_regex`) —
where the navigation ended. Element/state/function evaluators need the
OFFICIAL evaluator: set `WEBARENA_REPO_DIR` to a clone; tasks whose spec the
adapter cannot judge are reported UNJUDGED (an honest miss), never
auto-passed.
