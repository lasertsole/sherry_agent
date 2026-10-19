"""Terminal-Bench eval suite — the external benchmark, driven by the main agent.

What is measured: Terminal-Bench's own pytest verdict, task by task. The task's
Docker image is built from its ``Dockerfile``; its working tree is copied to the
host so the agent's FILE tools can edit it, bind-mounted back into the container
at the same paths, and the ``terminal`` tool executes inside that container. The
task's ``run-tests.sh`` then runs in the container and the pytest short summary
is parsed with Terminal-Bench's rule (a non-empty result set where every test
passed).

Faithfulness notes (also written into the report):

- the instruction is the task's own ``instruction.md``/``task.yaml`` text; the
  hidden tests are copied into ``/tests`` only after the agent's turn;
- a task whose compose file needs extra services is marked unsupported instead
  of being run against a half-built environment;
- every task's oracle (``solution.sh`` / ``solution.yaml``) is run through the
  same image + test path before any model call (``--skip-self-check`` disables
  that check, never the test execution);
- the test script's verdict is the only pass signal; Docker and the task image
  are prerequisites, so without them the suite SKIPS loudly and exits non-zero.

Usage::

    uv run python evals/evals.py terminal_bench
    uv run python evals/terminal_bench/suite.py --dry-run
    uv run python evals/terminal_bench/suite.py hello-world fix-permissions
    uv run python evals/terminal_bench/suite.py --all --n-tasks 5 --keep-workdirs

``--dry-run`` validates the dataset and task files without Docker or a model.
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
from evals.terminal_bench import tasks as task_data  # noqa: E402
from evals.terminal_bench.tasks import Task  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
_MAX_TIMEOUT_S = 7200.0
_BUILD_TIMEOUT_S = 1800.0

_PROMPT = """{instruction}

---
The task container's working directory is the SAME tree as this session's
project directory, bind-mounted into the container; the instruction's container
paths were rewritten to that project directory. The `terminal` tool runs inside
the container with a persistent shell; `read_file`, `write_file` and
`patch_file` edit the same bytes from the host side.

- Use the container's tools and install what you need there.
- The tests live outside the tree and are not visible to you; do not try to
  modify them.
