"""DeepSWE local mode — the external benchmark without Docker.

Docker is required by the official DeepSWE harness, but the default slice is
pure Python and the task images contain nothing the host cannot reproduce:
a pinned checkout, a virtualenv built from the task's ``environment/Dockerfile``
``pip install`` lines, the same hidden ``tests/test.patch``, and the benchmark's
own ``grader.py``. This module keeps that verifier chain byte-for-byte:

- the checkout is time-travelled exactly like the image build (base commit,
  future branches/tags removed, origin removed);
- the agent works in a real host checkout with the REAL main agent, the host
  ``terminal`` / file tools, and web tools removed;
- grading happens in a separate clean copy: the agent's ``git diff`` and the
  held-out test patch are applied by the task's ``grader.py``; the task's
  ``test.sh`` produces the JUnit reports and ``grader.py grade`` writes
  ``reward.json``.

What is NOT reproduced: the container's network namespace, its exact base
image, and its filesystem. ``--local`` is a developer/CI convenience for a
machine without Docker; a local score is not an official DeepSWE score. The
report says so, and only Python tasks are accepted.

Usage::

    uv run python evals/deepswe/suite.py --local
    uv run python evals/deepswe/suite.py --local --skip-self-check
    uv run python evals/deepswe/suite.py --local httpx-streaming-json-iteration
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
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
from evals.deepswe import instances  # noqa: E402
from evals.deepswe.instances import Task  # noqa: E402

_DEFAULT_PYTHON = "3.12"
_LOCAL_NETWORK = "host (no container; network isolation is best-effort)"
_PROXY_ENV = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"})
_EXCLUDED_LOCAL_PATHS = (
    ".venv/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "__pycache__/",
    ".sherry/",
    "*.pyc",
)

_LOCAL_PROMPT = """{instruction}

---
You are working in `{repo}` at commit `{base}`. This is a local copy of the
task's pinned checkout, not a container. A virtualenv built from the task's
Dockerfile is at `{repo}/.venv`; use `{repo}/.venv/bin/python` (or
`.venv/bin/python` from the project directory) to run the project's checks.
The `terminal` tool runs on the host in this directory, and `read_file`,
`write_file` and `patch_file` edit the same files.

