"""Project-root ``.network`` — the network service configuration file.

These settings used to live elsewhere: the Tavily + SearXNG web-search backends
and the browser agent's switches came from the ``.env`` environment file (the
模型联网 / 模型配置 panels wrote them through ``PUT /env``), and the LangSmith
tracing settings lived in ``sherry.jsonc``. They are stored in ``.network``
(``APP_HOME``, i.e. the repository root — or the app-data directory under
``SHERRY_HOME``) as JSON5 (comments + trailing commas allowed) and read through
this module. The MODEL (LLM) configuration lives in ``.models``; what remains in
``.env`` is the non-secret application configuration (sandbox, gateway, login).

Two access paths exist on purpose:

* :func:`get_network_setting` — the typed read (the boot's LangSmith init).
* :func:`hydrate_network_env` — materialises every key into ``os.environ`` at
  import, so the existing readers (``os.getenv("TAVILY_API_KEY")`` in the search
  tool, the import-time feature snapshots in ``config/features`` — the browser
  agent's switches and the SearXNG connection) keep observing these values
  exactly as they did when they lived in ``.env``. It never overwrites a key
  that is already set, and it runs BEFORE the environment file is (re)loaded —
  so ``.network`` wins over a legacy ``.env`` entry by order (see
  ``config.path.load_env``).

An empty value is written through as an empty string (exactly what dotenv does
for a bare ``KEY =`` line); the one exception is ``SHERRY_BROWSER_HEADLESS``,
whose code default is dynamic (headed when a DISPLAY exists) and which is
therefore left ABSENT when the file leaves it empty.

A missing file is seeded from the shipped ``.network.example``, with values
overlaid from the legacy homes — ``.env`` (search + browser keys) and
``sherry.jsonc``'s ``LANGSMITH`` object (tracing) — so an upgrade keeps its
configuration.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import json5

_CODE_ROOT = Path(__file__).resolve().parents[1]
_APP_HOME_RAW = os.environ.get("SHERRY_HOME", "").strip()
APP_HOME = Path(_APP_HOME_RAW).expanduser().resolve() if _APP_HOME_RAW else _CODE_ROOT

NETWORK_CONFIG_PATH = APP_HOME / ".network"
NETWORK_CONFIG_TEMPLATE = _CODE_ROOT / ".network.example"

# Single source of truth for accepted keys and their typed defaults. The keys
# mirror the historical .env / sherry.jsonc spelling one-for-one, so a migration
# is a value copy and the panels' prefix grouping keeps working.
NETWORK_SETTING_DEFAULTS: dict[str, Any] = {
    # Web-search backends
    "TAVILY_API_KEY": "",
    "SEARXNG_URL": "",
    "SEARXNG_PROXY": "",
    "SEARXNG_ENGINES": "",
    "SEARXNG_CATEGORIES": "",
    "SEARXNG_VERIFY_TLS": True,
    # Browser agent
    "SHERRY_BROWSER_AGENT_ENABLED": 0,
    "SHERRY_BROWSER_HEADLESS": "",
    "SHERRY_BROWSER_EXECUTABLE": "",
    "SHERRY_BROWSER_ALLOW_EVALUATE": 0,
    # LangSmith tracing
    "LANGSMITH_TRACING_V2": False,
    "LANGSMITH_API_KEY": "",
    "LANGSMITH_PROJECT": "EMA_AI_agent",
}

NETWORK_SETTING_KEYS: tuple[str, ...] = tuple(NETWORK_SETTING_DEFAULTS)

_INT_KEYS = frozenset(
    {
        "SHERRY_BROWSER_AGENT_ENABLED",
        "SHERRY_BROWSER_ALLOW_EVALUATE",
    }
)
_BOOL_KEYS = frozenset(
    {
        "SEARXNG_VERIFY_TLS",
        "LANGSMITH_TRACING_V2",
    }
)
#: Keys whose reader default depends on the host (browser headless follows
#: DISPLAY): an empty file value must stay ABSENT from the environment, or the
#: hydration would pin it to the code's fallback.
_EMPTY_MEANS_ABSENT = frozenset({"SHERRY_BROWSER_HEADLESS"})

#: The sherry.jsonc object each tracing key came from (legacy migration only).
_LANGSMITH_LEGACY_OBJECT = "LANGSMITH"
_LANGSMITH_LEGACY_LEAVES = {
    "LANGSMITH_TRACING_V2": "TRACING_V2",
    "LANGSMITH_API_KEY": "API_KEY",
    "LANGSMITH_PROJECT": "PROJECT",
}

# ``"KEY": value,`` — the line shape of the template and of every write.
_TEMPLATE_ASSIGN_RE = re.compile(r'^(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*)(.+?)(\s*,?\s*)$')
# ``KEY = value`` — the legacy .env shape (quotes stripped on read).
_DOTENV_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_DOTENV_COMMENT_RE = re.compile(r"^\s*#")


def _coerce(key: str, value: Any) -> Any:
    """Coerce a parsed value to the key's declared type."""
    if key in _INT_KEYS:
        return int(value)
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return str(value)


