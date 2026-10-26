"""SWE-bench_Lite spike suite — the external benchmark, actually run.

Same harness as ``evals/coding_capability`` (the real main agent through
``server.service.messages.async_generate``, the neutral persona, the 编码助手
preset's agent block, auto-granted approvals, the isolated workspace), fed with
**external** instances instead of self-designed tasks: SWE-bench_Lite problems,
each judged by its own ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` sets after the
harness applies the instance's test patch.

What the report claims, and what it does not:

- it claims ``N of M instances resolved`` on the selected slice — the
  benchmark's own metric, on the benchmark's own verdict;
- it does NOT claim a leaderboard position: the agent is not SWE-agent (no
  scaffold from the paper), the interpreter/env differs from the official
  images, and the slice is three instances, not 300. The small-sample honesty
  discipline applies unchanged: report ``N of M``, never a capability percentage.

Usage::

    uv run python evals/evals.py swebench
    uv run python evals/swebench/suite.py [instance_id …] [--update-baseline]
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.coding_capability import suite as harness  # noqa: E402
from evals.swebench import instances  # noqa: E402
from evals.swebench.instances import Instance  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")

#: The agent's instructions. The problem statement is passed through verbatim;
#: this block only states the harness contract (where the environment is, what
#: not to touch). SWE-bench's own scaffolded agents get an equivalent hint from
#: the image, where the environment python IS the default `python`.
_TASK_TEMPLATE = """{problem}

---
You are working inside a checkout of `{repo}` at the commit the issue was
reported against; that checkout is the session's project directory. Fix the
issue in the library source.

