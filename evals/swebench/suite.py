"""SWE-bench_Lite spike suite — the external benchmark, actually run.

Same harness as ``evals/coding_capability`` (the real main agent through
``server.service.messages.async_generate``, the neutral persona — or, with
``--persona coding``, the shipped 编码助手 preset's system prompt — that preset's
agent block, auto-granted approvals, the isolated workspace), fed with
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
from evals.swebench import teardown  # noqa: E402
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

- The project's environment interpreter is the `python` on PATH. The
  Evaluation tests section below gives the exact command for this instance.
- Before you stop: run the tests that cover every source file you changed —
  the neighboring test module, plus your reproduction of the reported issue.
  A patch that fixes the reported case but breaks a neighboring test is not
  complete; investigate every failure you introduced.
- Do not repeat an identical command after its output has told you what you
  need. Once a candidate patch exists, spend the remaining budget verifying
  and refining it, not re-probing the environment.
- The target tests are in the checkout. Before editing, read the
  FAIL_TO_PASS and PASS_TO_PASS test bodies and extract their exact input →
  expected-output cases; derive the predicate from those cases, not only from
  the issue prose. Run both lists after every source edit. If one fails, the
  next change must be in the library source (or a read of the failing test);
  do not keep rewriting a reproduction script.
{target_tests}
- Do not modify anything under a test directory, and do not run `git commit`.
"""

#: The test patch is exposed in the PROMPT (not applied to the checkout before
#: the run): the agent sees the exact expectations the grader will use, but it
#: still has to implement the fix in the library source. The official test patch
#: is applied at judging time exactly as before. A very large patch is clipped;
#: the F2P names stay visible even when the diff does not.
_TEST_EXPOSURE_MAX_CHARS = 12000


def _target_tests_block(
    instance: Instance,
    fail_to_pass: list[str],
    pass_to_pass: list[str] | None = None,
    *,
    preapplied: bool = False,
) -> str:
    """Render the read-only evaluation-tests block for the task prompt."""
    patch = instance.test_patch.strip()
    if not patch:
        return ""
    if len(patch) > _TEST_EXPOSURE_MAX_CHARS:
        patch = (
            patch[:_TEST_EXPOSURE_MAX_CHARS]
            + "\n...[test patch truncated; read the test files in the checkout]"
        )
    names = "\n".join(f"- {name}" for name in fail_to_pass[:20]) or "- (none listed)"
    keep_names = "\n".join(f"- {name}" for name in (pass_to_pass or [])[:20]) or "- (none listed)"
    if preapplied:
        intro = (
            "The evaluation test patch is ALREADY APPLIED to this checkout. "
            "Run the FAIL_TO_PASS tests below. Do not modify the test files; "
            "make the library source satisfy them."
        )
    else:
        intro = (
            "The grader will apply this test patch and run the FAIL_TO_PASS tests "
            "below. Do not modify the tests; make the library source satisfy them."
        )
    commands = _suggested_commands(instance, fail_to_pass, pass_to_pass)
    return f"""
## Evaluation tests (read-only)

{intro}

FAIL_TO_PASS:
{names}

PASS_TO_PASS (must keep passing):
{keep_names}

Suggested feedback loop (run after every source edit; a non-zero exit is the
actionable failure — fix the library source and rerun):
{commands}

```diff
{patch}
```
"""


