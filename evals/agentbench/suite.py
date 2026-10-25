"""AgentBench eval suite — the official DBBench test set, driven by the main agent.

What is measured: AgentBench's own DBBench verdict, task by task. Each task is
one row of the official ``data/dbbench/standard.jsonl`` (300 labelled
questions); the official table is materialised in SQLite and the agent receives
the same two functions the official environment exposes —
``execute_sql`` and ``commit_final_answer``. The row's own ``label`` is compared
with the committed answer using AgentBench's ported comparison rules.

Faithfulness notes (also written into the report):

- no question, table or answer is generated here: the data file comes from a
  pinned checkout of https://github.com/THUDM/AgentBench, and the default arm
  is an index slice of its official 300-task test set;
- the official environment is MySQL 8; this arm uses SQLite over the SAME
  official columns + rows, so SQL-dialect differences remain (documented);
- INSERT/DELETE/UPDATE rows and multi-table rows are reported UNSUPPORTED with
  the reason (their official verdict is a table hash / a join fixture this arm
  does not reproduce);
- the benchmark's comparison is the only pass signal; a harness/setup failure
  is never reported as a model failure;
- without ``MAIN_LLM_API_KEY`` the suite SKIPS loudly and exits non-zero.

Usage::

    uv run python evals/evals.py agentbench
    uv run python evals/agentbench/suite.py --dry-run
    uv run python evals/agentbench/suite.py dbbench-000 dbbench-001 --timeout 900
    uv run python evals/agentbench/suite.py --all --n-tasks 20
"""

from __future__ import annotations

import asyncio
import os
import shutil
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
from evals.agentbench import instances  # noqa: E402
from evals.agentbench.instances import Task  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
_DEFAULT_TIMEOUT_S = 900.0
_MAX_TIMEOUT_S = 3600.0

_PROVIDER_ERROR_MARKERS = (
    "badrequesterror",
    "ratelimiterror",
    "apiconnectionerror",
    "apierror",
    "internalservererror",
    "serviceunavailable",
    "connecterror",
    "readtimeout",
    "timeout",
    "stream stalled",
    "error code: 4",
    "error code: 5",
)


