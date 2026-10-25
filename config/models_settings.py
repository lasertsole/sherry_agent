"""Project-root ``.models`` — the model (LLM) configuration file.

These settings used to live in the ``.env`` environment file: every model
family's provider / endpoint / key, the local-model switches and the
SkillSpector scanner's LLM selection. They are stored in ``.models``
(``APP_HOME``, i.e. the repository root — or the app-data directory under
``SHERRY_HOME``) as JSON5 (comments + trailing commas allowed) and read through
this module. The environment file is no longer a source for these keys — what
remains in ``.env`` is the non-secret application configuration (sandbox,
gateway, login), which is tracked in git, while ``.models`` (like ``.network``)
is gitignored because it holds API keys.

Two access paths exist on purpose:

* :func:`get_model_setting` — the typed read used by modules that only need a
  value or two (the boot's MAX_TOKEN gate, the embed/reranker clients).
* :func:`hydrate_model_env` — materialises every key into ``os.environ`` at
  import, so the long tail of existing readers (``os.getenv("MAIN_LLM_API_KEY")``
  in the model clients, the import-time feature snapshots in ``config/features``
  and the subprocess env scrubbing) keeps observing these values exactly as it
  did when they lived in ``.env``. It never overwrites a key that is already set
  (an explicit process variable outranks the file), and it runs BEFORE the
  environment file is (re)loaded — so ``.models`` wins over a legacy ``.env``
  entry by order (see ``config.path.load_env``).

An empty value is written through as an empty string (exactly what dotenv does
for a bare ``KEY =`` line).

A missing file is seeded from the shipped ``.models.example``, with values
overlaid from a legacy ``.env`` (the pre-split spelling — including the old
``ITTT_model_PROVIDER`` capitalisation) so an upgrade keeps its configuration.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import json5

_CODE_ROOT = Path(__file__).resolve().parents[1]
_APP_HOME_RAW = os.environ.get("SHERRY_HOME", "").strip()
APP_HOME = Path(_APP_HOME_RAW).expanduser().resolve() if _APP_HOME_RAW else _CODE_ROOT

MODELS_CONFIG_PATH = APP_HOME / ".models"
MODELS_CONFIG_TEMPLATE = _CODE_ROOT / ".models.example"

# Single source of truth for accepted keys and their typed defaults. The keys
# mirror the historical ``.env`` spelling one-for-one, so a migration is a value
# copy and the panels' prefix grouping keeps working.
MODEL_SETTING_DEFAULTS: dict[str, Any] = {
    # Main chat model
    "MAIN_LLM_PROVIDER": "",
    "MAIN_LLM_NAME": "",
    "MAIN_LLM_API_BASE": "",
    "MAIN_LLM_API_KEY": "",
    "MAIN_LLM_MAX_TOKEN": 200000,
    "MAIN_LLM_MAX_OUTPUT_TOKEN": "",
    # Sampling-time repetition penalties (empty = REPETITION_PENALTY default).
    "MAIN_LLM_FREQUENCY_PENALTY": "",
    "MAIN_LLM_PRESENCE_PENALTY": "",
    "MAIN_LLM_ENABLE_THINKING": False,
    "MAIN_LLM_REASONING_EFFORT": "",
    # Auxiliary model
    "AUXILIARY_LLM_MODEL_LOCAL": False,
    "AUXILIARY_LLM_PROVIDER": "",
    "AUXILIARY_LLM_API_NAME": "",
    "AUXILIARY_LLM_API_BASE": "",
    "AUXILIARY_LLM_API_KEY": "",
    "AUXILIARY_LLM_MAX_TOKEN": 200000,
    "AUXILIARY_LLM_MAX_OUTPUT_TOKEN": "",
    "AUXILIARY_LLM_FREQUENCY_PENALTY": "",
    "AUXILIARY_LLM_PRESENCE_PENALTY": "",
    # TaskFlow decision model (empty provider / base = follow AUXILIARY_LLM)
    "TASKFLOW_LLM_MODEL_LOCAL": False,
    "TASKFLOW_LLM_PROVIDER": "",
    "TASKFLOW_LLM_API_NAME": "",
    "TASKFLOW_LLM_API_BASE": "",
    "TASKFLOW_LLM_API_KEY": "",
    "TASKFLOW_LLM_MAX_TOKEN": 200000,
    "TASKFLOW_LLM_MAX_OUTPUT_TOKEN": "",
    "TASKFLOW_LLM_FREQUENCY_PENALTY": "",
    "TASKFLOW_LLM_PRESENCE_PENALTY": "",
    # Compression model (empty provider / base = follow AUXILIARY_LLM)
    "COMPRESSION_LLM_MODEL_LOCAL": False,
    "COMPRESSION_LLM_PROVIDER": "",
    "COMPRESSION_LLM_API_NAME": "",
    "COMPRESSION_LLM_API_BASE": "",
    "COMPRESSION_LLM_API_KEY": "",
    "COMPRESSION_LLM_MAX_TOKEN": 200000,
    "COMPRESSION_LLM_MAX_OUTPUT_TOKEN": "",
    "COMPRESSION_LLM_FREQUENCY_PENALTY": "",
    "COMPRESSION_LLM_PRESENCE_PENALTY": "",
    # Reasoner model
    "REASONER_LLM_PROVIDER": "",
    "REASONER_LLM_NAME": "",
    "REASONER_LLM_API_BASE": "",
    "REASONER_LLM_API_KEY": "",
    "REASONER_LLM_MAX_TOKEN": "",
    "REASONER_LLM_MAX_OUTPUT_TOKEN": "",
    "REASONER_LLM_FREQUENCY_PENALTY": "",
    "REASONER_LLM_PRESENCE_PENALTY": "",
    # Image-to-Text model
    "ITTT_MODEL_LOCAL": False,
    "ITTT_MODEL_PROVIDER": "",
    "ITTT_API_NAME": "",
    "ITTT_API_BASE": "",
    "ITTT_API_KEY": "",
    # Video-Text-to-Text model
    "VTTT_MODEL_LOCAL": False,
    "VTTT_MODEL_PROVIDER": "",
    "VTTT_API_NAME": "",
    "VTTT_API_BASE": "",
    "VTTT_API_KEY": "",
    # Text-to-Image model
    "TTI_MODEL_PROVIDER": "",
    "TTI_API_NAME": "",
    "TTI_API_BASE": "",
    "TTI_API_KEY": "",
    # Reranker model
    "RERANKER_MODEL_LOCAL": False,
    "RERANKER_MODEL_PROVIDER": "openai",
    "RERANKER_API_NAME": "bge-reranker-v2-m3",
    "RERANKER_API_BASE": "",
    "RERANKER_API_KEY": "",
    # Embedding model
    "EMBEDDING_MODEL_LOCAL": True,
    "EMBEDDING_MODEL_PROVIDER": "openai",
    "EMBEDDING_API_NAME": "bge-m3",
    "EMBEDDING_API_BASE": "",
    "EMBEDDING_API_KEY": "",
    # Speech-to-Text model
    "STT_MODEL_PROVIDER": "",
    "STT_API_NAME": "",
    "STT_API_BASE": "",
    "STT_API_KEY": "",
    # SkillSpector scanner (its own LLM selection)
    "SKILL_SCANNER_ENABLED": 1,
    "SKILL_SCANNER_LLM": 0,
    "SKILL_SCANNER_TIMEOUT": 120,
    "SKILLSPECTOR_PROVIDER": "openai",
    "SKILLSPECTOR_MODEL": "",
    # USD per 1M tokens. Stored as a JSON object (serialized as a JSON string
    # in the flat .models file); the token-budget middleware resolves
    # user table → litellm cache → _default. User-editable, no code change.
    "MODEL_PRICING_TABLE": {
        "glm-5": {"input": 0.5, "output": 1.5},
        "deepseek-chat": {"input": 0.14, "output": 0.28},
        "kimi-latest": {"input": 0.55, "output": 2.19},
        "_default": {"input": 0.5, "output": 1.5},
    },
}

MODEL_SETTING_KEYS: tuple[str, ...] = tuple(MODEL_SETTING_DEFAULTS)

#: Keys whose value is a JSON object rather than a scalar.
_JSON_DICT_KEYS = frozenset({"MODEL_PRICING_TABLE"})

#: Per-family sampling-time repetition penalties. Empty = use the global
#: REPETITION_PENALTY default; a set value must be a finite number in
#: [-2.0, 2.0] (OpenAI / Google penalty range).
_PENALTY_PREFIXES: tuple[str, ...] = (
    "MAIN_LLM",
    "AUXILIARY_LLM",
    "TASKFLOW_LLM",
    "COMPRESSION_LLM",
    "REASONER_LLM",
)
_PENALTY_KEYS = frozenset(
    f"{prefix}_{kind}"
    for prefix in _PENALTY_PREFIXES
    for kind in ("FREQUENCY_PENALTY", "PRESENCE_PENALTY")
)
PENALTY_MIN = -2.0
PENALTY_MAX = 2.0

_INT_KEYS = frozenset(
    {
        "MAIN_LLM_MAX_TOKEN",
        "MAIN_LLM_MAX_OUTPUT_TOKEN",
        "AUXILIARY_LLM_MAX_TOKEN",
        "AUXILIARY_LLM_MAX_OUTPUT_TOKEN",
        "TASKFLOW_LLM_MAX_TOKEN",
        "TASKFLOW_LLM_MAX_OUTPUT_TOKEN",
        "COMPRESSION_LLM_MAX_TOKEN",
        "COMPRESSION_LLM_MAX_OUTPUT_TOKEN",
        "SKILL_SCANNER_ENABLED",
        "SKILL_SCANNER_LLM",
        "SKILL_SCANNER_TIMEOUT",
    }
)
# Output-cap keys: unlike the context-window keys, an EMPTY string is a valid
# value meaning "do not send the field" (provider default).
_MAX_OUTPUT_KEYS = frozenset(
    {
        "MAIN_LLM_MAX_OUTPUT_TOKEN",
        "AUXILIARY_LLM_MAX_OUTPUT_TOKEN",
        "TASKFLOW_LLM_MAX_OUTPUT_TOKEN",
        "COMPRESSION_LLM_MAX_OUTPUT_TOKEN",
        "REASONER_LLM_MAX_OUTPUT_TOKEN",
    }
)
_BOOL_KEYS = frozenset(
    {
        "MAIN_LLM_ENABLE_THINKING",
        "AUXILIARY_LLM_MODEL_LOCAL",
        "TASKFLOW_LLM_MODEL_LOCAL",
        "COMPRESSION_LLM_MODEL_LOCAL",
        "ITTT_MODEL_LOCAL",
        "VTTT_MODEL_LOCAL",
        "RERANKER_MODEL_LOCAL",
        "EMBEDDING_MODEL_LOCAL",
    }
)

#: Legacy .env spellings that map onto a canonical key (the shipped
#: .env.example wrote the ITTT provider key with a lowercase ``m`` while the
#: reader always used the uppercase one).
_LEGACY_ALIASES = {"ITTT_model_PROVIDER": "ITTT_MODEL_PROVIDER"}

# ``"KEY": value,`` — the line shape of the template and of every write.
_TEMPLATE_ASSIGN_RE = re.compile(r'^(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*)(.+?)(\s*,?\s*)$')
# ``KEY = value`` — the legacy .env shape (quotes stripped on read).
_DOTENV_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_DOTENV_COMMENT_RE = re.compile(r"^\s*#")


def _coerce(key: str, value: Any) -> Any:
    """Coerce a parsed value to the key's declared type."""
    if key in _JSON_DICT_KEYS:
        if isinstance(value, dict):
            return dict(value)
        text = str(value).strip()
        if not text:
            return dict(MODEL_SETTING_DEFAULTS[key])
        parsed = json5.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"{key} must be a JSON object")
        return parsed
    if key in _PENALTY_KEYS:
        text = str(value).strip()
        if not text:
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"{key} must be a number between {PENALTY_MIN} and {PENALTY_MAX}"
            ) from None
        if not math.isfinite(number) or not (PENALTY_MIN <= number <= PENALTY_MAX):
            raise ValueError(f"{key} must be between {PENALTY_MIN} and {PENALTY_MAX}")
        return number
    if key in _INT_KEYS:
        if key in _MAX_OUTPUT_KEYS and str(value).strip() == "":
            return ""
        return int(value)
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return str(value)