def _suggested_commands(
    instance: Instance,
    fail_to_pass: list[str],
    pass_to_pass: list[str] | None,
) -> str:
    """The exact runner command for both target sets, in this repo's dialect.

    Django test ids are labels (``app.module.Class.test``); a pytest repo uses
    node ids. Running the apps that own PASS_TO_PASS is cheaper than listing
    every id and still covers them, which is what the successful runs did.
    """
    if getattr(instance, "judge_mode", "") == "django":
        exact = " ".join(fail_to_pass[:20])
        apps = sorted(
            {
                label.split(".", 1)[0]
                for label in [*fail_to_pass, *(pass_to_pass or [])]
                if "." in label
            }
        )
        lines = [f"- `python tests/runtests.py {exact}` — the exact FAIL_TO_PASS set."]
        if apps:
            lines.append(
                f"- `python tests/runtests.py {' '.join(apps)}` — the apps that own PASS_TO_PASS."
            )
        return "\n".join(lines)

    exact = " ".join(fail_to_pass[:20])
    files = sorted({node.split("::", 1)[0] for node in [*fail_to_pass, *(pass_to_pass or [])]})
    lines = [f"- `python -m pytest {exact}` — the exact FAIL_TO_PASS set."]
    if files:
        lines.append(f"- `python -m pytest {' '.join(files)}` — the files that own PASS_TO_PASS.")
    return "\n".join(lines)


def _looks_like_test(path: str) -> bool:
    """Heuristic for the dirty-diff gate: a test path or a ``test_*.py`` file."""
    return "tests" in path.split("/") or Path(path).name.startswith("test_")


def _dirty_test_edits(changed: list[str], test_paths: list[str]) -> list[str]:
    """Agent edits to test files, excluding the harness's own test patch.

    A clean diff is part of the product contract: touching a test file (other
    than the harness-owned patch, which is reset before judging) makes the run
    dirty even when the target tests pass.
    """
    harness_owned = set(test_paths)
    return [path for path in changed if path not in harness_owned and _looks_like_test(path)]


#: Appended to the neutral working rules for the swebench arm ONLY (the
#: coding-capability baseline keeps the untouched neutral rules so its pinned
#: threshold stays comparable). It records this harness's environment facts —
#: probed, not assumed: terminal egress reaches the public internet directly,
#: the search tool has no backend, and the checkout is a shallow clone — plus
#: the pointer to the contract-first workflow. The persona digest in the report
#: covers this file, so the configuration of any run stays auditable.
_SWEBENCH_AGENTS_APPENDIX = """

## Environment and workflow notes (this evaluation arm)

- Network: probe with a short `curl` only when it can change the fix; `web_search`
  has no backend configured here.
- The checkout is a shallow clone at a single commit: `git log` shows no
  future commits; an upstream fix can only be found over the network.
- Bug-fix tasks: synthesize the behavior contract before patching — see the
  `bugfix-contract` skill. When the network is reachable, check the project's
  issue tracker / upstream history for an existing fix before inventing one.
"""

_NETWORK_UNAVAILABLE_NOTE = (
    "\n- **Network unavailable this run** (terminal probe: HTTP {code}). "
    "Do NOT keep retrying curl / git / GitHub: inspect the local code and tests, "
    "make the smallest verifiable change, and run the target test.\n"
)


