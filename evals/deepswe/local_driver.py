"""Docker-free DeepSWE rerun: drive the product agent on the host, grade locally.

Runs only the task ids passed on the command line. Each task gets a fresh copy
of the prepared local checkout, a separate worker process (hard timeout), and a
clean local verifier run through ``deepswe_local.py grade``.

This is the repo home of the one-shot ``run_l1_local_agent.py`` used on
2026-11-27; that copy now forwards here. Three measured defects are encoded as
rules, because the first run reported all three as ordinary model results:

* **stale verifier artifacts** — every ``*.xml`` / ``*.json`` artifact the
  verifier produced must carry a timestamp from THIS run. A suite that cannot
  build (no network: cargo ``rc=102``) leaves the previous run's junit file in
  place, the task script copies it anyway, and the grader turns it into a fake
  score (measured: five identical XMLs with a two-day-old timestamp, reported
  as ``F2P 104/104, P2P 0/250``).
* **cancelled / late-error turns** — a cancelled turn ends with a ``stopped``
  frame, never a verdict; it is an INVALID run, not a failure of the model
  (measured: pest cancelled at 981.8 s and graded as a normal miss).
* **unfinished plans** — this driver is single-turn (no auto-turn / plan
  continuation), so a plan still open at the end means the task never got the
  attempts the container harness gives it; that is ``flow_incomplete``, invalid.

The sandbox policy is explicit: a headless host run has no operator to approve
a per-command sandbox, so a subagent's ``terminal`` is refused outright under
``auto`` with no OS sandbox backend (measured 2026-11-27). The driver sets
``SANDBOX_POLICY=off`` unless ``SHERRY_LOCAL_EVAL_SANDBOX_POLICY`` says
otherwise, and only picks ``auto`` when ``bwrap`` exists.

Environment:
  DEEPSWE_LOCAL_ROOT            prepared checkouts + ``deepswe_local.py``
                                (default ``~/Desktop/project/deepswe-local``)
  DEEPSWE_TASKS_DIR             task definitions (default ``<root>/../deep-swe/tasks``)
  SHERRY_LOCAL_EVAL_SANDBOX_POLICY  force the worker's sandbox policy
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SHERRY_ROOT = Path(__file__).resolve().parents[2]
WORKER_MODULE = "evals.deepswe.local_agent_worker"
TIMEOUT_S = 3600.0
MAX_ITERATIONS = "180"

#: Artifact timestamps are compared against the grade start with this slack (a
#: filesystem clock can trail the wall clock by a second or two).
_ARTIFACT_SLACK_S = 120.0

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


def local_root() -> Path:
    """The prepared-checkout root (``deepswe-local``), overridable by env."""
    configured = os.environ.get("DEEPSWE_LOCAL_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "Desktop" / "project" / "deepswe-local"


def tasks_dir() -> Path:
    """The DeepSWE task definitions (``instruction.md`` + tests), overridable by env."""
    configured = os.environ.get("DEEPSWE_TASKS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return local_root().parent / "deep-swe" / "tasks"


def sandbox_policy() -> str:
    """The worker's ``SANDBOX_POLICY`` for a headless host run.

    ``auto`` refuses every subagent exec when no OS sandbox backend exists
    ("no operator approval path"), which silently removes the ability to build
    or test from a delegated child. An explicit env value wins; otherwise
    ``auto`` only when bwrap is actually installed.
    """
    configured = os.environ.get("SHERRY_LOCAL_EVAL_SANDBOX_POLICY")
    if configured:
        return configured
    return "auto" if shutil.which("bwrap") else "off"


def compliance_from_tool_names(tool_names: dict[str, int]) -> bool:
    """The container report's rule: the agent CALLED both planning tools.

    The gate counters are a separate, weaker signal (they only say whether the
    gate would have blocked); the round-1 driver read non-existent keys
    (``todowrite`` / ``taskflow_flow``) off the gate bucket and answered False
    for every task regardless of what ran.
    """
    return bool(tool_names.get("todowrite")) and bool(tool_names.get("taskflow_flow"))


def _artifact_timestamp(path: Path) -> float | None:
    """The artifact's own creation time, when it records one.

    junit: ``<testsuites timestamp="…">``; ctrf: ``results.summary.start/stop``
    (epoch ms or s). Formats without a timestamp (vitest junit) answer ``None``
    — the caller treats that as "unknown", never as stale.
    """
    try:
        if path.suffix == ".xml":
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
            match = re.search(r'timestamp="([0-9T:.\-+]+)"', head)
            if not match:
                return None
            return datetime.fromisoformat(match.group(1)).timestamp()
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            results = payload.get("results")
            summary = results.get("summary") if isinstance(results, dict) else None
            for source in (summary, payload):
                if not isinstance(source, dict):
                    continue
                for key in ("stop", "start"):
                    value = source.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    if value <= 0:
                        continue
                    return float(value) / 1000.0 if value > 1e11 else float(value)
    except (OSError, ValueError):
        return None
    return None


def stale_verifier_artifacts(out_dir: Path, started_at: float) -> list[str]:
    """Verifier artifacts whose OWN timestamp predates this grade run.

    A non-empty result means the suite did not run: the task script copied a
    previous run's report (measured 2026-11-27 — cargo had no network, the old
    junit survived, and all five copies carried a two-day-old timestamp).
    """
    stale: list[str] = []
    if not out_dir.is_dir():
        return stale
    cutoff = started_at - _ARTIFACT_SLACK_S
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.name == "reward.json":
            continue
        if path.suffix not in {".xml", ".json"}:
            continue
        stamp = _artifact_timestamp(path)
        if stamp is not None and stamp < cutoff:
            stale.append(str(path.relative_to(out_dir)))
    return stale


def _git(repo: Path, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _prepare_base(task: str) -> Path:
    base = local_root() / "work" / task / "base"
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


def _build_prompt(task: str, repo: Path, base_commit: str) -> str:
    instruction_file = tasks_dir() / task / "instruction.md"
    if not instruction_file.is_file():
        instruction_file = local_root() / "tasks-py-node" / task / "instruction.md"
    instruction = instruction_file.read_text(encoding="utf-8")
    instruction = instruction.replace("/app", str(repo))
    return f"""{instruction}

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
    env["SANDBOX_POLICY"] = sandbox_policy()
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