def parse_model_value(key: str, raw: str) -> Any:
    """Coerce a raw UI string into the key's typed value (write path)."""
    if key not in MODEL_SETTING_DEFAULTS:
        raise KeyError(f"Unknown models setting: {key!r}")
    return _coerce(key, raw)


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
        if not match:
            continue
        key = _LEGACY_ALIASES.get(match.group(1), match.group(1))
        if key not in MODEL_SETTING_DEFAULTS:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            found[key] = value
    return found


def _seed_text() -> str:
    """The template's text with legacy ``.env`` values overlaid (empty = keep)."""
    try:
        text = MODELS_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        return ""
    legacy = _legacy_env_values()
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


def ensure_models_config_file() -> None:
    """Seed ``.models`` from the shipped template when it does not exist.

    An EXISTING file also gains the two optional internal families
    (``TASKFLOW_LLM_*`` / ``COMPRESSION_LLM_*``) when it has none of their
    keys: they shipped after most files were created, and the 模型配置 panel
    only renders keys the file contains. The append is additive — a family
    that is already (even partially) present is left untouched, and an
    unparseable file is never rewritten.
    """
    if MODELS_CONFIG_PATH.exists():
        _ensure_context_window_floor()
        _ensure_pricing_table_key()
        _ensure_optional_family_keys()
        _ensure_output_cap_keys()
        _ensure_penalty_keys()
        return
    text = _seed_text()
    if not text:
        return
    try:
        MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        MODELS_CONFIG_PATH.write_text(text, encoding="utf-8")
    except OSError:
        pass