def _append_agents_appendix(workspace: Path, web: dict[str, Any] | None = None) -> None:
    """Append the swebench arm's environment notes to the seeded rules file.

    The network line is dynamic: a run whose probe cannot reach GitHub gets an
    explicit "stop probing" directive, which removes the exact loop that
    exhausted the Sympy instance's call budget.
    """
    rules = workspace / "AGENTS.md"
    appendix = _SWEBENCH_AGENTS_APPENDIX
    if web is not None and not web.get("terminal_egress"):
        code = web.get("http_code") or "unreachable"
        appendix += _NETWORK_UNAVAILABLE_NOTE.format(code=code)
    rules.write_text(
        rules.read_text(encoding="utf-8") + appendix,
        encoding="utf-8",
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

    raw = sys.argv[1:]
    args: list[str] = []
    flags: set[str] = set()
    cache_dir: Path | None = None
    persona = "neutral"
    index = 0
    while index < len(raw):
        item = raw[index]
        if item == "--instance-cache-dir":
            if index + 1 >= len(raw):
                raise SystemExit("--instance-cache-dir requires a directory")
            cache_dir = Path(raw[index + 1]).expanduser().resolve()
            index += 2
            continue
        if item == "--persona":
            if index + 1 >= len(raw):
                raise SystemExit("--persona requires 'neutral' or 'coding'")
            persona = raw[index + 1]
            if persona not in ("neutral", "coding"):
                raise SystemExit(f"unknown --persona {persona!r} (expected 'neutral' or 'coding')")
            index += 2
            continue
        if item.startswith("--"):
            flags.add(item)
        else:
            args.append(item)
        index += 1
    # ``--hard`` runs the HARD slice (multi-test regressions, 100+ PASS_TO_PASS
    # tests); without it the small sympy spike is the default.
    default_slice = instances.HARD_INSTANCES if "--hard" in flags else instances.SPIKE_INSTANCES
    wanted = tuple(args) or default_slice
    raise SystemExit(
        _run(
            wanted,
            update_baseline="--update-baseline" in flags,
            skip_self_check="--skip-self-check" in flags,
            hard="--hard" in flags or not args,
            keep_instances="--keep-instances" in flags,
            cache_dir=cache_dir,
            persona=persona,
        )
    )


def _run(
    instance_ids: tuple[str, ...],
    *,
    update_baseline: bool,
    skip_self_check: bool,
    hard: bool = False,
    keep_instances: bool = False,
    cache_dir: Path | None = None,
    persona: str = "neutral",
) -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "swebench" / run_id
    results_dir.mkdir(parents=True)
    print(
        f"[evals] swebench run {run_id} | slice={'hard' if hard else 'spike'} | "
        f"persona={persona} | instances={','.join(instance_ids)}",
        flush=True,
    )
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = harness._session_names(live_workspace)

    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network

    # Bind the REAL build_main_tools before the sandbox applies: agent.core
    # imported it at module load, so this import must happen before the
    # sandbox redirects persistence.
    import agent.core  # noqa: F401

    # Pin after the workspace override and config hydration: importing
    # ``server.service.messages`` too early would freeze WORKSPACE_DIR onto the
    # live persona directory, defeating the sandbox isolation.
    harness.pin_eval_budget()
    harness.pin_eval_temperature()

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] swebench SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3

    web_probe = _probe_web()
    harness._seed_persona(eval_workspace, persona=persona)
    _append_agents_appendix(eval_workspace, web_probe)
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
        prepared, prepare_failures = _prepare_all(selected, results_dir, cache_dir=cache_dir)
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
        results = asyncio.run(_run_instances(prepared, results_dir, hard=hard, run_id=run_id))
        for instance in selected:
            failure = prepare_failures.get(instance.instance_id)
            if failure is None:
                continue
            result = _harness_error_result(instance, RuntimeError(f"materialize failed: {failure}"))
            _write_instance_result(results_dir, result)
            results.append(result)
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
    token_cache = sum(int(r.get("tokens", {}).get("cache_read", 0)) for r in results)
    total_cost = round(sum(float(r.get("cost_usd") or 0.0) for r in results), 6)
    repair_stats: dict[str, int] = {}
    for result in results:
        for key, value in (result.get("tool_result_stats") or {}).items():
            repair_stats[key] = repair_stats.get(key, 0) + int(value)
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
        "model": {
            **harness._model_info(),
            "thinking_enabled": False,
            "temperature": harness.EVAL_TEMPERATURE,
        },
        "budget": {"main_max_iterations": harness.eval_main_max_iterations()},
        "agent_config": harness._coding_preset_payload(),
        # This run's probed network reality (default-off philosophy: the record
        # says what was reachable; nothing was switched on for it).
        "environment": {
            "web": web_probe,
            "sandbox_backend": _sandbox_backend(),
        },
        "persona_mode": persona,
        "persona": harness._persona_evidence(results[0]["id"] if results else None),
        "isolation": harness._isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "harness_self_check": {k: ok for k, (ok, _) in self_check.items()},
        "aggregate": {
            "instances_total": len(results),
            "resolved": sum(1 for r in results if r["resolved"]),
            "invalid_runs": [r["id"] for r in results if r["invalid_run"]],
            "harness_errors": [r["id"] for r in results if r.get("failure_kind") == "harness"],
            "resolve_rate": round(
                sum(1 for r in results if r["resolved"])
                / max(1, len(results) - sum(1 for r in results if r["invalid_run"])),
                4,
            ),
            "resolve_rate_basis": ("valid runs only (provider and harness errors excluded)"),
            "tokens": {
                "input": token_input,
                "output": token_output,
                "cache_read": token_cache,
            },
            "cost_usd": total_cost,
            "tool_result_stats": repair_stats,
            "duration_s": round(time.monotonic() - started, 1),
        },
        "instances": results,
    }
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return _publish(report, results_dir, update_baseline=update_baseline)


