"""Plan-aware knowledge storage for plan extraction.

Sidecar JSON documents live under ``config.path.PLAN_KNOWLEDGE_DIR``
(``workspace/knowledge/plans/<plan-name>/``): one ``task-<position>.json`` per
todo, one ``wave-<index>.json`` per wave, and one ``plan-summary.json`` per
plan. The plan file and ``todos.db`` are read-only for this layer.

Writes raise :class:`ValueError` on invalid input; the tool layer converts that
into the repo's error-as-text contract. Reads are fail-open: a missing
directory or a malformed document never raises, it yields an empty result.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from config.path import PLAN_KNOWLEDGE_DIR

_KNOWLEDGE_ROOT: Path = PLAN_KNOWLEDGE_DIR

_SCHEMA_VERSION = 1

Layer = Literal["task", "wave", "plan"]


def _parse_index(suffix: str) -> int | None:
    """Parse a ``task-<n>`` / ``wave-<n>`` filename suffix into an int."""
    try:
        return int(suffix)
    except ValueError:
        return None


def _read_json(file_path: Path) -> object | None:
    """Read and parse one JSON document; None when absent or malformed."""
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _filename_for(layer: Layer, position: int | None, wave_index: int | None) -> str:
    """Map a layer selector to its knowledge filename."""
    if layer == "task":
        if position is None:
            raise ValueError("position is required for the task layer")
        return f"task-{position}.json"
    if layer == "wave":
        if wave_index is None:
            raise ValueError("wave_index is required for the wave layer")
        return f"wave-{wave_index}.json"
    if layer == "plan":
        return "plan-summary.json"
    raise ValueError(f"unknown layer: {layer}")


def _numbered(title: str, items: list) -> list[str]:
    """Render ``title`` + 1-based numbered items; empty input renders nothing."""
    if not items:
        return []
    return [f"\n## {title}", *(f"  {index}. {item}" for index, item in enumerate(items, 1))]


def _format_plan(plan_name: str, all_data: dict, *, full: bool) -> str:
    plan = all_data.get("plan")
    if not plan:
        return f"No plan summary found for: {plan_name}"
    lines = [
        f"# Knowledge: {plan_name}",
        f"Method: {plan.get('method', 'unknown')}",
        f"Total waves: {plan.get('total_waves', '?')}",
        f"Total tasks: {plan.get('total_tasks', '?')}",
    ]
    lines += _numbered("Key Failures", plan.get("key_failures", [])[:5])
    lines += _numbered("Key Successes", plan.get("key_successes", [])[:5])
    patterns = plan.get("reusable_patterns", [])
    if patterns:
        lines.append("\n## Reusable Patterns")
        lines.extend(f"  - {pattern}" for pattern in patterns)
    if full:
        return "\n".join(lines)
    tasks = all_data.get("tasks", {})
    if tasks:
        lines.append("\n## Task Index")
        for position in sorted(tasks):
            task = tasks[position]
            lines.append(
                f"  Task {position}: method={task.get('method', 'unknown')} "
                f"(failures={len(task.get('failure_set', []))}, "
                f"steps={len(task.get('success_path', []))})"
            )
        lines.append(
            f"\nUse knowledge(action='read', plan_name='{plan_name}', "
            "layer='task', position=N) for task detail."
        )
    return "\n".join(lines)


def _format_task(plan_name: str, all_data: dict, position: int | None) -> str:
    if position is None:
        return "Error: layer='task' requires position"
    task = all_data.get("tasks", {}).get(position)
    if not task:
        return f"No knowledge found for task {position} in {plan_name}"
    lines = [
        f"# Task {position} Knowledge: {plan_name}",
        f"Method: {task.get('method', 'unknown')}",
    ]
    failures = task.get("failure_set", [])
    lines += _numbered("Failure Set", failures) if failures else ["\n## Failure Set: (none)"]
    steps = task.get("success_path", [])
    lines += _numbered("Success Path", steps) if steps else ["\n## Success Path: (none)"]
    runs = task.get("subagent_runs", [])
    if runs:
        lines.append("\n## Subagent Runs")
        lines.extend(
            f"  - {run.get('task_name', '?')}: outcome={run.get('outcome', '?')}" for run in runs
        )
    return "\n".join(lines)


def _format_wave(plan_name: str, all_data: dict, wave_index: int | None) -> str:
    if wave_index is None:
        return "Error: layer='wave' requires wave_index"
    wave = all_data.get("waves", {}).get(wave_index)
    if not wave:
        return f"No knowledge found for wave {wave_index} in {plan_name}"
    lines = [
        f"# Wave {wave_index} Knowledge: {plan_name}",
        f"Task count: {wave.get('task_count', '?')}",
    ]
    lines += _numbered("Failure Patterns", wave.get("wave_failure_patterns", []))
    lines += _numbered("Success Patterns", wave.get("wave_success_patterns", []))
    summaries = wave.get("tasks_summary", [])
    if summaries:
        lines.append("\n## Tasks Summary")
        lines.extend(
            f"  - Task {summary.get('position')}: method={summary.get('method', '?')}"
            for summary in summaries
        )
    return "\n".join(lines)


class KnowledgeStore:
    """Filesystem-backed store for per-plan extraction knowledge."""

    @staticmethod
    async def write(
        layer: Layer,
        plan_name: str,
        data: dict,
        position: int | None = None,
        wave_index: int | None = None,
    ) -> str:
        """Persist one knowledge document; returns the written file path."""
        filename = _filename_for(layer, position, wave_index)
        dir_path = _KNOWLEDGE_ROOT / plan_name
        dir_path.mkdir(parents=True, exist_ok=True)
        payload = {
            **data,
            "schema_version": _SCHEMA_VERSION,
            "plan_name": plan_name,
            "extracted_at": datetime.now().strftime("%Y%m%d%H%M%S"),
        }
        file_path = dir_path / filename
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(file_path)

    @staticmethod
    def read_all(plan_name: str) -> dict:
        """Read every knowledge document for a plan into task/wave/plan buckets."""
        dir_path = _KNOWLEDGE_ROOT / plan_name
        if not dir_path.is_dir():
            return {}
        result: dict = {"tasks": {}, "waves": {}, "plan": None}
        for file_path in sorted(dir_path.iterdir()):
            if file_path.suffix != ".json":
                continue
            data = _read_json(file_path)
            if data is None:
                continue
            stem = file_path.stem
            if stem.startswith("task-"):
                position = _parse_index(stem.removeprefix("task-"))
                if position is not None:
                    result["tasks"][position] = data
            elif stem.startswith("wave-"):
                wave_index = _parse_index(stem.removeprefix("wave-"))
                if wave_index is not None:
                    result["waves"][wave_index] = data
            elif stem == "plan-summary":
                result["plan"] = data
        return result

    @staticmethod
    def read_summary(plan_name: str) -> dict | None:
        """Read only ``plan-summary.json``; None when absent or malformed."""
        data = _read_json(_KNOWLEDGE_ROOT / plan_name / "plan-summary.json")
        return data if isinstance(data, dict) else None

    @staticmethod
    def read_formatted(
        plan_name: str,
        layer: str | None = None,
        position: int | None = None,
        wave_index: int | None = None,
    ) -> str:
        """Read knowledge as formatted text for tool output.

        - No layer: return plan summary + task index (overview)
        - layer="plan": return full plan summary
        - layer="task" + position: return full task detail
        - layer="wave" + wave_index: return full wave detail
        """
        all_data = KnowledgeStore.read_all(plan_name)
        if not all_data:
            return f"No knowledge found for plan: {plan_name}"
        if layer == "task":
            return _format_task(plan_name, all_data, position)
        if layer == "wave":
            return _format_wave(plan_name, all_data, wave_index)
        if layer in (None, "plan"):
            return _format_plan(plan_name, all_data, full=layer == "plan")
        return f"Unknown layer: {layer}"

    @staticmethod
    def list_plans() -> list[str]:
        """List plan names that have a knowledge directory, sorted."""
        if not _KNOWLEDGE_ROOT.is_dir():
            return []
        return sorted(entry.name for entry in _KNOWLEDGE_ROOT.iterdir() if entry.is_dir())


__all__ = ["KnowledgeStore", "Layer"]