#: The context-window floor before the 200K bump. An existing ``.models`` that
#: still carries this value was seeded when it was valid; migrate it so the boot
#: gate does not brick the install. Any OTHER below-floor value is left alone —
#: the save path and the boot gate report it as a real misconfiguration.
_LEGACY_CONTEXT_FLOOR_TOKEN = 131_072

#: The guarded context-window keys (the optional families share the same floor).
_CONTEXT_WINDOW_KEYS: tuple[str, ...] = (
    "MAIN_LLM_MAX_TOKEN",
    "AUXILIARY_LLM_MAX_TOKEN",
    "TASKFLOW_LLM_MAX_TOKEN",
    "COMPRESSION_LLM_MAX_TOKEN",
)


def _ensure_context_window_floor() -> None:
    """Migrate the legacy 128K default to the current 200K floor, in place.

    Only the exact previous floor is rewritten, so a deliberate (but invalid)
    value such as ``100000`` still fails the boot gate instead of being
    silently "fixed". Comments and line shape are preserved; an unparseable
    file is never rewritten.
    """
    try:
        text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
        data = json5.loads(text)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    legacy: set[str] = set()
    for key in _CONTEXT_WINDOW_KEYS:
        raw = data.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value == _LEGACY_CONTEXT_FLOOR_TOKEN:
            legacy.add(key)
    if not legacy:
        return
    target = MODEL_SETTING_DEFAULTS["MAIN_LLM_MAX_TOKEN"]
    out: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = _TEMPLATE_ASSIGN_RE.match(stripped)
        if match and match.group(2) in legacy:
            out.append(f"{match.group(1)}{target}{match.group(4)}{line[len(stripped) :]}")
            changed = True
        else:
            out.append(line)
    if not changed:
        return
    try:
        MODELS_CONFIG_PATH.write_text("".join(out), encoding="utf-8")
    except OSError:
        pass