- Complete the task; do not ask questions.
"""


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the suite (re-executing in a clean interpreter when needed)."""
    external_bench.maybe_reexec("SHERRY_EVAL_TBENCH_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == "terminal_bench":
        args = args[1:]

    wanted: list[str] = []
    flags: set[str] = set()
    n_tasks: int | None = None
    timeout: float | None = None
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
        }:
            flags.add(arg)
        elif arg.startswith("--"):
            raise SystemExit(f"unknown flag: {arg}")
        else:
            wanted.append(arg)
        index += 1

    if wanted and "--all" in flags:
        raise SystemExit("name task ids or use --all, not both")
    if timeout is not None:
        timeout = max(60.0, min(float(timeout), _MAX_TIMEOUT_S))

    if "--dry-run" not in flags:
        docker_ok, docker_detail = external_bench.docker_available()
        if not docker_ok:
            print(
                f"[evals] terminal_bench SKIPPED: Docker is not usable ({docker_detail}). "
                "A skipped suite is not a pass.",
                flush=True,
            )
            raise SystemExit(3)

    try:
        task_root, commit = task_data.fetch_tasks()
        tasks = task_data.load_tasks(task_root)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    if wanted:
        ids = tuple(wanted)
    elif "--all" in flags:
        ids = tuple(task_id for task_id, task in tasks.items() if task.supported)
        unsupported = [task_id for task_id, task in tasks.items() if not task.supported]
        if unsupported:
            print(
                f"[evals] skipping {len(unsupported)} task(s) the single-client "
                f"runner cannot represent: {unsupported[:8]}"
                + (" …" if len(unsupported) > 8 else ""),
                flush=True,
            )
    else:
        ids = task_data.DEFAULT_SLICE
    if n_tasks is not None:
        ids = ids[: max(0, n_tasks)]
    try:
        selected = task_data.select(tasks, ids)
    except (KeyError, ValueError) as exc:
        print(f"[evals] {exc}", flush=True)
        raise SystemExit(2) from exc

    print(
        f"[evals] Terminal-Bench tasks: {len(tasks)} at {task_root} (commit {commit})",
        flush=True,
    )
    if "--dry-run" in flags:
        for task in selected:
            print(
                f"[evals] dry-run ok: {task.task_id} "
                f"agent_timeout={task.agent_timeout_s:.0f}s "
                f"test_timeout={task.test_timeout_s:.0f}s "
                f"oracle={'solution.sh' if task.solution_sh else 'solution.yaml'}",
                flush=True,
            )
        print(
            "[evals] dry-run only; a real run needs Docker and MAIN_LLM_API_KEY "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return

    raise SystemExit(
        asyncio.run(
            _run(
                selected,
                commit=commit,
                timeout=timeout,
                skip_self_check="--skip-self-check" in flags,
                keep_workdirs="--keep-workdirs" in flags,
                update_baseline="--update-baseline" in flags,
            )
        )
    )


# ── Suite setup ─────────────────────────────────────────────────────────────


async def _run(
    selected: list[Task],
    *,
    commit: str,
    timeout: float | None,
    skip_self_check: bool,
    keep_workdirs: bool,
    update_baseline: bool,
) -> int:
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / "terminal_bench" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = _session_names(live_workspace)

    docker_ok, docker_detail = external_bench.docker_available()
    if not docker_ok:
        print(
            f"[evals] terminal_bench SKIPPED: Docker is not usable ({docker_detail}). "
            "A skipped suite is not a pass.",
            flush=True,
        )
        return 3

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    # Bind the REAL build_main_tools before EvalSandbox patches it for child
    # agents: agent.core imported it at module load, so this import must happen
    # before sandbox.apply() or init() would see only the child-safe subset.
    import agent.core  # noqa: F401

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] terminal_bench SKIPPED: MAIN_LLM_API_KEY is not configured "
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
        external_bench.trim_tools()
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

        prepared, self_checks = _preflight(
            selected,
            results_dir,
            commit=commit,
            skip_self_check=skip_self_check,
        )
        if prepared is None:
            return 2

        for task, image, workdir in prepared:
            result, payload = await _run_task(
                task,
                image,
                workdir,
                results_dir,
                timeout=timeout,
                keep_workdirs=keep_workdirs,
                self_check=self_checks.get(task.task_id, "skipped"),
            )
            last_payload = payload or last_payload
            results.append(result)
            print(
                f"[evals] {task.task_id}: passed={result['passed']} "
                f"duration={result['duration_s']}s tool_calls={result['tool_calls']} "
                f"tokens={result['tokens']}",
                flush=True,
            )
    except Exception:  # noqa: BLE001 — a setup bug is a harness failure, not a zero
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()
        if not keep_workdirs:
            shutil.rmtree(results_dir / "tasks", ignore_errors=True)

    report = {
        "run_id": run_id,
        "suite": "terminal_bench",
        "kind": f"Terminal-Bench slice ({len(results)} tasks) — external benchmark",
        "results_dir": str(results_dir),
        "benchmark": {
            "dataset": task_data.TERMINAL_BENCH_REPO,
            "version": "Terminal-Bench 2.0-era tasks",
            "commit": commit,
            "harness": (
                "host agent + the task's Docker image (bind-mounted tree) + its own "
                "run-tests.sh pytest verdict; NOT the official tmux/harbor harness"
            ),
            "judge": "pytest short summary, Terminal-Bench's parser rule",
            "agent_network": "container default bridge; host web tools disabled",
            "docker": docker_detail,
        },
        "model": {**harness._model_info(), "thinking_enabled": False},
        "agent_config": last_payload,
        "persona": harness._persona_evidence(results[0]["session_id"] if results else None),
        "isolation": harness._isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "gold_self_check": self_checks,
        "aggregate": _aggregate(results, started),
        "tasks": results,
    }
    external_bench.write_report(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=update_baseline
    )