- There is no internet access; do not install packages.
- Do not modify test files and do not run `git commit`.
- Complete the task; do not ask questions.
"""


# ── Entry point ─────────────────────────────────────────────────────────────


async def run(
    selected: list[Task],
    *,
    timeout: float,
    skip_self_check: bool,
    keep_workdirs: bool,
    update_baseline: bool,
) -> int:
    """Run the local DeepSWE slice; return the suite exit code."""
    if not selected:
        print("[evals] deepswe-local: no tasks selected", flush=True)
        return 2
    unsupported = [task.task_id for task in selected if task.language != "python"]
    if unsupported:
        print(
            f"[evals] deepswe-local supports Python tasks only; unsupported: {unsupported}",
            flush=True,
        )
        return 2

    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / "deepswe-local" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    # The task checkout must NOT live under this repository: the agent's own
    # self-path write guard would refuse every write below ``sherry_agent/``.
    work_root = Path(
        os.environ.get(
            "SHERRY_EVAL_LOCAL_WORK_ROOT",
            str(external_bench.CACHE_ROOT / "deepswe-local-work"),
        )
    )
    work_dir = work_root / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = _session_names(live_workspace)

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] deepswe-local SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3

    from evals.coding_capability import suite as harness

    started = time.monotonic()
    last_payload: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    gold_checked = not skip_self_check
    try:
        prepared = _preflight(
            selected,
            work_dir,
            skip_self_check=skip_self_check,
            timeout=timeout,
        )
        if prepared is None:
            return 2

        for task in selected:
            result, payload = await _run_task(
                task,
                prepared[task.task_id],
                results_dir,
                work_dir,
                timeout=timeout,
                keep_workdirs=keep_workdirs,
                gold_checked=gold_checked,
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
        if not keep_workdirs:
            shutil.rmtree(work_dir, ignore_errors=True)

    report = {
        "run_id": run_id,
        "suite": "deepswe-local",
        "kind": f"DeepSWE slice ({len(results)} tasks) — local host mode",
        "results_dir": str(results_dir),
        "work_dir": str(work_dir),
        "benchmark": {
            "dataset": instances.DEEPSWE_REPO,
            "version": "DeepSWE v1.1",
            "harness": (
                "host agent + local time-travelled checkout + task venv + the task's "
                "grader.py/test.sh (model.patch + held-out test.patch -> reward.json); "
                "Docker is NOT used; NOT the official DeepSWE scaffold"
            ),
            "verifier": "tests/grader.py + tests/test.sh + tests/config.json",
            "agent_network": _LOCAL_NETWORK,
            "docker": "not used (local mode)",
            "python": _python_version_label(),
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
        report,
        results_dir,
        results_dir / "baseline.json",
        update_baseline=update_baseline,
    )


# ── Preflight ───────────────────────────────────────────────────────────────


def _preflight(
    selected: list[Task],
    work_dir: Path,
    *,
    skip_self_check: bool,
    timeout: float,
) -> dict[str, Path] | None:
    """Clone every task and verify its GOLD patch before any model call."""
    bases: dict[str, Path] = {}
    for task in selected:
        base = _ensure_base(task, work_dir)
        bases[task.task_id] = base
        if skip_self_check:
            print(f"[evals] {task.task_id}: gold self-check skipped by request", flush=True)
            continue
        gold_repo = work_dir / "gold" / task.task_id / "repo"
        gold_out = work_dir / "gold" / task.task_id / "verifier"
        _copy_repo(base, gold_repo)
        try:
            _setup_venv(gold_repo, task)
        except RuntimeError as exc:
            print(
                f"[evals] HARNESS FAILURE: could not prepare the gold environment "
                f"for {task.task_id}: {exc}",
                flush=True,
            )
            return None
        reward, resolved, _ = _grade_local(
            task,
            gold_repo,
            task.gold_patch,
            gold_out,
            timeout=min(task.verifier_timeout_s, timeout) + 120.0,
        )
        if not resolved:
            print(
                f"[evals] HARNESS FAILURE: {task.task_id} is not resolved by its GOLD "
                f"patch (reward={reward}); the task/verifier is unusable. "
                f"See {gold_out / 'verifier-output.txt'}",
                flush=True,
            )
            return None
        print(f"[evals] {task.task_id}: gold patch resolved", flush=True)
    return bases


# ── One task ────────────────────────────────────────────────────────────────


def _run_agent_worker(
    *,
    session_id: str,
    repo: Path,
    results_dir: Path,
    prompt_file: Path,
    result_file: Path,
    worker_log: Path,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    """Run the agent turn in its own process group, with a hard kill deadline."""
    empty: dict[str, Any] = {
        "tool_calls": 0,
        "text": "",
        "resumes": 0,
        "interrupted": 0,
        "timeout": False,
        "error": "",
    }
    env = os.environ.copy()
    venv = repo / ".venv"
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(venv)
    env["PIP_NO_INDEX"] = "1"
    env["SHERRY_WORKSPACE_DIR"] = str(results_dir / "sandbox" / "workspace")
    command = [
        sys.executable,
        "-m",
        "evals.deepswe.agent_worker",
        "--session-id",
        session_id,
        "--repo",
        str(repo),
        "--results-dir",
        str(results_dir),
        "--prompt-file",
        str(prompt_file),
        "--output",
        str(result_file),
    ]
    with worker_log.open("wb") as log:
        proc = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            proc.wait(timeout=max(1.0, float(timeout)))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:  # Windows: no process groups in the POSIX sense
                    proc.kill()
            except (OSError, AttributeError):
                proc.kill()
            try:
                proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                proc.kill()
    if timed_out:
        empty["timeout"] = True
        empty["error"] = f"agent worker exceeded the {int(timeout)}s wall clock"
        return empty, {}, {}
    if result_file.is_file():
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("drive"), dict):
            drive = {**empty, **data["drive"]}
            payload = data.get("agent_config")
            tokens = data.get("tokens")
            return (
                drive,
                payload if isinstance(payload, dict) else {},
                tokens if isinstance(tokens, dict) else {},
            )
    empty["error"] = f"agent worker produced no result (exit {proc.returncode}); see {worker_log}"
    return empty, {}, {}


async def _run_task(
    task: Task,
    base: Path,
    results_dir: Path,
    work_dir: Path,
    *,
    timeout: float,
    keep_workdirs: bool,
    gold_checked: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from evals.coding_capability import suite as harness

    task_root = work_dir / "tasks" / task.task_id
    agent_repo = task_root / "agent"
    verify_repo = task_root / "verify"
    payload: dict[str, Any] = {}
    session_id = f"evals-deepswe-local-{task.task_id}-{uuid.uuid4().hex[:6]}"
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
        "test_edits": [],
        "reply": "",
        "tokens": {},
        "gold_self_check": "checked" if gold_checked else "skipped",
    }
    try:
        _copy_repo(base, agent_repo)
        _setup_venv(agent_repo, task)

        prompt = _LOCAL_PROMPT.format(
            instruction=external_bench.localize_instruction(task.instruction, agent_repo, "/app"),
            repo=agent_repo,
            base=task.base_commit[:12],
        )
        prompt_file = task_root / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        result_file = task_root / "agent-result.json"
        worker_log = task_root / "agent.log"
        drive, payload, tokens = _run_agent_worker(
            session_id=session_id,
            repo=agent_repo,
            results_dir=results_dir,
            prompt_file=prompt_file,
            result_file=result_file,
            worker_log=worker_log,
            timeout=timeout,
        )
        result["tool_calls"] = drive["tool_calls"]
        result["resumes"] = drive["resumes"]
        result["timeout"] = drive["timeout"]
        result["error"] = drive["error"]
        result["reply"] = drive["text"][:2000]
        result["tokens"] = tokens

        patch = _agent_patch(agent_repo, task.base_commit)
        patch_path = results_dir / f"patch-{task.task_id}.patch"
        patch_path.write_text(patch, encoding="utf-8")
        result["patch_path"] = str(patch_path)
        changed = _changed_paths(patch)
        result["changed_paths"] = changed
        test_edits = _test_paths(changed)
        result["test_edits"] = test_edits

        if test_edits:
            result["failure_kind"] = "test_edit"
            result["error"] = (
                result["error"] + f" the agent modified test files: {test_edits}"
            ).strip()
        else:
            _copy_repo(base, verify_repo)
            _setup_venv(verify_repo, task)
            grade_dir = results_dir / "verifier" / task.task_id
            reward, resolved, output = _grade_local(
                task,
                verify_repo,
                patch,
                grade_dir,
                timeout=min(task.verifier_timeout_s, timeout) + 120.0,
            )
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
            del output
        result["duration_s"] = round(time.monotonic() - started, 1)
    except Exception as exc:  # noqa: BLE001 — a harness error is reported as such
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration_s"] = round(time.monotonic() - started, 1)
        print(f"[evals] {task.task_id}: HARNESS ERROR: {result['error']}", flush=True)
    finally:
        if not keep_workdirs:
            shutil.rmtree(task_root, ignore_errors=True)
    return result, payload


# ── Local verifier bridge ───────────────────────────────────────────────────


def _grade_local(
    task: Task,
    repo: Path,
    patch: str,
    out_dir: Path,
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, bool, str]:
    """Apply *patch* + the held-out test patch in a local verifier copy."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    artifacts = out_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "model.patch").write_text(patch, encoding="utf-8")
    tests_dir = _copy_local_tests(task, out_dir)
    script = _localize_verifier_script(task, repo, out_dir, tests_dir)
    env = _local_env(repo, out_dir, tests_dir, artifacts)
    print(f"[evals] {task.task_id}: grading locally in {repo} …", flush=True)
    completed = _run_with_env(
        ["bash", str(script)],
        cwd=repo,
        env=env,
        timeout=timeout,
    )
    (out_dir / "verifier-output.txt").write_text(completed.output, encoding="utf-8")
    reward = external_bench.read_reward(out_dir)
    return reward, external_bench.reward_resolved(reward), completed.output


