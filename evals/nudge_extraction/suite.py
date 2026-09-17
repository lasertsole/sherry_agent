"""Eval suite: real plan-aware extraction scored by an auxiliary-LLM judge.

The suite seeds a self-contained, completed-plan run (plan file + completed
todos + synthetic subagent run records), invokes the
production plan-extraction pass
(``agent.middlewares.summarization.nudges._nudge_plan_extraction``), then asks
an auxiliary LLM whether the skill it produced is genuinely grounded in THIS
run, reusable, and non-generic. Every write is redirected into the sandbox, so
the repo's real ``skills/auto/`` and ``workspace/`` are never touched.

Usage:
    uv run python evals/evals.py nudge_extraction
"""

from __future__ import annotations

import asyncio
import csv
import faulthandler
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.sandbox import EvalSandbox  # noqa: E402

_SUITE = "nudge_extraction"
_PLAN_NAME = "duckdb-csv-ingest"
_EXTRACTION_TIMEOUT_S = 480.0
_JUDGE_TIMEOUT_S = 180.0
_MAX_SKILL_CHARS = 12_000
_MAX_CONTEXT_CHARS = 12_000

_SYSTEM_PROMPT = (
    "You are Sherry, a senior engineering assistant. You have just finished a "
    "long-running plan and are now consolidating what was learned."
)
_SESSION_TAIL = (
    "Plan complete: the DuckDB CSV loader now ingests the three-file sample drop "
    "cleanly and a second run is a no-op. Nice work."
)
_TECHNIQUE_SUMMARY = """\
Reusable technique discovered during this plan:
- DuckDB read_csv_auto only samples the first ~20k rows; an all-NULL column in
  that window is typed VARCHAR and later integer rows then fail. Pass
  sample_size=-1 so type inference scans the whole file.
- Partner files share logical columns in different physical order; pass
  union_by_name=true so columns align by name instead of position.
- CREATE OR REPLACE TABLE fails with a TransactionContext Error while a prior
  result on the connection is still open; materialize a temp table inside an
  explicit transaction and rename, or drain the previous result first.
"""
_PLAN_MD = f"""# Plan: DuckDB CSV ingest hardening

## Goal
Load partner-delivered CSV drops into a local analytics table with DuckDB,
keeping the swap atomic and tolerating dirty schema across files.

## Tasks
1. Inspect the sample drop and confirm delimiter/encoding.
2. Build a loader that appends all *.csv files in a directory.
3. Make repeated runs idempotent (no duplicate rows).

## Findings / pitfalls (reusable)
{_TECHNIQUE_SUMMARY}
## Done when
- the 3-file sample drop loads with mixed column order,
- a second run is a no-op (row count unchanged),
- no VARCHAR-vs-INT conversion warnings.
"""

_JUDGE_PROMPT_TEMPLATE = """\
You are an exacting evaluation judge. Decide whether an automated post-plan
knowledge extraction pass produced a GENUINELY USEFUL, REUSABLE skill.

Use ONLY the material below. Do not reward generic advice, and do not assume
anything that is not shown here.

=== RUN CONTEXT (what actually happened) ===
{context}

=== PRODUCED SKILL (SKILL.md content) ===
{skill}

Return STRICT JSON only, with exactly these keys:
{
  "skill_created": true/false,
  "grounded_in_run": true/false,
  "reusable": true/false,
  "quality": <integer 0-10>,
  "reason": "<1-2 sentences citing concrete evidence from the material>"
}

Judging rules:
- skill_created: true only if a non-empty SKILL.md with real, actionable
  instructions was produced.
- grounded_in_run: true only if the skill refers to the specific task, tools,
  findings, or pitfalls from THIS run. Generic boilerplate unrelated to the run
  => false, with quality <= 4.
- reusable: true only if it is a class-level procedure applicable to future
  similar work. A one-off task narrative or a single hardcoded result => false.
- quality: 0 = empty/irrelevant, 6 = solid and grounded, 10 = excellent.
- If no skill was produced: skill_created=false and quality=0.
"""


