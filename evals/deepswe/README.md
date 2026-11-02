# DeepSWE

DeepSWE is an external coding benchmark: each task pins a repository commit,
ships a hidden test patch, and grades the agent's diff with its own
`tests/grader.py`. The default suite uses the benchmark's Docker images and a
separate verifier image. **`--local` is the Docker-free host mode.**

## Why local mode exists

The default slice is pure Python, and the task images contain nothing the host
toolchain cannot reproduce. A local run still keeps the parts that define the
benchmark:

- the checkout is time-travelled to the pinned commit (future branches/tags and
  `origin` removed), exactly like the image build;
- dependencies come from the task's `environment/Dockerfile` `pip install` lines,
  installed into a per-task `.venv`;
- the hidden `tests/test.patch` and `tests/grader.py` are the judge; the agent's
  `git diff` is applied in a **separate clean checkout**;
- every task's GOLD patch is graded before any model call.

What it does not reproduce: the container's network namespace, base image and
filesystem. A local score is a developer/CI convenience, **not** an official
DeepSWE score.

## Commands

```bash
# Docker-backed (official harness shape; needs Docker and MAIN_LLM_API_KEY)
uv run python evals/deepswe/suite.py --dry-run
uv run python evals/deepswe/suite.py
uv run python evals/deepswe/suite.py --all --n-tasks 5 --keep-workdirs

# Docker-free host mode: Python tasks only
uv run python evals/deepswe/suite.py --local
uv run python evals/deepswe/suite.py --local --skip-self-check
uv run python evals/deepswe/suite.py --local httpx-streaming-json-iteration --timeout 1800
```

`--local` requires `git`, `uv`, network access for the repository clone and the
task dependencies, and `MAIN_LLM_API_KEY`. It writes the report under
`evals/results/deepswe-local/<run_id>/`; task checkouts and venvs live under
`~/.cache/sherry_evals/deepswe-local-work/<run_id>/` so the agent's self-path
guard never sees them (override with `SHERRY_EVAL_LOCAL_WORK_ROOT`).

The agent turn runs in a disposable process group. The `--timeout` value is a
**hard** wall clock: on expiry the worker and its detached subagents are killed,
the partial patch is graded if it applies, and the report records
`timeout: true`. This matters because a production turn can outlive an
`asyncio` cancellation while `sessions_yield` waits for a child.

`--local` removes web tools and sets `PIP_NO_INDEX=1`; it does not create a
network namespace. Do not treat a local run as the benchmark's no-internet
guarantee.

## Verified locally (2026-11-01)

- GOLD self-check, both default tasks: `httpx-streaming-json-iteration`
  1404/1404 P2P + 108/108 F2P; `cattrs-partial-structuring-recovery` 7/7 P2P +
  69/69 F2P.
- A real `cattrs` local run hit the hard timeout at 1119 s; its partial patch
  still graded 60/69 F2P with 7/7 P2P, and the report recorded
  `failure_kind: timeout`. The child/patch paths were captured and graded
  outside the checkout.
- An earlier complete local run returned a 23/69 F2P model result, not a harness
  crash. A run whose checkout was placed under the repository itself was
  invalidated by the agent's self-path write guard; the runner now puts task
  checkouts outside the checkout for that reason.

The local runner's unit tests are in `tests/evals/test_deepswe_local.py`.
Failure records are kept in [RESULTS.md](RESULTS.md).
