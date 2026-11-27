"""Docker-free L1 rerun: drive Sherry agent on the host, grade with deepswe-local.

Runs only the task ids passed on the command line. Each task gets a fresh copy
of the prepared local checkout, a separate worker process (hard timeout), and
a clean local verifier run through ``deepswe_local.py grade``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parent
SHERRY_ROOT = LOCAL_ROOT.parent / "sherry_agent"
TASKS_DIR = LOCAL_ROOT.parent / "deep-swe" / "tasks"
WORKER_MODULE = "evals.deepswe.local_agent_worker"
TIMEOUT_S = 3600.0
MAX_ITERATIONS = "180"

ORCHESTRATION_MANDATE = """
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

Decompose the flow so it RUNS in parallel, not as one serial chain. Write
`depends_on` ONLY for real data dependencies. Plan ONCE; do not rewrite the
whole step list with `taskflow_step(action="update_steps")`. While step
children run, use one `taskflow_step(action="wait_all")` instead of polling.
"""


def _git(repo: Path, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _prepare_base(task: str) -> Path:
    base = LOCAL_ROOT / "work" / task / "base"
    if not base.is_dir():
        raise RuntimeError(f"missing prepared checkout: {base}")
    for args in (("reset", "--hard", "HEAD"), ("clean", "-fd")):
        cp = _git(base, *args)
        if cp.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed in {base}: {cp.stderr[-800:]}")
    # Cargo/linker leaves chains of .l2s.* symlinks in target/ that make cp -a
    # recurse into them. They are disposable build temporaries, not sources.
    target_dir = base / "target"
    if target_dir.is_dir():
        subprocess.run(
            ["find", str(target_dir), "-type", "l", "-delete"],
            capture_output=True,
            timeout=120.0,
        )
    return base


def _build_prompt(task: str, repo: Path, base_commit: str) -> Path:
    instruction_file = TASKS_DIR / task / "instruction.md"
    if not instruction_file.is_file():
        instruction_file = LOCAL_ROOT / "tasks-py-node" / task / "instruction.md"
    instruction = instruction_file.read_text(encoding="utf-8")
    instruction = instruction.replace("/app", str(repo))
    prompt = f"""{instruction}

---
You are working in `{repo}` at commit `{base_commit[:12]}`. This is a local
copy of the task's pinned checkout, not a container. The project's own
toolchain is installed on the host; use `terminal` to run the project's checks.
`read_file`, `write_file` and `patch_file` edit the same files.