def _write_report(results_dir: Path, report: dict[str, Any]) -> None:
    """Persist ``eval_report.json`` + ``scores.csv`` for one run."""
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (results_dir / "scores.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["check", "passed", "detail"])
        writer.writeheader()
        for check in report.get("checks", []):
            writer.writerow(
                {
                    "check": check.get("check", ""),
                    "passed": check.get("passed", False),
                    "detail": json.dumps(check.get("detail", {}), ensure_ascii=False)[:500],
                }
            )


def _snapshot(paths: list[Path]) -> dict[str, tuple[int, int]]:
    """Map every file under *paths* to ``(size, mtime_ns)`` for pollution diffing."""
    snapshot: dict[str, tuple[int, int]] = {}
    for base in paths:
        if base.is_file():
            stat = base.stat()
            snapshot[str(base.relative_to(REPO_ROOT))] = (stat.st_size, stat.st_mtime_ns)
            continue
        if not base.is_dir():
            continue
        for file_path in sorted(base.rglob("*")):
            if file_path.is_file():
                stat = file_path.stat()
                snapshot[str(file_path.relative_to(REPO_ROOT))] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> dict[str, list[str]]:
    """Return the added / removed / changed file lists between two snapshots."""
    common = set(before) & set(after)
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(key for key in common if before[key] != after[key]),
    }


def _install_nudge_tools() -> list[str]:
    """Bind the nudge-marked tool set the production extraction agent may call.

    Production wires the full main-tool list and lets ``_NudgeLimitTool`` reject
    every non-nudge tool at call time. Here only the nudge-marked tools are
    bound, which is the same effective toolset without paying for MCP/terminal
    construction inside an eval.
    """
    import agent.core as agent_core
    from agent.tools.memory import build_memory_tool
    from agent.tools.skill_tools import (
        build_skill_list_tool,
        build_skill_manage_tool,
        build_skill_view_tool,
    )
    from agent.tools.todolist.knowledge import build_knowledge_tools

    tools = [
        *build_knowledge_tools(),
        build_memory_tool(),
        build_skill_manage_tool(),
        build_skill_list_tool(),
        build_skill_view_tool(),
    ]
    setattr(agent_core, "_tools", tools)  # noqa: B010 — core exposes a module global
    return [tool.name for tool in tools]


async def _seed_todos(session_id: str, plan_path: Path) -> list[dict[str, Any]]:
    """Write the completed todo list (all rows carry the plan ref) to the store."""
    from agent.tools.todolist.registry.store_sqlite import replace_all

    todos: list[dict[str, Any]] = [
        {
            "content": "Inspect the sample CSV drop (delimiter/encoding).",
            "status": "completed",
            "priority": "high",
            "position": 0,
            "plan_ref": str(plan_path),
        },
        {
            "content": (
                "Build a DuckDB loader using full-sample type inference and "
                "union_by_name column alignment."
            ),
            "status": "completed",
            "priority": "high",
            "position": 1,
            "plan_ref": str(plan_path),
        },
        {
            "content": "Make the table swap idempotent inside an explicit transaction.",
            "status": "completed",
            "priority": "medium",
            "position": 2,
            "plan_ref": str(plan_path),
        },
    ]
    await replace_all(session_id, todos)
    return todos