# ── Preflight: build every image, run every oracle ─────────────────────────


def _preflight(
    selected: list[Task],
    results_dir: Path,
    *,
    commit: str,
    skip_self_check: bool,
) -> tuple[list[tuple[Task, str, str]] | None, dict[str, str]]:
    prepared: list[tuple[Task, str, str]] = []
    self_checks: dict[str, str] = {}
    for task in selected:
        image = external_bench.image_tag(
            "terminal-bench", task.task_id, commit[:8] if commit != "local" else "local"
        )
        built = external_bench.build_image(image, task.task_dir, timeout=_BUILD_TIMEOUT_S)
        if not built.ok:
            print(
                f"[evals] HARNESS FAILURE: could not build the image for "
                f"{task.task_id}: {built.output[-2000:]}",
                flush=True,
            )
            return None, self_checks
        workdir = external_bench.image_workdir(image)
        if skip_self_check:
            self_checks[task.task_id] = "skipped"
        else:
            ok, output = _self_check(task, image, workdir, results_dir)
            if not ok:
                print(
                    f"[evals] HARNESS FAILURE: {task.task_id} is not resolved by its "
                    f"oracle; the task/runner is unusable. "
                    f"See {results_dir / 'gold' / f'{task.task_id}.txt'}",
                    flush=True,
                )
                return None, self_checks
            self_checks[task.task_id] = "checked"
            del output
        prepared.append((task, image, workdir))
    return prepared, self_checks


def _self_check(
    task: Task,
    image: str,
    workdir: str,
    results_dir: Path,
) -> tuple[bool, str]:
    """Run the task's oracle, then its tests, in a throwaway container."""
    print(f"[evals] {task.task_id}: running the oracle self-check …", flush=True)
    container = external_bench.create_container(
        image,
        external_bench.container_name("tbench-oracle", task.task_id),
        workdir=workdir,
        env={"TEST_DIR": "/tests"},
    )
    output_parts: list[str] = []
    try:
        bridge = external_bench.DockerBridge(container, host_workdir=Path(workdir))
        bridge.copy_tests(task.task_dir)
        solution_timeout = max(300.0, task.test_timeout_s)
        if task.solution_sh:
            bridge.copy_source(task.task_dir / "solution.sh", "/oracle")
            output_parts.append(bridge.exec("bash /oracle/solution.sh", timeout=solution_timeout))
        else:
            for command, block in task.solution_yaml_commands:
                if block:
                    output_parts.append(bridge.exec(command, timeout=solution_timeout))
                else:
                    # The official oracle sends a non-blocking command and moves
                    # on; keep the same shape instead of waiting for a server.
                    bridge.exec(f"{command} >/dev/null 2>&1 &", timeout=10.0)
        output_parts.append(
            bridge.execute_tests(workdir=workdir, timeout=task.test_timeout_s + 60.0)
        )
    finally:
        external_bench.remove_container(container)
    output = "\n".join(output_parts)
    gold_dir = results_dir / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)
    (gold_dir / f"{task.task_id}.txt").write_text(output, encoding="utf-8")
    parsed = external_bench.parse_pytest_output(output)
    return external_bench.pytest_resolved(parsed), output


# ── One task ────────────────────────────────────────────────────────────────


