"""DeepSWE eval suite — the external benchmark, driven by the real main agent.

What is measured: DeepSWE's own binary reward, task by task. The agent edits a
host copy of the task's pinned checkout; the same tree is bind-mounted into the
task's official Docker image, where its ``terminal`` commands run with the
task's toolchain. Grading happens in the benchmark's separate verifier image:
the agent's ``git diff`` is applied there together with the held-out test patch,
and the task's ``grader.py`` produces ``reward.json``.

Faithfulness notes (also written into the report):

- the agent sees ``instruction.md`` only — the tests, the verifier and the
  reference patch stay outside the session;
- every task's GOLD patch is run through the verifier before any model call; a
  task the reference solution cannot resolve is a harness failure, not a model
  failure (``--skip-self-check`` skips that check, never the verifier);
- the agent container runs with ``--network none`` and the host agent's web
  tools are removed, so the no-internet rule is enforced on both sides;
- the verifier verdict is the only pass signal. Docker and the task images are
  prerequisites: without them the suite SKIPS loudly and exits non-zero.

Usage::

    uv run python evals/evals.py deepswe
    uv run python evals/deepswe/suite.py --dry-run
    uv run python evals/deepswe/suite.py httpx-streaming-json-iteration --timeout 1800
    uv run python evals/deepswe/suite.py --l1 --experience-extraction --timeout 3600
    uv run python evals/deepswe/suite.py --all --n-tasks 5 --keep-workdirs

``--dry-run`` validates the dataset and task files without Docker or a model.
``--preflight-only`` runs the container path (pull / verifier build / GOLD
self-check) without a model key, so a broken engine fails before tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import shutil
import tempfile
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import external_bench  # noqa: E402
from evals.deepswe import instances  # noqa: E402
from evals.deepswe.instances import Task  # noqa: E402
from evals.shared.eval_checklist import acceptance_checklist  # noqa: E402
from evals.shared.eval_iteration_budget import (  # noqa: E402
    pin_eval_budget,
    resolve_eval_iteration_budget,
)
from evals.shared.eval_temperature import pin_eval_temperature  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
_DEFAULT_TIMEOUT_S = 3600.0
_MAX_TIMEOUT_S = 7200.0
_NETWORK = "no-network"
#: Task-image pull robustness: a transient registry failure (public ECR resets
#: a layer fetch mid-burst) retries with a growing pause before the whole sweep
#: is declared a harness failure.
_PULL_RETRIES = 3
_PULL_RETRY_PAUSE_S = 20.0
#: The LEAF budget of the spawn depth ladder (``resolve_run_timeout_for_requester``):
#: 1× the configured spawn default (now 1800 s), because the product default was
#: raised from 600 s to 1800 s to cover a full read + implement + run-tests step
#: (DeepSWE: 9/20 resolved at 1800 s vs 20/20 at 7200 s). The eval no longer
#: compensates with a 3× multiplier — it matches the product. Under the ladder
#: this value governs the LEAF; a depth-1 step child gets 3× IT (1800 → 5400).
#: ``SHERRY_EVAL_CHILD_RUN_TIMEOUT_S`` (seconds) overrides the value outright,
#: clamped to the spawn bounds.
_EVAL_CHILD_RUN_TIMEOUT_MULTIPLIER = 1.0
_EVAL_CHILD_RUN_TIMEOUT_ENV = "SHERRY_EVAL_CHILD_RUN_TIMEOUT_S"

#: L1 regression set (plan 2026-11-23): 12 tasks, all five languages, a mix of
#: baselined and untested tasks. This is the ONLY set that may opt into
#: experience extraction; L2/full runs deliberately keep the historical path so
#: the two arms stay comparable.
L1_TASK_IDS: tuple[str, ...] = (
    "actionlint-action-pinning-lint",
    "bandit-interprocedural-taint-checks",
    "arktype-json-schema-refs-dependencies",
    "aiomonitor-task-snapshots-diff",
    "abs-module-cache-flags",
    "bandit-incremental-cache-control",
    "koota-entity-snapshot-rollback",
    "returns-validated-error-accumulation",
    "langchain-request-coalescing",
    "happy-dom-abort-pending-body-reads",
    "csstree-shorthand-expansion-compression",
    "pest-character-class-coalescing",
)
_L1_TASK_ID_SET = frozenset(L1_TASK_IDS)


def is_l1_task(task_id: str) -> bool:
    """True when *task_id* belongs to the 12-task L1 regression set."""
    return task_id in _L1_TASK_ID_SET


def resolve_experience_extraction(task_id: str, requested: bool) -> bool:
    """Only L1 tasks may enable the extraction arm, even if the flag is set."""
    return bool(requested and is_l1_task(task_id))


def _eval_child_run_timeout_s() -> float:
    """The eval's step-child wall clock: 3× the configured spawn default.

    ``SHERRY_EVAL_CHILD_RUN_TIMEOUT_S`` (seconds) overrides the multiplier's
    result outright; the value is clamped to the spawn bounds (a 60 s floor,
    the configured 7200 s ceiling).
    """
    from agent.tools.subagent.config import get_config

    cfg = get_config()
    default = cfg.run_timeout_seconds * _EVAL_CHILD_RUN_TIMEOUT_MULTIPLIER
    raw = os.environ.get(_EVAL_CHILD_RUN_TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(60.0, min(value, cfg.run_timeout_max_seconds))


_PROMPT = """{instruction}

---
You are working in `{repo}` at commit `{base}`. The checkout is this session's
project directory; the SAME tree is mounted in the task container at `/app`,
and the task text's `/app` paths were rewritten to that project directory. The
`terminal` tool runs inside the container with the task's official toolchain;
`read_file`, `write_file` and `patch_file` edit the same bytes from the host
side. Use both to build and run the project's checks.

- There is no internet access.
- Do not modify test files and do not run `git commit`.
- Complete the task; do not ask questions.
- The project directory is `{project_dir}` and is the ONLY valid root for file tools. Never use `/app`, `C:\app`, or any path outside it — the terminal translates project paths for you.
- Edge cases are part of the contract: empty input, whitespace-only input, partial/truncated input, and calls made after the stream has ended must behave predictably — an empty result, not an exception, unless the task says otherwise. Recursive or cyclic inputs (self-referencing schemas, linked structures) must terminate with a clear error or a well-defined result — never by overflowing the call stack.

{checklist}

## Orchestration mandate (harness requirement, not advice)

Plan and execute this task THROUGH Sherry's planning and orchestration tools;
the harness records tool usage and reports a run that skips them:

1. First call `todowrite` to plan the work (investigation, fix, verification
   items) and keep every item updated as you go; `todoread` shows the list.
2. Create ONE TaskFlow flow for the task with `taskflow_flow(action="plan")`
   (a short description is enough), dispatch and drive its steps through
   `taskflow_step` (spawn a step child for the implementation), and close it
   with `taskflow_flow(action="finish")` when the flow's criteria are met.
3. Keep the todolist and the flow board consistent: an item or step is DONE
   only when its verification command passed.

## Parallelism (harness requirement, not advice)

Decompose the flow so it RUNS in parallel, not as one serial chain:

- Write `depends_on` ONLY for real data dependencies (a step consumes another
  step's output). Independent steps MUST be sibling branches: two
  investigations, the implementation and its documentation, per-file fixes, a
  fix and its benchmark — all without an edge between them.
- Never chain steps that could run together. A flow whose every step has
  out-degree 1 wastes the engine: READY siblings are dispatched in waves and
  run as concurrent children.
- After every result lands, the resume re-scans the whole flow and dispatches
  every newly READY step — do not serialize on your own; let the board's
  ready set drive the wave.
- While step children run, do one `taskflow_step(action="wait_all")` (it
  wakes as results land) instead of polling with terminal commands.
- Plan ONCE. After `taskflow_flow(action="plan")` returns, do NOT rewrite the
  step list with `taskflow_step(action="update_steps")` — hand-writing a long
  step array is the most common source of malformed tool arguments and kills
  the turn. To correct course, dispatch a remaining ready step
  (`action="dispatch"`) or wait; add follow-up work as ONE short step, never a
  full-list rewrite.
"""


#: One extra driven turn per unfinished plan item, bounded like the production
#: continuation ceiling (the harness owns the auto-turn the server would fire).
_MAX_PLAN_CONTINUATIONS = 3

#: How long grading may be deferred for step children still in flight. The
#: graded patch is the whole score, so a just-finished child's edits must land
#: before it is captured; past this window the run grades what exists.
_FLOW_SETTLE_WAIT_S = 180.0

#: Failure kinds that mean the run never produced a verdict about the agent, so
#: the task must not count as a (resolved or unresolved) attempt:
#: ``provider`` (terminal API error), ``cancelled`` (the turn was cancelled —
#: measured 2026-11-27: pest's turn ended at 981.8 s with ``Request cancelled``
#: and was graded as a normal failure), ``harness`` (the harness itself raised /
#: the verifier produced no reward, e.g. the stale-junit grade of 2026-11-27).
_INVALID_FAILURE_KINDS: frozenset[str] = frozenset({"provider", "cancelled", "harness"})

#: A terminal provider error does not heal on its own (balance exhausted, key
#: revoked). Two consecutive runs failing this way stop the suite: the remaining
#: tasks are reported as NOT RUN, never as failed work (measured: four tasks ran
#: the whole extract/start/verify pipeline after the balance was already gone).
_PROVIDER_ABORT_AFTER = 2
_TERMINAL_PROVIDER_MARKERS = (
    "insufficient_balance",
    "insufficient balance",
    "invalid_api_key",
    "error code: 401",
    "error code: 402",
    "error code: 403",
)


#: A driven turn may not START with less than this left in the task budget.
_DRIVE_BUDGET_FLOOR_S = 1.0


def _drive_deadline(timeout: float) -> float:
    """The one deadline every driven turn of a task shares (see _next_drive_turn)."""
    return time.monotonic() + timeout


class BudgetSpentError(Exception):
    """The task's single drive budget ran out before another turn could start."""


async def _next_drive_turn(harness: Any, session_id: str, prompt: str, deadline: float) -> Any:
    """Run one driven turn inside the task's deadline.

    EVERY driven turn shares one budget (``--timeout``), so the wait is the time
    left, not the whole flag again (measured: 9434 s against the 3600 s default).
    Raises :class:`BudgetSpentError` when nothing usable is left and lets
    ``asyncio.TimeoutError`` through when the turn itself overruns the deadline.
    Both suites drive through this one function.
    """
    remaining = deadline - time.monotonic()
    if remaining < _DRIVE_BUDGET_FLOOR_S:
        raise BudgetSpentError
    return await asyncio.wait_for(harness._drive(session_id, prompt), timeout=remaining)


def _is_terminal_provider_error(text: str) -> bool:
    """True for an error the next task cannot get past (429/timeouts are NOT)."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _TERMINAL_PROVIDER_MARKERS)


class _ProviderAbort:
    """Counts CONSECUTIVE terminal provider failures; the loop stops when tripped."""

    def __init__(self, threshold: int = _PROVIDER_ABORT_AFTER) -> None:
        self.threshold = threshold
        self.count = 0

    @property
    def tripped(self) -> bool:
        return self.count >= self.threshold

    def observe(self, result: dict[str, Any]) -> None:
        if result.get("invalid_run") and _is_terminal_provider_error(
            str(result.get("error") or "")
        ):
            self.count += 1
        else:
            self.count = 0


_CONTINUATION_PROMPT = """[SYSTEM DIRECTIVE: PLAN CONTINUATION]

Your plan is NOT finished. Open items:
{items}

{checklist}

Finish them now — including the system [VERIFY] acceptance checks — close the
TaskFlow flow with taskflow_flow(action="finish"), then summarize. The [VERIFY]
reviewer must answer EVERY acceptance item above with PASS/FAIL and evidence.
Do not ask questions."""

#: Injected instead when the flow still has DISPATCHED/READY steps: the work is
#: in flight, so the agent's job this turn is to WAIT for the results, resume
#: the steps, and only then close the flow. A generic "finish now" directive
#: here made an agent declare completion while four children were mid-flight.
_WAIT_PROMPT = """[SYSTEM DIRECTIVE: PLAN CONTINUATION]

Your plan is NOT finished — step children are still running. Open items:
{items}

Call `taskflow_step(action="wait_all")` now to wait for the in-flight steps,
then resume each settled step and continue closing the flow with
`taskflow_flow(action="finish")` when every step is terminal. Do not declare
completion while steps are running. Do not ask questions."""


def _open_plan_items(results_dir: Path, session_id: str) -> list[str]:
    """Open todos + non-terminal flows for ONE session's plan.

    The production auto-turn trigger is a SERVER hook; the eval harness has no
    server, so ``PlanContinuationEnforcer._inject`` silently returns False and
    a mandatory [VERIFY] todo would never run. This reader is what lets the
    harness drive the continuation itself.

    The registry is shared by every task in a run, so the read is SCOPED: an
    unscoped read reported another task's open todos as this task's plan (a
    finished task then never took the "plan complete" exit and kept receiving
    continuations) and another task's flow as in-flight (injecting the WAIT
    prompt into a flow-less task).
    """
    # EvalSandbox redirects the plan registry under its own root; older runs
    # may keep it at the results root.
    candidates = (
        results_dir / "sandbox" / "stores" / "registry.db",
        results_dir / "stores" / "registry.db",
    )
    database = next((path for path in candidates if path.is_file()), None)
    if database is None:
        return []
    items: list[str] = []
    try:
        with sqlite3.connect(database, timeout=5) as connection:
            for content, status in connection.execute(
                "SELECT content, status FROM todos "
                "WHERE session_id = ? AND status NOT IN ('completed', 'cancelled') "
                "ORDER BY todo_id",
                (session_id,),
            ):
                items.append(f"todo: {str(content)[:100]} ({status})")
            for flow_id, status in connection.execute(
                "SELECT flow_id, status FROM task_flows "
                "WHERE session_id = ? AND status NOT IN ('done', 'failed', 'cancelled')",
                (session_id,),
            ):
                items.append(f"flow: {flow_id} ({status})")
    except sqlite3.Error:
        return items
    return items


def _flow_in_flight(results_dir: Path, session_id: str) -> bool:
    """True when any non-terminal flow of THIS session has READY/DISPATCHED steps.

    Reads the same (session-scoped) sandbox registry as ``_open_plan_items``;
    a read failure is reported as False (the generic continuation then applies).
    """
    candidates = (
        results_dir / "sandbox" / "stores" / "registry.db",
        results_dir / "stores" / "registry.db",
    )
    database = next((path for path in candidates if path.is_file()), None)
    if database is None:
        return False
    try:
        with sqlite3.connect(database, timeout=5) as connection:
            rows = list(
                connection.execute(
                    "SELECT state_json FROM task_flows "
                    "WHERE session_id = ? AND status NOT IN ('done', 'failed', 'cancelled')",
                    (session_id,),
                )
            )
    except sqlite3.Error:
        return False
    for (raw,) in rows:
        try:
            steps = (json.loads(raw) or {}).get("steps") or []
        except (TypeError, ValueError):
            continue
        if any(
            isinstance(step, dict) and str(step.get("status") or "") in {"ready", "dispatched"}
            for step in steps
        ):
            return True
    return False


def _work_root(results_dir: Path) -> Path:
    """Where task checkouts live: OUTSIDE the sherry checkout.

    The agent's self-path guard refuses writes inside the product checkout, so
    a task tree under ``evals/results`` makes every real edit fail (measured:
    three attempts produced an EMPTY patch while the agent believed it had
    written the module). Local mode already stages its checkouts outside;
    the container arm now does the same. ``SHERRY_EVAL_WORK_ROOT`` overrides.
    """
    override = os.environ.get("SHERRY_EVAL_WORK_ROOT", "").strip()
    base = (
        Path(override)
        if override
        else Path(tempfile.gettempdir()) / "sherry_evals" / "deepswe-work"
    )
    return base / results_dir.name


def _acceptance_checklist(instruction: str, *, limit: int = 8) -> list[str]:
    """Backward-compatible local alias for the shared checklist extractor."""
    return acceptance_checklist(instruction, limit=limit)


def _checklist_block(items: list[str]) -> str:
    """The prompt block that makes the acceptance items explicit."""
    if not items:
        return ""
    lines = "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))
    return (
        "## Acceptance checklist (from the task text)\n\n"
        "Every item is binary: the work is done only when each one is PROVEN.\n\n" + lines
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the suite (re-executing in a clean interpreter when needed)."""
    external_bench.maybe_reexec("SHERRY_EVAL_DEEPSWE_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == "deepswe":
        args = args[1:]

    wanted: list[str] = []
    flags: set[str] = set()
    n_tasks: int | None = None
    timeout = _DEFAULT_TIMEOUT_S
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--n-tasks":
            if index + 1 >= len(args):
                raise SystemExit("--n-tasks needs a value")
            n_tasks = int(args[index + 1])
            index += 2
            continue
        if arg.startswith("--n-tasks="):
            n_tasks = int(arg.split("=", 1)[1])
        elif arg == "--timeout":
            if index + 1 >= len(args):
                raise SystemExit("--timeout needs a value")
            timeout = float(args[index + 1])
            index += 2
            continue
        elif arg.startswith("--timeout="):
            timeout = float(arg.split("=", 1)[1])
        elif arg in {
            "--all",
            "--dry-run",
            "--skip-self-check",
            "--keep-workdirs",
            "--update-baseline",
            "--preflight-only",
            "--l1",
            "--experience-extraction",
        }:
            flags.add(arg)
        elif arg.startswith("--"):
            raise SystemExit(f"unknown flag: {arg}")
        else:
            wanted.append(arg)
        index += 1

    if wanted and "--all" in flags:
        raise SystemExit("name task ids or use --all, not both")
    if "--l1" in flags and (wanted or "--all" in flags):
        raise SystemExit("--l1 selects the fixed 12-task set; do not pass task ids or --all")
    timeout = max(60.0, min(float(timeout), _MAX_TIMEOUT_S))

    if "--dry-run" not in flags:
        docker_ok, docker_detail = external_bench.docker_available()
        if not docker_ok:
            print(
                f"[evals] deepswe SKIPPED: Docker is not usable ({docker_detail}). "
                "A skipped suite is not a pass.",
                flush=True,
            )
            raise SystemExit(3)

    try:
        task_root, commit = instances.fetch_tasks()
        tasks = instances.load_tasks(task_root)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    if "--l1" in flags:
        ids = L1_TASK_IDS
    else:
        ids = (
            tuple(wanted)
            if wanted
            else tuple(tasks)
            if "--all" in flags
            else instances.DEFAULT_SLICE
        )
    if n_tasks is not None:
        ids = ids[: max(0, n_tasks)]
    try:
        selected = instances.select(tasks, ids)
    except (KeyError, ValueError) as exc:
        print(f"[evals] {exc}", flush=True)
        raise SystemExit(2) from exc

    experience_extraction = "--experience-extraction" in flags
    if experience_extraction:
        l1_ids = [task.task_id for task in selected if is_l1_task(task.task_id)]
        if not l1_ids:
            raise SystemExit(
                "--experience-extraction only applies to the 12-task L1 set; "
                "no L1 task was selected"
            )
        skipped = [task.task_id for task in selected if task.task_id not in _L1_TASK_ID_SET]
        print(
            f"[evals] experience extraction enabled for {len(l1_ids)} L1 task(s)"
            + (f"; skipped for {len(skipped)} non-L1 task(s)" if skipped else ""),
            flush=True,
        )

    print(f"[evals] DeepSWE tasks: {len(tasks)} at {task_root} (commit {commit})", flush=True)
    if "--dry-run" in flags:
        for task in selected:
            print(
                f"[evals] dry-run ok: {task.task_id} lang={task.language} "
                f"base={task.base_commit[:10]} image={task.image}",
                flush=True,
            )
        print(
            "[evals] dry-run only; a real run needs Docker and MAIN_LLM_API_KEY; "
            "a skipped suite is not a pass",
            flush=True,
        )
        return

    raise SystemExit(
        asyncio.run(
            _run(
                selected,
                timeout=timeout,
                skip_self_check="--skip-self-check" in flags,
                keep_workdirs="--keep-workdirs" in flags,
                update_baseline="--update-baseline" in flags,
                preflight_only="--preflight-only" in flags,
                experience_extraction=experience_extraction,
            )
        )
    )


# ── Suite setup ─────────────────────────────────────────────────────────────


async def _run(
    selected: list[Task],
    *,
    timeout: float,
    skip_self_check: bool,
    keep_workdirs: bool,
    update_baseline: bool,
    preflight_only: bool = False,
    experience_extraction: bool = False,
) -> int:
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / "deepswe" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = _session_names(live_workspace)

    docker_ok, docker_detail = external_bench.docker_available()
    if not docker_ok:
        print(
            f"[evals] deepswe SKIPPED: Docker is not usable ({docker_detail}). "
            "A skipped suite is not a pass.",
            flush=True,
        )
        return 3

    if preflight_only:
        # Validate the whole container path (pull, verifier build, GOLD grading)
        # WITHOUT a model key: the expensive turn is gated on MAIN_LLM_API_KEY,
        # but everything before it can and should be proven working first.
        prepared = _preflight(
            selected,
            results_dir,
            skip_self_check=skip_self_check,
            docker_detail=docker_detail,
        )
        if prepared is None:
            return 2
        for task, verifier_image in prepared:
            print(f"[evals] preflight ok: {task.task_id} (verifier {verifier_image})", flush=True)
        print(
            f"[evals] preflight-only: {len(prepared)} task(s) proven on "
            f"{external_bench.container_bin()}; a scored run still needs MAIN_LLM_API_KEY",
            flush=True,
        )
        if not keep_workdirs:
            shutil.rmtree(_work_root(results_dir), ignore_errors=True)
        return 0

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    # Bind the REAL build_main_tools before the sandbox applies: agent.core
    # imported it at module load, so this import must happen before the
    # sandbox redirects persistence.
    import agent.core  # noqa: F401

    # Pin the eval controls AFTER the workspace override and config hydration:
    # importing server.service.messages too early would freeze WORKSPACE_DIR
    # onto the live persona directory, defeating the sandbox isolation.
    pin_eval_budget(resolve_eval_iteration_budget())
    pin_eval_temperature()

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] deepswe SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3

    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    last_payload: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    try:
        harness._seed_persona(eval_workspace)
        _append_agents_note(eval_workspace)
        harness.init_agent()
        # DeepSWE steps (read the task's code, implement, run the task's tests)
        # blow the 600 s spawn default: the step child burns to TIMEOUT, the
        # step fails, and the orchestration retry loop eats the task's whole
        # wall clock — the observed "every task hits --timeout" pattern. The
        # CONCURRENCY itself works (step children demonstrably overlap); the
        # per-child BUDGET was the weak spot. The model can still raise a step
        # further via step_timeout_seconds (clamped at 7200 s).
        _child_timeout = _eval_child_run_timeout_s()
        from agent.tools.subagent.config import get_config

        _cfg = get_config()
        if _cfg.run_timeout_seconds < _child_timeout:
            _cfg.run_timeout_seconds = _child_timeout
        external_bench.trim_tools()
        # The gate counts per session; read a task's numbers through
        # ``orchestration_state(session_id)`` and the run totals through
        # ``orchestration_state()`` — never the install-time return value.
        external_bench.install_orchestration_gate()
        missing = sorted(
            {"read_file", "write_file", "patch_file", "terminal"}
            - {getattr(tool, "name", "") for tool in _agent_tools()}
        )
        if missing:
            print(
                f"[evals] HARNESS FAILURE: the main agent is missing {missing}",
                flush=True,
            )
            return 2

        prepared = _preflight(
            selected,
            results_dir,
            skip_self_check=skip_self_check,
            docker_detail=docker_detail,
        )
        if prepared is None:
            return 2

        abort = _ProviderAbort()
        for index, (task, verifier_image) in enumerate(prepared):
            if abort.tripped:
                # A terminal provider error (balance exhausted, key revoked)
                # does not heal: starting the remaining containers only burns
                # them. They are reported as NOT RUN instead of as failed tasks.
                remaining = prepared[index:]
                for pending_task, _ in remaining:
                    results.append(
                        _not_run_result(
                            pending_task,
                            experience_extraction=resolve_experience_extraction(
                                pending_task.task_id, experience_extraction
                            ),
                            gold_checked=not skip_self_check,
                        )
                    )
                print(
                    f"[evals] provider is terminal ({abort.count} consecutive runs); "
                    f"marking {len(remaining)} remaining task(s) as not run",
                    flush=True,
                )
                break
            result, payload = await _run_task(
                task,
                verifier_image,
                results_dir,
                timeout=timeout,
                keep_workdirs=keep_workdirs,
                gold_checked=not skip_self_check,
                experience_extraction=resolve_experience_extraction(
                    task.task_id, experience_extraction
                ),
            )
            last_payload = payload or last_payload
            results.append(result)
            abort.observe(result)
            print(
                f"[evals] {task.task_id}: resolved={result['resolved']} "
                f"reward={result['reward']} duration={result['duration_s']}s "
                f"tool_calls={result['tool_calls']} tokens={result['tokens']} "
                f"todolist={result['todolist_calls']} taskflow={result['taskflow_calls']} "
                f"orchestration={_compliance_label(result['orchestration_compliant'])}",
                flush=True,
            )
    except Exception:  # noqa: BLE001 — a setup bug is a harness failure, not a zero
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()
        if not keep_workdirs:
            shutil.rmtree(_work_root(results_dir), ignore_errors=True)

    gate_totals = external_bench.orchestration_state()
    report = {
        "run_id": run_id,
        "suite": "deepswe",
        "kind": f"DeepSWE slice ({len(results)} tasks) — external benchmark",
        "results_dir": str(results_dir),
        "benchmark": {
            "dataset": instances.DEEPSWE_REPO,
            "version": "DeepSWE v1.1",
            "harness": (
                "host agent + official task image (bind-mounted tree) + the task's "
                "separate verifier image (model.patch + held-out test.patch -> reward.json); "
                "NOT the official mini-swe-agent scaffold"
            ),
            "verifier": "tests/Dockerfile + tests/test.sh + tests/grader.py",
            "agent_network": _NETWORK,
            "docker": docker_detail,
        },
        "model": {**harness._model_info(), "thinking_enabled": False},
        "agent_config": last_payload,
        "persona": harness._persona_evidence(results[0]["session_id"] if results else None),
        "isolation": harness._isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "gold_self_check": {
            task.task_id: result["gold_self_check"]
            for task, result in zip(selected, results, strict=False)
        },
        "orchestration": {
            "enforced": True,
            "gate": gate_totals,
            "reviewer_checklist_injections": int(gate_totals.get("reviewer_checklist_injected", 0)),
            "path_denials": int(gate_totals.get("path_denials", 0)),
            "blocked_writes": int(gate_totals.get("blocked_writes", 0)),
            "policy": (
                "the prompt MANDATES planning through todowrite/todoread and execution through "
                "one taskflow flow; usage is counted from tool_start frames and "
                "reported per task (orchestration_compliant = both > 0). The gate counters are "
                "per SESSION: todowrite + taskflow_flow(plan) must exist in the calling task "
                "before it may write, and `gate` here is the run-wide total"
            ),
            "todolist_calls": sum(int(r.get("todolist_calls") or 0) for r in results),
            "taskflow_calls": sum(int(r.get("taskflow_calls") or 0) for r in results),
            **_orchestration_task_lists(results),
        },
        "experience_extraction": _experience_extraction_summary(results),
        "aggregate": _aggregate(results, started),
        "tasks": results,
    }
    external_bench.write_report(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=update_baseline
    )