# ── preparation, validation, runs ───────────────────────────────────────────


def _prepare_all(
    selected: list[Instance],
    results_dir: Path,
    *,
    cache_dir: Path | None = None,
) -> tuple[list[Instance], dict[str, str]]:
    """Clone + install every instance; one network failure never aborts the slice."""
    work_dir = results_dir / "instances"
    work_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[Instance] = []
    failures: dict[str, str] = {}
    for instance in selected:
        print(
            f"[evals] materialising {instance.instance_id} "
            f"({instance.repo}@{instance.base_commit[:10]})…",
            flush=True,
        )
        started = time.monotonic()
        try:
            prepared.append(instances.materialize(instance, work_dir, cache_dir=cache_dir))
        except Exception as exc:  # noqa: BLE001 — record invalid, continue
            failures[instance.instance_id] = f"{type(exc).__name__}: {exc}"
            print(
                f"[evals] materialise FAILED {instance.instance_id}: {type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        print(
            f"[evals] materialised {instance.instance_id} in {time.monotonic() - started:.1f}s",
            flush=True,
        )
    return prepared, failures


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
    prepared: list[Instance],
    results_dir: Path,
    *,
    hard: bool = False,
    run_id: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    _write_run_meta(results_dir, run_id=run_id, hard=hard)
    for instance in prepared:
        timeout_s = _TURN_TIMEOUT_HARD_S if hard else _TURN_TIMEOUT_S
        print(
            f"[evals] starting {instance.instance_id} (turn_timeout={timeout_s:.0f}s)…",
            flush=True,
        )
        try:
            result = await _run_instance(instance, results_dir, hard=hard)
        except Exception as exc:  # noqa: BLE001 — one instance must not abort the slice
            print(
                f"[evals] {instance.instance_id}: HARNESS ERROR {type(exc).__name__}: {exc}",
                flush=True,
            )
            result = _harness_error_result(instance, exc)
        _write_instance_result(results_dir, result)
        results.append(result)
        print(
            f"[evals] {instance.instance_id}: resolved={result['resolved']} "
            f"failure={result.get('failure_kind') or 'none'} "
            f"duration={result['duration_s']}s tool_calls={result['tool_calls']} "
            f"tokens={result['tokens']}",
            flush=True,
        )
    return results


#: A hard instance gets a longer turn budget: the fix spans a real codebase and
#: the model has to find its way around first.
_TURN_TIMEOUT_S = 1200.0
_TURN_TIMEOUT_HARD_S = 2400.0


def _tool_result_stats(messages: list[Any]) -> dict[str, int]:
    """Count the tool-result shapes the 4.3 analysis needs to tell apart."""
    from langchain_core.messages import ToolMessage

    from pub.func.transcript_repair import EMPTY_TOOL_RESULT, MISSING_TOOL_RESULT

    stats = {
        "empty": 0,
        "evicted": 0,
        "missing": 0,
        "deduped": 0,
        "pruned": 0,
        "ttl": 0,
        "sliced": 0,
    }
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        text = str(message.content or "")
        repair = str((getattr(message, "additional_kwargs", None) or {}).get("sherry_repair") or "")
        if repair == "missing_tool_result" or text == MISSING_TOOL_RESULT:
            stats["missing"] += 1
        elif text == EMPTY_TOOL_RESULT or repair == "empty_result":
            stats["empty"] += 1
        elif text.startswith("[evicted to: "):
            stats["evicted"] += 1
        elif text.startswith("[Duplicated call"):
            stats["deduped"] += 1
        elif text.startswith("[Old tool result content cleared]"):
            stats["pruned"] += 1
        elif "tool result expired (TTL" in text:
            stats["ttl"] += 1
        elif "Output was truncated due to eviction threshold" in text:
            stats["sliced"] += 1
    return stats