def _seed_subagent_runs(session_id: str) -> list[dict[str, str]]:
    """Register two synthetic, already-completed subagent run records."""
    from agent.tools.subagent.registry import set_run
    from agent.tools.subagent.types.registry import (
        CompletionState,
        ExecutionState,
        ExecutionStatus,
        RunOutcome,
        RunOutcomeStatus,
        SubagentRunRecord,
    )

    requester = f"agent:main:session:{session_id}"
    fixtures: list[tuple[str, str, str]] = [
        (
            "csv_schema_audit",
            "Audit the three CSV drops for column order and null patterns.",
            "All three files share the same logical columns but in different "
            "physical order. Two columns are all-NULL in the first 20000 rows; "
            "with the default DuckDB sample the inferred type was VARCHAR and the "
            "later integer rows raised 'Could not convert string to INT32'. The "
            "fix is read_csv_auto(sample_size=-1) plus union_by_name=true so "
            "columns align by name rather than position.",
        ),
        (
            "idempotent_swap_tests",
            "Verify the loader is idempotent and the swap is atomic.",
            "Re-running the loader must not duplicate rows. CREATE OR REPLACE "
            "TABLE t AS SELECT * FROM read_csv_auto(...) raised 'TransactionContext "
            "Error: cannot create or replace a table while a query is active' when "
            "the previous result was still open. Materializing into a temp table "
            "inside an explicit transaction and renaming made the swap atomic and "
            "the second run a no-op.",
        ),
    ]
    seeded: list[dict[str, str]] = []
    for index, (task_name, task, result_text) in enumerate(fixtures):
        run = SubagentRunRecord(
            run_id=f"evals-nudge-{task_name}-{uuid.uuid4().hex[:8]}",
            child_session_key=f"agent:main:subagent:evals-nudge-{index}",
            requester_session_key=requester,
            task=task,
            task_name=task_name,
            execution=ExecutionState(
                status=ExecutionStatus.TERMINAL,
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
            completion=CompletionState(required=True, result_text=result_text),
        )
        set_run(run)
        seeded.append({"task_name": task_name, "task": task, "result_text": result_text})
    return seeded


def _scan_knowledge(plans_dir: Path) -> dict[str, Any]:
    """Summarize the knowledge JSON documents written under the sandbox root."""
    result: dict[str, Any] = {
        "plan_dirs": [],
        "task_files": 0,
        "wave_files": 0,
        "plan_summaries": 0,
        "files": [],
    }
    if not plans_dir.is_dir():
        return result
    for plan_dir in sorted(entry for entry in plans_dir.iterdir() if entry.is_dir()):
        files = sorted(plan_dir.glob("*.json"))
        if not files:
            continue
        result["plan_dirs"].append(plan_dir.name)
        for file_path in files:
            result["files"].append(str(file_path.relative_to(plans_dir)))
            if file_path.name.startswith("task-"):
                result["task_files"] += 1
            elif file_path.name.startswith("wave-"):
                result["wave_files"] += 1
            elif file_path.name == "plan-summary.json":
                result["plan_summaries"] += 1
    return result


def _list_skill_files(auto_dir: Path) -> list[Path]:
    """Return every SKILL.md under the sandbox auto-skills directory."""
    if not auto_dir.is_dir():
        return []
    return sorted(auto_dir.rglob("SKILL.md"))


def _render_context(fixture: dict[str, Any]) -> str:
    """Render the run context handed to the judge (plan + todos + run results)."""
    lines: list[str] = [
        f"Plan name: {fixture['plan_name']}",
        "",
        "=== PLAN FILE ===",
        fixture["plan_content"],
        "",
        "=== COMPLETED TODOS ===",
    ]
    for todo in fixture["todos"]:
        lines.append(f"- [{todo.get('status')}] {todo.get('content')}")
    lines.append("")
    lines.append("=== SUBAGENT RUN RESULTS ===")
    for run in fixture["subagent_runs"]:
        lines.append(f"- {run['task_name']}: {run['task']}")
        lines.append(f"  result: {run['result_text']}")
    lines.append("")
    lines.append("=== SESSION TAIL ===")
    lines.append(_SESSION_TAIL)
    return "\n".join(lines)[:_MAX_CONTEXT_CHARS]


async def _judge_skill(context_text: str, skill_text: str) -> dict[str, Any]:
    """Ask the auxiliary LLM to score the produced skill; retry once on failure."""
    import json_repair

    from models import build_auxiliary_llm

    prompt = _JUDGE_PROMPT_TEMPLATE.replace("{context}", context_text).replace(
        "{skill}", skill_text or "(no SKILL.md produced)"
    )
    llm = build_auxiliary_llm()
    last_error = ""
    for attempt in range(1, 3):
        try:
            response = await asyncio.wait_for(llm.ainvoke(prompt), timeout=_JUDGE_TIMEOUT_S)
            raw = getattr(response, "content", "")
            if not isinstance(raw, str):
                raw = str(raw)
            parsed = json_repair.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"judge returned {type(parsed).__name__}, expected object")
            return {
                "skill_created": bool(parsed.get("skill_created", False)),
                "grounded_in_run": bool(parsed.get("grounded_in_run", False)),
                "reusable": bool(parsed.get("reusable", False)),
                "quality": int(parsed.get("quality", 0) or 0),
                "reason": str(parsed.get("reason", "")),
                "raw": raw[:4000],
                "attempts": attempt,
            }
        except Exception as exc:  # noqa: BLE001 — a judge failure is a recorded data point
            last_error = f"{type(exc).__name__}: {exc}"
    return {
        "skill_created": False,
        "grounded_in_run": False,
        "reusable": False,
        "quality": 0,
        "reason": f"judge failed after 2 attempts: {last_error}",
        "raw": "",
        "attempts": 2,
        "error": last_error,
    }