# ── Preflight ───────────────────────────────────────────────────────────────


def _preflight(
    selected: list[Task],
    results_dir: Path,
    *,
    skip_self_check: bool,
    docker_detail: str,
) -> list[tuple[Task, str]] | None:
    """Pull/build/verify every task BEFORE a single model call."""
    del docker_detail
    prepared: list[tuple[Task, str]] = []
    for task in selected:
        if not external_bench.image_exists(task.image):
            # A long run meets transient registry hiccups (public ECR layer
            # fetches reset mid-burst); one failed pull must not abort a
            # 100+-task sweep, so retry with a growing pause first.
            pulled: external_bench.CommandResult | None = None
            for attempt in range(1, _PULL_RETRIES + 1):
                pulled = external_bench.pull_image(task.image)
                if pulled.ok:
                    break
                print(
                    f"[evals] pull attempt {attempt}/{_PULL_RETRIES} failed for "
                    f"{task.task_id}: {pulled.output[-500:]}",
                    flush=True,
                )
                if attempt < _PULL_RETRIES:
                    time.sleep(_PULL_RETRY_PAUSE_S * attempt)
            if pulled is None or not pulled.ok:
                print(
                    f"[evals] HARNESS FAILURE: could not pull {task.image} for "
                    f"{task.task_id} after {_PULL_RETRIES} attempts: "
                    f"{(pulled.output if pulled else '')[-1500:]}",
                    flush=True,
                )
                return None
        verifier_image = external_bench.image_tag(
            "deepswe", task.task_id, f"{task.base_commit[:8]}-verifier"
        )
        built = external_bench.build_image(
            verifier_image,
            task.task_dir / "tests",
            timeout=task.build_timeout_s,
            network="none",
        )
        if not built.ok:
            print(
                f"[evals] HARNESS FAILURE: could not build the verifier for "
                f"{task.task_id}: {built.output[-2000:]}",
                flush=True,
            )
            return None
        if skip_self_check:
            print(f"[evals] {task.task_id}: gold self-check skipped by request", flush=True)
        else:
            gold_dir = results_dir / "gold" / task.task_id
            reward, resolved, _ = _grade(task, verifier_image, task.gold_patch, gold_dir)
            if not resolved:
                print(
                    f"[evals] HARNESS FAILURE: {task.task_id} is not resolved by its GOLD "
                    f"patch (reward={reward}); the task/verifier is unusable. "
                    f"See {gold_dir / 'verifier-output.txt'}",
                    flush=True,
                )
                return None
            print(f"[evals] {task.task_id}: gold patch resolved", flush=True)
        prepared.append((task, verifier_image))
    return prepared


