"""The uniform report every new benchmark suite hands to the harness.

One builder, one aggregate shape, one summary — so seven suites cannot drift
into seven report formats. The envelope matches ``external_bench.publish``'s
contract (``aggregate.resolve_rate`` vs ``baseline.json``'s threshold) and
carries the plan's extra top-level summaries (token / context / code-intel)
when the caller supplies them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = ["build_report", "write_summary_txt"]


def build_report(
    *,
    run_id: str,
    suite: str,
    kind: str,
    results_dir: Path,
    results: list[dict[str, Any]],
    started: float,
    model_info: dict[str, Any],
    benchmark: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The standard envelope. ``results`` are per-task dicts with at least
    ``id`` / ``resolved`` / ``duration_s`` / ``tokens`` / ``failure_kind``."""
    resolved = sum(1 for result in results if result.get("resolved"))
    invalid = [result["id"] for result in results if result.get("invalid_run")]
    valid = len(results) - len(invalid)
    report: dict[str, Any] = {
        "run_id": run_id,
        "suite": suite,
        "kind": kind,
        "results_dir": str(results_dir),
        "model": model_info,
        "benchmark": benchmark,
        "aggregate": {
            "tasks_total": len(results),
            "resolved": resolved,
            "invalid_runs": invalid,
            "resolve_rate": round(resolved / max(1, valid), 4),
            "resolve_rate_basis": "valid runs only (provider failures excluded)",
            "duration_s": round(time.monotonic() - started, 1),
        },
        "token_summary": token_summary(results),
        "context_summary": context_summary(results),
        "tasks": results,
    }
    if extra:
        report.update(extra)
    return report


def token_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The plan's ``token_summary`` block: totals + per-task averages.

    Each task's ``tokens`` dict is ``{"input": n, "output": n}`` (the shape
    ``coding_capability.suite._session_tokens`` returns) optionally carrying a
    ``"cost_usd"`` already computed by the tracker.
    """
    total_input = sum(int((r.get("tokens") or {}).get("input") or 0) for r in results)
    total_output = sum(int((r.get("tokens") or {}).get("output") or 0) for r in results)
    total_cost = sum(float((r.get("tokens") or {}).get("cost_usd") or 0.0) for r in results)
    count = max(1, len(results))
    return {
        "total_tokens": total_input + total_output,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cost_usd": round(total_cost, 4),
        "avg_tokens_per_task": round((total_input + total_output) / count, 1),
        "avg_cost_per_task": round(total_cost / count, 6),
    }


def context_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The plan's ``context_summary`` block from per-task context analysis."""
    rates = [
        float(r["context"]["hit_rate"])
        for r in results
        if isinstance(r.get("context"), dict) and "hit_rate" in r["context"]
    ]
    effective = sum(
        int(r["context"].get("effective_tokens") or 0)
        for r in results
        if isinstance(r.get("context"), dict)
    )
    wasted = sum(
        int(r["context"].get("wasted_tokens") or 0)
        for r in results
        if isinstance(r.get("context"), dict)
    )
    return {
        "avg_hit_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "tasks_analyzed": len(rates),
        "total_effective_tokens": effective,
        "total_wasted_tokens": wasted,
    }


def code_intel_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The plan's ``code_intel_summary`` block from per-task tracker output."""
    by_tool: dict[str, int] = {}
    degraded = 0
    tasks_with_calls = 0
    total = 0
    for result in results:
        usage = result.get("code_intel_usage")
        if not isinstance(usage, dict):
            continue
        calls = usage.get("tool_calls") or {}
        task_total = sum(int(count) for count in calls.values())
        total += task_total
        degraded += int(usage.get("degraded_calls") or 0)
        if task_total:
            tasks_with_calls += 1
        for name, count in calls.items():
            by_tool[name] = by_tool.get(name, 0) + int(count)
    return {
        "tasks_with_code_intel_calls": tasks_with_calls,
        "total_code_intel_calls": total,
        "avg_calls_per_task": round(total / max(1, len(results)), 2),
        "degraded_rate": round(degraded / max(1, total), 4) if total else 0.0,
        "by_tool": dict(sorted(by_tool.items(), key=lambda item: -item[1])),
    }


def write_summary_txt(results_dir: Path, report: dict[str, Any]) -> Path:
    """A human-readable one-pager next to eval_report.json."""
    aggregate = report["aggregate"]
    lines = [
        f"suite: {report['suite']}  run: {report['run_id']}",
        f"resolve rate: {aggregate['resolve_rate']} "
        f"({aggregate['resolved']}/{aggregate['tasks_total']}, "
        f"invalid: {len(aggregate['invalid_runs'])})",
        f"duration: {aggregate['duration_s']}s",
        f"tokens: {json.dumps(report.get('token_summary', {}))}",
    ]
    context = report.get("context_summary")
    if context and context.get("avg_hit_rate") is not None:
        lines.append(f"context hit rate: {context['avg_hit_rate']}")
    lines.append("")
    for task in report["tasks"]:
        lines.append(
            f"  {task['id']}: resolved={task.get('resolved')} "
            f"kind={task.get('failure_kind') or '-'} "
            f"duration={task.get('duration_s')}s"
        )
    path = results_dir / "summary.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