def main() -> None:
    """Run the suite (re-executing in a clean interpreter when needed)."""
    external_bench.maybe_reexec("SHERRY_EVAL_AGENTBENCH_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == "agentbench":
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
        elif arg in {"--all", "--dry-run", "--keep-workdirs", "--update-baseline"}:
            flags.add(arg)
        elif arg.startswith("--"):
            raise SystemExit(f"unknown flag: {arg}")
        else:
            wanted.append(arg)
        index += 1

    if wanted and "--all" in flags:
        raise SystemExit("name task ids or use --all, not both")
    timeout = max(60.0, min(float(timeout), _MAX_TIMEOUT_S))

    try:
        data_root, commit = instances.fetch_tasks()
        tasks = instances.load_tasks(data_root)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    supported = [task for task in tasks.values() if task.supported]
    if wanted:
        ids = tuple(wanted)
    elif "--all" in flags:
        ids = tuple(task.task_id for task in supported)
    else:
        # The default arm is the first ten SUPPORTED official rows; the full
        # file is the benchmark, this is only an index slice of it.
        ids = tuple(task.task_id for task in supported[: len(instances.DEFAULT_SLICE)])
    if n_tasks is not None:
        ids = ids[: max(0, n_tasks)]
    try:
        selected = instances.select(tasks, ids)
    except KeyError as exc:
        print(f"[evals] {exc}", flush=True)
        raise SystemExit(2) from exc

    print(
        f"[evals] AgentBench DBBench: {len(tasks)} official tasks at {data_root} "
        f"(commit {commit}); supported={len(supported)}",
        flush=True,
    )
    unsupported = [task.task_id for task in tasks.values() if not task.supported]
    if unsupported:
        print(
            f"[evals] skipping {len(unsupported)} unsupported official task(s): "
            f"{unsupported[:8]}" + (" …" if len(unsupported) > 8 else ""),
            flush=True,
        )
    if any(not task.supported for task in selected):
        bad = [task.task_id for task in selected if not task.supported]
        print(f"[evals] selected task(s) not representable in this arm: {bad}", flush=True)
        raise SystemExit(2)

    if "--dry-run" in flags:
        for task in selected:
            print(
                f"[evals] dry-run ok: {task.task_id} type={task.query_type} "
                f"table={task.table_name!r} columns={len(task.columns)} rows={len(task.rows)} "
                f"label={task.label}",
                flush=True,
            )
        print(
            "[evals] dry-run only; a real run needs MAIN_LLM_API_KEY "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return

    code = asyncio.run(
        _run(
            selected,
            commit=commit,
            timeout=timeout,
            keep_workdirs="--keep-workdirs" in flags,
            update_baseline="--update-baseline" in flags,
        )
    )
    # The agent runtime leaves non-daemon background threads behind (channels /
    # curator wiring imported by config.path). `raise SystemExit` would wait for
    # them and keep a finished eval process alive; the report is already on
    # disk, so terminate the interpreter explicitly.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


async def _run(
    selected: list[Task],
    *,
    commit: str,
    timeout: float,
    keep_workdirs: bool,
    update_baseline: bool,
) -> int:
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / "agentbench" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = _session_names(live_workspace)

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] agentbench SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3

    import agent.core  # noqa: F401 — binds the real tool builder before sandbox.apply()

    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    last_payload: dict[str, Any] = {}
    try:
        harness._seed_persona(eval_workspace)
        harness.init_agent()
        for task in selected:
            result, payload = await _run_task(
                task,
                results_dir,
                timeout=timeout,
                keep_workdirs=keep_workdirs,
            )
            last_payload = payload or last_payload
            results.append(result)
            print(
                f"[evals] {task.task_id}: passed={result['passed']} "
                f"duration={result['duration_s']}s tool_calls={result['tool_calls']} "
                f"tokens={result['tokens']} failure={result['failure_kind'] or '-'}",
                flush=True,
            )
    except Exception:  # noqa: BLE001 — a setup bug is a harness failure
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = {
        "run_id": run_id,
        "suite": "agentbench",
        "kind": f"AgentBench DBBench slice ({len(results)} tasks) — external benchmark",
        "results_dir": str(results_dir),
        "benchmark": {
            "dataset": instances.AGENTBENCH_REPO,
            "data_file": instances.DATA_FILE,
            "commit": commit,
            "official_task_count": 300,
            "environment": (
                "official table columns + rows materialised in SQLite; same "
                "execute_sql / commit_final_answer function names as the official "
                "MySQL environment (SQL dialect differs)"
            ),
            "judge": "AgentBench DBBench comparison (result_processor.py), ported",
            "unsupported": "INSERT/DELETE/UPDATE and multi-table rows",
        },
        "model": {**harness._model_info(), "thinking_enabled": False},
        "agent_config": last_payload,
        "isolation": harness._isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "aggregate": _aggregate(results, started),
        "tasks": results,
    }
    external_bench.write_report(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=update_baseline
    )


async def _run_task(
    task: Task,
    results_dir: Path,
    *,
    timeout: float,
    keep_workdirs: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from evals.agentbench.grading import compare_results
    from evals.agentbench.tools import install_dbbench_tools
    from evals.coding_capability import suite as harness

    task_dir = results_dir / "tasks" / task.task_id
    db_path = task_dir / "task.sqlite"
    session_id = f"evals-agentbench-{task.task_id}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    result: dict[str, Any] = {
        "id": task.task_id,
        "category": "agentbench-dbbench",
        "query_type": task.query_type,
        "session_id": session_id,
        "passed": False,
        "failure_kind": "",
        "invalid_run": False,
        "duration_s": 0.0,
        "tool_calls": 0,
        "sql_calls": 0,
        "resumes": 0,
        "timeout": False,
        "error": "",
        "reply": "",
        "final_answers": None,
        "label": list(task.label),
        "tokens": {},
        "db_path": str(db_path),
    }
    payload: dict[str, Any] = {}
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        instances.materialize_sqlite(task, db_path)
        state = install_dbbench_tools(db_path)
        await _bind_session(session_id, task_dir)
        payload = _agent_payload()
        try:
            drive = await asyncio.wait_for(
                harness._drive(session_id, instances.task_prompt(task)), timeout=timeout
            )
        except TimeoutError:
            drive = {
                "tool_calls": 0,
                "text": "",
                "resumes": 0,
                "interrupted": 0,
                "timeout": True,
                "error": "",
            }
        result["tool_calls"] = drive["tool_calls"]
        result["resumes"] = drive["resumes"]
        result["timeout"] = drive["timeout"]
        result["error"] = drive["error"]
        result["reply"] = drive["text"][:2000]
        result["sql_calls"] = len(state.sql_calls)
        result["final_answers"] = state.final_answers
        result["tokens"] = await harness._session_tokens(session_id)

        if drive["timeout"]:
            result["failure_kind"] = "timeout"
        elif drive["error"] and _is_provider_error(drive["error"]):
            result["failure_kind"] = "provider"
            result["invalid_run"] = True
        elif state.final_answers is None:
            result["failure_kind"] = "model"
            result["error"] = result["error"] or "no commit_final_answer call"
        else:
            result["passed"] = compare_results(
                state.final_answers, list(task.label), task.query_type
            )
            result["failure_kind"] = "" if result["passed"] else "model"
    except Exception as exc:  # noqa: BLE001 — setup failure is a harness failure
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        result["duration_s"] = round(time.monotonic() - started, 1)
        if not keep_workdirs:
            shutil.rmtree(task_dir, ignore_errors=True)
    return result, payload


async def _bind_session(session_id: str, task_dir: Path) -> None:
    """Bind the session to the task dir and install the official tool face."""
    from runtime import state_register_db
    from runtime.session.state_keys import StateKey
    from runtime.session.state_register import state_register_mem

    for register in (state_register_mem, state_register_db):
        register.set_state(session_id, StateKey.PROJECT_DIR, str(task_dir))
        register.set_state(session_id, StateKey.LLM_THINKING_ENABLED, False)

    from agent.middlewares.humanInTheLoop.approval import set_session_yolo

    set_session_yolo(session_id)

    from server.service.agent_config_service import invalidate_session_prompt
    from server.service.session_settings_service import _delete_value, _write_value

    payload = _agent_payload()
    _write_value(session_id, StateKey.AGENT_CONFIG, payload)
    _delete_value(session_id, StateKey.AGENT_CONFIG_PENDING)
    invalidate_session_prompt(session_id)


def _agent_payload() -> dict[str, Any]:
    """Required tools + the two DBBench tools; everything else disabled."""
    from agent.tools.catalog import REQUIRED_TOOLS

    tools = sorted(set(REQUIRED_TOOLS) | {"execute_sql", "commit_final_answer"})
    return {
        "tools": tools,
        "middlewares_disabled": [],
        "skills": [],
        "middleware_options": {"Summarization": {"nudge": False}},
    }


def _is_provider_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PROVIDER_ERROR_MARKERS)


