"""Reusable TaskFlow PLAN templates (WorkMode stage 4).

A template captures the *decomposition structure* of a finished flow — the
step ids, tasks, dependencies, roles and acceptance criteria — WITHOUT runtime
state (child session keys, retry counters, results, timestamps). This is the
planning-layer complement to PTC/LDW replay, which reuses an EXECUTION sequence:
load a template to recreate the step skeleton, then let the children execute it
fresh (optionally pre-run via PTC).

Storage: one JSON file per template at
``TASKFLOW_TEMPLATES_DIR/<name>.json`` (APP_HOME-scoped, shared across sessions).
Writes are atomic (tmp file + ``os.replace``) under ``file_write_lock``; reads
are lock-free with a safe fallback. A name is a filename stem (1-64 safe chars)
so a template can never escape the directory.

Fail-open contract: a missing/corrupt file is reported, never raised into a
tool call; every public function returns a structured result an agent can show.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from config import TASKFLOW_TEMPLATES_DIR
from loguru import logger

__all__ = [
    "TemplateStoreError",
    "delete_template",
    "list_templates",
    "load_template",
    "save_template",
]

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Step fields captured in a template. Runtime fields (status, retry_count,
#: child_session_key, results, timestamps, judge_feedback …) are deliberately
#: EXCLUDED — a loaded step is an empty shell to run afresh.
_STEP_FIELDS = (
    "step_id",
    "task",
    "depends_on",
    "functional_role",
    "judge_criteria",
    "validation_criteria",
    "response_schema",
    "expected_params",
    "input_bindings",
    "file_deps",
    "retry_policy",
    "priority",
    "step_model",
    "step_timeout_seconds",
    "self_evaluate",
    "self_eval_min_score",
    "self_eval_max_retries",
)


class TemplateStoreError(ValueError):
    """Invalid template name or payload."""


def _validate_name(name: str) -> str:
    trimmed = (name or "").strip()
    if not _NAME_RE.fullmatch(trimmed) or trimmed in {".", ".."}:
        raise TemplateStoreError(
            f"invalid template name {name!r}: use 1-64 letters, digits, dot, dash, underscore"
        )
    return trimmed


def _templates_dir() -> Path:
    # Module-level seam so tests can redirect storage to a tmp dir.
    return TASKFLOW_TEMPLATES_DIR


def _template_path(name: str) -> Path:
    return _templates_dir() / f"{_validate_name(name)}.json"


def _extract_steps(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("steps") if isinstance(state, dict) else None
    if not isinstance(raw, list):
        return []
    steps: list[dict[str, Any]] = []
    for step in raw:
        if not isinstance(step, dict):
            continue
        clean = {key: step[key] for key in _STEP_FIELDS if key in step}
        if clean.get("step_id"):
            steps.append(clean)
    return steps


async def save_template(
    name: str,
    state: dict[str, Any],
    *,
    description: str = "",
) -> dict[str, Any]:
    """Save a flow's step skeleton as a template. Returns a summary dict."""
    from agent.tools.pub_base.file_lock import file_write_lock

    path = _template_path(name)
    steps = _extract_steps(state)
    if not steps:
        raise TemplateStoreError("flow has no steps to save as a template")
    payload = {
        "name": _validate_name(name),
        "description": str(description or ""),
        "steps": steps,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_write_lock(path):
            fd, tmp_name = tempfile.mkstemp(prefix=".tmpl-", suffix=".json", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
    except OSError as exc:
        logger.warning("template save failed for {}: {}", name, exc)
        return {"success": False, "message": f"template save failed: {exc}"}
    return {
        "success": True,
        "name": payload["name"],
        "steps": len(steps),
        "description": payload["description"],
    }


def load_template(name: str) -> dict[str, Any] | None:
    """Load a template's step skeleton; ``None`` when missing or corrupt."""
    path = _template_path(name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("template not found: {}", name)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("template load failed for {}: {}", name, exc)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        logger.warning("template {}: malformed payload", name)
        return None
    steps = [step for step in raw["steps"] if isinstance(step, dict)]
    return {
        "name": raw.get("name") or _validate_name(name),
        "description": str(raw.get("description") or ""),
        "steps": steps,
    }


def list_templates() -> list[dict[str, Any]]:
    """List saved templates (name + step count), newest first by mtime."""
    directory = _templates_dir()
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            count = len(raw.get("steps") or []) if isinstance(raw, dict) else 0
        except (OSError, json.JSONDecodeError):
            continue
        result.append(
            {
                "name": path.stem,
                "steps": count,
                "description": str(raw.get("description") or "") if isinstance(raw, dict) else "",
            }
        )
    result.sort(key=lambda item: item["name"])
    return result


async def delete_template(name: str) -> bool:
    """Delete a template; True only when a file was actually removed."""
    from agent.tools.pub_base.file_lock import file_write_lock

    path = _template_path(name)
    try:
        with file_write_lock(path):
            if not path.exists():
                return False
            path.unlink()
            return True
    except OSError as exc:
        logger.warning("template delete failed for {}: {}", name, exc)
        return False
