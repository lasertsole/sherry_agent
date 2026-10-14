"""Coding-capability baseline eval suite.

Drives the REAL main agent — the same in-process path the server uses
(``server.service.messages.async_generate``) — against a handful of small,
self-contained coding tasks, each in its own throwaway git repository, and
judges every task by running **that repository's own test suite**. No LLM judge,
no network, no external benchmark.

Reporting discipline (``TODO/对标计划_编码能力实测方案.md`` §六), enforced here:

- this is a SELF-DESIGNED task set. The result is "N of M tasks passed", never a
  capability percentage, and it is not comparable to any public leaderboard;
- the verdict comes from the harness running ``python -m pytest`` in the task
  repo, never from the agent's own report of it;
- the persona is neutral BY CONSTRUCTION and the evidence is recorded: the suite
  points ``SHERRY_WORKSPACE_DIR`` at a throwaway directory, writes its own
  neutral ``AGENTS.md`` plus empty ``SOUL.md`` / ``USER.md`` / ``ROLE.md``, and
  scans the ASSEMBLED system prompt for persona markers (§3.5 criterion 2);
- the agent configuration is the built-in 编码助手 preset, derived from the live
  tool / skill catalogues at run time (all tools except the memory store, the
  required skills plus the coding skill set, and the compression nudge off) —
  the persona files of that preset are NOT reused, because its shipped
  ``AGENTS.md`` carries the "ORCHESTRATOR. NEVER THE IMPLEMENTER" doctrine that
  the plan identifies as the worst way to get a coding number (§3.3);
- the module is a task whose tests do not pass with the intended fix are
  reported as HARNESS failures before any model call, so "the model did not fix
  it" and "the harness did not run" never blur together (§3.5 criterion 1);
- a run that cannot start at all (no model key) is SKIPPED loudly and exits
  non-zero, never 0 (§4.5);
- approvals are auto-granted (``set_session_yolo``) so no instance needs a human.

Usage::

    uv run python evals/evals.py coding_capability          # via the runner
    uv run python evals/coding_capability/suite.py          # standalone
    uv run python evals/coding_capability/suite.py slugify-bugfix --update-baseline
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.coding_capability import tasks  # noqa: E402
from evals.coding_capability.tasks import TASKS, Task  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
#: Below this pass rate the suite exits non-zero (the plan's §4.2 mechanism).
DEFAULT_THRESHOLD_PASS_RATE = 0.5
_MAX_RESUMES = 3
#: A provider-side failure (a stalled stream the model then refuses to replay, a
#: rate limit, a 5xx) is an INVALID RUN, never a capability signal: the task is
#: retried once in a fresh session, and if it fails again it is reported and
#: EXCLUDED from the pass-rate denominator (the plan's §4.4 rule).
_MAX_ATTEMPTS = 2
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

#: The neutral arm's rules. Deliberately free of persona names and of the
#: shipped template's orchestrator doctrine: this is the only persona file the
#: plan says must be self-built (§3.3 "A 的真实前置工作").
_NEUTRAL_AGENTS_MD = """# Working rules