# ── One task ────────────────────────────────────────────────────────────────


def _not_run_result(
    task: Task, *, experience_extraction: bool, gold_checked: bool
) -> dict[str, Any]:
    """The report row for a task the suite never started (provider is terminal).

    Shape-compatible with ``_run_task``'s result so every reader (aggregate,
    compliance lists, extraction summary) treats it uniformly; ``not_run`` is
    what distinguishes it from a task that ran and failed.
    """
    return {
        "id": task.task_id,
        "repo": task.repo,
        "base_commit": task.base_commit,
        "language": task.language,
        "image": task.image,
        "session_id": "",
        "resolved": False,
        "reward": None,
        "reward_detail": None,
        "failure_kind": "provider",
        "invalid_run": True,
        "not_run": True,
        "duration_s": 0.0,
        "tool_calls": 0,
        "tool_names": {},
        "todolist_calls": 0,
        "taskflow_calls": 0,
        "orchestration_compliant": None,
        "resumes": 0,
        "timeout": False,
        "error": "not run: the provider is terminal (the previous tasks failed on it)",
        "patch_path": "",
        "verifier_output_path": "",
        "changed_paths": [],
        "reply": "",
        "tokens": {},
        "gold_self_check": "checked" if gold_checked else "skipped",
        "experience_extraction": {
            "enabled": experience_extraction,
            "attempted": False,
            "forced": False,
            "fired": False,
            "produced": False,
            "snapshot_hash": "",
        },
        "continuations": 0,
        "no_progress_stop": False,
    }


