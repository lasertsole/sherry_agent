# OSWorld

The desktop benchmark, TERMINAL-FIRST: the agent gets a DockerBridge terminal
into the task's Ubuntu container and works the command line — GUI/VNC driving
is the plan's declared degradation path (its kill criterion names VNC
instability), so `--vnc` / `SHERRY_OSWORLD_VNC=1` is the opt-in branch and the
report records which mode ran.

## Prerequisites

1. A task image: build the plan's Ubuntu+xfce image once and point the suite
   at it (`OSWORLD_CONTAINER_IMAGE` or `--image`; default
   `sherry-eval-osworld-ubuntu`):
   ```bash
   docker build -t sherry-eval-osworld-ubuntu - < osworld/Dockerfile
   ```
2. The dataset: `OSWORLD_DATA_DIR` (or `--dataset`) — the benchmark's
   `evaluation_examples/examples` JSONs flattened into one JSON/JSONL of
   `{id, instruction, config: {evaluator: {func, expected, ...}}}`.
3. `MAIN_LLM_API_KEY`, Docker.

## Run

```bash
uv run python evals/evals.py osworld --dry-run
uv run python evals/evals.py osworld --all --batch 0-49 --timeout 1800
```

## Grading fidelity

Native judging covers the state getters that map cleanly to `docker exec`:
`vm_file_exists`, `vm_file_content_matches`, `vm_command_line(_result)`. GUI
getters (window state, accessibility trees) are UNJUDGED with an honest
detail — the plan runs Ubuntu-only; macOS/Windows VM tasks are out of scope
for this adapter.
