"""OSWorld — the desktop benchmark, terminal-first (the plan's kill criterion).

The official OSWorld runs tasks inside an Ubuntu desktop VM and evaluates the
machine's STATE afterwards (files, processes, command outputs) through getter
functions. The plan's own degradation rule is implemented here: GUI
interaction (VNC + screenshots driving a desktop) is the UNSTABLE path, so
this adapter is TERMINAL-FIRST — the agent gets a DockerBridge terminal into
the task's Ubuntu container and works the desktop's command line; a
screenshot-capable branch stays fail-open behind ``SHERRY_OSWORLD_VNC=1`` for
a host that has a working VNC bridge, and the report records which mode ran.

Dataset: ``OSWORLD_DATA_DIR`` (the benchmark's ``evaluation_examples/examples``
JSONs flattened, or ``--dataset``): records of
``{id, snapshot, config: {setup?, evaluator: {func, expected, ...}}}``. The
evaluator subset judged natively — the state checks that map cleanly to
``docker exec``:

- ``vm_file_exists`` / ``vm_file_content_matches`` — a file must (match a
  pattern and) exist under the container's HOME;
- ``vm_command_line`` / ``vm_command_line_result`` — a command's exit status /
  output must match the expectation.

Anything else (GUI getters, macOS/Windows hosts) is UNJUDGED with an honest
detail — the plan says Ubuntu-only anyway.
"""

from __future__ import annotations