def _copy_local_tests(task: Task, out_dir: Path) -> Path:
    """Copy the hidden tests and rewrite config report paths for the host."""
    dest = out_dir / "tests"
    shutil.copytree(task.task_dir / "tests", dest)
    config_path = dest / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{task.task_id}: tests/config.json is unreadable: {exc}") from exc
    grade = config.get("grade")
    if isinstance(grade, dict):
        reports = grade.get("reports")
        if isinstance(reports, list):
            grade["reports"] = [str(out_dir / Path(str(report)).name) for report in reports]
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return dest


def _localize_verifier_script(task: Task, repo: Path, out_dir: Path, tests_dir: Path) -> Path:
    """Adapt the task's container paths to this host checkout."""
    source = (tests_dir / "test.sh").read_text(encoding="utf-8")
    script = source
    for old, new in (
        ("/logs/verifier", str(out_dir)),
        ("/tests", str(tests_dir)),
        ("/app", str(repo)),
    ):
        script = script.replace(old, new)
    target = out_dir / "run-verifier.sh"
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)
    return target


def _local_env(repo: Path, out_dir: Path, tests_dir: Path, artifacts: Path) -> dict[str, str]:
    """A verifier environment with no container, no proxy and the task venv."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in _PROXY_ENV and not key.startswith("PIP_")
    }
    venv_bin = repo / ".venv" / "bin"
    env.update(
        {
            "PATH": f"{venv_bin}{os.pathsep}{env.get('PATH', '')}",
            "VIRTUAL_ENV": str(repo / ".venv"),
            "TESTS_DIR": str(tests_dir),
            "VERIFIER_DIR": str(out_dir),
            "APP_DIR": str(repo),
            "ARTIFACTS_DIR": str(artifacts),
            "PYTHONUNBUFFERED": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    return env


def _run_with_env(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> external_bench.CommandResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return external_bench.CommandResult(127, f"{argv[0]}: {exc}")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return external_bench.CommandResult(124, f"{stdout}{stderr}", True)
    return external_bench.CommandResult(
        completed.returncode, f"{completed.stdout}{completed.stderr}"
    )


# ── Repository and environment setup ────────────────────────────────────────


def _ensure_base(task: Task, results_dir: Path) -> Path:
    base = results_dir / "tasks" / task.task_id / "base"
    if (base / ".git").is_dir():
        return base
    _clone_repo(task, base)
    return base


def _clone_repo(task: Task, dest: Path) -> None:
    """Clone the task repo at its base commit with future history removed."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = external_bench.run(["git", "clone", "--quiet", task.repo, str(dest)], timeout=1800.0)
    if not cloned.ok:
        raise RuntimeError(f"{task.task_id}: git clone failed: {cloned.output[-1500:]}")
    commands = [
        ["checkout", "-B", "local-eval", task.base_commit],
        ["remote", "remove", "origin"],
    ]
    for args in commands:
        result = _git(dest, *args)
        if not result.ok:
            raise RuntimeError(
                f"{task.task_id}: git {' '.join(args)} failed: {result.output[-1500:]}"
            )
    branches = _git(dest, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if branches.ok:
        for branch in branches.output.split():
            if branch and branch != "local-eval":
                _git(dest, "branch", "-D", branch)
    tags = _git(dest, "tag")
    if tags.ok:
        for tag in tags.output.split():
            ancestor = _git(dest, "merge-base", "--is-ancestor", tag, "HEAD")
            if not ancestor.ok:
                _git(dest, "tag", "-d", tag)
    _git(dest, "reflog", "expire", "--expire=now", "--all")
    _git(dest, "gc", "--prune=now")
    _git(dest, "submodule", "update", "--init", "--recursive")
    _git(dest, "config", "core.hooksPath", "/dev/null")
    _write_excludes(dest)


def _copy_repo(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, symlinks=True)
    _write_excludes(dest)


def _write_excludes(repo: Path) -> None:
    """Keep harness-only files out of the agent's captured patch."""
    info = repo / ".git" / "info"
    if not info.is_dir():
        return
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    additions = [line for line in _EXCLUDED_LOCAL_PATHS if line not in lines]
    if additions:
        exclude.write_text(existing + "\n" + "\n".join(additions) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str, timeout: float = 300.0) -> external_bench.CommandResult:
    return external_bench.run(["git", "-C", str(repo), *args], timeout=timeout)


def _setup_venv(repo: Path, task: Task) -> Path:
    """Create the task venv from the task Dockerfile's pip install lines."""
    venv = repo / ".venv"
    if venv.exists():
        shutil.rmtree(venv)
    python = _python_version(repo)
    created = external_bench.run(
        ["uv", "venv", "--python", python, str(venv)], cwd=repo, timeout=900.0
    )
    if not created.ok:
        alternate = "3.13" if python == "3.12" else "3.12"
        created = external_bench.run(
            ["uv", "venv", "--python", alternate, str(venv)],
            cwd=repo,
            timeout=900.0,
        )
        if not created.ok:
            raise RuntimeError(f"{task.task_id}: uv venv failed: {created.output[-1500:]}")
    venv_python = venv / "bin" / "python"
    for args in _pip_commands(task):
        installed = external_bench.run(
            ["uv", "pip", "install", "--python", str(venv_python), *args],
            cwd=repo,
            timeout=1800.0,
        )
        if not installed.ok:
            raise RuntimeError(
                f"{task.task_id}: pip install {' '.join(args)} failed: {installed.output[-2000:]}"
            )
    check = external_bench.run(
        [str(venv_python), "-c", "import pytest, pytest_timeout"],
        cwd=repo,
        timeout=120.0,
    )
    if not check.ok:
        installed = external_bench.run(
            ["uv", "pip", "install", "--python", str(venv_python), "pytest", "pytest-timeout"],
            cwd=repo,
            timeout=900.0,
        )
        if not installed.ok:
            raise RuntimeError(f"{task.task_id}: pytest install failed: {installed.output[-1500:]}")
    return venv


def _pip_commands(task: Task) -> list[list[str]]:
    """Extract ``pip install`` argument lists from the task's Dockerfile."""
    dockerfile = task.task_dir / "environment" / "Dockerfile"
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except OSError:
        return [["-e", "."]]
    instructions: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        pending += line
        instructions.append(pending)
        pending = ""
    if pending:
        instructions.append(pending)

    commands: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for instruction in instructions:
        stripped = instruction.strip()
        if not stripped.startswith("RUN "):
            continue
        for segment in stripped[4:].split("&&"):
            match = re.search(
                r"\b(?:python(?:3(?:\.\d+)?)?\s+-m\s+)?pip\s+install\s+(.+)",
                segment,
            )
            if not match:
                continue
            args = shlex.split(match.group(1))
            if not args:
                continue
            key = tuple(args)
            if key not in seen:
                seen.add(key)
                commands.append(args)
    return commands or [["-e", "."]]


def _python_version(repo: Path) -> str:
    """Pick the host interpreter closest to the repo's requires-python floor."""
    pyproject = repo / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return _DEFAULT_PYTHON
    match = re.search(r"requires-python\s*=\s*[\"']([^\"']+)[\"']", text)
    if match and re.search(r"3\.13", match.group(1)):
        return "3.13"
    return _DEFAULT_PYTHON


def _python_version_label() -> str:
    return f"uv venv {_DEFAULT_PYTHON}/3.13 from the repo's requires-python floor"


# ── Small helpers ───────────────────────────────────────────────────────────


def _session_names(workspace: Path) -> set[str]:
    sessions = workspace / "sessions"
    if not sessions.is_dir():
        return set()
    return {entry.name for entry in sessions.iterdir() if entry.is_dir()}


def _append_agents_note(workspace: Path) -> None:
    rules = workspace / "AGENTS.md"
    note = (
        "\n## DeepSWE local environment\n\n"
        "- The session's project directory is a local copy of the task's pinned "
        "checkout; a virtualenv built from the task Dockerfile is at `.venv`. "
        "Run checks with `.venv/bin/python -m pytest`.\n"
        "- The `terminal` tool runs on the host; there is no Docker and no "
        "internet. Never modify tests or run `git commit`.\n"
    )
    rules.write_text(rules.read_text(encoding="utf-8") + note, encoding="utf-8")


def _agent_patch(repo: Path, base_commit: str) -> str:
    add = _git(repo, "add", "-A")
    if not add.ok:
        raise RuntimeError(f"git add failed: {add.output[-1000:]}")
    diff = _git(repo, "diff", "--binary", "--cached", base_commit, timeout=300.0)
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


def _test_paths(paths: list[str]) -> list[str]:
    """Files whose modification invalidates a local run (held-out tests)."""
    markers = {"conftest.py", "pytest.ini", "tox.ini"}
    hits: list[str] = []
    for path in paths:
        name = Path(path).name
        if (
            path.startswith("tests/")
            or path.startswith("test/")
            or name in markers
            or name.startswith("test_")
        ):
            hits.append(path)
    return hits


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