def _aggregate(results: list[dict[str, Any]], started: float) -> dict[str, Any]:
    passed = sum(1 for result in results if result["passed"])
    invalid = [result["id"] for result in results if result["invalid_run"]]
    valid = len(results) - len(invalid)
    harness_errors = [result["id"] for result in results if result["failure_kind"] == "harness"]
    return {
        "tasks_total": len(results),
        "tasks_passed": passed,
        "tasks_valid": valid,
        "invalid_runs": invalid,
        "harness_errors": harness_errors,
        "pass_rate": round(passed / valid, 4) if valid else 0.0,
        "resolve_rate": round(passed / valid, 4) if valid else 0.0,
        "pass_rate_basis": "valid runs only (provider failures excluded)",
        "duration_s": round(time.monotonic() - started, 1),
        "tool_calls": sum(result["tool_calls"] for result in results),
        "sql_calls": sum(result["sql_calls"] for result in results),
        "tokens": {
            "input": sum(result["tokens"].get("input", 0) for result in results),
            "output": sum(result["tokens"].get("output", 0) for result in results),
        },
    }


def _session_names(workspace: Path) -> set[str]:
    sessions = workspace / "sessions"
    if not sessions.is_dir():
        return set()
    return {child.name for child in sessions.iterdir() if child.is_dir()}


if __name__ == "__main__":
    main()