async def _session_evidence(session_id: str) -> dict[str, Any]:
    """Final assistant text + token/cache/cost evidence for the report."""
    result: dict[str, Any] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cost_usd": 0.0,
        "final_text": "",
        "tool_result_stats": _tool_result_stats([]),
    }
    try:
        from langchain_core.messages import AIMessage

        from agent.state_port import read_messages
        from config.features.infra_side.model_pricing import resolve_model_pricing
        from context_engine.store.core import _cache_read_tokens_from_usage
        from pub.func.extract_text_from_content import extract_text_from_content

        messages = await read_messages(session_id)
    except Exception:  # noqa: BLE001 — evidence is reported, never fatal
        return result

    result["tool_result_stats"] = _tool_result_stats(messages)
    model = ""
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, dict):
            continue
        result["input"] += int(usage.get("input_tokens") or 0)
        result["output"] += int(usage.get("output_tokens") or 0)
        cache = _cache_read_tokens_from_usage(usage)
        if cache is not None:
            result["cache_read"] += int(cache)
        model = str(usage.get("model_name") or model)
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = extract_text_from_content(message.content)
            if text.strip():
                result["final_text"] = text
                break
    try:
        pricing = resolve_model_pricing(model or os.getenv("MAIN_LLM_NAME", ""))
        result["cost_usd"] = round(
            (
                result["input"] * float(pricing["input"])
                + result["output"] * float(pricing["output"])
            )
            / 1_000_000,
            6,
        )
    except Exception:  # noqa: BLE001, S110
        pass
    return result


def _sandbox_backend() -> str:
    """The backend the eval actually used (``none`` on a host without bwrap)."""
    try:
        from agent.tools.pub_base.sandbox import get_backend, read_policy

        backend = get_backend(read_policy())
        return type(backend).__name__ if backend is not None else "none"
    except Exception:  # noqa: BLE001 — an env fact, never fatal
        return "unknown"