#: Optional internal model families added after the split-out file shipped.
#: A file without ANY of a family's keys gets the whole block appended so the
#: panel can render it; the values are the typed defaults.
_OPTIONAL_FAMILY_TITLES: dict[str, str] = {
    "TASKFLOW_LLM_": "── TaskFlow 决策模型 TaskFlow decision model — "
    "empty provider/base follows AUXILIARY_LLM_* ──",
    "COMPRESSION_LLM_": "── 压缩模型 Compression model — empty provider/base "
    "follows AUXILIARY_LLM_* ──",
}


def _ensure_pricing_table_key() -> None:
    """Add ``MODEL_PRICING_TABLE`` to an existing ``.models`` when absent.

    The value is a JSON OBJECT, serialized as a JSON string so the flat
    line-based file and the panel's key/value editor keep working unchanged.
    Idempotent; an unparseable file is never rewritten.
    """
    try:
        text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
        data = json5.loads(text)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or "MODEL_PRICING_TABLE" in data:
        return
    inner = json.dumps(MODEL_SETTING_DEFAULTS["MODEL_PRICING_TABLE"], ensure_ascii=False)
    line = f'  "MODEL_PRICING_TABLE": {json.dumps(inner, ensure_ascii=False)},'

    open_brace = -1
    offset = 0
    for source_line in text.splitlines(keepends=True):
        if source_line.lstrip().startswith("//"):
            offset += len(source_line)
            continue
        pos = source_line.find("{")
        if pos != -1:
            open_brace = offset + pos
            break
        offset += len(source_line)
    if open_brace == -1:
        return
    updated = text[: open_brace + 1] + "\n" + line + text[open_brace + 1 :]
    try:
        MODELS_CONFIG_PATH.write_text(updated, encoding="utf-8")
    except OSError:
        pass


