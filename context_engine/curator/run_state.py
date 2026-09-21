"""Curator run state: the run accumulator, snapshots and report finalization.

Split out of ``orchestrator.py``; the orchestrator re-imports these names so the
public API and the test-pinned private surface stay unchanged.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from context_engine.curator.report import _build_rename_summary, _write_run_report
from context_engine.curator.review import _cached_agent_created_report
from context_engine.curator.state import load_state, save_state
from context_engine.curator.transitions import apply_automatic_transitions


@dataclass
class _ReviewRun:
    start: datetime
    dry_run: bool
    prefix: str
    counts: dict[str, Any]
    auto_summary: str
    before_report: list = field(default_factory=list)
    before_names: set = field(default_factory=set)
    llm_meta: dict = field(default_factory=dict)
    final_summary: str = ""


def _collect_auto_transition_counts(dry_run: bool, start: datetime) -> dict[str, Any]:
    if dry_run:
        try:
            report = _cached_agent_created_report()
            return {
                "checked": len(report),
                "marked_stale": 0,
                "archived": 0,
                "reactivated": 0,
                "seeded": 0,
            }
        except Exception:
            return {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0, "seeded": 0}
    return apply_automatic_transitions(now=start)


def _build_auto_summary(counts: dict[str, Any]) -> str:
    auto_parts = []
    if counts["marked_stale"]:
        auto_parts.append(f"{counts['marked_stale']} marked stale")
    if counts["archived"]:
        auto_parts.append(f"{counts['archived']} archived")
    if counts["reactivated"]:
        auto_parts.append(f"{counts['reactivated']} reactivated")
    return ", ".join(auto_parts) if auto_parts else "no changes"


def _record_intermediate_state(run: _ReviewRun) -> None:
    state = load_state()
    if not run.dry_run:
        state["last_run_at"] = run.start.isoformat()
        state["run_count"] = int(state.get("run_count", 0)) + 1
        # Surface the last maintenance time to the client (manual or auto run).
        state["last_maintenance_at"] = run.start.isoformat()
    state["last_run_summary"] = f"{run.prefix}{run.auto_summary}"
    save_state(state)


def _snapshot_agent_skills() -> tuple[list, set]:
    try:
        before_report = _cached_agent_created_report()
    except Exception:
        before_report = []
    before_names = {
        r["name"] for r in before_report if isinstance(r, dict) and isinstance(r.get("name"), str)
    }
    return before_report, before_names


def _append_rename_summary(run: _ReviewRun) -> str:
    try:
        rename_lines = _build_rename_summary(
            before_names=run.before_names,
            after_report=_cached_agent_created_report(refresh=True),
            tool_calls=run.llm_meta.get("tool_calls", []) or [],
            model_final=run.llm_meta.get("final", "") or "",
        )
        if rename_lines:
            return f"{run.final_summary}\n{rename_lines}"
    except Exception as e:
        logger.debug("Curator rename summary build failed: {}", e)
    return run.final_summary


def _finalize_run(run: _ReviewRun, on_summary: Callable[[str], None] | None) -> dict[str, Any]:
    elapsed = (datetime.now(UTC) - run.start).total_seconds()
    try:
        after_report = _cached_agent_created_report(refresh=True)
    except Exception:
        after_report = []
    try:
        report_path = _write_run_report(
            started_at=run.start,
            elapsed_seconds=elapsed,
            auto_counts=run.counts,
            auto_summary=run.auto_summary,
            before_report=run.before_report,
            before_names=run.before_names,
            after_report=after_report,
            llm_meta=run.llm_meta,
        )
        rp = str(report_path) if report_path else None
    except Exception:
        rp = None
    state = load_state()
    state["last_run_duration_seconds"] = round(elapsed, 2)
    state["last_run_summary"] = run.final_summary
    if rp:
        state["last_report_path"] = rp
    save_state(state)

    if on_summary:
        try:
            on_summary(f"curator: {run.final_summary}")
        except Exception as e:
            logger.debug("Curator on_summary callback failed: {}", e)

    result: dict[str, Any] = {
        "started_at": run.start.isoformat(),
        "auto_transitions": run.counts,
        "summary_so_far": run.auto_summary,
    }
    # When the LLM layer fails (not configured / call exception), carry an error
    # marker so the HTTP handler can surface success=False and the frontend
    # doesn't falsely report "maintenance complete".
    if run.llm_meta.get("error"):
        result["error"] = str(run.llm_meta["error"])
        result["summary_so_far"] = (
            f"{run.auto_summary}; llm: {run.llm_meta.get('summary') or 'error'}"
        )
    return result
