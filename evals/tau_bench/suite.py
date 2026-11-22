"""TAU-bench — the tool-use agent benchmark, wrapped as Sherry tools.

tau-bench's shape: an ENVIRONMENT holds a simulated business API (airline /
retail); a USER SIMULATOR (another LLM) converses with the agent; the agent
calls the environment's tools through the conversation; the episode ends when
both sides stop, and the environment's own reward checks the final state plus
the agent's response against the task's expected actions.

What this adapter owns:

* the environment's TOOLS are wrapped as real Sherry tools
  (:mod:`evals.tau_bench.api_wrapper` — one LangChain tool per API method,
  schema generated from the package's own tool signature) so the agent calls
  them exactly like production tools;
* the USER SIMULATOR runs the main model through the same chat interface the
  package expects (``litellm`` when the package pulls it; the adapter passes
  ``user_model`` through untouched so the package drives its own simulator);
* the episode loop is ours: agent turn → user turn → … until the package's
  ``env.step`` returns a reward or the turn budget ends.

The package is a lazy import: a host without ``tau_bench`` SKIPS (exit 3)
before any model call. The task list comes from the package's own env
(``tau_bench.envs.<env>.data``) filtered by ids; ``TAU_BENCH_DATA_DIR`` may
override it with a local tasks JSON (documented in the README). The adapter
pins the SMALL surface it calls in ``api_wrapper`` — unit tests run that
surface against a fake module, so a future package refactor shows up as a
clear test failure, not a silent mis-score.
"""

from __future__ import annotations

import asyncio
import os
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
from evals.shared import batch_runner, env_setup, score_reporter  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "tau_bench"
DATA_ENV_VAR = "TAU_BENCH_DATA_DIR"
_SCENES = ("airline", "retail")


def import_tau_bench() -> dict[str, Any]:
    """The package surface this adapter calls, resolved once (or an explicit
    ImportError the caller turns into a skip). importlib on purpose: the
    package is OPTIONAL, so a static import would fail type resolution."""
    from importlib import import_module  # noqa: PLC0415 — deliberately lazy

    return {
        "tau_bench": import_module("tau_bench"),
        "envs_data": import_module("tau_bench.envs.data"),
    }


def available_tasks(surface: dict[str, Any], scene: str) -> list[dict[str, Any]]:
    """The package's task list for one scene: ``tau_bench.envs.<scene>.data``
    exposes ``tasks`` as a list of Task objects with ``task_id``-shaped ids —
    we read through ``getattr`` so a package-side rename fails loudly here."""
    module = surface["envs_data"].get(scene) if hasattr(surface["envs_data"], "get") else None
    if module is None:
        from importlib import import_module  # noqa: PLC0415

        module = import_module(f"tau_bench.envs.{scene}")
    tasks = getattr(module, "tasks", None) or getattr(module, "TASKS", None)
    if tasks is None:
        raise RuntimeError(f"tau_bench.envs.{scene} exposes no tasks list")
    out: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        out.append(
            {
                "task_id": str(getattr(task, "task_id", index)),
                "index": index,
                "raw": task,
            }
        )
    return out


def load_local_tasks(path: Path, scene: str) -> list[dict[str, Any]]:
    """A local tasks JSON overriding the package's list (schema in README)."""
    records = env_setup.load_json_records(path)
    out: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if scene and str(record.get("scene") or "").strip().lower() not in ("", scene):
            continue
        out.append(
            {
                "task_id": str(record.get("task_id") or index),
                "index": index,
                "raw": record,
                "local": True,
            }
        )
    if not out:
        raise ValueError(f"{path}: no tasks for scene {scene!r}")
    return out


