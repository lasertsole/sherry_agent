"""Project-root ``sherry.jsonc`` — application-level settings split out of ``.env``.

The eight keys in :data:`SHERRY_SETTING_KEYS` used to live in the ``.env``
environment file (editable through the environment-config dialog). They are now
stored in ``sherry.jsonc`` at the repository root as JSON5 (comments + trailing
commas allowed) and read through this module instead of ``os.getenv`` — the
environment file is no longer a source for them.

Values are typed: ``TOOL_CALL_TIMEOUT_MINUTES`` coerces to ``int``,
``LANGSMITH_TRACING_V2`` to ``bool``, everything else to ``str``. A missing
file, a missing key, or an unparseable value falls back to the typed default
so the application always boots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json5

SHERRY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "sherry.jsonc"

# Single source of truth for accepted keys and their typed defaults.
# Secret-class keys (TAVILY_API_KEY) stay in the gitignored .env on purpose —
# sherry.jsonc is git-tracked and must never hold API keys.
SHERRY_SETTING_DEFAULTS: dict[str, Any] = {
    "TOOL_CALL_TIMEOUT_MINUTES": 5,
    "LOG_LEVEL": "INFO",
    "SUBAGENT_TODO_DONE_FUNC": "archive",
    "WORKSPACE_TEMPLATE_LANG": "en",
    "LANGSMITH_TRACING_V2": False,
    "LANGSMITH_API_KEY": "",
    "LANGSMITH_PROJECT": "EMA_AI_agent",
}

SHERRY_SETTING_KEYS: tuple[str, ...] = tuple(SHERRY_SETTING_DEFAULTS)

_INT_KEYS = frozenset({"TOOL_CALL_TIMEOUT_MINUTES"})
_BOOL_KEYS = frozenset({"LANGSMITH_TRACING_V2"})


def _coerce(key: str, value: Any) -> Any:
    """Coerce a parsed value to the key's declared type."""
    if key in _INT_KEYS:
        return int(value)
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == "true"
    return str(value)


def load_sherry_settings() -> dict[str, Any]:
    """Load every setting from ``sherry.jsonc`` merged over the typed defaults.

    A missing or unparseable file silently yields the typed defaults — the
    application must boot even when the config is absent or half-edited.
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
        if key in data:
            try:
                settings[key] = _coerce(key, data[key])
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