- The session's project directory is the repository you are asked to change.
- Make the change yourself: read the files, edit them, then run the tests.
- Change the implementation only. Never edit, delete or add tests.
- Run `python -m pytest -q` in the repository and iterate until it passes.
- Keep the change minimal and focused, and do not run git commit.
"""

_EMPTY_PERSONA_FILES = ("SOUL.md", "USER.md", "ROLE.md")

#: Markers a neutral arm must not contain: the persona name in any shipped
#: spelling and the orchestrator doctrine (§3.5 criterion 2).
_FORBIDDEN_MARKERS = ("Tachibana Sherry", "橘雪莉", "ORCHESTRATOR", "NEVER THE IMPLEMENTER")

_PROCEED_DIRECTIVE = (
    "Do not ask questions and do not delegate. Implement the change yourself in the "
    "repository and make `python -m pytest -q` pass."
)


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    """Run the suite, re-executing in a clean interpreter when needed."""
    # ``WORKSPACE_DIR`` is an import-time constant of config.path, so the
    # override must be visible to the FIRST importer. Dispatched from
    # evals/evals.py the process may already have imported other suites, so the
    # suite always runs in a child interpreter that sets it before anything else.
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
    selected = [t for t in TASKS if not args or t.id in args]
    unknown = [a for a in args if a not in {t.id for t in TASKS}]
    if unknown:
        raise SystemExit(f"unknown task id(s): {unknown}; available: {[t.id for t in TASKS]}")

    raise SystemExit(
        _run(
            selected,
            update_baseline="--update-baseline" in flags,
            skip_self_check="--skip-self-check" in flags,
        )
    )


def _run(selected: list[Task], *, update_baseline: bool, skip_self_check: bool) -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "coding_capability" / run_id
    results_dir.mkdir(parents=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    live_workspace = REPO_ROOT / "workspace"
    live_sessions = _session_names(live_workspace)

    # Point every workspace-derived path at the throwaway directory BEFORE the
    # first agent import (the constant is computed once), then let config.path
    # hydrate .env / .models / .network into the process environment — the model
    # key check below must run AFTER that hydration, or a configured checkout
    # would look unconfigured.
    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates the environment as a side effect

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(
            "[evals] coding_capability SKIPPED: MAIN_LLM_API_KEY is not configured "
            "(a skipped suite is not a pass)",
            flush=True,
        )
        return 3

    _seed_persona(eval_workspace)

    # The agent's tool list is assembled here, BEFORE the sandbox patches
    # ``agent.tools.build_main_tools`` to the child-safe subset: the sandbox's
    # restriction is for spawned children (§4.6), while the MAIN agent — the one
    # this suite drives — must keep write_file / patch_file / terminal.
    init_agent()
    main_tools = _main_tool_names()
    missing = sorted(
        {"read_file", "write_file", "patch_file", "search_files", "terminal"} - main_tools
    )
    if missing:
        print(
            f"[evals] HARNESS FAILURE: the main agent is missing coding tools: {missing}",
            flush=True,
        )
        return 2

    from evals.sandbox import EvalSandbox  # imported late: it pulls the runtimes

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    # The task agent must be able to RUN the tests. This host has no OS sandbox
    # backend (PRoot container), so `required` would refuse every command and
    # every task would fail for a harness reason; `auto` degrades with a warning
    # (the same default the shipped .env now carries) and every bare run is
    # recorded as unsandboxed in the evidence ledger.
    os.environ["SANDBOX_POLICY"] = "auto"

    started = time.monotonic()
    try:
        self_check = {} if skip_self_check else _self_check(selected)
        if any(not ok for ok, _ in self_check.values()):
            for task_id, (ok, output) in self_check.items():
                if not ok:
                    print(
                        f"[evals] HARNESS FAILURE: task {task_id!r} does not pass with its "
                        f"reference fix:\n{output}",
                        flush=True,
                    )
            return 2
        results = asyncio.run(_run_tasks(selected, results_dir))
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = _build_report(
        run_id=run_id,
        results_dir=results_dir,
        selected=selected,
        results=results,
        self_check=self_check,
        live_workspace=live_workspace,
        live_sessions=live_sessions,
        eval_workspace=eval_workspace,
        duration_s=time.monotonic() - started,
    )
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return _publish(report, results_dir, update_baseline=update_baseline)


def _session_names(workspace: Path) -> set[str]:
    sessions = workspace / "sessions"
    if not sessions.is_dir():
        return set()
    return {entry.name for entry in sessions.iterdir() if entry.is_dir()}


def _seed_persona(workspace: Path) -> None:
    """Write the neutral arm: rules only, no persona, no template copy.

    ``workspace/template/`` is deliberately NOT copied — the shipped templates
    carry the persona (and its orchestrator doctrine) that the plan forbids as a
    "neutral" arm (§3.3). The memory note is seeded instead, so the memory store
    finds an empty page rather than warning about a missing template.
    """
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_NEUTRAL_AGENTS_MD, encoding="utf-8")
    (workspace / "memory" / "FACTS.md").write_text("", encoding="utf-8")
    for name in _EMPTY_PERSONA_FILES:
        (workspace / name).write_text("", encoding="utf-8")


def init_agent() -> None:
    """One-time agent assembly (the plan's P0: without it the tool list is empty)."""
    from agent import core as agent_core

    agent_core.init()


def _self_check(selected: list[Task]) -> dict[str, tuple[bool, str]]:
    print("[evals] verifying each task against its reference fix…", flush=True)
    return {task.id: tasks.verify_reference_fix(task) for task in selected}


# ── The per-task run ────────────────────────────────────────────────────────


async def _run_tasks(selected: list[Task], results_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for task in selected:
        print(f"[evals] === task: {task.id} ===", flush=True)
        result = await _run_one(task, results_dir)
        results.append(result)
        print(
            f"[evals] {task.id}: passed={result['passed']} duration={result['duration_s']}s "
            f"tool_calls={result['tool_calls']} resumes={result['resumes']} "
            f"commits={result['commits']} tokens={result['tokens']}",
            flush=True,
        )
    return results


async def _run_one(task: Task, results_dir: Path) -> dict[str, Any]:
    """Run one task, retrying a provider-side failure once (fresh session)."""
    attempt = 1
    while True:
        result = await _attempt(task, results_dir, attempt)
        # A pass, or a genuine "the agent did not fix it", is final: only a
        # provider-side failure is worth a second attempt.
        if result["passed"] or result["failure_kind"] != "provider" or attempt >= _MAX_ATTEMPTS:
            result["attempts"] = attempt
            return result
        print(
            f"[evals] {task.id}: attempt {attempt} hit a provider failure "
            f"({result['error'][:120]}…); retrying once",
            flush=True,
        )
        attempt += 1


async def _attempt(task: Task, results_dir: Path, attempt: int) -> dict[str, Any]:
    # A fresh repository AND a fresh session per attempt: a retry must not
    # inherit the half-edited tree or the poisoned transcript of the try before.
    repo = results_dir / "repos" / task.id
    base_sha = tasks.build_repo(task, repo)
    session_id = f"evals-coding-{task.id}-{uuid.uuid4().hex[:8]}"
    await _bind_session(session_id, repo)

    started = time.monotonic()
    try:
        drive = await asyncio.wait_for(_drive(session_id, task.prompt), timeout=task.timeout_s)
    except TimeoutError:
        drive = {
            "tool_calls": 0,
            "text": "",
            "resumes": 0,
            "interrupted": 0,
            "timeout": True,
            "error": "",
        }
    duration_s = round(time.monotonic() - started, 1)

    exit_code, output = tasks.run_tests(repo)
    stat, diff = tasks.git_diff(repo, base_sha)
    if diff:
        suffix = f"-attempt{attempt}" if attempt > 1 else ""
        (results_dir / f"diff-{task.id}{suffix}.patch").write_text(diff, encoding="utf-8")
    # The tests define the task: a green suite reached by editing them is not a
    # pass, and the report says so explicitly rather than trusting pytest.
    tampered = tasks.tampered_paths(tasks.changed_paths(repo, base_sha))
    passed = exit_code == 0 and not tampered
    if tampered:
        output = f"{output}\n[evals] tests were modified: {tampered}"
    return {
        "id": task.id,
        "passed": passed,
        "failure_kind": _failure_kind(passed, drive),
        "invalid_run": (not passed) and _failure_kind(passed, drive) == "provider",
        "pytest_exit_code": exit_code,
        "pytest_tail": output,
        "duration_s": duration_s,
        "tool_calls": drive["tool_calls"],
        "resumes": drive["resumes"],
        "interrupts": drive["interrupted"],
        "timeout": drive["timeout"],
        "error": drive["error"],
        "commits": tasks.commit_count(repo),
        "test_tampering": tampered,
        "diff_stat": stat.splitlines()[-1] if stat else "",
        "reply": drive["text"][:2000],
        "tokens": await _session_tokens(session_id),
    }


def _failure_kind(passed: bool, drive: dict[str, Any]) -> str:
    """``""`` (a real verdict) / ``provider`` (invalid run) / ``timeout`` / ``harness``."""
    if passed:
        return ""
    if drive["timeout"]:
        return "timeout"
    error = str(drive.get("error") or "")
    if error:
        lowered = error.lower()
        if any(marker in lowered for marker in _PROVIDER_ERROR_MARKERS):
            return "provider"
        return "harness"
    return ""  # the turn completed; the agent simply did not make the tests pass


def _main_tool_names() -> set[str]:
    """The tools the assembled MAIN agent will actually see."""
    from agent import core as agent_core

    tools = agent_core.get_agent_tools() or []
    return {getattr(tool, "name", "") for tool in tools}


async def _bind_session(session_id: str, repo: Path) -> None:
    """Bind the session to the task repo, grant approvals, apply the preset."""
    from runtime import state_register_db
    from runtime.session.state_keys import StateKey
    from runtime.session.state_register import state_register_mem

    for register in (state_register_mem, state_register_db):
        register.set_state(session_id, StateKey.PROJECT_DIR, str(repo))

    from agent.middlewares.humanInTheLoop.approval import set_session_yolo

    set_session_yolo(session_id)

    # Pin the thinking switch to the shipped default (reasoning OFF, see
    # models/LLMs/main_llm.py): the report must say which one served the run.
    from runtime.session.state_keys import StateKey as _StateKey

    for register in (state_register_mem, state_register_db):
        register.set_state(session_id, _StateKey.LLM_THINKING_ENABLED, False)

    # The payload is written straight to the state register rather than through
    # ``agent_config_service.apply_agent_config_choice``: that service validates
    # names against the LIVE catalogue, which the eval sandbox has already
    # narrowed to the child-safe subset. The runtime readers (agent_switch,
    # ToolSelectionMiddleware, the skill selection) read this state directly, so
    # the session behaves exactly as if the preset had been applied.
    payload = _coding_preset_payload()
    from server.service.session_settings_service import _delete_value, _write_value

    # The shared dual-writer (mem + durable), with the same parked-key cleanup
    # the HTTP path performs — readers expect a DICT, not a JSON string.
    _write_value(session_id, StateKey.AGENT_CONFIG, payload)
    _delete_value(session_id, StateKey.AGENT_CONFIG_PENDING)

    from server.service.agent_config_service import invalidate_session_prompt

    invalidate_session_prompt(session_id)


def _coding_preset_payload() -> dict[str, Any]:
    """The 编码助手 preset's agent block, derived from the live catalogues.

    Mirrors ``client/app/composables/persona-catalog.ts::builtinAgentConfig`` for
    the ``coding`` preset: every catalogue tool EXCEPT the memory store, the
    required skills plus the coding skill set, all middlewares on, and the
    compression nudge off.
    """
    from agent.tools.catalog import REQUIRED_TOOLS, TOOL_GROUPS
    from skills.loader import REQUIRED_SKILLS, skills_catalog

    # The catalogue's own static name source (``tool_catalog()`` reads the live
    # tool list, which the eval sandbox has already narrowed for children).
    # Every 任务与计划 / 子代理 tool stays available; only the memory store is off.
    tools = sorted({name for names in TOOL_GROUPS.values() for name in names} - {"memory"})
    dropped = set(REQUIRED_TOOLS) - set(tools)
    if dropped:  # the service's own rule: a required tool cannot be disabled
        raise RuntimeError(f"the coding preset would drop required tools: {sorted(dropped)}")
    available = {entry["name"] for entry in skills_catalog()}
    wanted = set(REQUIRED_SKILLS) | {"code-wiki", "taskflow", "ulw-execute", "todolist"}
    return {
        "tools": tools,
        "middlewares_disabled": [],
        "skills": sorted(wanted & available),
        "middleware_options": {"Summarization": {"nudge": False}},
    }


async def _drive(session_id: str, prompt: str) -> dict[str, Any]:
    """One turn, resuming past any HITL question with a canned directive."""
    from agent.state_port import read_interrupt
    from pub.types.message import MultiModalMessage
    from server.service.messages import async_generate, resume_agent

    state: dict[str, Any] = {
        "tool_calls": 0,
        "text": "",
        "resumes": 0,
        "interrupted": 0,
        "timeout": False,
        "error": "",
    }

    async def consume(stream: Any) -> None:
        async for frame in stream:
            data = json.loads(frame) if isinstance(frame, str) else frame
            if not isinstance(data, dict):
                continue
            kind = data.get("type") or data.get("event")
            if kind == "tool_start":
                state["tool_calls"] += 1
            elif kind == "text":
                state["text"] += str(data.get("content") or "")

    try:
        await consume(async_generate(session_id, MultiModalMessage(text=prompt), is_stream=True))
        for _ in range(_MAX_RESUMES):
            pending = await read_interrupt(session_id)
            if pending is None:
                break
            state["interrupted"] += 1
            await consume(resume_agent(session_id, "reject", _PROCEED_DIRECTIVE))
            state["resumes"] += 1
    except TimeoutError:
        state["timeout"] = True
    except Exception as exc:  # noqa: BLE001 — the report must carry the failure
        state["error"] = f"{type(exc).__name__}: {exc}"
    return state


async def _session_tokens(session_id: str) -> dict[str, int]:
    from agent.state_port import read_messages

    try:
        messages = await read_messages(session_id)
    except Exception:  # noqa: BLE001 — token accounting is reported, never fatal
        return {}
    input_tokens = output_tokens = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
    return {"input": input_tokens, "output": output_tokens}


# ── Report, persona evidence, baseline ──────────────────────────────────────


def _build_report(
    *,
    run_id: str,
    results_dir: Path,
    selected: list[Task],
    results: list[dict[str, Any]],
    self_check: dict[str, tuple[bool, str]],
    live_workspace: Path,
    live_sessions: set[str],
    eval_workspace: Path,
    duration_s: float,
) -> dict[str, Any]:
    passed = sum(1 for r in results if r["passed"])
    invalid = [r["id"] for r in results if r.get("invalid_run")]
    valid = len(results) - len(invalid)
    return {
        "run_id": run_id,
        "suite": "coding_capability",
        "kind": "self-designed task set (NOT an external benchmark; not comparable)",
        "results_dir": str(results_dir),
        "model": {**_model_info(), "thinking_enabled": False},
        "agent_config": _coding_preset_payload(),
        "persona": _persona_evidence(results[0]["id"] if results else None),
        "isolation": _isolation_evidence(live_workspace, live_sessions, eval_workspace),
        "harness_self_check": {task_id: ok for task_id, (ok, _) in self_check.items()},
        "aggregate": {
            "tasks_total": len(results) or len(selected),
            "tasks_passed": passed,
            # Provider failures are excluded from the basis (plan §4.4) and are
            # listed separately, so a provider outage can never masquerade as a
            # capability number in either direction.
            "tasks_valid": valid,
            "invalid_runs": invalid,
            "pass_rate": round(passed / valid, 4) if valid else 0.0,
            "pass_rate_basis": "valid runs only (provider failures excluded)",
            "duration_s": round(duration_s, 1),
            "tool_calls": sum(r["tool_calls"] for r in results),
            "tokens": {
                "input": sum(r["tokens"].get("input", 0) for r in results),
                "output": sum(r["tokens"].get("output", 0) for r in results),
            },
        },
        "tasks": results,
    }


def _model_info() -> dict[str, str]:
    # Non-secret fields only: never the API key.
    return {
        "provider": os.getenv("MAIN_LLM_PROVIDER", ""),
        "model": os.getenv("MAIN_LLM_NAME", ""),
        "base_url": os.getenv("MAIN_LLM_API_BASE", ""),
        "max_token": os.getenv("MAIN_LLM_MAX_TOKEN", ""),
        "auxiliary_model": os.getenv("AUXILIARY_LLM_API_NAME", ""),
        "reasoner_model": os.getenv("REASONER_LLM_API_NAME", ""),
    }


def _persona_evidence(session_id: str | None) -> dict[str, Any]:
    """The plan's criterion 2: prove the neutral arm really is neutral."""
    from config.path import WORKSPACE_DIR
    from workspace.prompt_builder import build_system_prompt

    prompt = build_system_prompt(session_id=session_id or "")
    digests: dict[str, str] = {}
    for name in ("AGENTS.md", *_EMPTY_PERSONA_FILES):
        path = WORKSPACE_DIR / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else ""
        digests[name] = digest
    return {
        "workspace_dir": str(WORKSPACE_DIR),
        "file_digests": digests,
        "assembled_prompt_chars": len(prompt),
        "forbidden_markers_found": sorted(m for m in _FORBIDDEN_MARKERS if m in prompt),
        "assembled_prompt_head": prompt[:600],
    }


def _isolation_evidence(
    live_workspace: Path, live_sessions: set[str], eval_workspace: Path
) -> dict[str, Any]:
    """The plan's P2a ④: the run's state landed in the instance directory."""
    return {
        "eval_workspace": str(eval_workspace),
        "eval_workspace_has_sessions": (eval_workspace / "sessions").is_dir(),
        "live_workspace": str(live_workspace),
        "live_sessions_unchanged": _session_names(live_workspace) == live_sessions,
        "sandbox_policy": os.getenv("SANDBOX_POLICY", ""),
        "note": (
            "commands run UNSANDBOXED here: this host has no OS sandbox backend "
            "(PRoot), so the plan's one-shot-container requirement is not met. "
            "Task repositories are throwaway directories, prompts are ours, and "
            "every bare run is recorded 'sandboxed: false' in the evidence ledger."
        ),
    }


def _publish(report: dict[str, Any], results_dir: Path, *, update_baseline: bool) -> int:
    aggregate = report["aggregate"]
    threshold = DEFAULT_THRESHOLD_PASS_RATE
    baseline: dict[str, Any] | None = None
    if BASELINE_PATH.is_file():
        loaded = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            baseline = loaded
            threshold = float(baseline.get("threshold_pass_rate") or threshold)
            print(f"[evals] baseline: {_delta(baseline, aggregate)}", flush=True)

    print(f"\n[evals] coding_capability aggregate: {aggregate}", flush=True)
    print(
        f"[evals] persona check: {report['persona']['forbidden_markers_found'] or 'clean'}",
        flush=True,
    )
    print(f"[evals] isolation: {report['isolation']}", flush=True)
    print(f"[evals] report: {results_dir}", flush=True)

    if update_baseline or baseline is None:
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "captured_run": report["run_id"],
                    "threshold_pass_rate": DEFAULT_THRESHOLD_PASS_RATE,
                    "model": report["model"],
                    "aggregate": aggregate,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[evals] baseline updated: {BASELINE_PATH}", flush=True)

    invalid = report["aggregate"]["invalid_runs"]
    if invalid:
        print(f"[evals] INVALID RUNS (excluded from the pass rate): {invalid}", flush=True)
    harness_errors = [r["id"] for r in report["tasks"] if r["failure_kind"] == "harness"]
    if harness_errors:
        print(f"[evals] FAILED: harness error in task(s): {harness_errors}", flush=True)
        return 1
    if not report["aggregate"]["tasks_valid"]:
        print("[evals] FAILED: no valid run at all (every task hit a provider failure)", flush=True)
        return 1
    if report["aggregate"]["pass_rate"] < threshold:
        print(
            f"[evals] FAILED: pass rate {report['aggregate']['pass_rate']} < threshold {threshold}",
            flush=True,
        )
        return 1
    return 0


def _delta(baseline: dict[str, Any], aggregate: dict[str, Any]) -> str:
    """Human-readable baseline comparison (the plan's §4.1 trend read-out)."""
    old = baseline.get("aggregate", {})
    parts = [
        f"{key} {old[key]} -> {aggregate[key]}"
        for key in ("tasks_passed", "pass_rate", "duration_s")
        if key in old and key in aggregate
    ]
    tokens_old = old.get("tokens", {})
    tokens_new = aggregate.get("tokens", {})
    if tokens_old or tokens_new:
        parts.append(
            f"tokens in/out {tokens_old.get('input', 0)}/{tokens_old.get('output', 0)}"
            f" -> {tokens_new.get('input', 0)}/{tokens_new.get('output', 0)}"
        )
    old_model = str(baseline.get("model", {}).get("model", "") or "?")
    new_model = str(_model_info().get("model", "") or "?")
    parts.append(f"model {old_model} -> {new_model}")
    return "; ".join(parts)


if __name__ == "__main__":
    main()