def main() -> None:
    """Run the suite."""
    external_bench.maybe_reexec("SHERRY_EVAL_TAUBENCH_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = _parse_args(args)
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)
    try:
        surface = import_tau_bench()
    except ImportError as exc:
        print(
            f"[evals] {SUITE} SKIPPED: the tau_bench package is not installed "
            f"({exc}); install it into this venv to run the suite. "
            "A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3) from exc

    scene = options["scene"]
    try:
        local_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
        local_file = options["dataset"] or _find_dataset(local_dir)
        tasks = (
            load_local_tasks(Path(local_file), scene)
            if local_file
            else available_tasks(surface, scene)
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    ids = [str(task["task_id"]) for task in tasks]
    ids = batch_runner.apply_batch(ids, options["batch"])
    by_id = {str(task["task_id"]): task for task in tasks}
    if options["tasks"]:
        unknown = [t for t in options["tasks"] if t not in by_id]
        if unknown:
            raise SystemExit(f"unknown task id(s): {unknown}")
        selected = [by_id[task_id] for task_id in options["tasks"]]
    else:
        selected = [by_id[task_id] for task_id in ids]
    if options["n_tasks"]:
        selected = selected[: options["n_tasks"]]

    print(
        f"[evals] {SUITE}: {len(tasks)} tasks [scene={scene}] ({len(selected)} selected)",
        flush=True,
    )
    if options["dry_run"]:
        for task in selected:
            print(f"[evals] dry-run ok: {task['task_id']}", flush=True)
        return

    raise SystemExit(asyncio.run(_run(surface, selected, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "tasks.json")
    except ValueError:
        return None


def _parse_args(args: list[str]) -> dict[str, Any]:
    from evals.devbench.suite import _parse_args as base_parse

    options = base_parse(args)
    index = 0
    while index < len(args):
        if args[index] == "--scene":
            if index + 1 >= len(args):
                raise SystemExit("--scene needs a value")
            value = args[index + 1].strip().lower()
            if value not in _SCENES:
                raise SystemExit(f"--scene must be one of {', '.join(_SCENES)}")
            options["scene"] = value
            index += 2
            continue
        index += 1
    options.setdefault("scene", "retail")
    return options


async def _run(
    surface: dict[str, Any],
    selected: list[dict[str, Any]],
    options: dict[str, Any],
) -> int:
    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox
    from evals.tau_bench import api_wrapper

    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SHERRY_WORKSPACE_DIR"] = str(results_dir / "sandbox" / "workspace")
    import config.path  # noqa: F401
    import agent.core  # noqa: F401

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    try:
        harness.init_agent()
        external_bench.trim_tools()  # the episode surface is the API tools + question
        scene = options["scene"]
        tools_by_id: dict[str, list[Any]] = {}
        for task in selected:
            wrapped, env = api_wrapper.build_episode_tools(surface, scene, task)
            tools_by_id[str(task["task_id"])] = wrapped
            task["_env"] = env

        for task in selected:
            task_id = str(task["task_id"])
            session_id = f"evals-{SUITE}-{task_id}-{uuid.uuid4().hex[:6]}"
            started_task = time.monotonic()
            result: dict[str, Any] = {
                "id": task_id,
                "scene": scene,
                "session_id": session_id,
                "resolved": False,
                "failure_kind": "",
                "invalid_run": False,
                "duration_s": 0.0,
                "tool_calls": 0,
                "tool_names": {},
                "timeout": False,
                "error": "",
                "reward_detail": "",
                "tokens": {},
            }
            try:
                api_wrapper.install_episode_tools(tools_by_id[task_id])
                drive = await asyncio.wait_for(
                    api_wrapper.run_episode(
                        surface, scene, task, harness, session_id, timeout=float(options["timeout"])
                    ),
                    timeout=float(options["timeout"]) + 120.0,
                )
                result.update(
                    {
                        "tool_calls": int(drive.get("tool_calls") or 0),
                        "tool_names": dict(drive.get("tool_names") or {}),
                        "timeout": bool(drive.get("timeout")),
                        "error": str(drive.get("error") or ""),
                        "reply": str(drive.get("text") or "")[:1500],
                        "tokens": await harness._session_tokens(session_id),
                        "reward_detail": str(drive.get("reward_detail") or ""),
                    }
                )
                result["resolved"] = bool(drive.get("resolved"))
                if not result["resolved"]:
                    result["failure_kind"] = harness._failure_kind(False, drive)
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            except Exception as exc:  # noqa: BLE001
                result["failure_kind"] = "harness"
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            finally:
                api_wrapper.remove_episode_tools()
            results.append(result)
            print(f"[evals] {task_id}: resolved={result['resolved']}", flush=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = score_reporter.build_report(
        run_id=run_id,
        suite=SUITE,
        kind=f"TAU-bench slice ({len(results)} tasks) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info()},
        benchmark={
            "dataset": f"tau_bench.envs.{options['scene']}"
            + (f" or {options.get('dataset')}" if options.get("dataset") else ""),
            "grading": "the package's own reward (final state + expected actions)",
            "user_simulator": "the main model through the package's interface",
        },
    )
    external_bench.write_report(results_dir, report)
    score_reporter.write_summary_txt(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=bool(options["update_baseline"])
    )


if __name__ == "__main__":
    main()
