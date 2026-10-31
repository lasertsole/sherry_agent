"""Read/write the project root ``sherry.jsonc`` in a safe, structured way.

Mirrors :mod:`server.service.env` for the application-level settings that were
split out of ``.env`` (key list and typing live in
``config/sherry_settings.py``). The file is a JSON5 object — flat
``"KEY": value,`` lines plus one-level nested group objects (``"heartbeat"``,
``"curator"``) — so edits are applied line-wise to preserve comments and
ordering; values are coerced to their declared types (int / float / bool /
str) before writing. Grouped entries are addressed with dotted keys
(``"curator.interval_hours"`` is the ``"interval_hours"`` line inside the
``"curator"`` object).
"""

import json
import re
from typing import Any

from config.sherry_settings import (
    SHERRY_CONFIG_PATH,
    SHERRY_SETTING_DEFAULTS,
    SHERRY_SETTING_KEYS,
    parse_sherry_value,
)

#: Re-exported for callers/tests of this module; the registry lives beside the
#: list loader that owns the semantics (``config.sherry_settings``).
from config.sherry_settings import LIST_SETTING_KEYS as LIST_SETTING_KEYS

#: Entry cap per list (a hand-typed config never needs more) and the per-entry
#: length cap. Shell metacharacters are refused so a value can never re-enter a
#: command string as syntax.
_MAX_LIST_ENTRIES = 200
_MAX_ENTRY_CHARS = 256

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


