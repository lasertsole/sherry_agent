"""Read/write the project root ``sherry.jsonc`` in a safe, structured way.

Mirrors :mod:`server.service.env` for the application-level settings that were
split out of ``.env`` (key list and typing live in
``config/sherry_settings.py``). The file is a JSON5 object — flat
``"KEY": value,`` lines plus one-level nested group objects (``"LANGSMITH"``,
``"curator"``) — so edits are applied line-wise to preserve comments and
ordering; values are coerced to their declared types (int / float / bool /
str) before writing. Grouped entries are addressed with dotted keys
(``"LANGSMITH.TRACING_V2"`` is the ``"TRACING_V2"`` line inside the
``"LANGSMITH"`` object).
"""

import json
import re

from config.sherry_settings import (
    SHERRY_CONFIG_PATH,
    SHERRY_SETTING_DEFAULTS,
    SHERRY_SETTING_KEYS,
    parse_sherry_value,
)

# Line shapes authored by this service (and the shipped default file):
#   "KEY": value,      — one entry per line; the trailing comma is optional on
#                        the last entry only.
#   "GROUP": {         — opens a nested group object (one level deep).
#   }                  — closes it.
_ASSIGN_RE = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(.+?)\s*,?\s*$')
_COMMENT_RE = re.compile(r"^\s*//")
_GROUP_CLOSE_RE = re.compile(r"^\s*\}\s*,?\s*$")


def _scan_lines(text: str):
    """Yield ``(line, entry)`` for every line of a sherry.jsonc document.

    ``entry`` is ``(dotted_key, match)`` for known entry lines (with ``match``
    = the :data:`_ASSIGN_RE` match on that line) and ``None`` for everything
    else — comment/blank lines, group open/close lines, and unknown keys.
    """
    group = None
    for line in text.splitlines():
        if _COMMENT_RE.match(line) or not line.strip():
            yield line, None
            continue
        if _GROUP_CLOSE_RE.match(line):
            group = None
            yield line, None
            continue
        m = _ASSIGN_RE.match(line)
        if not m:
            yield line, None
            continue
        key, raw_value = m.group(1), m.group(2)
        if raw_value == "{":
            group = key
            yield line, None
            continue
        full_key = f"{group}.{key}" if group else key
        if full_key in SHERRY_SETTING_DEFAULTS:
            yield line, (full_key, m)
        else:
            yield line, None


def read_sherry_config() -> dict:
    """Return the sherry.jsonc entries in file order.

    Response shape::

        {"entries": [{"key": "...", "value": "...", "value_edited": False}, ...]}

    Grouped settings are flattened to their dotted keys. Values are rendered
    as UI strings (bools as ``true``/``false``, ints as decimal strings).
    Raises ``FileNotFoundError`` when the file does not exist.
    """
    if not SHERRY_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Sherry config file not found: {SHERRY_CONFIG_PATH}")

    text = SHERRY_CONFIG_PATH.read_text(encoding="utf-8")
    entries: list[dict] = []
    for _line, entry in _scan_lines(text):
        if entry is None:
            continue
        key, m = entry
        raw_value = m.group(2)
        try:
            typed = json.loads(raw_value)
        except ValueError:
            typed = raw_value
        if isinstance(typed, bool):
            display = "true" if typed else "false"
        else:
            display = str(typed)
        entries.append({"key": key, "value": display, "value_edited": False})

    return {"entries": entries}


def write_sherry_config(changes: dict[str, str]) -> None:
    """Apply value updates to ``sherry.jsonc``.

    ``changes`` maps dotted key -> new UI value. Only known keys
    (``SHERRY_SETTING_KEYS``) are accepted; values are coerced to their
    declared types (an unparseable int raises ``ValueError``). Comments, blank
    lines, and the original ordering are preserved. A ``.bak`` backup of the
    pre-write file is kept, mirroring the .env service.
    """
    if not isinstance(changes, dict):
        raise ValueError("changes must be a mapping of key to value")

    if not SHERRY_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Sherry config file not found: {SHERRY_CONFIG_PATH}")

    unknown = [k for k in changes if k not in SHERRY_SETTING_KEYS]
    if unknown:
        raise ValueError(f"Unknown sherry config keys: {', '.join(sorted(unknown))}")

    # Coerce BEFORE touching the file so a bad value aborts without a backup churn.
    typed_changes: dict[str, str] = {}
    for key, raw in changes.items():
        typed = parse_sherry_value(key, raw)
        typed_changes[key] = json.dumps(typed)

    text = SHERRY_CONFIG_PATH.read_text(encoding="utf-8")
    backup_path = SHERRY_CONFIG_PATH.with_name(SHERRY_CONFIG_PATH.name + ".bak")
    backup_path.write_text(text, encoding="utf-8")

    updated_keys = set(typed_changes.keys())
    out: list[str] = []
    for line, entry in _scan_lines(text):
        if entry is None or entry[0] not in updated_keys:
            out.append(line)
            continue
        key, m = entry
        # Preserve the original line's indentation and trailing comma.
        indent = line[: len(line) - len(line.lstrip())]
        had_comma = line.rstrip().endswith(",")
        comma = "," if had_comma else ""
        out.append(f'{indent}"{m.group(1)}": {typed_changes[key]}{comma}')
        updated_keys.discard(key)
    # Defensively append any key that had no line in the file.
    for key in sorted(updated_keys):
        out.append(f'"{key}": {typed_changes[key]},')

    trailing_newline = "\n" if text.endswith("\n") else ""
    SHERRY_CONFIG_PATH.write_text("\n".join(out) + trailing_newline, encoding="utf-8")