async def _run_task(
    task: Task,
    verifier_image: str,
    results_dir: Path,
    *,
    timeout: float,
    keep_workdirs: bool,
    gold_checked: bool,
    experience_extraction: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from evals.coding_capability import suite as harness

    task_root = _work_root(results_dir) / task.task_id
    host_workdir = task_root / "workspace"
    container = ""
    payload: dict[str, Any] = {}
    session_id = f"evals-deepswe-{task.task_id}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    result: dict[str, Any] = {
        "id": task.task_id,
        "repo": task.repo,
        "base_commit": task.base_commit,
        "language": task.language,
        "image": task.image,
        "session_id": session_id,
        "resolved": False,
        "reward": None,
        "failure_kind": "",
        "invalid_run": False,
        "duration_s": 0.0,
        "tool_calls": 0,
        "tool_names": {},
        "todolist_calls": 0,
        "taskflow_calls": 0,
        "orchestration_compliant": False,
        "resumes": 0,
        "timeout": False,
        "error": "",
        "patch_path": "",
        "verifier_output_path": "",
        "changed_paths": [],
        "reply": "",
        "tokens": {},
        "gold_self_check": "checked" if gold_checked else "skipped",
        "experience_extraction": {
            "enabled": experience_extraction,
            "attempted": False,
            "forced": False,
            "fired": False,
            "produced": False,
            "snapshot_hash": "",
        },
    }
    try:
        external_bench.extract_workdir(
            task.image,
            "/app",
            host_workdir,
            name=external_bench.container_name("deepswe-extract", task.task_id),
        )
        container = external_bench.start_agent_container(
            task.image,
            external_bench.container_name("deepswe", task.task_id),
            host_workdir=host_workdir,
            workdir="/app",
            network="none",
        )
        bridge = external_bench.DockerBridge(
            container,
            host_workdir=host_workdir,
            # On Windows the container only sees the tree at /app; commands the
            # agent builds with host paths are translated by the bridge.
            container_workdir="/app" if os.name == "nt" else None,
        )
        external_bench.install_terminal_bridge(bridge)
        payload = await external_bench.bind_external_session(session_id, host_workdir)
        if experience_extraction:
            from evals.shared.eval_session import set_session_experience_extraction

            set_session_experience_extraction(session_id, enabled=True)

        checklist_items = _acceptance_checklist(task.instruction)
        external_bench.set_reviewer_checklist(checklist_items)
        checklist = _checklist_block(checklist_items)
        result["acceptance_checklist"] = checklist_items
        prompt = _PROMPT.format(
            instruction=external_bench.localize_instruction(task.instruction, host_workdir, "/app"),
            repo=task.repo,
            base=task.base_commit[:12],
            project_dir=host_workdir,
            checklist=checklist,
        )
        drive: dict[str, Any] = {
            "tool_calls": 0,
            "tool_names": {},
            "text": "",
            "resumes": 0,
            "interrupted": 0,
            "timeout": False,
            "deadline_hit": False,
            "error": "",
        }
        continuations = 0
        result["no_progress_stop"] = False
        gate_before = dict(external_bench.orchestration_state())
        last_patch = _agent_patch(host_workdir, task.base_commit)
        # ONE budget for every driven turn: `--timeout` used to be handed to
        # each of the (up to 4) `asyncio.wait_for` calls, so a task could take
        # 4x its stated budget (measured: 9434 s against the 3600 s default).
        deadline = _drive_deadline(timeout)
        for attempt in range(1 + _MAX_PLAN_CONTINUATIONS):
            try:
                turn = await _next_drive_turn(harness, session_id, prompt, deadline)
            except (BudgetSpentError, TimeoutError):
                # Timeout is terminal here (break, no next wait_for), and every
                # `_drive` rebuilds the main graph+client via
                # `_GenerateTurn._prepare(force_rebuild=True)`. So unlike the
                # subagent goal loop there is no stale pool to reset — see the
                # known-gap note in wiki/evals/README.md.
                drive["timeout"] = True
                drive["deadline_hit"] = True
                break
            drive["tool_calls"] += int(turn.get("tool_calls") or 0)
            for name, count in (turn.get("tool_names") or {}).items():
                drive["tool_names"][name] = drive["tool_names"].get(name, 0) + int(count)
            drive["text"] += str(turn.get("text") or "")
            drive["resumes"] += int(turn.get("resumes") or 0)
            drive["interrupted"] += int(turn.get("interrupted") or 0)
            drive["error"] = str(turn.get("error") or drive["error"])
            if turn.get("timeout") or turn.get("error"):
                drive["timeout"] = bool(turn.get("timeout"))
                break
            open_items = _open_plan_items(results_dir, session_id)
            if not open_items:
                break
            if attempt >= _MAX_PLAN_CONTINUATIONS:
                break
            in_flight = _flow_in_flight(results_dir, session_id)
            # No-progress stop: a continuation turn that changed NOTHING is
            # churn (measured: repeated "closure flow" passes). Stop and let
            # the result record the unfinished plan instead of burning turns.
            # A flow with DISPATCHED/READY steps is NOT churn: the main agent is
            # waiting for step children that are still working, so its turn
            # legitimately produces no diff (measured: an empty patch graded
            # while four step children were mid-flight).
            current_patch = _agent_patch(host_workdir, task.base_commit)
            if attempt > 0 and current_patch == last_patch and not in_flight:
                result["no_progress_stop"] = True
                break
            last_patch = current_patch
            continuations += 1
            prompt = (
                _WAIT_PROMPT.format(items="\n".join(f"- {item}" for item in open_items[:12]))
                if in_flight
                else _CONTINUATION_PROMPT.format(
                    items="\n".join(f"- {item}" for item in open_items[:12]),
                    checklist=checklist,
                )
            )
        result["continuations"] = continuations
        # Window-based deltas (merged totals) for the path-guard counters, so a
        # step child's denial inside this task's window is still attributed to
        # it; the main session's own planning/blocked numbers come per session.
        gate_after = external_bench.orchestration_state()
        result["orchestration_gate"] = external_bench.orchestration_state(session_id)
        result["path_denials"] = int(gate_after.get("path_denials", 0)) - int(
            gate_before.get("path_denials", 0)
        )
        result["blocked_writes"] = int(gate_after.get("blocked_writes", 0)) - int(
            gate_before.get("blocked_writes", 0)
        )
        result["tool_calls"] = drive["tool_calls"]
        tool_names: dict[str, int] = dict(drive.get("tool_names") or {})
        result["tool_names"] = tool_names
        result["todolist_calls"] = sum(
            count for name, count in tool_names.items() if name.startswith("todo")
        )
        result["taskflow_calls"] = sum(
            count for name, count in tool_names.items() if name.startswith("taskflow")
        )
        result["orchestration_compliant"] = bool(
            result["todolist_calls"] and result["taskflow_calls"]
        )
        result["resumes"] = drive["resumes"]
        result["timeout"] = drive["timeout"]
        result["budget_s"] = timeout
        result["deadline_hit"] = drive["deadline_hit"]
        result["error"] = drive["error"]
        result["reply"] = drive["text"][:2000]
        result["tokens"] = await harness._session_tokens(session_id)

        # A step child that finished just as the last turn ended may still be
        # writing its edits; wait (bounded) for in-flight steps to settle so the
        # graded patch contains their work instead of racing it.
        settle_deadline = time.monotonic() + _FLOW_SETTLE_WAIT_S
        while _flow_in_flight(results_dir, session_id) and time.monotonic() < settle_deadline:
            await asyncio.sleep(5.0)

        if experience_extraction:
            # L1-only special handling: the eval harness forces the product's
            # skill-review pass after the run, independent of the product's
            # plan-completion hook. Product code is not modified.
            from evals.shared.eval_session import force_experience_extraction

            result["experience_extraction"].update(await force_experience_extraction(session_id))
            result["experience_extraction"]["forced"] = True
            result["experience_extraction"]["attempted"] = True

        patch = _agent_patch(host_workdir, task.base_commit)
        patch_path = results_dir / f"patch-{task.task_id}.patch"
        patch_path.write_text(patch, encoding="utf-8", newline="\n")
        result["patch_path"] = str(patch_path)
        result["patch_bytes"] = len(patch.encode("utf-8"))
        result["changed_paths"] = _changed_paths(patch)
        large_files = _large_patch_files(patch)
        if large_files:
            result["large_patch_files"] = large_files
            print(
                f"[evals] {task.task_id}: LARGE PATCH {result['patch_bytes']} bytes ("
                + ", ".join(f"{entry['path']} {entry['bytes']} B" for entry in large_files)
                + "); check for a swept build artifact",
                flush=True,
            )
        elif result["patch_bytes"] > _PATCH_TOTAL_WARN_BYTES:
            print(
                f"[evals] {task.task_id}: LARGE PATCH {result['patch_bytes']} bytes; "
                "check for a swept build artifact",
                flush=True,
            )

        grade_dir = results_dir / "verifier" / task.task_id
        reward, resolved, output = _grade(task, verifier_image, patch, grade_dir)
        result["verifier_output_path"] = str(grade_dir / "verifier-output.txt")
        result["reward"] = reward
        result["reward_detail"] = reward
        result["resolved"] = resolved
        if reward is None:
            result["failure_kind"] = "harness"
            result["error"] = (result["error"] + " verifier produced no reward").strip()
            result["invalid_run"] = True
        elif not resolved:
            kind = harness._failure_kind(False, drive)
            result["failure_kind"] = kind
            result["invalid_run"] = kind in _INVALID_FAILURE_KINDS
        if result["invalid_run"]:
            # Never produced a verdict about the agent: neither compliant nor a
            # violation (the old False painted it as a prompt violation).
            result["orchestration_compliant"] = None
        result["duration_s"] = round(time.monotonic() - started, 1)
        del output
    except Exception as exc:  # noqa: BLE001 — a harness error is reported as such
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["invalid_run"] = True
        result["orchestration_compliant"] = None
        result["duration_s"] = round(time.monotonic() - started, 1)
        print(f"[evals] {task.task_id}: HARNESS ERROR: {result['error']}", flush=True)
    finally:
        # HeartbeatStaleness arms a repeating 1-min timer in before_agent and
        # normally clears it in after_agent. A turn aborted by the provider
        # circuit-breaker (or by cancellation) can skip after_agent, leaving
        # the timer firing forever — the eval process then stays alive long
        # after eval_report.json has been written. Eval sessions are owned by
        # the harness: always release their timers here.
        from runtime import timer_call_register

        timer_call_register.clear_session(session_id)
        if container:
            external_bench.remove_container(container)
        if not keep_workdirs:
            shutil.rmtree(task_root, ignore_errors=True)
    return result, payload


