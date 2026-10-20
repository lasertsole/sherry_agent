"""ToolBench eval suite — the official test instructions + official ToolEval.

What is measured: ToolBench's own pass rate, task by task. The instruction and
the test-query ids come from the official dataset (HF mirror pinned to one
commit); the agent's tool calls go through the official ToolBench RapidAPI
proxy with the official ``category`` / ``tool_name`` / ``api_name`` payload; the
trajectory is converted into ToolBench's own answer format; and the verdict is
the official ToolEval evaluator (its prompts and functions, imported from the
pinned repository).

Faithfulness notes (also written into the report):

- no question is generated: every task is an official query selected by the
  official ``test_query_ids`` file;
- execution needs the official ``TOOLBENCH_KEY`` (or a compatible proxy URL via
  ``SHERRY_TOOLBENCH_SERVICE_URL``) and judging needs an OpenAI-compatible
  judge key (``SHERRY_TOOLBENCH_JUDGE_KEY`` / ``OPENAI_API_KEY`` /
  ``SHERRY_TOOLBENCH_JUDGE_KEYS``); without them the suite SKIPS loudly and
  exits non-zero. ``SHERRY_TOOLBENCH_JUDGE_MODEL`` (default the official
  ``gpt-3.5-turbo-16k``) and ``SHERRY_TOOLBENCH_EVAL_TIMES`` (default 1) tune
  the official evaluator, and ``SHERRY_TOOLBENCH_DATA`` points at an official
  dataset download instead of the pinned HF mirror;
- the agent interface is ONE dispatcher (``toolbench_call``) carrying the
  official function names instead of dozens of dynamic LangChain tools; the
  official function schemas are still listed verbatim in the prompt;
- the official evaluator runs in an isolated ``openai<1`` environment because
  its code uses the legacy ChatCompletion function API;
- the evaluator's verdict is the only pass signal; a harness/judge failure is
  never reported as a model failure.

Usage::

    uv run python evals/evals.py toolbench
    uv run python evals/toolbench/suite.py --dry-run
    uv run python evals/toolbench/suite.py --dry-run --fetch
    uv run python evals/toolbench/suite.py --subset G1_instruction --n-tasks 5
    uv run python evals/toolbench/suite.py --all-subsets --n-tasks 10
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
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
from evals.toolbench import tasks as task_data  # noqa: E402
from evals.toolbench.convert import build_answer, write_converted  # noqa: E402
from evals.toolbench.tasks import Task  # noqa: E402
from evals.toolbench.tools import (  # noqa: E402
    DEFAULT_SERVICE_URL,
    ToolBenchEvalState,
    available_tools,
    build_prompt,
    install_toolbench_tools,
)

BASELINE_PATH = Path(__file__).with_name("baseline.json")
JUDGE_RUNNER = Path(__file__).with_name("judge_runner.py")
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
    external_bench.maybe_reexec("SHERRY_EVAL_TOOLBENCH_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == "toolbench":
        args = args[1:]

    wanted: list[str] = []
    flags: set[str] = set()
    subsets: list[str] = []
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
        elif arg == "--subset":
            if index + 1 >= len(args):
                raise SystemExit("--subset needs a value")
            subsets.append(args[index + 1])
            index += 2
            continue
        elif arg.startswith("--subset="):
            subsets.append(arg.split("=", 1)[1])
        elif arg in {
            "--all",
            "--all-subsets",
            "--dry-run",
            "--fetch",
            "--keep-workdirs",
            "--update-baseline",
        }:
            flags.add(arg)
        elif arg.startswith("--"):
            raise SystemExit(f"unknown flag: {arg}")
        else:
            wanted.append(arg)
        index += 1

    if wanted and "--all" in flags:
        raise SystemExit("name task ids or use --all, not both")
    timeout = max(60.0, min(float(timeout), _MAX_TIMEOUT_S))
    chosen_subsets = tuple(
        dict.fromkeys(
            (list(task_data.SUBSET_SOURCES) if "--all-subsets" in flags else [])
            or subsets
            or [task_data.DEFAULT_SUBSET]
        )
    )
    unknown_subsets = [name for name in chosen_subsets if name not in task_data.SUBSET_SOURCES]
    if unknown_subsets:
        raise SystemExit(
            f"unknown subset(s): {unknown_subsets}; known: {sorted(task_data.SUBSET_SOURCES)}"
        )

    try:
        # Dry-run validates the official example files unless --fetch is given;
        # an actual run always downloads the full official instruction files.
        example = "--dry-run" in flags and "--fetch" not in flags
        loaded = task_data.load_tasks(chosen_subsets, example=example)
        repo_root, commit = task_data.fetch_repo()
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    supported = [
        task for subset in chosen_subsets for task in loaded.get(subset, ()) if task.supported
    ]
    if wanted:
        ids = tuple(wanted)
    elif "--all" in flags:
        ids = tuple(task.task_id for task in supported)
    else:
        ids = tuple(
            task.task_id
            for subset in chosen_subsets
            for task in loaded.get(subset, ())
            if task.supported
        )[: task_data.DEFAULT_SLICE]
    if n_tasks is not None:
        ids = ids[: max(0, n_tasks)]
    try:
        selected = task_data.select(loaded, ids)
    except KeyError as exc:
        print(f"[evals] {exc}", flush=True)
        raise SystemExit(2) from exc

    unsupported = [
        task.task_id
        for subset in chosen_subsets
        for task in loaded.get(subset, ())
        if not task.supported
    ]
    print(
        f"[evals] ToolBench: {len(supported)} supported official test task(s) across "
        f"{list(chosen_subsets)} (repo {commit}); selected {len(selected)}"
        + (" [official EXAMPLE files — dry-run only]" if example else ""),
        flush=True,
    )
    if unsupported:
        print(
            f"[evals] skipping {len(unsupported)} unsupported official task(s): "
            f"{unsupported[:8]}" + (" …" if len(unsupported) > 8 else ""),
            flush=True,
        )

    if "--dry-run" in flags:
        for task in selected:
            print(
                f"[evals] dry-run ok: {task.task_id} functions={len(available_tools(task))} "
                f"query={task.query[:80]!r}",
                flush=True,
            )
        print(
            "[evals] dry-run only; a real run needs TOOLBENCH_KEY, MAIN_LLM_API_KEY "
            "and an OpenAI-compatible judge key (a skipped suite is not a pass)",
            flush=True,
        )
        return

    code = asyncio.run(
        _run(
            selected,
            subsets=chosen_subsets,
            repo_root=repo_root,
            commit=commit,
            timeout=timeout,
            keep_workdirs="--keep-workdirs" in flags,
            update_baseline="--update-baseline" in flags,
        )
    )
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


async def _run(
    selected: list[Task],
    *,
    subsets: tuple[str, ...],
    repo_root: Path,
    commit: str,
    timeout: float,
    keep_workdirs: bool,
    update_baseline: bool,
) -> int:
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / "toolbench" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = _session_names(live_workspace)

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    toolbench_key = os.environ.get("TOOLBENCH_KEY", "").strip()
    if not toolbench_key:
        print(
            "[evals] toolbench SKIPPED: TOOLBENCH_KEY is not configured "
            "(the official RapidAPI proxy needs it; a skipped suite is not a pass)",
            flush=True,
        )
        return 3
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] toolbench SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3
    judge_key = (
        os.environ.get("SHERRY_TOOLBENCH_JUDGE_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("SHERRY_TOOLBENCH_JUDGE_KEYS", "").strip()
    )
    if not judge_key:
        print(
            "[evals] toolbench SKIPPED: no judge key (SHERRY_TOOLBENCH_JUDGE_KEY / "
            "OPENAI_API_KEY / SHERRY_TOOLBENCH_JUDGE_KEYS); a skipped suite is not a pass",
            flush=True,
        )
        return 3

    import agent.core  # noqa: F401 — binds the real tool builder before sandbox.apply()

    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    service_url = os.environ.get("SHERRY_TOOLBENCH_SERVICE_URL", DEFAULT_SERVICE_URL).strip()
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    converted: dict[str, dict[str, Any]] = {}
    last_payload: dict[str, Any] = {}
    try:
        harness._seed_persona(eval_workspace)
        harness.init_agent()
        external_bench.trim_tools()
        for task in selected:
            result, payload, entry = await _run_task(
                task,
                results_dir,
                timeout=timeout,
                keep_workdirs=keep_workdirs,
                service_url=service_url,
                toolbench_key=toolbench_key,
            )
            last_payload = payload or last_payload
            results.append(result)
            if entry is not None:
                converted[str(task.query_id)] = entry
            print(
                f"[evals] {task.task_id}: duration={result['duration_s']}s "
                f"tool_calls={result['tool_calls']} tokens={result['tokens']} "
                f"finished={result['finished']}",
                flush=True,
            )
    except Exception:  # noqa: BLE001 — a setup bug is a harness failure
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    converted_path = results_dir / "converted_answers.json"
    write_converted(converted, converted_path)
    verdicts, judge_error = _judge(
        converted_path,
        selected,
        repo_root,
        results_dir,
        times=int(os.environ.get("SHERRY_TOOLBENCH_EVAL_TIMES", "1") or "1"),
    )
    for result in results:
        verdict = verdicts.get(str(result["query_id"]), {})
        result["judge"] = verdict
        if verdict.get("harness_error"):
            result["failure_kind"] = "harness"
            result["error"] = str(verdict["harness_error"])
        elif result["failure_kind"] == "":
            if judge_error and not verdict:
                # The judge never produced a verdict for this task: that is a
                # HARNESS failure, never a model failure.
                result["failure_kind"] = "harness"
                result["error"] = judge_error
            else:
                result["passed"] = bool(verdict.get("passed"))
                if not result["passed"]:
                    result["failure_kind"] = "model"

    report = {
        "run_id": run_id,
        "suite": "toolbench",
        "kind": f"ToolBench slice ({len(results)} tasks) — external benchmark",
        "results_dir": str(results_dir),
        "benchmark": {
            "dataset": f"https://huggingface.co/datasets/{task_data.HF_REPO}",
            "dataset_commit": task_data.HF_PIN,
            "toolbench_repo": task_data.TOOLBENCH_REPO,
            "toolbench_commit": commit,
            "subsets": list(subsets),
            "official_test_ids": True,
            "tool_interface": (
                "one `toolbench_call` dispatcher over the official function names; "
                "official function schemas are listed verbatim in the prompt"
            ),
            "execution": f"official ToolBench RapidAPI proxy at {service_url}",
            "judge": (
                "official ToolEval evaluator (pinned repository, official prompts), "
                f"model={os.environ.get('SHERRY_TOOLBENCH_JUDGE_MODEL', 'gpt-3.5-turbo-16k')}, "
                f"times={os.environ.get('SHERRY_TOOLBENCH_EVAL_TIMES', '1')}"
            ),
            "judge_error": judge_error,
        },
        "model": {**harness._model_info(), "thinking_enabled": False},
        "agent_config": last_payload,
        "isolation": harness._isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "aggregate": _aggregate(results, started),
        "tasks": results,
        "converted_answers": str(converted_path),
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
    service_url: str,
    toolbench_key: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    from evals.coding_capability import suite as harness

    task_dir = results_dir / "tasks" / task.task_id
    session_id = f"evals-toolbench-{task.subset}-{task.query_id}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    state = ToolBenchEvalState()
    result: dict[str, Any] = {
        "id": task.task_id,
        "category": f"toolbench-{task.subset}",
        "query_id": task.query_id,
        "subset": task.subset,
        "session_id": session_id,
        "passed": False,
        "failure_kind": "",
        "invalid_run": False,
        "duration_s": 0.0,
        "tool_calls": 0,
        "resumes": 0,
        "timeout": False,
        "finished": False,
        "error": "",
        "reply": "",
        "final_answer": "",
        "tokens": {},
    }
    payload: dict[str, Any] = {}
    entry: dict[str, Any] | None = None
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        install_toolbench_tools(
            task,
            state,
            service_url=service_url,
            toolbench_key=toolbench_key,
            request_timeout=30.0,
        )
        await _bind_session(session_id, task_dir)
        payload = _agent_payload()
        try:
            drive = await asyncio.wait_for(
                harness._drive(session_id, build_prompt(task)), timeout=timeout
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
        result["finished"] = state.finished
        result["final_answer"] = state.final_answer
        result["tokens"] = await harness._session_tokens(session_id)
        entry = build_answer(task, state, drive["text"])
        if drive["timeout"]:
            result["failure_kind"] = "timeout"
        elif drive["error"] and _is_provider_error(drive["error"]):
            result["failure_kind"] = "provider"
            result["invalid_run"] = True
    except Exception as exc:  # noqa: BLE001 — setup failure is a harness failure
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        result["duration_s"] = round(time.monotonic() - started, 1)
        if not keep_workdirs:
            shutil.rmtree(task_dir, ignore_errors=True)
    return result, payload, entry


def _judge(
    converted_path: Path,
    selected: list[Task],
    repo_root: Path,
    results_dir: Path,
    *,
    times: int,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Run the official evaluator in an isolated ``openai<1`` environment."""
    test_ids: dict[str, int] = {}
    for task in selected:
        test_ids[str(task.query_id)] = 0
    test_ids_path = results_dir / "test_query_ids.json"
    test_ids_path.write_text(json.dumps(test_ids), encoding="utf-8")
    out_path = results_dir / "judge_verdicts.json"
    tooleval = repo_root / "toolbench" / "tooleval"
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "openai==0.28.1",
        "--with",
        "pyyaml",
        "--with",
        "tenacity",
        "python",
        str(JUDGE_RUNNER),
        "--repo-tooleval",
        str(tooleval),
        "--converted",
        str(converted_path),
        "--test-ids",
        str(test_ids_path),
        "--out",
        str(out_path),
        "--times",
        str(max(1, times)),
        "--model",
        os.environ.get("SHERRY_TOOLBENCH_JUDGE_MODEL", "gpt-3.5-turbo-16k"),
        "--evaluator",
        os.environ.get("SHERRY_TOOLBENCH_EVALUATOR", "tooleval_gpt-3.5-turbo_default"),
    ]
    pool = os.environ.get("SHERRY_TOOLBENCH_JUDGE_KEYS", "").strip()
    if pool:
        cmd.extend(["--api-pool", pool])
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr)[-2000:]
        return {}, f"judge exited {completed.returncode}: {detail}"
    try:
        return json.loads(out_path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as exc:
        return {}, f"judge output unreadable: {type(exc).__name__}: {exc}"


async def _bind_session(session_id: str, task_dir: Path) -> None:
    """Bind the session to the task dir and install the ToolBench tool face."""
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
    """Required tools + the two ToolBench tools; everything else disabled."""
    from agent.tools.catalog import REQUIRED_TOOLS

    tools = sorted(set(REQUIRED_TOOLS) | {"toolbench_call", "Finish"})
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
