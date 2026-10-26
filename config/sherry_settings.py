"""Project-root ``sherry.jsonc`` — application-level settings split out of ``.env``.

These settings used to live in the ``.env`` environment file (editable through
the environment-config dialog). They are stored in ``sherry.jsonc`` at the
repository root as JSON5 (comments + trailing commas allowed) and read through
this module instead of ``os.getenv`` — the environment file is no longer a
source for them.

Most settings are top-level keys. Grouped settings live in nested objects and
are addressed with dotted paths: the ``"curator"`` object holds the background
skill-maintenance settings, so ``"curator.enabled"`` resolves to the ``enabled``
leaf inside the ``"curator"`` object.

Values are typed: ``TOOL_CALL_TIMEOUT_MINUTES`` and the curator counters coerce
to numbers, the boolean switches to ``bool``, everything else to ``str``. A
missing file, a missing key, or an unparseable value falls back to the typed
default so the application always boots.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import json5

_CODE_ROOT = Path(__file__).resolve().parents[1]
_APP_HOME_RAW = os.environ.get("SHERRY_HOME", "").strip()
APP_HOME = Path(_APP_HOME_RAW).expanduser().resolve() if _APP_HOME_RAW else _CODE_ROOT

SHERRY_CONFIG_PATH = APP_HOME / "sherry.jsonc"
if APP_HOME != _CODE_ROOT and not SHERRY_CONFIG_PATH.exists():
    # Seed the editable copy from the shipped file so the App Config panel has the
    # full key list (comments included) to write into.
    template = _CODE_ROOT / "sherry.jsonc"
    if template.exists():
        try:
            SHERRY_CONFIG_PATH.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

# Single source of truth for accepted keys and their typed defaults. Nested
# objects in sherry.jsonc are addressed with dotted paths ("GROUP.KEY").
# Secret-class keys live in the gitignored .env / .models / .network on purpose —
# sherry.jsonc is git-tracked and must never hold an API key.
SHERRY_SETTING_DEFAULTS: dict[str, Any] = {
    "TOOL_CALL_TIMEOUT_MINUTES": 5,
    "LOG_LEVEL": "INFO",
    # Persisted process-level default project directory ("where the agent
    # works"). Empty = unset -> ROOT_DIR. SHERRY_PROJECT_DIR still wins over
    # this key so an operator can repoint a single launch (see
    # config/path.py::resolve_default_project_dir).
    "project_dir": "",
    "WORKSPACE_TEMPLATE_LANG": "en",
    # Global heartbeat scheduler switch (the Heartbeat panel's toggle). Off by
    # default: the periodic heartbeat is opt-in, and the boot path reads this
    # value, so a session that never touches the switch never wakes the agent.
    "heartbeat.enabled": False,
    # LangSmith tracing moved to the .network file (LANGSMITH_* keys) — it is a
    # network service, and that file is gitignored while this one is tracked.
    "curator.enabled": True,
    "curator.interval_hours": 168,
    "curator.min_idle_hours": 2,
    "curator.stale_after_days": 30,
    "curator.archive_after_days": 90,
    "curator.consolidate": True,
    "curator.prune_builtins": True,
    # User-configurable lifecycle hooks (agent/middlewares/user_hooks): the
    # master switch for the ``hooks`` section's commands. OFF by default and
    # registered here so the App Config panel can flip it and the shipped
    # sherry.jsonc stays complete; the per-event hook LISTS are read by
    # ``load_sherry_hooks`` (they are lists of objects, not scalars).
    "hooks.enabled": False,
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
        "heartbeat.enabled",
        "hooks.enabled",
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


def load_sherry_list_setting(key: str) -> list[str]:
    """Load a top-level string-list setting from ``sherry.jsonc``.

    List settings are intentionally outside the typed scalar registry above:
    the caller owns the default and merges these user entries itself (see
    ``path_utils._get_yolo_deny_paths``). A missing file/key, a non-list
    value, or an unparseable document yields an empty list — the same
    always-boot fallback the scalar loader uses.
    """
    try:
        data = json5.loads(SHERRY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


#: Lifecycle-hook event vocabulary (``hooks.<Event>`` in sherry.jsonc). The
#: parser accepts all of them so a config written ahead of the implementation
#: validates; the dispatcher serves the Step-1 subset and logs the rest.
HOOK_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "UserPromptSubmit",
    "SessionStart",
)

#: Fields one hook entry may carry (anything else is ignored).
_HOOK_ENTRY_KEYS: frozenset[str] = frozenset({"command", "matcher", "timeout_ms", "blocking"})


def load_sherry_hooks() -> dict[str, Any]:
    """Load the ``hooks`` section of ``sherry.jsonc``, validated.

    Shape::

        {"enabled": false, "PostToolUse": [{"command": ..., "matcher": "*",
                                            "timeout_ms": 5000, "blocking": false}]}

    Each entry needs a non-empty ``command`` (a string; split with
    ``shlex.split`` at dispatch time, never a shell) and defaults
    ``matcher="*"`` / ``timeout_ms=USER_HOOKS["default_timeout_ms"]`` /
    ``blocking=False``. ``timeout_ms`` is clamped to
    ``USER_HOOKS["max_timeout_ms"]`` and at most
    ``USER_HOOKS["max_hooks_per_event"]`` entries survive per event — extras
    are dropped with a warning.

    Fail-soft like the rest of this module: a missing key/file, a non-dict
    value or an entry without a usable command is skipped, never raised — the
    application must boot on a half-edited config. Returns
    ``{"enabled": bool, <Event>: [spec, ...]}`` with the events that carry at
    least one usable spec; an absent/disabled section yields ``{"enabled": False}``.
    """
    from config.features import USER_HOOKS

    try:
        data = json5.loads(SHERRY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": False}
    if not isinstance(data, dict):
        return {"enabled": False}
    section = data.get("hooks")
    if not isinstance(section, dict):
        return {"enabled": False}

    result: dict[str, Any] = {"enabled": section.get("enabled") is True}
    for event in HOOK_EVENTS:
        raw_entries = section.get(event)
        if not isinstance(raw_entries, list):
            continue
        specs: list[dict[str, Any]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            command = raw.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            matcher = raw.get("matcher")
            timeout = raw.get("timeout_ms")
            try:
                timeout_ms = (
                    int(timeout) if timeout is not None else USER_HOOKS["default_timeout_ms"]
                )
            except (TypeError, ValueError):
                timeout_ms = USER_HOOKS["default_timeout_ms"]
            timeout_ms = max(1, min(timeout_ms, USER_HOOKS["max_timeout_ms"]))
            specs.append(
                {
                    "command": command.strip(),
                    "matcher": matcher.strip() if isinstance(matcher, str) else "*",
                    "timeout_ms": timeout_ms,
                    "blocking": raw.get("blocking") is True,
                    "unknown_keys": sorted(set(raw) - _HOOK_ENTRY_KEYS),
                }
            )
        if len(specs) > USER_HOOKS["max_hooks_per_event"]:
            # Lazy loguru import, the pattern config/path.py uses: the config
            # layer must stay importable before the logging sinks exist.
            from loguru import logger

            logger.warning(
                "sherry.jsonc hooks.{}: {} entries declared, only the first {} are kept",
                event,
                len(specs),
                USER_HOOKS["max_hooks_per_event"],
            )
            specs = specs[: USER_HOOKS["max_hooks_per_event"]]
        if specs:
            result[event] = specs
    return result


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