def drive_outcome(drive: dict[str, Any]) -> str:
    """``""`` / ``timeout`` / ``cancelled`` / ``provider`` / ``harness``.

    Uses the same rules as the container suite (``coding_capability._failure_kind``)
    so the two paths cannot disagree about what a cancelled turn is.
    """
    from evals.coding_capability.suite import _failure_kind

    return _failure_kind(False, drive)


def _apply_invalid_rules(result: dict[str, Any]) -> None:
    """Mark the task invalid when the run produced no verdict about the agent.

    Three rules (measured 2026-11-27): a cancelled turn, a harness failure and a
    stale verifier artifact are already named in ``failure_kind``; an unfinished
    plan is named here — this driver is single-turn, so an open plan means the
    task never got the continuation attempts the container harness gives it. A
    RESOLVED task keeps its verdict regardless of bookkeeping left behind.
    """
    from evals.deepswe.suite import _INVALID_FAILURE_KINDS

    if result.get("plan_open") and not result.get("failure_kind") and not result.get("resolved"):
        result["failure_kind"] = "flow_incomplete"
        result["error"] = (
            str(result.get("error") or "")
            + f" plan still open after one turn: {result['plan_open'][:3]}"
        ).strip()
    invalid_kinds = _INVALID_FAILURE_KINDS | {"stale_verifier", "flow_incomplete"}
    if result.get("failure_kind") in invalid_kinds:
        result["invalid_run"] = True
        result["orchestration_compliant"] = None