def parse_network_value(key: str, raw: str) -> Any:
    """Coerce a raw UI string into the key's typed value (write path)."""
    if key not in NETWORK_SETTING_DEFAULTS:
        raise KeyError(f"Unknown network setting: {key!r}")
    return _coerce(key, raw)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _legacy_env_values() -> dict[str, str]:
    """Values of the moved keys as a legacy ``.env`` spells them (migration only)."""
    try:
        text = (APP_HOME / ".env").read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        if _DOTENV_COMMENT_RE.match(line):
            continue
        match = _DOTENV_ASSIGN_RE.match(line)
        if not match or match.group(1) not in NETWORK_SETTING_DEFAULTS:
            continue
        value = _strip_quotes(match.group(2).strip())
        if value:
            found[match.group(1)] = value
    return found


def _legacy_langsmith_values() -> dict[str, str]:
    """The tracing values from sherry.jsonc's ``LANGSMITH`` object (migration only)."""
    try:
        data = json5.loads((APP_HOME / "sherry.jsonc").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    group = data.get(_LANGSMITH_LEGACY_OBJECT)
    if not isinstance(group, dict):
        return {}
    found: dict[str, str] = {}
    for key, leaf in _LANGSMITH_LEGACY_LEAVES.items():
        value = group.get(leaf)
        if value is None:
            continue
        text = ("true" if value else "false") if isinstance(value, bool) else str(value).strip()
        if text:
            found[key] = text
    return found


def _seed_text() -> str:
    """The template's text with legacy values overlaid (empty = keep).

    ``.env`` supplies the search + browser keys; sherry.jsonc's ``LANGSMITH``
    object overlays the tracing keys (its current home, so it wins over a
    historic .env spelling).
    """
    try:
        text = NETWORK_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        return ""
    legacy = {**_legacy_env_values(), **_legacy_langsmith_values()}
    if not legacy:
        return text
    out: list[str] = []
    for line in text.splitlines():
        match = _TEMPLATE_ASSIGN_RE.match(line)
        if match is None or match.group(2) not in legacy:
            out.append(line)
            continue
        key = match.group(2)
        try:
            rendered = json.dumps(_coerce(key, legacy[key]), ensure_ascii=False)
        except (TypeError, ValueError):
            out.append(line)  # a legacy value that does not fit the type is kept out
            continue
        out.append(f"{match.group(1)}{rendered}{match.group(4)}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def ensure_network_config_file() -> None:
    """Seed ``.network`` from the shipped template when it does not exist."""
    if NETWORK_CONFIG_PATH.exists():
        return
    text = _seed_text()
    if not text:
        return
    try:
        NETWORK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        NETWORK_CONFIG_PATH.write_text(text, encoding="utf-8")
    except OSError:
        pass


def load_network_settings() -> dict[str, Any]:
    """Load every setting from ``.network`` merged over the typed defaults.

    A missing or unparseable file silently yields the typed defaults — the
    application must boot even when the config is absent or half-edited.
    """
    settings: dict[str, Any] = dict(NETWORK_SETTING_DEFAULTS)
    try:
        text = NETWORK_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return settings
    try:
        data = json5.loads(text)
    except ValueError:
        return settings
    if not isinstance(data, dict):
        return settings
    for key in NETWORK_SETTING_KEYS:
        if key not in data:
            continue
        try:
            settings[key] = _coerce(key, data[key])
        except (TypeError, ValueError):
            continue
    return settings


def get_network_setting(key: str) -> Any:
    """Return one typed setting, falling back to its default."""
    if key not in NETWORK_SETTING_DEFAULTS:
        raise KeyError(f"Unknown network setting: {key!r}")
    return load_network_settings()[key]


def render_network_value(value: Any) -> str:
    """The value as an environment string (bools as ``true``/``false``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def hydrate_network_env(env: MutableMapping[str, str] | None = None) -> None:
    """Materialise every setting into the process environment.

    A key that is ALREADY set in the environment is left alone: an explicit
    process variable (an operator's shell, CI, a test) outranks the file. The
    ``.network`` value therefore wins over a legacy ``.env`` entry by ORDER —
    the callers hydrate BEFORE (re)loading the environment file, and that load
    never overwrites an already-set key. Idempotent.
    """
    target = env if env is not None else os.environ
    settings = load_network_settings()
    for key in NETWORK_SETTING_KEYS:
        if key in target:
            continue
        value = settings[key]
        if key in _EMPTY_MEANS_ABSENT and not str(value).strip():
            continue
        target[key] = render_network_value(value)


ensure_network_config_file()
hydrate_network_env()
