"""Read/write the project-root ``.network`` in a safe, structured way.

Mirrors :mod:`server.service.env` and :mod:`server.service.sherry_config` for
network configuration that was split out of ``.env`` (the Tavily +
SearXNG search backends, the browser agent's switches) and ``sherry.jsonc``
(LangSmith tracing) — the key list and typing live in
``config/network_settings.py``.

The file is a JSON5 object of flat ``"KEY": value,`` lines, so edits are applied
line-wise and comments / ordering survive. Reads answer the SAME grouped shape
``GET /env`` uses — ``{groups: [{name, kind, entries}]}`` — so the 模型配置 tab
renders the browser / tracing groups with the same cards it already uses for
``.env`` groups. The search keys (``TAVILY_`` / ``SEARXNG_``) stay WITHHELD from
that payload: the 模型联网 tab owns them, reads them from
``GET /sessions/search_backend`` and writes them HERE (through
``PUT /network-config``). Writes stay allowed for them.
"""

import json
import re

from config.features import SERVER_HTTP
from config.network_settings import (
    NETWORK_CONFIG_PATH,
    NETWORK_SETTING_DEFAULTS,
    parse_network_value,
)

# Reuse the env panel's display contract: same canonical group order, same
# model-vs-plain split, same hidden prefixes.
GROUP_PREFIXES: tuple[str, ...] = SERVER_HTTP["env_group_prefixes"]
MODEL_GROUP_PREFIXES: tuple[str, ...] = SERVER_HTTP["env_model_group_prefixes"]
PANEL_HIDDEN_PREFIXES: tuple[str, ...] = SERVER_HTTP["env_panel_hidden_prefixes"]
OTHER_GROUP = "other"

# ``"KEY": value,`` — the line shape of the template and of every write.
_ASSIGN_RE = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(.+?)\s*,?\s*$')
_COMMENT_RE = re.compile(r"^\s*//")


def _hidden_from_panel(key: str) -> bool:
    """Whether another panel owns this key (the 模型联网 tab does)."""
    return key.startswith(PANEL_HIDDEN_PREFIXES)


def _group_of(key: str) -> str:
    for prefix in GROUP_PREFIXES:
        if key.startswith(prefix):
            return prefix[:-1]
    return OTHER_GROUP


def _display_value(raw: str) -> str:
    """Render one line's value as the UI string (bools as true/false)."""
    try:
        typed = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(typed, bool):
        return "true" if typed else "false"
    return typed if isinstance(typed, str) else str(typed)


def read_network_config() -> dict:
    """Return the .network contents grouped by prefix, preserving file order.

    Response shape (identical to ``GET /env``)::

        {"groups": [{"name": "SHERRY_BROWSER", "kind": "plain",
                     "entries": [{"key": "...", "value": "...", "value_edited": false}]}]}

    Raises ``FileNotFoundError`` when ``.network`` does not exist yet.
    """
    if not NETWORK_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Network config file not found: {NETWORK_CONFIG_PATH}")

    groups: dict[str, list[dict]] = {}
    for line in NETWORK_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        if _COMMENT_RE.match(line):
            continue
        match = _ASSIGN_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key not in NETWORK_SETTING_DEFAULTS or _hidden_from_panel(key):
            continue
        groups.setdefault(_group_of(key), []).append(
            {"key": key, "value": _display_value(match.group(2)), "value_edited": False}
        )

    ordered_groups: list[dict] = []
    ordered_names = [p[:-1] for p in GROUP_PREFIXES] + (
        [OTHER_GROUP] if OTHER_GROUP in groups else []
    )
    for name in ordered_names:
        if name not in groups:
            continue
        ordered_groups.append(
            {
                "name": name,
                "kind": ("model" if name + "_" in MODEL_GROUP_PREFIXES else "plain"),
                "entries": groups[name],
            }
        )
    return {"groups": ordered_groups}


def network_file_values() -> dict[str, str]:
    """Every ``"KEY": value`` pair in ``.network`` — hidden keys included.

    This is the file view the 模型联网 panel edits (it owns the withheld search
    keys and reads them from ``GET /sessions/search_backend``): the values a
    save would produce and what the next boot reads. A missing or unreadable
    file yields an empty mapping rather than raising — the panel renders its
    "not configured" state either way.
    """
    try:
        text = NETWORK_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if _COMMENT_RE.match(line):
            continue
        match = _ASSIGN_RE.match(line)
        if not match or match.group(1) not in NETWORK_SETTING_DEFAULTS:
            continue
        values[match.group(1)] = _display_value(match.group(2))
    return values


def write_network_config(changes: dict[str, str]) -> None:
    """Apply value updates to ``.network``.

    ``changes`` maps key -> new value. Only known keys (the
    ``config/network_settings`` registry) are accepted; values are coerced to
    their declared types (an unparseable int raises ``ValueError``). Comments,
    blank lines, and the original ordering are preserved, and a ``.bak`` backup
    of the pre-write file is kept, mirroring the .env / sherry.jsonc services.
    """
    if not isinstance(changes, dict):
        raise ValueError("changes must be a mapping of key to value")

    if not NETWORK_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Network config file not found: {NETWORK_CONFIG_PATH}")

    unknown = [k for k in changes if k not in NETWORK_SETTING_DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown network config keys: {', '.join(sorted(unknown))}")

    # Coerce BEFORE touching the file so a bad value aborts without a backup churn.
    typed_changes: dict[str, str] = {}
    for key, raw in changes.items():
        typed_changes[key] = json.dumps(parse_network_value(key, raw), ensure_ascii=False)

    text = NETWORK_CONFIG_PATH.read_text(encoding="utf-8")
    backup_path = NETWORK_CONFIG_PATH.with_name(NETWORK_CONFIG_PATH.name + ".bak")
    backup_path.write_text(text, encoding="utf-8")

    updated_keys = set(typed_changes.keys())
    out: list[str] = []
    for line in text.splitlines():
        match = None if _COMMENT_RE.match(line) else _ASSIGN_RE.match(line)
        if match is None or match.group(1) not in updated_keys:
            out.append(line)
            continue
        key = match.group(1)
        # Preserve the original line's indentation and trailing comma.
        indent = line[: len(line) - len(line.lstrip())]
        comma = "," if line.rstrip().endswith(",") else ""
        out.append(f'{indent}"{key}": {typed_changes[key]}{comma}')
        updated_keys.discard(key)
    # Defensively append any key that had no line in the file.
    for key in sorted(updated_keys):
        out.append(f'"{key}": {typed_changes[key]},')

    trailing_newline = "\n" if text.endswith("\n") else ""
    NETWORK_CONFIG_PATH.write_text("\n".join(out) + trailing_newline, encoding="utf-8")
