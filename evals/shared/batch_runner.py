"""Batch slicing and resume for the long-running benchmark suites.

Every new suite plans for runs measured in DAYS (the plan's totals are ~539
hours), so the selection layer must answer three questions uniformly:

* ``--batch 3-47`` — an inclusive, zero-based slice of the task list;
* ``--resume`` — skip task ids a PREVIOUS run already graded (the report JSON
  of the newest run under the suite's results root carries them);
* ``--n-tasks N`` — the first N of whatever selection remains.

All three compose (resume applies last, so a batch re-run only grades the
tasks its previous invocation left unfinished).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = ["parse_batch", "apply_batch", "completed_task_ids", "apply_resume"]


def parse_batch(spec: str) -> tuple[int, int]:
    """``"0-49"`` → ``(0, 49)``; a bare ``"5"`` → ``(5, 5)``. Inclusive."""
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", str(spec).strip())
    if not match:
        raise ValueError(f"invalid --batch spec {spec!r}: expected '0-49' or '5'")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        raise ValueError(f"invalid --batch spec {spec!r}: end < start")
    return start, end


def apply_batch(ids: list[str], spec: str | None) -> list[str]:
    """An inclusive zero-based slice of *ids* (out-of-range ends clamp)."""
    if not spec:
        return list(ids)
    start, end = parse_batch(spec)
    return list(ids[start : end + 1])


def completed_task_ids(results_root: Path) -> set[str]:
    """Task ids the newest run under *results_root* already graded.

    A run's ``eval_report.json`` carries its tasks; a task counts as completed
    when it has a verdict — ``resolved`` is a bool OR ``failure_kind`` is
    ``harness``/``provider`` (a TIMEOUT is NOT completed: the task should be
    retried with a longer clock).
    """
    report = _newest_report(results_root)
    if report is None:
        return set()
    completed: set[str] = set()
    for task in report.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        # A TIMEOUT is NOT completed (the task should be retried with a longer
        # clock); a graded pass, a graded miss and a harness/provider failure
        # all carry their verdict.
        if task.get("failure_kind") == "timeout":
            continue
        if task.get("resolved") is True or task.get("failure_kind") in {"", "harness", "provider"}:
            completed.add(task_id)
    return completed


def apply_resume(ids: list[str], results_root: Path) -> list[str]:
    """Drop ids the newest report already carries a verdict for."""
    completed = completed_task_ids(results_root)
    if not completed:
        return list(ids)
    return [task_id for task_id in ids if task_id not in completed]


def _newest_report(results_root: Path) -> dict[str, Any] | None:
    """The newest eval_report.json under a suite's results root (any depth 1)."""
    if not results_root.is_dir():
        return None
    candidates = [
        entry / "eval_report.json"
        for entry in sorted(results_root.iterdir())
        if entry.is_dir() and (entry / "eval_report.json").is_file()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        payload = json.loads(newest.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