def _grade(task: str, patch_file: Path, out_dir: Path, log_file: Path) -> dict[str, Any] | None:
    command = [
        sys.executable,
        str(local_root() / "deepswe_local.py"),
        "--tasks",
        str(tasks_dir()),
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
            cwd=str(local_root()),
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


def _open_plan_items(sandbox_results_dir: Path, session_id: str) -> list[str]:
    """The task's open todos + non-terminal flows (session-scoped registry)."""
    from evals.deepswe import suite as deepswe

    return deepswe._open_plan_items(sandbox_results_dir, session_id)


def _run_one(task: str, run_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    run_dir = run_root / task
    run_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "id": task,
        "mode": "local-agent",
        "drive_mode": "single_turn",
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
        "plan_open": [],
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
        tool_names = drive.get("tool_names") or {}
        result["tool_calls"] = int(drive.get("tool_calls") or 0)
        result["tool_names"] = tool_names
        result["tokens"] = payload.get("tokens") or {}
        result["experience_extraction"] = payload.get("experience_extraction") or {}
        result["orchestration_gate"] = payload.get("orchestration_gate") or {}
        # The container report's rule: compliance = the agent CALLED both
        # planning tools. The gate bucket is informational (it says whether the
        # gate would have blocked, and it never keys a session before R1).
        result["orchestration_compliant"] = compliance_from_tool_names(tool_names)
        result["error"] = str(drive.get("error") or "")
        kind = drive_outcome(drive)
        if kind:
            result["failure_kind"] = kind
        result["plan_open"] = _open_plan_items(run_dir / "sandbox", session_id)[:10]

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
            verifier_started = time.time()
            reward = _grade(task, patch_file, run_dir / "verifier", run_dir / "grade.log")
            result["reward"] = reward
            stale = stale_verifier_artifacts(run_dir / "verifier", verifier_started)
            if stale:
                result["failure_kind"] = "stale_verifier"
                result["stale_artifacts"] = stale[:10]
                result["error"] = (
                    result["error"] + f" verifier reused stale artifacts: {stale[:3]}"
                ).strip()
            elif reward is not None:
                value = reward.get("reward")
                result["resolved"] = bool(value is not None and float(value) >= 1.0)
            elif not result["failure_kind"]:
                result["failure_kind"] = "harness"
                result["error"] = (result["error"] + " verifier produced no reward").strip()

        _apply_invalid_rules(result)
    except Exception as exc:  # noqa: BLE001 - one bad task must not stop the rest
        result["failure_kind"] = "harness"
        result["invalid_run"] = True
        result["orchestration_compliant"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["duration_s"] = round(time.monotonic() - started, 1)
    print(
        f"[local-l1] {task}: resolved={result['resolved']} "
        f"reward={result['reward']} tool_calls={result['tool_calls']} "
        f"orchestration={result.get('orchestration_compliant')} "
        f"invalid={result['invalid_run']} kind={result['failure_kind'] or '-'} "
        f"duration={result['duration_s']}s",
        flush=True,
    )
    return result


def main() -> int:
    """Run each task id from argv through worker + verifier, then write the report."""
    tasks = sys.argv[1:]
    if not tasks:
        raise SystemExit("usage: python -m evals.deepswe.local_driver <task-id> [task-id ...]")
    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_root = local_root() / "results" / f"local-agent-{run_id}"
    run_root.mkdir(parents=True, exist_ok=True)
    if sandbox_policy() != "auto":
        print(
            "[local-l1] WARNING: SANDBOX_POLICY="
            f"{sandbox_policy()} (no bwrap found) — the agent runs unsandboxed; "
            "subagents keep their exec tools",
            flush=True,
        )
    results = [_run_one(task, run_root) for task in tasks]
    report = {
        "run_id": run_id,
        "mode": "local-agent (Docker-free, DeepSeek via .models)",
        "drive_mode": "single_turn",
        "tasks": results,
        "summary": {
            "total": len(results),
            "resolved": sum(1 for r in results if r["resolved"]),
            "invalid": sum(1 for r in results if r["invalid_run"]),
            "provider_failures": sum(1 for r in results if r.get("failure_kind") == "provider"),
        },
    }
    (run_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# Local agent rerun {run_id}",
        "",
        "Single-turn drive: `flow_incomplete` / `cancelled` / `stale_verifier` are INVALID,",
        "not model results.",
        "",
        "| task | resolved | reward | F2P | P2P | tool_calls | orchestration | invalid | duration |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        reward = r.get("reward") or {}
        lines.append(
            f"| {r['id']} | {r['resolved']} | {reward.get('reward')} | "
            f"{reward.get('f2p_passed')}/{reward.get('f2p_total')} | "
            f"{reward.get('p2p_passed')}/{reward.get('p2p_total')} | "
            f"{r['tool_calls']} | {r.get('orchestration_compliant')} | "
            f"{r['invalid_run']} | {r['duration_s']}s |"
        )
    (run_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[local-l1] report: {run_root / 'report.json'}", flush=True)
    print(f"[local-l1] summary: {run_root / 'summary.md'}", flush=True)
    return 0 if all(r["resolved"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