async def _seed_fixture(session_id: str, results_dir: Path, sandbox: EvalSandbox) -> dict[str, Any]:
    """Create the plan file, todos and subagent runs."""
    plan_path = results_dir / "plans" / f"{_PLAN_NAME}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(_PLAN_MD, encoding="utf-8")

    todos = await _seed_todos(session_id, plan_path)
    subagent_runs = _seed_subagent_runs(session_id)
    return {
        "plan_name": _PLAN_NAME,
        "plan_path": str(plan_path),
        "plan_content": _PLAN_MD,
        "todos": todos,
        "subagent_runs": subagent_runs,
        "sandbox_auto_skills_dir": str(sandbox.auto_skills_dir),
        "sandbox_knowledge_dir": str(sandbox.knowledge_plans_dir),
    }


async def _run_checks(session_id: str, results_dir: Path, sandbox: EvalSandbox) -> dict[str, Any]:
    """Seed the fixture, run the real extraction, and evaluate every check."""
    from langchain_core.messages import HumanMessage

    from agent.middlewares.summarization.nudges import _nudge_plan_extraction

    checks: list[dict[str, Any]] = []
    fixture = await _seed_fixture(session_id, results_dir, sandbox)
    nudge_tool_names = _install_nudge_tools()

    extraction_error = ""
    extraction_started = time.monotonic()
    extraction_attempts = 0
    knowledge = _scan_knowledge(sandbox.knowledge_plans_dir)
    skill_files: list[Path] = []
    # The production pass is fail-open: a transient LLM failure is swallowed and
    # leaves no output. Retry the pass once when it produced nothing at all.
    for attempt in range(1, 3):
        extraction_attempts = attempt
        try:
            await asyncio.wait_for(
                _nudge_plan_extraction(
                    session_id, _SYSTEM_PROMPT, [HumanMessage(content=_SESSION_TAIL)]
                ),
                timeout=_EXTRACTION_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — extraction is fail-open; record the failure
            extraction_error = f"{type(exc).__name__}: {exc}"
        knowledge = _scan_knowledge(sandbox.knowledge_plans_dir)
        skill_files = _list_skill_files(sandbox.auto_skills_dir)
        produced = bool(knowledge["files"]) or bool(skill_files)
        if produced:
            break
    extraction_latency = round(time.monotonic() - extraction_started, 2)

    knowledge_passed = (
        knowledge["plan_summaries"] >= 1
        and knowledge["task_files"] >= 1
        and knowledge["wave_files"] >= 1
    )
    checks.append({"check": "knowledge_written", "passed": knowledge_passed, "detail": knowledge})

    skill_text = "\n\n".join(file_path.read_text(encoding="utf-8") for file_path in skill_files)[
        :_MAX_SKILL_CHARS
    ]
    checks.append(
        {
            "check": "skills_created",
            "passed": len(skill_files) >= 1,
            "detail": {
                "count": len(skill_files),
                "skill_files": [
                    str(file_path.relative_to(sandbox.auto_skills_dir)) for file_path in skill_files
                ],
            },
        }
    )

    context_text = _render_context(fixture)
    judge = await _judge_skill(context_text, skill_text)
    judge_passed = judge["skill_created"] and judge["grounded_in_run"] and judge["quality"] >= 6
    checks.append({"check": "ai_judge_skill_quality", "passed": judge_passed, "detail": judge})

    return {
        "checks": checks,
        "judge": judge,
        "fixture": {
            "plan_name": fixture["plan_name"],
            "todos": len(fixture["todos"]),
            "subagent_runs": len(fixture["subagent_runs"]),
        },
        "extraction_error": extraction_error,
        "extraction_latency_s": extraction_latency,
        "extraction_attempts": extraction_attempts,
        "nudge_tools": nudge_tool_names,
        "skill_chars": len(skill_text),
    }


def main() -> None:
    """Run the nudge-extraction eval under the sandbox and write the report."""
    faulthandler.dump_traceback_later(900, exit=True)

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)

    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / _SUITE / run_id
    results_dir.mkdir(parents=True)

    missing = [
        name
        for name in ("MAIN_LLM_API_KEY", "AUXILIARY_LLM_API_KEY")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        report: dict[str, Any] = {
            "run_id": run_id,
            "status": "skipped",
            "reason": f"missing env vars: {', '.join(missing)}",
            "aggregate": None,
            "checks": [],
        }
        _write_report(results_dir, report)
        print(f"[evals] {_SUITE} SKIPPED — {report['reason']}", flush=True)
        print(f"[evals] report: {results_dir}", flush=True)
        return

    protected = [
        REPO_ROOT / "skills" / "auto",
        REPO_ROOT / "workspace" / "knowledge",
        REPO_ROOT / "skills" / "skills_snapshot.json",
    ]
    protection_before = _snapshot(protected)

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    started = time.monotonic()
    session_id = f"evals-nudge-{uuid.uuid4().hex[:8]}"
    try:
        outcome = asyncio.run(_run_checks(session_id, results_dir, sandbox))
    except Exception as exc:  # noqa: BLE001 — suite failure is recorded, not raised
        outcome = {
            "checks": [
                {"check": "suite", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
            ],
            "judge": {},
            "fixture": {},
        }
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    pollution = _diff_snapshots(protection_before, _snapshot(protected))
    pollution_passed = not (pollution["added"] or pollution["removed"] or pollution["changed"])
    outcome["checks"].append(
        {"check": "no_repo_pollution", "passed": pollution_passed, "detail": pollution}
    )

    checks = outcome["checks"]
    passed = sum(1 for check in checks if check.get("passed"))
    aggregate = {
        "checks_total": len(checks),
        "checks_passed": passed,
        "pass_rate": round(passed / len(checks), 4) if checks else 0.0,
        "duration_s": round(time.monotonic() - started, 2),
    }
    report = {
        "run_id": run_id,
        "status": "ok",
        "session_id": session_id,
        "aggregate": aggregate,
        "fixture": outcome.get("fixture", {}),
        "extraction": {
            "error": outcome.get("extraction_error", ""),
            "latency_s": outcome.get("extraction_latency_s"),
            "attempts": outcome.get("extraction_attempts", 0),
            "nudge_tools": outcome.get("nudge_tools", []),
            "skill_chars": outcome.get("skill_chars", 0),
        },
        "judge": outcome.get("judge", {}),
        "checks": checks,
        "repo_pollution": pollution,
    }
    _write_report(results_dir, report)

    print(f"\n[evals] {_SUITE} aggregate: {aggregate}", flush=True)
    for check in checks:
        print(f"  [{check['check']}] passed={check['passed']}", flush=True)
    print(f"[evals] report: {results_dir}", flush=True)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