import asyncio
import os
import re
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
from evals.shared import batch_runner, container_manager, env_setup, score_reporter  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "osworld"
DATA_ENV_VAR = "OSWORLD_DATA_DIR"
_IMAGE_ENV_VAR = "OSWORLD_CONTAINER_IMAGE"
DEFAULT_IMAGE = "sherry-eval-osworld-ubuntu"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Parse + validate the evaluation examples (an evaluator is required)."""
    records = env_setup.load_json_records(path)
    cleaned: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        task_id = str(record.get("id") or record.get("task_id") or f"osworld-{index:04d}")
        config = record.get("config") or {}
        evaluator = config.get("evaluator") or {}
        if not evaluator:
            raise ValueError(f"{path}: record {task_id} has no evaluator")
        cleaned.append(
            {
                "task_id": task_id,
                "instruction": str(record.get("instruction") or record.get("description") or ""),
                "config": config,
                "evaluator": evaluator,
            }
        )
    return cleaned


# ── The native judge (docker-exec getters) ──────────────────────────────────


def evaluate(evaluator: dict[str, Any], container: str) -> tuple[bool, str]:
    """Judge one evaluator spec inside the task container (fail-closed on an
    unknown func: the task is unjudged, never auto-passed)."""
    func = str(evaluator.get("func") or evaluator.get("type") or "")
    expected = evaluator.get("expected") or evaluator.get("result")
    postconfig = evaluator.get("postconfig") or []
    if func == "vm_file_exists":
        path = str(_first_postconfig_path(postconfig) or evaluator.get("path") or "")
        if not path:
            return False, "vm_file_exists without a path"
        code, out = _exec(container, f"test -e {path} && echo EXISTS || echo MISSING")
        return ("EXISTS" in out), f"file {path}: {out.strip()[:80]}"
    if func == "vm_file_content_matches":
        path = str(_first_postconfig_path(postconfig) or evaluator.get("path") or "")
        pattern = str((expected or {}).get("pattern") or expected or "")
        if not path or not pattern:
            return False, "content matcher without path/pattern"
        code, out = _exec(container, f"cat {path} 2>/dev/null | head -c 100000")
        return bool(re.search(pattern, out)), f"pattern {pattern!r} in {path}"
    if func in ("vm_command_line", "vm_command_line_result"):
        command = str(_first_postconfig_command(postconfig) or evaluator.get("command") or "")
        if not command:
            return False, "command getter without a command"
        want_exit = (expected or {}).get("exit_code") if isinstance(expected, dict) else None
        want_output = (
            (expected or {}).get("stdout_regex")
            or (expected or {}).get("pattern")
            or (expected if isinstance(expected, str) else "")
        )
        code, out = _exec(container, command)
        ok = True
        if want_exit is not None:
            ok = ok and code == int(want_exit)
        if want_output:
            ok = ok and bool(re.search(str(want_output), out))
        return ok, f"exit={code} out={out.strip()[:80]!r}"
    return False, f"unjudged: evaluator func {func!r} needs the official OSWorld runner"


def _first_postconfig_path(postconfigs: list[dict[str, Any]]) -> str:
    for entry in postconfigs or []:
        if str(entry.get("type") or "") in ("vm_file", "file"):
            return str(entry.get("path") or "")
    return ""


def _first_postconfig_command(postconfigs: list[dict[str, Any]]) -> str:
    for entry in postconfigs or []:
        if str(entry.get("type") or "").startswith("vm_command"):
            return str(entry.get("command") or "")
    return ""


def _exec(container: str, command: str) -> tuple[int, str]:
    completed = external_bench.run(
        [external_bench.container_bin(), "exec", container, "/bin/bash", "-lc", command],
        timeout=120.0,
    )
    return completed.exit_code, completed.output


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the suite (per-task Ubuntu containers; terminal-first)."""
    external_bench.maybe_reexec("SHERRY_EVAL_OSWORLD_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = _parse_args(args)
    data_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
    dataset_file = options["dataset"] or _find_dataset(data_dir)
    if dataset_file is None:
        print(
            f"[evals] {SUITE} SKIPPED: no dataset (looked in {data_dir}; set "
            f"{DATA_ENV_VAR} or pass --dataset). A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3)
    try:
        records = load_dataset(Path(dataset_file))
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    image = options["image"]
    selected = _select(records, options)
    print(
        f"[evals] {SUITE}: {len(records)} tasks ({len(selected)} selected; image={image} "
        f"vnc={bool(options['vnc'])})",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(
                f"[evals] dry-run ok: {record['task_id']} func={record['evaluator'].get('func')}",
                flush=True,
            )
        return
    docker_ok, docker_detail = external_bench.docker_available()
    if not docker_ok:
        print(f"[evals] {SUITE} SKIPPED: Docker is not usable ({docker_detail})", flush=True)
        raise SystemExit(3)
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)
    raise SystemExit(asyncio.run(_run(records, selected, image, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    """The dataset file inside *data_dir*, or None (the suite skips)."""
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "tasks.json", "examples.json")
    except ValueError:
        return None


def _parse_args(args: list[str]) -> dict[str, Any]:
    from evals.devbench.suite import _parse_args as base_parse

    options = base_parse(args)
    index = 0
    while index < len(args):
        if args[index] in ("--image", "--vnc"):
            if args[index] == "--image":
                if index + 1 >= len(args):
                    raise SystemExit("--image needs a value")
                options["image"] = args[index + 1]
                index += 2
                continue
            options["vnc"] = True
            index += 1
            continue
        index += 1
    options.setdefault("image", os.environ.get(_IMAGE_ENV_VAR, DEFAULT_IMAGE))
    options.setdefault("vnc", os.environ.get("SHERRY_OSWORLD_VNC", "") == "1")
    return options


def _select(records: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [str(record["task_id"]) for record in records]
    ids = batch_runner.apply_batch(ids, options["batch"])
    by_id = {str(record["task_id"]): record for record in records}
    if options["tasks"]:
        unknown = [t for t in options["tasks"] if t not in by_id]
        if unknown:
            raise SystemExit(f"unknown task id(s): {unknown}")
        selected = [by_id[task_id] for task_id in options["tasks"]]
    else:
        selected = [by_id[task_id] for task_id in ids]
    if options["n_tasks"]:
        selected = selected[: options["n_tasks"]]
    if options["resume"]:
        keep = set(
            batch_runner.apply_resume(
                [str(record["task_id"]) for record in selected],
                REPO_ROOT / "evals" / "results" / SUITE,
            )
        )
        selected = [record for record in selected if str(record["task_id"]) in keep]
    return selected


async def _run(
    records: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    image: str,
    options: dict[str, Any],
) -> int:
    import agent.core  # noqa: F401
    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    try:
        harness._seed_persona(eval_workspace)
        harness.init_agent()
        external_bench.trim_tools()
        external_bench.install_orchestration_gate()
        for record in selected:
            session_id = f"evals-{SUITE}-{record['task_id']}-{uuid.uuid4().hex[:6]}"
            started_task = time.monotonic()
            container = container_manager.TaskContainer(
                suite=SUITE,
                task_id=record["task_id"],
                image=image,
                host_workdir=(
                    container_manager.work_root(
                        SUITE, results_dir, os.environ.get("SHERRY_EVAL_WORK_ROOT", "")
                    )
                    / str(record["task_id"])
                    / "workspace"
                ),
                network="none",
            )
            result: dict[str, Any] = {
                "id": record["task_id"],
                "session_id": session_id,
                "mode": "vnc" if options["vnc"] else "terminal-first",
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
                container.prepare()
                container.connect()
                await external_bench.bind_external_session(session_id, container.host_workdir)
                prompt = (
                    f"{record['instruction']}\n\n---\nYou are operating an Ubuntu "
                    "desktop machine THROUGH ITS COMMAND LINE (the `terminal` tool "
                    "runs inside the machine). Accomplish the task with shell "
                    "commands and file edits; verify your work before finishing. "
                    "Do not ask questions."
                )
                drive = await asyncio.wait_for(
                    harness._drive(session_id, prompt), timeout=float(options["timeout"])
                )
                result.update(
                    {
                        "tool_calls": int(drive.get("tool_calls") or 0),
                        "tool_names": dict(drive.get("tool_names") or {}),
                        "timeout": bool(drive.get("timeout")),
                        "error": str(drive.get("error") or ""),
                        "reply": str(drive.get("text") or "")[:1500],
                        "tokens": await harness._session_tokens(session_id),
                    }
                )
                resolved, detail = evaluate(record["evaluator"], container.container)
                result["resolved"] = resolved
                result["reward_detail"] = detail
                if not resolved and not drive["timeout"] and not drive["error"]:
                    result["failure_kind"] = harness._failure_kind(False, drive)
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            except Exception as exc:  # noqa: BLE001
                result["failure_kind"] = "harness"
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            finally:
                container.cleanup(keep_workdir=bool(options["keep_workdirs"]))
            results.append(result)
            print(
                f"[evals] {result['id']}: resolved={result['resolved']} ({result['reward_detail'][:60]})",
                flush=True,
            )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = score_reporter.build_report(
        run_id=run_id,
        suite=SUITE,
        kind=f"OSWorld slice ({len(results)} tasks, terminal-first) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info()},
        benchmark={
            "dataset": str(options.get("dataset") or DATA_ENV_VAR),
            "image": image,
            "mode": "terminal-first (VNC/GUI driving degraded per the plan's kill criterion)",
            "grading": (
                "native: vm_file_exists / vm_file_content_matches / vm_command_line "
                "judged via docker exec; GUI getters are unjudged"
            ),
        },
    )
    external_bench.write_report(results_dir, report)
    score_reporter.write_summary_txt(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=bool(options["update_baseline"])
    )


if __name__ == "__main__":
    main()