def _write_instance_result(results_dir: Path, result: dict[str, Any]) -> None:
    """Write one instance's result immediately (a later crash must not lose it)."""
    target = results_dir / "instance_results" / f"{result.get('id', 'unknown')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_run_meta(results_dir: Path, *, run_id: str, hard: bool) -> None:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        head = ""
    meta = {
        "run_id": run_id,
        "argv": sys.argv[1:],
        "git_head": head,
        "hard": hard,
        "delegation_pressure": os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1",
        "model": harness._model_info(),
        "sandbox_backend": _sandbox_backend(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (results_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _harness_error_result(instance: Instance, exc: Exception) -> dict[str, Any]:
    return {
        "id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "resolved": False,
        "failure_kind": "harness",
        "invalid_run": True,
        "fail_to_pass_ok": False,
        "pass_to_pass_ok": False,
        "test_patch_applied": False,
        "target_tests_exposed": bool(instance.test_patch.strip()),
        "unrunnable_test_ids": instance.unrunnable_ids(),
        "judge_detail": "",
        "judge_tests": None,
        "duration_s": 0.0,
        "tool_calls": 0,
        "resumes": 0,
        "timeout": False,
        "error": f"{type(exc).__name__}: {exc}",
        "changed_paths": [],
        "reply": "",
        "final_text": "",
        "tokens": {},
        "cost_usd": 0.0,
        "tool_result_stats": {},
        "quiesce": {},
        "harness_error": str(exc),
    }


async def _wait_for_descendants(session_id: str, timeout_s: float) -> dict[str, Any]:
    """Wait for spawned children to settle before teardown (delegation pressure).

    ``_drive`` returns when the MAIN turn ends; a delegated child is detached
    and the teardown cascade would otherwise kill it before it writes. Poll the
    registry until no descendant is RUNNING/PENDING (or the timeout fires).
    """
    from agent.tools.subagent.registry.queries import count_active_descendant_runs
    from agent.tools.subagent.registry.session_keys import denormalize_session_key

    try:
        requester = denormalize_session_key(session_id)
    except Exception as exc:  # noqa: BLE001 — report, never mask the instance
        return {"ok": False, "active": None, "error": f"{type(exc).__name__}: {exc}"}
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        try:
            active = count_active_descendant_runs(requester)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "active": None, "error": f"{type(exc).__name__}: {exc}"}
        if active == 0:
            return {"ok": True, "active": 0}
        await asyncio.sleep(2.0)
    try:
        active = count_active_descendant_runs(requester)
    except Exception:  # noqa: BLE001
        active = None
    return {"ok": False, "active": active, "timed_out": True}


async def _run_instance(
    instance: Instance, results_dir: Path, *, hard: bool = False
) -> dict[str, Any]:
    fail_to_pass, pass_to_pass = instance.node_ids()
    unrunnable = instance.unrunnable_ids()
    test_paths = instances.test_files_of(instance.test_patch)
    tests_preapplied = bool(instance.test_patch.strip()) and instances.apply_patch(
        instance, instance.test_patch
    )
    target_tests = _target_tests_block(
        instance, fail_to_pass, pass_to_pass, preapplied=tests_preapplied
    )
    prompt = _TASK_TEMPLATE.format(
        problem=instance.problem_statement.strip(),
        repo=instance.repo,
        target_tests=target_tests,
    )
    session_id = f"evals-swe-{instance.instance_id}-{uuid.uuid4().hex[:6]}"
    await harness._bind_session(session_id, instance.repo_dir)

    from config.features import SHADOW_GIT
    from agent.tools.pub_base import shadow_git

    previous_store = SHADOW_GIT["store_dir"]
    SHADOW_GIT["store_dir"] = str(results_dir / "shadow")

    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{instance.python_exe.parent}{os.pathsep}{original_path}"
    previous_scm = os.environ.get("SETUPTOOLS_SCM_PRETEND_VERSION")
    if instance.version:
        os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"] = instance.version
    started = time.monotonic()
    timeout_s = _TURN_TIMEOUT_HARD_S if hard else _TURN_TIMEOUT_S
    drive: dict[str, Any] = {
        "tool_calls": 0,
        "text": "",
        "resumes": 0,
        "interrupted": 0,
        "timeout": False,
        "error": "",
    }
    quiesce: dict[str, Any] = {}
    try:
        try:
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
        except Exception as exc:  # noqa: BLE001 — the report must carry the failure
            drive = {
                "tool_calls": 0,
                "text": "",
                "resumes": 0,
                "interrupted": 0,
                "timeout": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1":
            # The main turn may end right after spawning; let the detached child
            # finish (and write its diff) before the teardown cascade kills it.
            child_wait = await _wait_for_descendants(session_id, timeout_s)
            print(
                f"[evals] {instance.instance_id}: delegated child wait {child_wait}",
                flush=True,
            )
    finally:
        os.environ["PATH"] = original_path
        if previous_scm is None:
            os.environ.pop("SETUPTOOLS_SCM_PRETEND_VERSION", None)
        else:
            os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"] = previous_scm
        # Stop every post-turn writer BEFORE the harness reads the tree.
        quiesce = await teardown.quiesce_session(session_id, instance.repo_dir)

    try:
        agent_diff = instances.diff(instance, exclude_paths=test_paths)
        changed = instances.changed_paths(instance, exclude_paths=test_paths)
        (results_dir / f"diff-{instance.instance_id}.patch").write_text(
            agent_diff, encoding="utf-8"
        )

        if quiesce.get("timed_out"):
            evidence = await _session_evidence(session_id)
            return {
                "id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "resolved": False,
                "failure_kind": "harness",
                "invalid_run": True,
                "fail_to_pass_ok": False,
                "pass_to_pass_ok": False,
                "test_patch_applied": False,
                "target_tests_exposed": bool(instance.test_patch.strip()),
                "unrunnable_test_ids": unrunnable,
                "judge_detail": "",
                "judge_tests": None,
                "duration_s": round(time.monotonic() - started, 1),
                "tool_calls": drive["tool_calls"],
                "resumes": drive["resumes"],
                "timeout": drive["timeout"],
                "error": "quiesce timed out: " + json.dumps(quiesce, ensure_ascii=False),
                "changed_paths": changed,
                "reply": drive["text"][:2000],
                "final_text": evidence["final_text"],
                "tokens": {
                    "input": evidence["input"],
                    "output": evidence["output"],
                    "cache_read": evidence["cache_read"],
                },
                "cost_usd": evidence["cost_usd"],
                "tool_result_stats": evidence.get("tool_result_stats", {}),
                "quiesce": quiesce,
                "harness_error": "quiesce timed out",
            }

        ok_f2p = ok_p2p = False
        f2p_tests: list[dict[str, Any]] = []
        p2p_tests: list[dict[str, Any]] = []
        if tests_preapplied:
            test_patch_applied = instances.reset_test_files(instance)
        else:
            test_patch_applied = instances.apply_patch(instance, instance.test_patch)
        if test_patch_applied:
            ok_f2p, out_f2p, f2p_tests = instances.run_tests(instance, fail_to_pass)
            ok_p2p, out_p2p, p2p_tests = instances.run_tests(instance, pass_to_pass)
            resolved = ok_f2p and ok_p2p
            detail = f"FAIL_TO_PASS ok={ok_f2p}\n{out_f2p}\nPASS_TO_PASS ok={ok_p2p}\n{out_p2p}"
        else:
            resolved = False
            detail = "the test patch does not apply — the agent changed test files"

        dirty_test_edits = _dirty_test_edits(changed, test_paths)
        if dirty_test_edits:
            resolved = False
            detail += f"\nDIRTY TEST EDITS: {', '.join(dirty_test_edits)}"
        passed = bool(resolved)
        failure_kind = "dirty_diff" if dirty_test_edits else harness._failure_kind(passed, drive)
        evidence = await _session_evidence(session_id)
        return {
            "id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
            "resolved": passed,
            "failure_kind": failure_kind,
            "invalid_run": (not passed) and failure_kind in {"provider", "harness"},
            "fail_to_pass_ok": bool(test_patch_applied and ok_f2p),
            "pass_to_pass_ok": bool(test_patch_applied and ok_p2p),
            "test_patch_applied": test_patch_applied,
            "target_tests_exposed": bool(instance.test_patch.strip()),
            "unrunnable_test_ids": unrunnable,
            "tests_preapplied": tests_preapplied,
            "dirty_test_edits": dirty_test_edits,
            "judge_detail": detail[-3000:],
            "judge_tests": (
                {"fail_to_pass": f2p_tests, "pass_to_pass": p2p_tests}
                if test_patch_applied
                else None
            ),
            "duration_s": round(time.monotonic() - started, 1),
            "tool_calls": drive["tool_calls"],
            "resumes": drive["resumes"],
            "timeout": drive["timeout"],
            "error": drive["error"],
            "changed_paths": changed,
            "reply": drive["text"][:2000],
            "final_text": evidence["final_text"],
            "tokens": {
                "input": evidence["input"],
                "output": evidence["output"],
                "cache_read": evidence["cache_read"],
            },
            "cost_usd": evidence["cost_usd"],
            "tool_result_stats": evidence.get("tool_result_stats", {}),
            "quiesce": quiesce,
            "harness_error": "",
        }
    finally:
        try:
            shadow_git.unfreeze(instance.repo_dir)
        except Exception:  # noqa: BLE001, S110
            pass
        SHADOW_GIT["store_dir"] = previous_store
        instances.revert(instance, trash_root=results_dir / "trash")


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
