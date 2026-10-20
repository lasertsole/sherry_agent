"""Read/write the project-root ``.models`` in a safe, structured way.

Mirrors :mod:`server.service.env` and :mod:`server.service.sherry_config` for the
model (LLM) configuration that was split out of ``.env`` (the key list and
typing live in ``config/models_settings.py``): every model family's provider /
endpoint / key, the local-model switches and the SkillSpector scanner's LLM
selection.

The file is a JSON5 object of flat ``"KEY": value,`` lines, so edits are applied
line-wise and comments / ordering survive. Reads answer the SAME grouped shape
``GET /env`` uses — ``{groups: [{name, kind, entries}]}`` — so the 模型配置 tab
renders the model families with the same cards and the model-profile manager.
"""

import json
import re

from config.features import MIN_REQUIRED_MAX_TOKEN, SERVER_HTTP
from config.features.agent_side.token_guard import MIN_MAX_OUTPUT_TOKEN
from config.models_settings import (
    MODELS_CONFIG_PATH,
    MODEL_SETTING_DEFAULTS,
    parse_model_value,
)

# Reuse the env panel's display contract: same canonical group order and the
# same model-vs-plain split.
GROUP_PREFIXES: tuple[str, ...] = SERVER_HTTP["env_group_prefixes"]
MODEL_GROUP_PREFIXES: tuple[str, ...] = SERVER_HTTP["env_model_group_prefixes"]
OTHER_GROUP = "other"

# MAX_TOKEN keys whose saved values must satisfy token_guard's 128K floor.
TOKEN_KEYS = frozenset({"MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"})

# MAX_OUTPUT keys: empty is valid (provider default), a set value must satisfy
# the 8K business floor (M9) — otherwise the next boot would abort.
MAX_OUTPUT_KEYS = frozenset({"MAIN_LLM_MAX_OUTPUT_TOKEN", "AUXILIARY_LLM_MAX_OUTPUT_TOKEN"})

# ``"KEY": value,`` — the line shape of the template and of every write.
_ASSIGN_RE = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(.+?)\s*,?\s*$')
_COMMENT_RE = re.compile(r"^\s*//")


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


def read_models_config() -> dict:
    """Return the .models contents grouped by prefix, preserving file order.

    Response shape (identical to ``GET /env``)::

        {"groups": [{"name": "MAIN_LLM", "kind": "model",
                     "entries": [{"key": "...", "value": "...", "value_edited": false}]}]}

    Raises ``FileNotFoundError`` when ``.models`` does not exist yet.
    """
    if not MODELS_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Models config file not found: {MODELS_CONFIG_PATH}")

    groups: dict[str, list[dict]] = {}
    for line in MODELS_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        if _COMMENT_RE.match(line):
            continue
        match = _ASSIGN_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key not in MODEL_SETTING_DEFAULTS:
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


def write_models_config(changes: dict[str, str]) -> None:
    """Apply value updates to ``.models``.

    ``changes`` maps key -> new value. Only known keys (the
    ``config/models_settings`` registry) are accepted; values are coerced to
    their declared types (an unparseable int raises ``ValueError``). Comments,
    blank lines, and the original ordering are preserved, and a ``.bak`` backup
    of the pre-write file is kept, mirroring the other config services.
    """
    if not isinstance(changes, dict):
        raise ValueError("changes must be a mapping of key to value")

    if not MODELS_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Models config file not found: {MODELS_CONFIG_PATH}")

    unknown = [k for k in changes if k not in MODEL_SETTING_DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown models config keys: {', '.join(sorted(unknown))}")

    # MAX_TOKEN guard FIRST: refuse to persist a sub-128K context window, so the
    # file can never be saved into a state that blocks the next server boot. It
    # runs before the coercion loop so a non-integer value gets this message
    # rather than a bare int() error.
    for key, value in changes.items():
        if key not in TOKEN_KEYS:
            continue
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer, got: {value}") from None
        if num < MIN_REQUIRED_MAX_TOKEN:
            raise ValueError(
                f"{key} must be >= {MIN_REQUIRED_MAX_TOKEN} (128K); got {num}. Refusing to save."
            )

    # OUTPUT cap guard: empty means "provider default"; a set value below 8K is
    # refused so the save cannot create a boot-aborting file.
    for key, value in changes.items():
        if key not in MAX_OUTPUT_KEYS:
            continue
        raw = str(value).strip()
        if not raw:
            continue
        try:
            num = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer, got: {value}") from None
        if num < MIN_MAX_OUTPUT_TOKEN:
            raise ValueError(
                f"{key} must be empty or >= {MIN_MAX_OUTPUT_TOKEN} (8K); got {num}. "
                "Refusing to save."
            )

    # Coerce BEFORE touching the file so a bad value aborts without a backup churn.
    typed_changes: dict[str, str] = {}
    for key, raw in changes.items():
        typed_changes[key] = json.dumps(parse_model_value(key, raw), ensure_ascii=False)

    text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
    backup_path = MODELS_CONFIG_PATH.with_name(MODELS_CONFIG_PATH.name + ".bak")
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
    # Defensively append any key that had no line in the file — BEFORE the
    # closing brace (a line after ``}`` would break the document and the next
    # load would silently fall back to the defaults).
    appended = [f'"{key}": {typed_changes[key]},' for key in sorted(updated_keys)]
    if appended:
        close_index = next(
            (i for i in range(len(out) - 1, -1, -1) if out[i].strip().startswith("}")),
            len(out),
        )
        prev_index = close_index - 1
        if prev_index >= 0:
            previous = out[prev_index].rstrip()
            if previous and not previous.endswith((",", "{")):
                out[prev_index] = previous + ","
        out[close_index:close_index] = appended

    trailing_newline = "\n" if text.endswith("\n") else ""
    MODELS_CONFIG_PATH.write_text("\n".join(out) + trailing_newline, encoding="utf-8")