def _ensure_optional_family_keys() -> None:
    """Append a missing optional family block to an existing ``.models``."""
    try:
        text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
        data = json5.loads(text)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return

    blocks: list[str] = []
    for prefix, title in _OPTIONAL_FAMILY_TITLES.items():
        family_keys = [key for key in MODEL_SETTING_KEYS if key.startswith(prefix)]
        if any(key in data for key in family_keys):
            continue
        blocks.append(f"  // {title}")
        blocks.extend(
            f'  "{key}": {json.dumps(MODEL_SETTING_DEFAULTS[key])},' for key in family_keys
        )
        blocks.append("")
    if not blocks:
        return

    # Insert the block right after the document's opening brace: every emitted
    # line carries its own trailing comma, so this stays valid JSON5 whether the
    # existing object is multi-line, one-line, or empty — and no existing line
    # (or trailing comment) has to be rewritten.
    open_brace = -1
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("//"):
            offset += len(line)
            continue
        pos = line.find("{")
        if pos != -1:
            open_brace = offset + pos
            break
        offset += len(line)
    if open_brace == -1:
        return
    blocks.append("")
    updated = text[: open_brace + 1] + "\n" + "\n".join(blocks) + text[open_brace + 1 :]
    try:
        MODELS_CONFIG_PATH.write_text(updated, encoding="utf-8")
    except OSError:
        pass


def _ensure_output_cap_keys() -> None:
    """Add a missing per-family ``*_MAX_OUTPUT_TOKEN`` to an existing file.

    The five chat families all gain the output-cap field (the reasoner shipped
    without one). An older file may contain the family's other keys but not this
    one, so the key is inserted after ``*_MAX_TOKEN``; a family with no keys at
    all is left alone. Idempotent; an unparseable file is never rewritten.
    """
    try:
        text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
        data = json5.loads(text)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return

    lines = text.splitlines(keepends=True)
    changed = False
    for prefix in _PENALTY_PREFIXES:
        key = f"{prefix}_MAX_OUTPUT_TOKEN"
        if key in data:
            continue
        if not any(name.startswith(f"{prefix}_") for name in data):
            continue
        target = -1
        for index, line in enumerate(lines):
            match = _TEMPLATE_ASSIGN_RE.match(line)
            if match and match.group(2) == f"{prefix}_MAX_TOKEN":
                target = index
                break
        if target == -1:
            for index, line in enumerate(lines):
                match = _TEMPLATE_ASSIGN_RE.match(line)
                if match and match.group(2).startswith(f"{prefix}_"):
                    target = index
        if target == -1:
            continue
        line = lines[target]
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.rstrip("\r\n")
        newline = line[len(stripped) :]
        if not stripped.rstrip().endswith(","):
            lines[target] = stripped.rstrip() + "," + (newline or "\n")
        lines[target + 1 : target + 1] = [f'{indent}"{key}": "",' + (newline or "\n")]
        changed = True
    if not changed:
        return
    try:
        MODELS_CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
    except OSError:
        pass