# ── Verifier bridge ─────────────────────────────────────────────────────────


def _grade(
    task: Task,
    verifier_image: str,
    patch: str,
    out_dir: Path,
) -> tuple[dict[str, Any] | None, bool, str]:
    """Apply *patch* + the held-out test patch in the verifier image and grade."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    artifacts = out_dir / "artifacts"
    artifacts.mkdir(parents=True)
    # newline=\nn: Path.write_text translates \nn to \r\nn on Windows,
    # and git apply inside the Linux verifier rejects a CRLF patch.
    # git apply inside the Linux verifier rejects a CRLF patch.
    (artifacts / "model.patch").write_text(patch, encoding="utf-8", newline="\n")
    network = "none" if task.verifier_network in {"no-network", "none"} else None
    args = [
        external_bench.container_bin(),
        "run",
        "--rm",
        "--entrypoint",
        "/bin/bash",
    ]
    if network:
        args += ["--network", network]
    if external_bench.container_bin() == "podman":
        # see external_bench.create_container: a propagated dead proxy breaks
        # the verifier's own localhost-based tests.
        args += ["--http-proxy=false"]
    args += [
        "-v",
        f"{artifacts}:/logs/artifacts:ro",
        "-v",
        f"{out_dir}:/logs/verifier",
        verifier_image,
        "/tests/test.sh",
    ]
    print(f"[evals] {task.task_id}: grading in {verifier_image} …", flush=True)
    completed = external_bench.run(args, timeout=task.verifier_timeout_s + 120.0)
    (out_dir / "verifier-output.txt").write_text(completed.output, encoding="utf-8")
    reward = external_bench.read_reward(out_dir)
    return reward, external_bench.reward_resolved(reward), completed.output


# ── Small helpers ───────────────────────────────────────────────────────────


def _agent_tools() -> list[Any]:
    import agent.core as agent_core

    return agent_core.get_agent_tools()


def _session_names(workspace: Path) -> set[str]:
    sessions = workspace / "sessions"
    if not sessions.is_dir():
        return set()
    return {entry.name for entry in sessions.iterdir() if entry.is_dir()}


def _append_agents_note(workspace: Path) -> None:
    rules = workspace / "AGENTS.md"
    note = (
        "\n## External benchmark environment\n\n"
        "- The session's project directory is a copy of the task container's `/app`; "
        "the same tree is bind-mounted at `/app` inside the container. The task "
        "text's container paths are rewritten to the project directory.\n"
        "- The `terminal` tool runs inside the task container, in a persistent shell; "
        "file tools edit the same files from the host side. Host paths inside "
        "terminal commands are translated to the container mount automatically.\n"
        "- The task image is the only toolchain. There is no internet access. "
        "Never modify tests or run `git commit`.\n"
    )
    rules.write_text(rules.read_text(encoding="utf-8") + note, encoding="utf-8")


def _agent_patch(host_workdir: Path, base_commit: str) -> str:
    add = external_bench.run(["git", "-C", str(host_workdir), "add", "-A"], timeout=120.0)
    if not add.ok:
        raise RuntimeError(f"git add failed: {add.output[-1000:]}")
    diff = external_bench.run(
        [
            "git",
            "-C",
            str(host_workdir),
            "diff",
            "--binary",
            "--cached",
            base_commit,
        ],
        timeout=300.0,
    )
    if not diff.ok:
        raise RuntimeError(f"git diff failed: {diff.output[-1000:]}")
    return diff.output


#: A patch bigger than this in one file, or in total, is printed as a warning:
#: a rebuilt binary or a swept build artifact is otherwise invisible in
#: ``changed_paths`` and inflates the graded patch (measured: an 11.5 MB
#: rebuilt binary inside an 8.3 MB patch).
_PATCH_FILE_WARN_BYTES = 1_000_000
_PATCH_TOTAL_WARN_BYTES = 5_000_000


def _patch_sections(patch: str) -> list[tuple[str, int]]:
    """``(path, encoded_bytes)`` per ``diff --git`` section, in patch order."""
    sections: list[tuple[str, int]] = []
    path = ""
    size = 0
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path:
                sections.append((path, size))
            path, size = "", 0
            target = line.rstrip("\n")[len("diff --git ") :].rsplit(" b/", 1)
            if len(target) == 2:
                path = target[1].strip()
        size += len(line.encode("utf-8"))
    if path:
        sections.append((path, size))
    return sections


def _changed_paths(patch: str) -> list[str]:
    """Every path the patch touches, BINARY files included.

    Parsed from the ``diff --git a/X b/X`` headers rather than ``+++ b/`` lines:
    a binary change is emitted as ``GIT binary patch`` with no ``---/+++`` pair,
    so the old reader silently dropped it (measured: a rebuilt 11.5 MB binary
    was reported as three Go files). A deletion is excluded — the file is gone
    after the patch — and a rename reports its new path.
    """
    paths: list[str] = []
    current: str | None = None
    deleted = False

    def _flush() -> None:
        if current and not deleted and current not in paths:
            paths.append(current)

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            _flush()
            current, deleted = None, False
            target = line[len("diff --git ") :].rsplit(" b/", 1)
            if len(target) == 2:
                current = target[1].strip()
        elif line.startswith("deleted file mode"):
            deleted = True
    _flush()
    return paths


def _large_patch_files(patch: str, *, limit: int = _PATCH_FILE_WARN_BYTES) -> list[dict[str, Any]]:
    """Files whose encoded patch section exceeds *limit* (binaries surface here)."""
    return [{"path": path, "bytes": size} for path, size in _patch_sections(patch) if size > limit]


def _compliance_label(value: bool | None) -> str:
    """The console label: ``ok`` / ``VIOLATED`` / ``n/a`` (a run that never happened)."""
    if value is None:
        return "n/a"
    return "ok" if value else "VIOLATED"


def _orchestration_task_lists(results: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Per-task compliance split. ``None`` is "could not be judged" (not run)."""
    return {
        "compliant_tasks": [
            result["id"] for result in results if result.get("orchestration_compliant") is True
        ],
        "violating_tasks": [
            result["id"] for result in results if result.get("orchestration_compliant") is False
        ],
        "not_run_tasks": [
            result["id"] for result in results if result.get("orchestration_compliant") is None
        ],
    }


