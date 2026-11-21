"""Expert registry — the persisted expert definitions both new modes share.

Expert-panel mode (Mode 2) dispatches work to experts; meeting mode seats them
as participants. Both reference experts by NAME, and one definition is stored
once at ``EXPERTS_DIR/<name>.json`` so several teams/meetings can reuse it.

A definition is intentionally small for the spike: identity, the persona that
the child system prompt (panel) or the discussion call (meeting) carries, and
the spawn contract overrides a TaskFlow step or a meeting seat may apply.

Spike scope (both plans' phase 1): JSON files, load/list/save/validate. The
CRUD API and the management UI are later phases — tests write the JSON directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from loguru import logger

from config import EXPERTS_DIR

__all__ = ["ExpertConfig", "ExpertRegistryError", "load_expert", "list_experts", "save_expert"]

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class ExpertRegistryError(RuntimeError):
    """An expert definition is missing, malformed, or the name is invalid."""


@dataclass(frozen=True)
class ExpertConfig:
    """One persisted expert (identity + persona + spawn contract overrides)."""

    name: str
    description: str = ""
    persona_prompt: str = ""
    model: str | None = None
    thinking: str | None = None
    functional_role: str | None = None
    depth: str | None = None
    skills: list[str] = field(default_factory=list)
    tools_allow: list[str] = field(default_factory=list)
    step_timeout_seconds: float | None = None
    goal_max_turns: int | None = None


def _validate_name(name: str) -> str:
    """A name is a filename stem: non-empty, no separators, bounded length."""
    trimmed = (name or "").strip()
    if not trimmed or len(trimmed) > 64 or not set(trimmed) <= _NAME_CHARS:
        raise ExpertRegistryError(
            f"invalid expert name {name!r}: use 1-64 chars of letters, digits, dot, dash, underscore"
        )
    return trimmed


def _expert_path(name: str) -> Path:
    return EXPERTS_DIR / f"{_validate_name(name)}.json"


def _parse_config(name: str, raw: dict) -> ExpertConfig:
    """Build a config from parsed JSON, ignoring unknown keys (forward compat)."""
    known = {f.name for f in fields(ExpertConfig)}
    data = {key: value for key, value in raw.items() if key in known}
    data["name"] = name
    return ExpertConfig(**data)


def load_expert(name: str) -> ExpertConfig:
    """Load one expert definition; raise a registry error when absent/malformed."""
    path = _expert_path(name)
    if not path.is_file():
        raise ExpertRegistryError(f"expert {name!r} is not registered (no {path})")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpertRegistryError(f"expert {name!r} definition is malformed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExpertRegistryError(f"expert {name!r} definition must be a JSON object")
    return _parse_config(path.stem, raw)


def list_experts() -> list[ExpertConfig]:
    """Every registered expert, ordered by name (missing dir = empty registry)."""
    if not EXPERTS_DIR.is_dir():
        return []
    configs: list[ExpertConfig] = []
    for path in sorted(EXPERTS_DIR.glob("*.json")):
        try:
            configs.append(_parse_config(path.stem, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # One malformed file must not hide the rest of the registry.
            logger.warning("expert registry: skipping malformed {}: {}", path.name, exc)
    return configs


def save_expert(config: ExpertConfig) -> Path:
    """Persist one expert definition (creating the registry dir on demand)."""
    name = _validate_name(config.name)
    if not config.persona_prompt.strip():
        raise ExpertRegistryError(f"expert {name!r} needs a persona_prompt")
    EXPERTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        key: getattr(config, key)
        for key in (
            "description",
            "persona_prompt",
            "model",
            "thinking",
            "functional_role",
            "depth",
            "skills",
            "tools_allow",
            "step_timeout_seconds",
            "goal_max_turns",
        )
    }
    path = EXPERTS_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
