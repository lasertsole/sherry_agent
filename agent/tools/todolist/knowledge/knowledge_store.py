"""Plan-aware knowledge storage for plan extraction.

Sidecar JSON documents live under ``config.path.PLAN_KNOWLEDGE_DIR`` in one
directory per **plan identity** (``<plan_key>/`` — see ``identity.py``): one
``task-<position>.json`` per todo, one ``wave-<index>.json`` per wave, one
``plan-summary.json`` per plan, plus a ``meta.json`` recording the
human-readable plan name and canonical reference for diagnostics. The internal
file layout is unchanged; only the directory name derives from the canonical
plan path instead of the bare plan name.

That makes same-named plans in different sessions physically isolated, while
sessions collaborating on one plan path share a directory. Legacy name-keyed
directories stay readable: when the key directory is absent, reads fall back
to ``<PLAN_KNOWLEDGE_DIR>/<plan-name>/``; writes always land in the key
directory. ``clear_session_plan_knowledge`` removes the session's private
identity directories and keeps ones shared through boulder ``session_ids``.

Writes raise :class:`ValueError` on invalid input; the tool layer converts that
into the repo's error-as-text contract. Reads are fail-open: a missing
directory or a malformed document never raises, it yields an empty result.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from config import path as config_path
from config.path import PLAN_KNOWLEDGE_DIR

from .identity import PlanIdentity, associated_plan_identities, private_plan_identities

_KNOWLEDGE_ROOT: Path = PLAN_KNOWLEDGE_DIR

_SCHEMA_VERSION = 1
_META_FILENAME = "meta.json"

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


def _relative_posix(path: Path) -> str:
    """Render *path* relative to ``ROOT_DIR`` when possible (meta diagnostics)."""
    try:
        return path.relative_to(config_path.ROOT_DIR).as_posix()
    except ValueError:
        return path.as_posix()


def _write_meta(dir_path: Path, identity: PlanIdentity) -> None:
    """Refresh ``meta.json`` so a key directory stays human-traceable."""
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "key": identity.key,
        "plan_name": identity.plan_name,
        "plan_ref": _relative_posix(identity.plan_ref) if identity.plan_ref else None,
        "updated_at": datetime.now().strftime("%Y%m%d%H%M%S"),
    }
    (dir_path / _META_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _existing_dir(identity: PlanIdentity) -> Path | None:
    """The identity's key directory, else the legacy name-keyed directory."""
    key_dir = _KNOWLEDGE_ROOT / identity.key
    if key_dir.is_dir():
        return key_dir
    legacy = _KNOWLEDGE_ROOT / identity.plan_name
    if legacy.is_dir():
        return legacy
    return None


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
        identity: PlanIdentity,
        *,
        layer: Layer,
        data: dict,
        position: int | None = None,
        wave_index: int | None = None,
    ) -> str:
        """Persist one knowledge document under the identity key; returns its path."""
        filename = _filename_for(layer, position, wave_index)
        dir_path = _KNOWLEDGE_ROOT / identity.key
        dir_path.mkdir(parents=True, exist_ok=True)
        payload = {
            **data,
            "schema_version": _SCHEMA_VERSION,
            "plan_name": identity.plan_name,
            "extracted_at": datetime.now().strftime("%Y%m%d%H%M%S"),
        }
        file_path = dir_path / filename
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_meta(dir_path, identity)
        return str(file_path)

    @staticmethod
    def read_all(identity: PlanIdentity) -> dict:
        """Read every knowledge document for a plan into task/wave/plan buckets."""
        dir_path = _existing_dir(identity)
        if dir_path is None:
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
    def read_summary(identity: PlanIdentity) -> dict | None:
        """Read only ``plan-summary.json``; None when absent or malformed."""
        dir_path = _existing_dir(identity)
        if dir_path is None:
            return None
        data = _read_json(dir_path / "plan-summary.json")
        return data if isinstance(data, dict) else None

    @staticmethod
    def read_formatted(
        identity: PlanIdentity,
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
        plan_name = identity.plan_name
        all_data = KnowledgeStore.read_all(identity)
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
    def has_knowledge(identity: PlanIdentity) -> bool:
        """True when the identity has a key or legacy directory on disk."""
        return _existing_dir(identity) is not None

    @staticmethod
    def list_plans(session_id: str) -> list[PlanIdentity]:
        """This session's associated plans that currently hold knowledge.

        Semantics changed with plan identity: this is no longer "every key
        directory on disk" (which leaked other sessions' plan names) but the
        session's associated identities intersected with the directories that
        exist. Sorted by readable name then key.
        """
        plans = [
            identity
            for identity in associated_plan_identities(session_id)
            if KnowledgeStore.has_knowledge(identity)
        ]
        return sorted(plans, key=lambda identity: (identity.plan_name, identity.key))

    @staticmethod
    def purge(identity: PlanIdentity) -> bool:
        """Delete the identity's key directory; legacy dirs are never removed."""
        dir_path = _KNOWLEDGE_ROOT / identity.key
        if not dir_path.is_dir():
            return False
        shutil.rmtree(dir_path)
        return True


def clear_session_plan_knowledge(session_id: str) -> int:
    """Delete this session's private plan-knowledge directories; return the count.

    Called by ``server.DAO.messages.clear_session``. Intent: session teardown
    removes the knowledge produced for the session's own plans, while plans
    shared with another session through boulder ``session_ids`` are retained so
    collaboration survives a member leaving. Legacy name-keyed directories are
    never deleted (read-only compatibility window).
    """
    removed = 0
    for identity in private_plan_identities(session_id):
        if KnowledgeStore.purge(identity):
            removed += 1
    return removed


__all__ = ["KnowledgeStore", "Layer", "clear_session_plan_knowledge"]
