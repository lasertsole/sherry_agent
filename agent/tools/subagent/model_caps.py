"""Per-model structured-output degradation labels (persisted).

A schema-bound child enforces its JSON shape at generation time. The DEFAULT is
LangChain's structured output (``create_agent(response_format=<schema>)``: a
structured-output tool with a forced ``tool_choice``). Some providers reject
that combination — DeepSeek in thinking mode answers ``400 Thinking mode does
not support this tool_choice`` — so the runner degrades ON ERROR, one step at a
time:

* level 0 — LangChain structured output (the default, untouched);
* level 1 — the child sends ``response_format={"type": "json_object"}`` itself,
  keeps the schema in its prompt, and the result is validated locally;
* level 2 — level 1 plus thinking OFF (some providers reject the forced
  ``tool_choice`` only while thinking).

The level is PERSISTED per ``(provider, model)`` so a model that already
degraded is never probed again: every later subagent starts at the learned
level instead of paying for the same rejected request (the user's "avoid
repeated degradation wasting tokens" rule). A missing/corrupt file reads as
"every model starts at 0", and a failed write is fail-open (the level still
applies for this process).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

from loguru import logger

__all__ = [
    "DEFAULT_LEVEL",
    "MAX_LEVEL",
    "clear_cache",
    "escalate",
    "get_level",
    "level_label",
]

DEFAULT_LEVEL = 0
MAX_LEVEL = 2

_lock = threading.Lock()
#: Parsed ``{key: {"level": int, "reason": str, "updated_at": str}}``; ``None``
#: until the first read (a missing file caches as ``{}``).
_cache: dict[str, dict[str, Any]] | None = None


def _key(provider: str | None, model: str | None) -> str:
    return f"{(provider or '').strip().lower()}/{(model or '').strip()}"


def _path():
    # Imported lazily so a test that repoints SRC_DIR sees the new path.
    from config.path import subagent_model_caps_path

    return subagent_model_caps_path()


def _load_locked() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cache = {}
        return _cache
    if not isinstance(raw, dict):
        _cache = {}
        return _cache
    floor: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            level = int(entry.get("level", DEFAULT_LEVEL))
        except (TypeError, ValueError):
            continue
        floor[str(key)] = {
            "level": max(DEFAULT_LEVEL, min(MAX_LEVEL, level)),
            "reason": str(entry.get("reason") or "")[:300],
            "updated_at": str(entry.get("updated_at") or ""),
        }
    _cache = floor
    return _cache


def _save_locked(entries: dict[str, dict[str, Any]]) -> None:
    from agent.tools.pub_base.atomic_write import atomic_write_text_no_follow

    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text_no_follow(path, json.dumps(entries, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - a label write must never fail a spawn
        logger.debug("subagent model caps: write failed (fail-open): {}", exc)


def get_level(provider: str | None, model: str | None) -> int:
    """The persisted strategy level for one ``(provider, model)``."""
    with _lock:
        entry = _load_locked().get(_key(provider, model))
    return int(entry["level"]) if entry else DEFAULT_LEVEL


def escalate(provider: str | None, model: str | None, level: int, reason: str = "") -> int:
    """Raise the label to ``level`` (monotonic) and persist; returns the level."""
    target = max(DEFAULT_LEVEL, min(MAX_LEVEL, int(level)))
    key = _key(provider, model)
    with _lock:
        entries = _load_locked()
        current = int(entries.get(key, {}).get("level", DEFAULT_LEVEL))
        if target <= current:
            return current
        entries[key] = {
            "level": target,
            "reason": (reason or "")[:300],
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _save_locked(entries)
    logger.warning(
        "subagent model caps: {} degraded to level {} ({})",
        key,
        target,
        (reason or "")[:160],
    )
    return target


def level_label(level: int) -> str:
    """A short human label for logs/records."""
    return {
        0: "langchain-structured",
        1: "self-json-object",
        2: "self-json-object+thinking-off",
    }.get(int(level), f"level-{level}")


def clear_cache() -> None:
    """Drop the in-process cache (tests / an externally edited file)."""
    global _cache
    with _lock:
        _cache = None