- There is no internet access; do not install packages.
- Do not modify test files and do not run `git commit`.
- Complete the task; do not ask questions.
- Edge cases are part of the contract: empty input, whitespace-only input,
  partial/truncated input, and calls after the stream has ended must behave
  predictably — an empty result, not an exception, unless the task says
  otherwise. Recursive or cyclic inputs must terminate with a clear error or a
  well-defined result, never by overflowing the call stack.
{ORCHESTRATION_MANDATE}
"""
    return prompt


def _run_worker(
    *,
    session_id: str,
    repo: Path,
    run_dir: Path,
    prompt_file: Path,
    result_file: Path,
    log_file: Path,
) -> None:
    env = os.environ.copy()
    env["SHERRY_EVAL_MAX_ITERATIONS"] = MAX_ITERATIONS
    env["SHERRY_FORCE_EXPERIENCE_EXTRACTION"] = "1"
    env["PYTHONPATH"] = str(SHERRY_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        WORKER_MODULE,
        "--session-id",
        session_id,
        "--repo",
        str(repo),
        "--results-dir",
        str(run_dir / "sandbox"),
        "--prompt-file",
        str(prompt_file),
        "--output",
        str(result_file),
    ]
    with log_file.open("wb") as log:
        proc = subprocess.Popen(
            command,
            cwd=str(SHERRY_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, AttributeError):
                proc.kill()
            try:
                proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        if timed_out and result_file.is_file():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                drive = payload.setdefault("drive", {})
                drive["timeout"] = True
                drive["error"] = f"agent worker exceeded {int(TIMEOUT_S)}s wall clock"
                result_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _changed_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and path not in paths:
                paths.append(path)
    return paths


def _test_edits(paths: list[str]) -> list[str]:
    markers = {"conftest.py", "pytest.ini", "tox.ini"}
    hits: list[str] = []
    for path in paths:
        name = Path(path).name
        if path.startswith(("tests/", "test/")) or name in markers or name.startswith("test_"):
            hits.append(path)
    return hits


def _grade(task: str, patch_file: Path, out_dir: Path, log_file: Path) -> dict[str, Any] | None:
    command = [
        sys.executable,
        str(LOCAL_ROOT / "deepswe_local.py"),
        "--tasks",
        str(TASKS_DIR),
        "grade",
        task,
        "--patch",
        str(patch_file),
        "--out-dir",
        str(out_dir),
        "--no-setup",
        "--offline",
    ]
    with log_file.open("wb") as log:
        completed = subprocess.run(
            command,
            cwd=str(LOCAL_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT_S + 120.0,
        )
    print(f"[local-l1] {task}: grade exit={completed.returncode}", flush=True)
    reward_path = out_dir / task / "reward.json"
    if not reward_path.is_file():
        reward_path = out_dir / "reward.json"
    if reward_path.is_file():
        return json.loads(reward_path.read_text(encoding="utf-8"))
    return None


def _run_one(task: str, run_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    run_dir = run_root / task
    run_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "id": task,
        "mode": "local-agent",
        "resolved": False,
        "reward": None,
        "failure_kind": "",
        "invalid_run": False,
        "duration_s": 0.0,
        "tool_calls": 0,
        "tokens": {},
        "experience_extraction": {},
        "changed_paths": [],
        "test_edits": [],
        "error": "",
    }
    try:
        base = _prepare_base(task)
        base_commit = _git(base, "rev-parse", "HEAD").stdout.strip()
        agent_repo = run_dir / "agent"
        if agent_repo.exists():
            shutil.rmtree(agent_repo)
        # shutil.copytree and xattr-preserving cp fail on an esbuild symlink
        # loop in node_modules; copying without xattrs preserves the toolchain.
        cp = subprocess.run(
            ["cp", "-a", "--no-preserve=xattr", f"{base}/.", str(agent_repo)],
            text=True,
            capture_output=True,
            timeout=600.0,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"cp -a failed: {cp.stderr[-800:]}")

        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(_build_prompt(task, agent_repo, base_commit), encoding="utf-8")
        result_file = run_dir / "agent-result.json"
        session_id = f"evals-deepswe-local-{task}-{uuid.uuid4().hex[:6]}"
        print(f"[local-l1] {task}: agent start session={session_id}", flush=True)
        _run_worker(
            session_id=session_id,
            repo=agent_repo,
            run_dir=run_dir,
            prompt_file=prompt_file,
            result_file=result_file,
            log_file=run_dir / "agent.log",
        )
        if not result_file.is_file():
            raise RuntimeError("agent worker produced no result file")
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        drive = payload.get("drive") or {}
        result["tool_calls"] = int(drive.get("tool_calls") or 0)
        result["tokens"] = payload.get("tokens") or {}
        result["experience_extraction"] = payload.get("experience_extraction") or {}
        result["orchestration_gate"] = payload.get("orchestration_gate") or {}
        result["orchestration_compliant"] = bool(
            (result["orchestration_gate"].get("todowrite") or 0) > 0
            and (result["orchestration_gate"].get("taskflow_flow") or 0) > 0
        )
        result["error"] = str(drive.get("error") or "")
        if drive.get("timeout"):
            result["failure_kind"] = "timeout"

        add = _git(agent_repo, "add", "-A")
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {add.stderr[-800:]}")
        diff = _git(agent_repo, "diff", "--binary", "--cached", base_commit, timeout=300.0)
        if diff.returncode != 0:
            raise RuntimeError(f"git diff failed: {diff.stderr[-800:]}")
        patch = diff.stdout
        patch_file = run_dir / "model.patch"
        patch_file.write_text(patch, encoding="utf-8", newline="\n")
        result["changed_paths"] = _changed_paths(patch)
        result["test_edits"] = _test_edits(result["changed_paths"])
        result["patch_path"] = str(patch_file)

        if result["test_edits"]:
            result["failure_kind"] = "test_edit"
            result["error"] = (result["error"] + f" test edits: {result['test_edits']}").strip()
        else:
            reward = _grade(task, patch_file, run_dir / "verifier", run_dir / "grade.log")
            result["reward"] = reward
            if reward is not None:
                value = reward.get("reward")
                result["resolved"] = bool(value is not None and float(value) >= 1.0)
            elif not result["failure_kind"]:
                result["failure_kind"] = "harness"
                result["error"] = (result["error"] + " verifier produced no reward").strip()
    except Exception as exc:  # noqa: BLE001 - one bad task must not stop the rest
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["duration_s"] = round(time.monotonic() - started, 1)
    print(
        f"[local-l1] {task}: resolved={result['resolved']} "
        f"reward={result['reward']} tool_calls={result['tool_calls']} "
        f"orchestration={result.get('orchestration_compliant')} "
        f"duration={result['duration_s']}s",
        flush=True,
    )
    return result


def main() -> int:
    tasks = sys.argv[1:]
    if not tasks:
        raise SystemExit("usage: run_l1_local_agent.py <task-id> [task-id ...]")
    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_root = LOCAL_ROOT / "results" / f"local-agent-{run_id}"
    run_root.mkdir(parents=True, exist_ok=True)
    results = [_run_one(task, run_root) for task in tasks]
    report = {
        "run_id": run_id,
        "mode": "local-agent (Docker-free, DeepSeek via .models)",
        "model": "deepseek-flash",
        "tasks": results,
        "summary": {
            "total": len(results),
            "resolved": sum(1 for r in results if r["resolved"]),
            "provider_failures": sum(1 for r in results if r.get("failure_kind") == "provider"),
        },
    }
    (run_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# Local agent rerun {run_id}",
        "",
        "| task | resolved | reward | F2P | P2P | tool_calls | orchestration | duration |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        reward = r.get("reward") or {}
        lines.append(
            f"| {r['id']} | {r['resolved']} | {reward.get('reward')} | "
            f"{reward.get('f2p_passed')}/{reward.get('f2p_total')} | "
            f"{reward.get('p2p_passed')}/{reward.get('p2p_total')} | "
            f"{r['tool_calls']} | {r.get('orchestration_compliant')} | {r['duration_s']}s |"
        )
    (run_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[local-l1] report: {run_root / 'report.json'}", flush=True)
    print(f"[local-l1] summary: {run_root / 'summary.md'}", flush=True)
    return 0 if all(r["resolved"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
