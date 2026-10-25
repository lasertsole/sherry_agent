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
    uv run python evals/deepswe/suite.py --all --n-tasks 5 --keep-workdirs

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
from evals.deepswe import instances  # noqa: E402
from evals.deepswe.instances import Task  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
_DEFAULT_TIMEOUT_S = 3600.0
_MAX_TIMEOUT_S = 7200.0
_NETWORK = "no-network"

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
"""


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

    ids = tuple(wanted) if wanted else tuple(tasks) if "--all" in flags else instances.DEFAULT_SLICE
    if n_tasks is not None:
        ids = ids[: max(0, n_tasks)]
    try:
        selected = instances.select(tasks, ids)
    except (KeyError, ValueError) as exc:
        print(f"[evals] {exc}", flush=True)
        raise SystemExit(2) from exc

    print(f"[evals] DeepSWE tasks: {len(tasks)} at {task_root} (commit {commit})", flush=True)
    if "--dry-run" in flags:
        for task in selected:
            print(
                f"[evals] dry-run ok: {task.task_id} lang={task.language} "
                f"base={task.base_commit[:10]} image={task.image}",
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
    timeout: float,
    skip_self_check: bool,
    keep_workdirs: bool,
    update_baseline: bool,
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

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    # Bind the REAL build_main_tools before EvalSandbox patches it for child
    # agents: agent.core imported it at module load, so this import must happen
    # before sandbox.apply() or init() would see only the child-safe subset.
    import agent.core  # noqa: F401

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

        prepared = _preflight(
            selected,
            results_dir,
            skip_self_check=skip_self_check,
            docker_detail=docker_detail,
        )
        if prepared is None:
            return 2

        for task, verifier_image in prepared:
            result, payload = await _run_task(
                task,
                verifier_image,
                results_dir,
                timeout=timeout,
                keep_workdirs=keep_workdirs,
                gold_checked=not skip_self_check,
            )
            last_payload = payload or last_payload
            results.append(result)
            print(
                f"[evals] {task.task_id}: resolved={result['resolved']} "
                f"reward={result['reward']} duration={result['duration_s']}s "
                f"tool_calls={result['tool_calls']} tokens={result['tokens']}",
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
            pulled = external_bench.pull_image(task.image)
            if not pulled.ok:
                print(
                    f"[evals] HARNESS FAILURE: could not pull {task.image} for "
                    f"{task.task_id}: {pulled.output[-1500:]}",
                    flush=True,
                )
                return None
        verifier_image = external_bench.image_tag(
            "deepswe", task.task_id, f"{task.base_commit[:8]}-verifier"
        )
        built = external_bench.build_image(
            verifier_image, task.task_dir / "tests", timeout=task.build_timeout_s
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


async def _run_task(
    task: Task,
    verifier_image: str,
    results_dir: Path,
    *,
    timeout: float,
    keep_workdirs: bool,
    gold_checked: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from evals.coding_capability import suite as harness

    task_root = results_dir / "tasks" / task.task_id
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
        "resumes": 0,
        "timeout": False,
        "error": "",
        "patch_path": "",
        "verifier_output_path": "",
        "changed_paths": [],
        "reply": "",
        "tokens": {},
        "gold_self_check": "checked" if gold_checked else "skipped",
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
        bridge = external_bench.DockerBridge(container, host_workdir=host_workdir)
        external_bench.install_terminal_bridge(bridge)
        payload = await external_bench.bind_external_session(session_id, host_workdir)

        prompt = _PROMPT.format(
            instruction=external_bench.localize_instruction(task.instruction, host_workdir, "/app"),
            repo=task.repo,
            base=task.base_commit[:12],
        )
        try:
            drive = await asyncio.wait_for(harness._drive(session_id, prompt), timeout=timeout)
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

        patch = _agent_patch(host_workdir, task.base_commit)
        patch_path = results_dir / f"patch-{task.task_id}.patch"
        patch_path.write_text(patch, encoding="utf-8")
        result["patch_path"] = str(patch_path)
        result["changed_paths"] = _changed_paths(patch)

        grade_dir = results_dir / "verifier" / task.task_id
        reward, resolved, output = _grade(task, verifier_image, patch, grade_dir)
        result["verifier_output_path"] = str(grade_dir / "verifier-output.txt")
        result["reward"] = reward
        result["reward_detail"] = reward
        result["resolved"] = resolved
        if reward is None:
            result["failure_kind"] = "harness"
            result["error"] = (result["error"] + " verifier produced no reward").strip()
        elif not resolved:
            kind = harness._failure_kind(False, drive)
            result["failure_kind"] = kind
            result["invalid_run"] = kind == "provider"
        result["duration_s"] = round(time.monotonic() - started, 1)
        del output
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
    (artifacts / "model.patch").write_text(patch, encoding="utf-8")
    network = "none" if task.verifier_network in {"no-network", "none"} else None
    args = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/bin/bash",
    ]
    if network:
        args += ["--network", network]
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
        "file tools edit the same files from the host side.\n"
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


def _changed_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and path not in paths:
                paths.append(path)
    return paths


def _aggregate(results: list[dict[str, Any]], started: float) -> dict[str, Any]:
    invalid = [result["id"] for result in results if result.get("invalid_run")]
    valid = len(results) - len(invalid)
    resolved = sum(1 for result in results if result.get("resolved"))
    return {
        "tasks_total": len(results),
        "resolved": resolved,
        "invalid_runs": invalid,
        "resolve_rate": round(resolved / max(1, valid), 4),
        "resolve_rate_basis": "valid runs only (provider failures excluded)",
        "duration_s": round(time.monotonic() - started, 1),
    }


if __name__ == "__main__":
    main()