def _display(value: Any) -> str:
    """Render one typed setting as the UI string the panel edits (bools as true/false)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


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
    seen: set[str] = set()
    for _line, entry in _scan_lines(text):
        if entry is None:
            continue
        key, m = entry
        seen.add(key)
        raw_value = m.group(2)
        try:
            typed = json.loads(raw_value)
        except ValueError:
            typed = raw_value
        entries.append(
            {
                "key": key,
                "value": _display(typed),
                # The registry default, so the panel's 重置为默认值 button does
                # not have to hardcode a second copy of it.
                "default": _display(SHERRY_SETTING_DEFAULTS[key]),
                "value_edited": False,
            }
        )
    # A registered key the file does not carry yet (an older sherry.jsonc, or a
    # key added in a later release) is still shown — with its default — so the
    # panel can edit it; the next write appends the missing line.
    for key in SHERRY_SETTING_KEYS:
        if key in seen:
            continue
        entries.append(
            {
                "key": key,
                "value": _display(SHERRY_SETTING_DEFAULTS[key]),
                "default": _display(SHERRY_SETTING_DEFAULTS[key]),
                "value_edited": False,
            }
        )

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
    # Defensively append any key that had no line in the file. The lines go
    # BEFORE the closing brace (appending after it would produce invalid
    # JSON5), and the entry above the brace gets its comma if the file's last
    # line had none.
    if updated_keys:
        new_lines = [f'  "{key}": {typed_changes[key]},' for key in sorted(updated_keys)]
        closing = next(
            (index for index in range(len(out) - 1, -1, -1) if out[index].strip() == "}"), None
        )
        if closing is None:
            out.extend(new_lines)
        else:
            for index in range(closing - 1, -1, -1):
                if not out[index].strip():
                    continue
                if not out[index].rstrip().endswith(","):
                    out[index] = out[index].rstrip() + ","
                break
            out[closing:closing] = new_lines

    trailing_newline = "\n" if text.endswith("\n") else ""
    SHERRY_CONFIG_PATH.write_text("\n".join(out) + trailing_newline, encoding="utf-8")


def _validate_list_key(key: str) -> None:
    if key not in LIST_SETTING_KEYS:
        raise ValueError(f"Unknown list setting: {key!r} (allowed: {', '.join(LIST_SETTING_KEYS)})")


def read_sherry_list(key: str) -> list[str]:
    """Return one list setting from THIS module's ``SHERRY_CONFIG_PATH``.

    Parsed here rather than through ``config.sherry_settings.load_sherry_list_setting``
    on purpose: that helper binds the path in the other module, so a reader and
    a writer would then disagree about which file they are looking at — and a
    test could patch one and not the other. The normalization matches the
    loader (strip, drop empties) so the UI shows exactly what the runtime uses.
    """
    _validate_list_key(key)
    if not SHERRY_CONFIG_PATH.exists():
        return []
    try:
        import json5

        data = json5.loads(SHERRY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def write_sherry_list(key: str, values: list[str]) -> list[str]:
    """Replace one list setting in ``sherry.jsonc``, preserving the rest.

    Line-wise on purpose (the same rule as the scalar writer): comments, blank
    lines and ordering survive, and the value is written as a single-line JSON
    array so the editor can find it again. Two shapes are REFUSED rather than
    guessed at, each with an actionable message: a hand-formatted MULTI-LINE
    array (keep it on one line) and a file with no multi-line top-level object
    (the shipped ``sherry.jsonc`` always is one, so this only bites a hand-made
    single-line file).

    Entries are normalized (stripped, lower-cased, deduplicated in order) and
    validated: no empty strings, no shell metacharacters, bounded length and
    count. Returns the written list.
    """
    _validate_list_key(key)
    if not isinstance(values, list):
        raise ValueError("values must be a list of strings")
    if len(values) > _MAX_LIST_ENTRIES:
        raise ValueError(f"too many entries (max {_MAX_LIST_ENTRIES})")

    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("every entry must be a string")
        entry = raw.strip().lower()
        if not entry:
            continue
        if len(entry) > _MAX_ENTRY_CHARS:
            raise ValueError(f"entry too long (max {_MAX_ENTRY_CHARS} chars): {entry[:40]}…")
        if re.search(r"[;&|<>$`\n\r]", entry):
            raise ValueError(f"entry may not contain shell metacharacters: {entry!r}")
        if entry not in normalized:
            normalized.append(entry)

    if not SHERRY_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Sherry config file not found: {SHERRY_CONFIG_PATH}")

    payload = json.dumps(normalized, ensure_ascii=False)
    text = SHERRY_CONFIG_PATH.read_text(encoding="utf-8")
    backup_path = SHERRY_CONFIG_PATH.with_name(SHERRY_CONFIG_PATH.name + ".bak")
    backup_path.write_text(text, encoding="utf-8")

    key_line = re.compile(
        r'^(?P<indent>\s*)"' + re.escape(key) + r'"\s*:\s*(?P<value>.*?)\s*,?\s*$'
    )
    out: list[str] = []
    replaced = False
    for line in text.splitlines():
        m = key_line.match(line)
        if m is None or replaced:
            out.append(line)
            continue
        current = m.group("value").strip()
        if current and not current.startswith("["):
            # Not an array (a scalar under our key): overwrite rather than guess.
            current = ""
        if current and not current.endswith("]"):
            raise ValueError(
                f'"{key}" spans multiple lines in sherry.jsonc; keep its array on a '
                "single line so it can be edited safely"
            )
        had_comma = line.rstrip().endswith(",")
        out.append(f'{m.group("indent")}"{key}": {payload}{"," if had_comma else ""}')
        replaced = True
    if not replaced:
        # The key had no line: insert it INSIDE the top-level object (before the
        # closing brace), never after it — appending past ``}`` would produce a
        # document the JSON5 loader rejects. The previous entry needs its
        # trailing comma back for the same reason.
        insert_at = None
        for idx in range(len(out) - 1, -1, -1):
            if out[idx].strip() in ("}", "},"):
                insert_at = idx
                break
        if insert_at is None:
            raise ValueError(f"sherry.jsonc has no multi-line top-level object to add {key!r} to")
        for idx in range(insert_at - 1, -1, -1):
            stripped = out[idx].strip()
            if not stripped or stripped.startswith("//"):
                continue
            if not out[idx].rstrip().endswith((",", "{")):
                out[idx] = out[idx].rstrip() + ","
            break
        out.insert(insert_at, f'  "{key}": {payload},')

    trailing_newline = "\n" if text.endswith("\n") else ""
    SHERRY_CONFIG_PATH.write_text("\n".join(out) + trailing_newline, encoding="utf-8")
    return normalized
