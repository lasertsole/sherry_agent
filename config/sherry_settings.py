"""Project-root ``sherry.jsonc`` — application-level settings split out of ``.env``.

These settings used to live in the ``.env`` environment file (editable through
the environment-config dialog). They are stored in ``sherry.jsonc`` at the
repository root as JSON5 (comments + trailing commas allowed) and read through
this module instead of ``os.getenv`` — the environment file is no longer a
source for them.

Most settings are top-level keys. Grouped settings live in nested objects and
are addressed with dotted paths: the ``"LANGSMITH"`` object holds the LangSmith
tracing settings and the ``"curator"`` object holds the background
skill-maintenance settings, so ``"LANGSMITH.TRACING_V2"`` resolves to the
``TRACING_V2`` leaf inside the ``"LANGSMITH"`` object.

Values are typed: ``TOOL_CALL_TIMEOUT_MINUTES`` and the curator counters coerce
to numbers, the boolean switches to ``bool``, everything else to ``str``. A
missing file, a missing key, or an unparseable value falls back to the typed
default so the application always boots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json5

SHERRY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "sherry.jsonc"

# Single source of truth for accepted keys and their typed defaults. Nested
# objects in sherry.jsonc are addressed with dotted paths ("GROUP.KEY").
# Secret-class keys (TAVILY_API_KEY) stay in the gitignored .env on purpose —
# sherry.jsonc is git-tracked and must never hold API keys.
SHERRY_SETTING_DEFAULTS: dict[str, Any] = {
    "TOOL_CALL_TIMEOUT_MINUTES": 5,
    "LOG_LEVEL": "INFO",
    "SUBAGENT_TODO_DONE_FUNC": "archive",
    "WORKSPACE_TEMPLATE_LANG": "en",
    "LANGSMITH.TRACING_V2": False,
    "LANGSMITH.API_KEY": "",
    "LANGSMITH.PROJECT": "EMA_AI_agent",
    "curator.enabled": True,
    "curator.interval_hours": 168,
    "curator.min_idle_hours": 2,
    "curator.stale_after_days": 30,
    "curator.archive_after_days": 90,
    "curator.consolidate": True,
    "curator.prune_builtins": True,
}

SHERRY_SETTING_KEYS: tuple[str, ...] = tuple(SHERRY_SETTING_DEFAULTS)

_INT_KEYS = frozenset(
    {
        "TOOL_CALL_TIMEOUT_MINUTES",
        "curator.interval_hours",
        "curator.stale_after_days",
        "curator.archive_after_days",
    }
)
_FLOAT_KEYS = frozenset({"curator.min_idle_hours"})
_BOOL_KEYS = frozenset(
    {
        "LANGSMITH.TRACING_V2",
        "curator.enabled",
        "curator.consolidate",
        "curator.prune_builtins",
    }
)


def _coerce(key: str, value: Any) -> Any:
    """Coerce a parsed value to the key's declared type."""
    if key in _INT_KEYS:
        return int(value)
    if key in _FLOAT_KEYS:
        return float(value)
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == "true"
    return str(value)


def load_sherry_settings() -> dict[str, Any]:
    """Load every setting from ``sherry.jsonc`` merged over the typed defaults.

    Dotted keys resolve through their nested group object. A missing or
    unparseable file silently yields the typed defaults — the application must
    boot even when the config is absent or half-edited.
    """
    settings: dict[str, Any] = dict(SHERRY_SETTING_DEFAULTS)
    try:
        text = SHERRY_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return settings
    try:
        data = json5.loads(text)
    except ValueError:
        return settings
    if not isinstance(data, dict):
        return settings
    for key in SHERRY_SETTING_KEYS:
        node: Any = data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            try:
                settings[key] = _coerce(key, node)
            except (TypeError, ValueError):
                continue
    return settings


def get_sherry_setting(key: str) -> Any:
    """Return one typed setting, falling back to its default."""
    if key not in SHERRY_SETTING_DEFAULTS:
        raise KeyError(f"Unknown sherry setting: {key!r}")
    return load_sherry_settings()[key]


def parse_sherry_value(key: str, raw: str) -> Any:
    """Coerce a raw UI string into the key's typed value.

    Used by the config service when writing values back to ``sherry.jsonc``.
    Raises ``ValueError`` for values that do not fit the key's type.
    """
    if key not in SHERRY_SETTING_DEFAULTS:
        raise KeyError(f"Unknown sherry setting: {key!r}")
    return _coerce(key, raw)