def _ensure_penalty_keys() -> None:
    """Add the per-family penalty keys to an existing ``.models`` when absent.

    The keys ship beside each family's output-token cap; an older file gets
    them inserted after that anchor (or after ``*_MAX_TOKEN`` for the reasoner,
    which has no output cap), falling back to the family's last existing key
    when neither anchor is present. A family with NO keys at all is left alone
    (the panel does not render it either). Idempotent; an unparseable file is
    never touched.
    """
    try:
        text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
        data = json5.loads(text)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return

    lines = text.splitlines(keepends=True)
    changed = False
    for prefix in _PENALTY_PREFIXES:
        missing = [
            key
            for key in (f"{prefix}_FREQUENCY_PENALTY", f"{prefix}_PRESENCE_PENALTY")
            if key not in data
        ]
        if not missing:
            continue
        anchor = (
            f"{prefix}_MAX_OUTPUT_TOKEN"
            if f"{prefix}_MAX_OUTPUT_TOKEN" in data
            else f"{prefix}_MAX_TOKEN"
            if f"{prefix}_MAX_TOKEN" in data
            else ""
        )
        target = -1
        if anchor:
            for index, line in enumerate(lines):
                match = _TEMPLATE_ASSIGN_RE.match(line)
                if match and match.group(2) == anchor:
                    target = index
                    break
        if target == -1:
            # A partially hand-written family (provider/name only) still gets
            # the pair, after its last line.
            for index, line in enumerate(lines):
                match = _TEMPLATE_ASSIGN_RE.match(line)
                if match and match.group(2).startswith(f"{prefix}_"):
                    target = index
        if target == -1:
            continue  # the family itself is absent — nothing to extend
        line = lines[target]
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.rstrip("\r\n")
        newline = line[len(stripped) :]
        if not stripped.rstrip().endswith(","):
            lines[target] = stripped.rstrip() + "," + (newline or "\n")
        insert = [f'{indent}"{key}": "",' + (newline or "\n") for key in missing]
        lines[target + 1 : target + 1] = insert
        changed = True
    if not changed:
        return
    try:
        MODELS_CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
    except OSError:
        pass


def load_model_settings() -> dict[str, Any]:
    """Load every setting from ``.models`` merged over the typed defaults.

    A missing or unparseable file silently yields the typed defaults — the
    application must boot even when the config is absent or half-edited.
    """
    settings: dict[str, Any] = dict(MODEL_SETTING_DEFAULTS)
    try:
        text = MODELS_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return settings
    try:
        data = json5.loads(text)
    except ValueError:
        return settings
    if not isinstance(data, dict):
        return settings
    for key in MODEL_SETTING_KEYS:
        if key not in data:
            continue
        try:
            settings[key] = _coerce(key, data[key])
        except (TypeError, ValueError):
            continue
    return settings


def get_model_setting(key: str) -> Any:
    """Return one typed setting, falling back to its default."""
    if key not in MODEL_SETTING_DEFAULTS:
        raise KeyError(f"Unknown models setting: {key!r}")
    return load_model_settings()[key]


def render_model_value(value: Any) -> str:
    """The value as an environment string (bools as ``true``/``false``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def hydrate_model_env(env: MutableMapping[str, str] | None = None) -> None:
    """Materialise every setting into the process environment.

    A key that is ALREADY set in the environment is left alone: an explicit
    process variable (an operator's shell, CI, a test) outranks the file, and
    this is also what keeps a later ``load_env()`` from clobbering it. The
    ``.models`` value therefore wins over a legacy ``.env`` entry by ORDER —
    the callers hydrate BEFORE (re)loading the environment file, and that load
    never overwrites an already-set key. Idempotent.
    """
    target = env if env is not None else os.environ
    settings = load_model_settings()
    for key in MODEL_SETTING_KEYS:
        if key in target:
            continue
        target[key] = render_model_value(settings[key])


ensure_models_config_file()
hydrate_model_env()