- The project's environment interpreter is the `python` on PATH (`python -m
  pytest <path>` runs a test file). Use it if you want to run tests.
- Do not modify anything under a test directory, and do not run `git commit`.
"""

#: Appended to the neutral working rules for the swebench arm ONLY (the
#: coding-capability baseline keeps the untouched neutral rules so its pinned
#: threshold stays comparable). It records this harness's environment facts —
#: probed, not assumed: terminal egress reaches the public internet directly,
#: the search tool has no backend, and the checkout is a shallow clone — plus
#: the pointer to the contract-first workflow. The persona digest in the report
#: covers this file, so the configuration of any run stays auditable.
_SWEBENCH_AGENTS_APPENDIX = """

## Environment and workflow notes (this evaluation arm)

- Network: the `terminal` tool reaches the public internet directly (a
  `curl -m 5` is a safe probe). `web_search` has no backend configured here.
- The checkout is a shallow clone at a single commit: `git log` shows no
  future commits; an upstream fix can only be found over the network.
- Bug-fix tasks: synthesize the behavior contract before patching — see the
  `bugfix-contract` skill. When the network is reachable, check the project's
  issue tracker / upstream history for an existing fix before inventing one.
"""


def _append_agents_appendix(workspace: Path) -> None:
    """Append the swebench arm's environment notes to the seeded rules file."""
    rules = workspace / "AGENTS.md"
    rules.write_text(
        rules.read_text(encoding="utf-8") + _SWEBENCH_AGENTS_APPENDIX, encoding="utf-8"
    )


def _probe_web() -> dict[str, Any]:
    """Probe this run's network reachability (bounded, best-effort)."""
    probe = {
        "probe_host": "api.github.com",
        "terminal_egress": False,
        "http_code": None,
        "search_backend": None,
    }
    # One retry: the host's egress can hiccup transiently.
    for _attempt in range(2):
        try:
            completed = subprocess.run(
                [
                    "curl",
                    "-m",
                    "6",
                    "-sS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "https://api.github.com/zen",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            code = (completed.stdout or "").strip()
            probe["http_code"] = code or None
            probe["terminal_egress"] = completed.returncode == 0 and code.startswith("2")
            if probe["terminal_egress"]:
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
    if os.getenv("TAVILY_API_KEY", "").strip():
        probe["search_backend"] = "tavily"
    elif os.getenv("SEARXNG_URL", "").strip():
        probe["search_backend"] = "searxng"
    return probe


def main() -> None:
    """Run the spike, re-executing in a clean interpreter when needed."""
    if os.environ.get("SHERRY_EVAL_CODING_CHILD") != "1":
        env = {**os.environ, "SHERRY_EVAL_CODING_CHILD": "1"}
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(REPO_ROOT),
            env=env,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)
        return

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    # ``--hard`` runs the HARD slice (multi-test regressions, 100+ PASS_TO_PASS
    # tests, one repo each); without it the small sympy spike is the default.
    default_slice = instances.HARD_INSTANCES if "--hard" in flags else instances.SPIKE_INSTANCES
    wanted = tuple(args) or default_slice
    raise SystemExit(
        _run(
            wanted,
            update_baseline="--update-baseline" in flags,
            skip_self_check="--skip-self-check" in flags,
            hard="--hard" in flags or not args,
            keep_instances="--keep-instances" in flags,
        )
    )


def _run(
    instance_ids: tuple[str, ...],
    *,
    update_baseline: bool,
    skip_self_check: bool,
    hard: bool = False,
    keep_instances: bool = False,
) -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "swebench" / run_id
    results_dir.mkdir(parents=True)
    print(
        f"[evals] swebench run {run_id} | slice={'hard' if hard else 'spike'} | "
        f"instances={','.join(instance_ids)}",
        flush=True,
    )
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = harness._session_names(live_workspace)

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    # Bind the REAL build_main_tools before EvalSandbox patches it for child
    # agents: agent.core imported it at module load, so this import must happen
    # before sandbox.apply() or init() would see only the child-safe subset.
    import agent.core  # noqa: F401

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] swebench SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3

    harness._seed_persona(eval_workspace)
    _append_agents_appendix(eval_workspace)
    harness.init_agent()
    main_tools = harness._main_tool_names()
    missing = sorted(
        {"read_file", "write_file", "patch_file", "search_files", "terminal"} - main_tools
    )
    if missing:
        print(
            f"[evals] HARNESS FAILURE: the main agent is missing coding tools: {missing}",
            flush=True,
        )
        return 2

    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"

    started = time.monotonic()
    try:
        # One cache for the whole suite (not per run): the dataset is stable and
        # a fresh run must not depend on the network to start.
        cache = results_dir.parent / "swebench_lite.json"
        if not cache.is_file() and Path("/tmp/swebench_lite.json").is_file():
            cache.write_text(
                Path("/tmp/swebench_lite.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
        rows = instances.fetch_dataset(cache)
        selected = instances.select(rows, instance_ids)
        print(
            f"[evals] dataset: {len(rows)} SWE-bench_Lite rows; running {len(selected)}", flush=True
        )
        prepared = _prepare_all(selected, results_dir)
        self_check = {} if skip_self_check else _self_check(prepared)
        if any(not ok for ok, _ in self_check.values()):
            for instance_id, (ok, output) in self_check.items():
                if not ok:
                    print(
                        f"[evals] HARNESS FAILURE: {instance_id} is not resolved by its GOLD "
                        f"patch — the instance is unusable:\n{output}",
                        flush=True,
                    )
            return 2
        results = asyncio.run(_run_instances(prepared, results_dir, hard=hard))
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    if not keep_instances:
        # A checkout + venv per instance is hundreds of megabytes INSIDE the
        # repository: left behind, it slows every repo-wide scan (ripgrep has a
        # wall-clock budget) and the unit suite's search test starts failing for
        # a reason that has nothing to do with the code. They are derived
        # artifacts — re-materialised in minutes when a run needs them.
        shutil.rmtree(results_dir / "instances", ignore_errors=True)

    token_input = sum(int(r.get("tokens", {}).get("input", 0)) for r in results)
    token_output = sum(int(r.get("tokens", {}).get("output", 0)) for r in results)
    report = {
        "run_id": run_id,
        "suite": "swebench",
        "kind": f"SWE-bench_Lite slice ({len(selected)} of 300 instances) — external benchmark",
        "results_dir": str(results_dir),
        "benchmark": {
            "dataset": "princeton-nlp/SWE-bench_Lite",
            "split": "test",
            "harness": (
                "host harness: pinned base commit + per-instance venv + the instance's own "
                "FAIL_TO_PASS / PASS_TO_PASS verdict; NOT the official docker harness"
            ),
            "instance_python": instances.INSTANCE_PYTHON,
            "slice": "hard" if hard else "spike",
        },
        "model": {**harness._model_info(), "thinking_enabled": False},
        "agent_config": harness._coding_preset_payload(),
        # This run's probed network reality (default-off philosophy: the record
        # says what was reachable; nothing was switched on for it).
        "environment": {"web": _probe_web()},
        "persona": harness._persona_evidence(results[0]["id"] if results else None),
        "isolation": harness._isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "harness_self_check": {k: ok for k, (ok, _) in self_check.items()},
        "aggregate": {
            "instances_total": len(results),
            "resolved": sum(1 for r in results if r["resolved"]),
            "invalid_runs": [r["id"] for r in results if r["invalid_run"]],
            "resolve_rate": round(
                sum(1 for r in results if r["resolved"])
                / max(1, len(results) - sum(1 for r in results if r["invalid_run"])),
                4,
            ),
            "resolve_rate_basis": "valid runs only (provider failures excluded)",
            "tokens": {"input": token_input, "output": token_output},
            "duration_s": round(time.monotonic() - started, 1),
        },
        "instances": results,
    }
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return _publish(report, results_dir, update_baseline=update_baseline)


# ── preparation, validation, runs ───────────────────────────────────────────


def _prepare_all(selected: list[Instance], results_dir: Path) -> list[Instance]:
    """Clone + install every instance (the slow, model-free half)."""
    work_dir = results_dir / "instances"
    work_dir.mkdir(parents=True, exist_ok=True)
    prepared = []
    for instance in selected:
        print(
            f"[evals] materialising {instance.instance_id} "
            f"({instance.repo}@{instance.base_commit[:10]})…",
            flush=True,
        )
        started = time.monotonic()
        prepared.append(instances.materialize(instance, work_dir))
        print(
            f"[evals] materialised {instance.instance_id} in {time.monotonic() - started:.1f}s",
            flush=True,
        )
    return prepared


def _self_check(prepared: list[Instance]) -> dict[str, tuple[bool, str]]:
    """The benchmark's own validation, reproduced: the gold patch must resolve it."""
    print("[evals] validating each instance against its GOLD patch…", flush=True)
    checks: dict[str, tuple[bool, str]] = {}
    for instance in prepared:
        fail_to_pass, pass_to_pass = instance.node_ids()
        started = time.monotonic()
        try:
            if not instances.apply_patch(instance, instance.gold_patch):
                checks[instance.instance_id] = (False, "the gold patch does not apply")
                continue
            if not instances.apply_patch(instance, instance.test_patch):
                checks[instance.instance_id] = (False, "the test patch does not apply")
                continue
            ok_f2p, out_f2p, _ = instances.run_tests(instance, fail_to_pass)
            ok_p2p, out_p2p, _ = instances.run_tests(instance, pass_to_pass)
            checks[instance.instance_id] = (
                ok_f2p and ok_p2p,
                f"FAIL_TO_PASS ok={ok_f2p}\n{out_f2p}\nPASS_TO_PASS ok={ok_p2p}\n{out_p2p}",
            )
        finally:
            instances.revert(instance)
            ok = checks.get(instance.instance_id, (False, ""))[0]
            print(
                f"[evals] gold check {instance.instance_id}: "
                f"{'PASS' if ok else 'FAIL'} ({time.monotonic() - started:.1f}s)",
                flush=True,
            )
    return checks


async def _run_instances(
    prepared: list[Instance], results_dir: Path, *, hard: bool = False
) -> list[dict[str, Any]]:
    results = []
    for instance in prepared:
        timeout_s = _TURN_TIMEOUT_HARD_S if hard else _TURN_TIMEOUT_S
        print(
            f"[evals] starting {instance.instance_id} (turn_timeout={timeout_s:.0f}s)…",
            flush=True,
        )
        result = await _run_instance(instance, results_dir, hard=hard)
        results.append(result)
        print(
            f"[evals] {instance.instance_id}: resolved={result['resolved']} "
            f"duration={result['duration_s']}s tool_calls={result['tool_calls']} "
            f"tokens={result['tokens']}",
            flush=True,
        )
    return results


#: A hard instance gets a longer turn budget: the fix spans a real codebase and
#: the model has to find its way around first.
_TURN_TIMEOUT_S = 1200.0
_TURN_TIMEOUT_HARD_S = 2400.0


async def _run_instance(
    instance: Instance, results_dir: Path, *, hard: bool = False
) -> dict[str, Any]:
    fail_to_pass, pass_to_pass = instance.node_ids()
    prompt = _TASK_TEMPLATE.format(problem=instance.problem_statement.strip(), repo=instance.repo)
    session_id = f"evals-swe-{instance.instance_id}-{uuid.uuid4().hex[:6]}"
    await harness._bind_session(session_id, instance.repo_dir)

    # The instance's interpreter has to be what the agent's own commands hit:
    # the official images make the environment python the default one, so this
    # is the closest equivalent to that (restored right after the turn).
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{instance.python_exe.parent}{os.pathsep}{original_path}"
    # The agent runs the repo's tests too, so the pretend version has to be in
    # ITS environment as well (a shallow clone cannot report the real version).
    previous_scm = os.environ.get("SETUPTOOLS_SCM_PRETEND_VERSION")
    if instance.version:
        os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"] = instance.version
    started = time.monotonic()
    try:
        timeout_s = _TURN_TIMEOUT_HARD_S if hard else _TURN_TIMEOUT_S
        drive = await asyncio.wait_for(harness._drive(session_id, prompt), timeout=timeout_s)
    except TimeoutError:
        drive = {
            "tool_calls": 0,
            "text": "",
            "resumes": 0,
            "interrupted": 0,
            "timeout": True,
            "error": "",
        }
    finally:
        os.environ["PATH"] = original_path
        if previous_scm is None:
            os.environ.pop("SETUPTOOLS_SCM_PRETEND_VERSION", None)
        else:
            os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"] = previous_scm
    duration_s = round(time.monotonic() - started, 1)

    agent_diff = instances.diff(instance)
    changed = instances.changed_paths(instance)
    (results_dir / f"diff-{instance.instance_id}.patch").write_text(agent_diff, encoding="utf-8")

    ok_f2p = ok_p2p = False
    f2p_tests: list[dict[str, Any]] = []
    p2p_tests: list[dict[str, Any]] = []
    test_patch_applied = instances.apply_patch(instance, instance.test_patch)
    if test_patch_applied:
        ok_f2p, out_f2p, f2p_tests = instances.run_tests(instance, fail_to_pass)
        ok_p2p, out_p2p, p2p_tests = instances.run_tests(instance, pass_to_pass)
        resolved = ok_f2p and ok_p2p
        detail = f"FAIL_TO_PASS ok={ok_f2p}\n{out_f2p}\nPASS_TO_PASS ok={ok_p2p}\n{out_p2p}"
    else:
        resolved = False
        detail = "the test patch does not apply — the agent changed test files"
    instances.revert(instance)

    passed = bool(resolved)
    return {
        "id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "resolved": passed,
        "failure_kind": harness._failure_kind(passed, drive),
        "invalid_run": (not passed) and harness._failure_kind(passed, drive) == "provider",
        "fail_to_pass_ok": bool(test_patch_applied and ok_f2p),
        "pass_to_pass_ok": bool(test_patch_applied and ok_p2p),
        "test_patch_applied": test_patch_applied,
        "judge_detail": detail[-3000:],
        # Structured per-test outcomes (observability — the verdict above is the
        # exit code's; this field makes near-misses like "same contract point,
        # N asserts" visible instead of a truncated text blob).
        "judge_tests": (
            {"fail_to_pass": f2p_tests, "pass_to_pass": p2p_tests} if test_patch_applied else None
        ),
        "duration_s": duration_s,
        "tool_calls": drive["tool_calls"],
        "resumes": drive["resumes"],
        "timeout": drive["timeout"],
        "error": drive["error"],
        "changed_paths": changed,
        "reply": drive["text"][:2000],
        "tokens": await harness._session_tokens(session_id),
    }


def _publish(report: dict[str, Any], results_dir: Path, *, update_baseline: bool) -> int:
    aggregate = report["aggregate"]
    threshold = 0.0
    baseline: dict[str, Any] | None = None
    if BASELINE_PATH.is_file():
        loaded = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            baseline = loaded
            threshold = float(baseline.get("threshold_resolve_rate") or 0.0)
            print(f"[evals] baseline: {harness._delta(baseline, aggregate)}", flush=True)
    print(f"\n[evals] swebench aggregate: {aggregate}", flush=True)
    token_totals = aggregate.get("tokens", {})
    print(
        "[evals] tokens: input={} output={} total={}".format(
            token_totals.get("input", 0),
            token_totals.get("output", 0),
            token_totals.get("input", 0) + token_totals.get("output", 0),
        ),
        flush=True,
    )
    print(
        f"[evals] persona check: {report['persona']['forbidden_markers_found'] or 'clean'}",
        flush=True,
    )
    print(f"[evals] report: {results_dir}", flush=True)

    if update_baseline or baseline is None:
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "captured_run": report["run_id"],
                    "threshold_resolve_rate": 0.0,
                    "model": report["model"],
                    "aggregate": aggregate,
                    "instances": [r["id"] for r in report["instances"]],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[evals] baseline updated: {BASELINE_PATH}", flush=True)

    invalid = aggregate["invalid_runs"]
    if invalid:
        print(f"[evals] INVALID RUNS (excluded from the resolve rate): {invalid}", flush=True)
    harness_errors = [r["id"] for r in report["instances"] if r["failure_kind"] == "harness"]
    if harness_errors:
        print(f"[evals] FAILED: harness error in instance(s): {harness_errors}", flush=True)
        return 1
    if aggregate["resolve_rate"] < threshold:
        print(
            f"[evals] FAILED: resolve rate {aggregate['resolve_rate']} < threshold {threshold}",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    main()