def _produced_extraction(data: dict[str, Any]) -> bool:
    """True when the extraction pass actually WROTE something.

    ``snapshot_hash`` alone cannot answer this: the runner records it when the
    pass concludes, which includes the "task judged non-generalizable" skip
    (nothing written). Reports written before the explicit ``produced`` field
    existed fall back to the hash, which over-reports — the per-task field is
    the authority from 2026-11-27 on.
    """
    if "produced" in data:
        return bool(data.get("produced"))
    return bool(data.get("snapshot_hash"))


def _experience_extraction_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The extraction block: attempted is the force call, fired the flag, produced the output."""
    per_task = {result["id"]: result.get("experience_extraction") or {} for result in results}
    return {
        "enabled_tasks": [task_id for task_id, data in per_task.items() if data.get("enabled")],
        "attempted_tasks": [
            task_id
            for task_id, data in per_task.items()
            if data.get("attempted") or data.get("forced")
        ],
        "forced_tasks": [task_id for task_id, data in per_task.items() if data.get("forced")],
        "fired_tasks": [task_id for task_id, data in per_task.items() if data.get("fired")],
        "produced_tasks": [
            task_id for task_id, data in per_task.items() if _produced_extraction(data)
        ],
        "policy": (
            "only the 12-task L1 set can opt in via --experience-extraction; the "
            "session-level skill_extraction_enabled override is written for those tasks "
            "only, and the eval harness forces the skill-review pass after the run so "
            "L2/full runs stay on the historical arm. `attempted` is the force call, "
            "`fired` the product's once-per-plan flag (a forced pass sets it before the "
            "call), and `produced` means the pass actually changed the auto-skill tree "
            "(a non-generalizable skip records a snapshot hash but writes nothing)"
        ),
    }


def _aggregate(results: list[dict[str, Any]], started: float) -> dict[str, Any]:
    invalid = [result["id"] for result in results if result.get("invalid_run")]
    # A task that never executed (provider abort, or a suite-level abort) cannot
    # count as attempted: `invalid_run` is the historical name for the provider
    # arm, `not_run` is set when the suite stopped before starting the task.
    not_run = [
        result["id"] for result in results if result.get("invalid_run") or result.get("not_run")
    ]
    total = len(results)
    valid = total - len(invalid)
    resolved = sum(1 for result in results if result.get("resolved"))
    return {
        "tasks_total": total,
        "attempted": total - len(not_run),
        "resolved": resolved,
        "not_run": not_run,
        "invalid_runs": invalid,
        "resolve_rate": round(resolved / max(1, valid), 4),
        "resolve_rate_basis": "valid runs only (provider failures excluded)",
        "resolve_rate_all": round(resolved / max(1, total), 4),
        "resolve_rate_all_basis": "every selected task, including the ones that never ran",
        "duration_s": round(time.monotonic() - started, 1),
    }


if __name__ == "__main__":
    main()