async def _run_task(
    task: Task,
    image: str,
    workdir: str,
    results_dir: Path,
    *,
    timeout: float | None,
    keep_workdirs: bool,
    self_check: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from evals.coding_capability import suite as harness

    task_root = results_dir / "tasks" / task.task_id
    host_workdir = task_root / "workspace"
    container = ""
    payload: dict[str, Any] = {}
    session_id = f"evals-tbench-{task.task_id}-{uuid.uuid4().hex[:6]}"
    agent_timeout = task.agent_timeout_s
    if timeout is not None:
        agent_timeout = min(agent_timeout, timeout)
    started = time.monotonic()
    result: dict[str, Any] = {
        "id": task.task_id,
        "category": "terminal-bench",
        "image": image,
        "workdir": workdir,
        "session_id": session_id,
        "passed": False,
        "failure_kind": "",
        "invalid_run": False,
        "duration_s": 0.0,
        "tool_calls": 0,
        "resumes": 0,
        "timeout": False,
        "test_timeout": False,
        "test_parse_error": False,
        "error": "",
        "changed_paths": [],
        "test_output_path": "",
        "tests": None,
        "reply": "",
        "tokens": {},
        "gold_self_check": self_check,
    }
    try:
        external_bench.extract_workdir(
            image,
            workdir,
            host_workdir,
            name=external_bench.container_name("tbench-extract", task.task_id),
        )
        container = external_bench.start_agent_container(
            image,
            external_bench.container_name("tbench", task.task_id),
            host_workdir=host_workdir,
            workdir=workdir,
            env={"TEST_DIR": "/tests"},
        )
        bridge = external_bench.DockerBridge(container, host_workdir=host_workdir)
        external_bench.install_terminal_bridge(bridge)
        payload = await external_bench.bind_external_session(session_id, host_workdir)

        prompt = _PROMPT.format(
            instruction=external_bench.localize_instruction(task.instruction, host_workdir, workdir)
        )
        try:
            drive = await asyncio.wait_for(
                harness._drive(session_id, prompt), timeout=agent_timeout
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
        result["tokens"] = await harness._session_tokens(session_id)

        try:
            test_output = bridge.run_tests(
                task.task_dir, workdir=workdir, timeout=task.test_timeout_s + 60.0
            )
        except Exception as exc:  # noqa: BLE001 — test execution failure is unresolved
            test_output = f"[sherry-eval] test execution failed: {type(exc).__name__}: {exc}"
            result["test_timeout"] = isinstance(exc, TimeoutError)
        test_path = results_dir / f"test-output-{task.task_id}.txt"
        test_path.write_text(test_output, encoding="utf-8")
        result["test_output_path"] = str(test_path)
        parsed = external_bench.parse_pytest_output(test_output)
        result["tests"] = parsed
        result["test_parse_error"] = parsed is None
        result["passed"] = external_bench.pytest_resolved(parsed)
        result["duration_s"] = round(time.monotonic() - started, 1)
        if not result["passed"]:
            kind = harness._failure_kind(False, drive)
            result["failure_kind"] = kind
            result["invalid_run"] = kind == "provider"
    except Exception as exc:  # noqa: BLE001 — a harness error is reported as such
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration_s"] = round(time.monotonic() - started, 1)
        print(f"[evals] {task.task_id}: HARNESS ERROR: {result['error']}", flush=True)
    finally:
        if container:
            external_bench.remove_container(container)
        if not keep_workdirs:
            shutil.rmtree(task_root, ignore_errors=True)
    return result, payload


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
        "- The task container's working directory is this session's project directory; "
        "the same tree is bind-mounted into the container, and the task text's "
        "container paths are rewritten to the project directory.\n"
        "- The `terminal` tool runs inside the container, in a persistent shell; file "
        "tools edit the same files from the host side.\n"
        "- Tests are hidden outside the tree. Use the container's tools; do not modify "
        "tests or ask questions.\n"
    )
    rules.write_text(rules.read_text(encoding="utf-8") + note, encoding="utf-8")


def _aggregate(results: list[dict[str, Any]], started: float) -> dict[str, Any]:
    invalid = [result["id"] for result in results if result.get("invalid_run")]
    valid = len(results) - len(invalid)
    passed = sum(1 for result in results if result.get("passed"))
    return {
        "tasks_total": len(results),
        "resolved": passed,
        "invalid_runs": invalid,
        "resolve_rate": round(passed / max(1, valid), 4),
        "resolve_rate_basis": "valid runs only (provider failures excluded); metric=pass rate",
        "duration_s": round(time.monotonic() - started, 1),
    }


if __name__ == "__main__":
    main()
