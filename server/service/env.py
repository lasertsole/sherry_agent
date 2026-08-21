"""Read/write the project root ``.env`` file in a safe, structured way.

The ``.env`` holds live secrets (API keys) plus model/provider configuration.
We parse it preserving comment lines and the original key order, group entries by
their shared prefix, and only ever write back known/allowlisted keys so comments
and unrelated variables are never clobbered.
"""
import re
from pathlib import Path

from config.path import ENV_PATH

# Canonical display groups with a human-friendly order. Each group is matched by
# an exact key prefix. Any key not belonging to a known group is attached to the
# "other" group. The mapping is based on the reference keys in .env.example.
GROUP_PREFIXES: list[str] = [
    "MAIN_LLM_",
    "REASONER_LLM_",
    "AUXILIARY_LLM_",
    "ITTT_",
    "VTTT_",
    "TTI_",
    "RERANKER_",
    "EMBEDDING_",
    "STT_",
]
OTHER_GROUP = "other"

# Simple env-line parser: KEY = VALUE  (allow surrounding whitespace, quoted values).
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_COMMENT_RE = re.compile(r"^\s*#")


class EnvEntry:
    """A single ``KEY = value`` line located in the original file."""

    __slots__ = ("key", "value", "raw")

    def __init__(self, key: str, value: str, raw: str) -> None:
        self.key = key
        # Strip outer double or single quotes so the UI edits the bare value; we
        # re-quote on write when the original was quoted.
        self.value = value.strip()
        if len(self.value) >= 2 and self.value[0] == self.value[-1] and self.value[0] in "\"'":
            self.value = self.value[1:-1]
        # Preserve whether the original value was quoted so we can restore it.
        self.raw = raw


def _parse_dotenv(text: str) -> tuple[list[EnvEntry], list[str]]:
    """Split the raw file into env entries and non-assignment lines (comments/blank)."""
    entries: list[EnvEntry] = []
    lines: list[str] = []
    for line in text.splitlines():
        lines.append(line)
        if _COMMENT_RE.match(line):
            continue
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        entries.append(EnvEntry(m.group(1), m.group(2), m.group(0)))
    return entries, lines


def _group_of(key: str) -> str:
    for prefix in GROUP_PREFIXES:
        if key.startswith(prefix):
            return prefix[:-1]
    return OTHER_GROUP


def read_env_file() -> dict:
    """Return the .env contents grouped by prefix, preserving file order.

    Response shape::

        {
          "groups": [
            {"name": "MAIN_LLM", "entries": [{"key": "...", "value": "..."}, ...]},
            ...
          ]
        }

    Raises ``FileNotFoundError`` when ``.env`` does not exist yet.
    """
    if not ENV_PATH.exists():
        raise FileNotFoundError(f"Environment file not found: {ENV_PATH}")

    text = ENV_PATH.read_text(encoding="utf-8")
    entries, _lines = _parse_dotenv(text)

    # Preserve insertion order of groups while following the canonical ordering.
    groups: dict[str, list[EnvEntry]] = {}
    for entry in entries:
        groups.setdefault(_group_of(entry.key), []).append(entry)

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
                "entries": [
                    {"key": e.key, "value": e.value, "value_edited": False} for e in groups[name]
                ],
            }
        )

    return {"groups": ordered_groups}


def write_env_file(changes: dict[str, str]) -> None:
    """Apply value updates to the ``.env`` file.

    ``changes`` maps key -> new value. Only keys already present in the file are
    accepted; unknown keys raise ``ValueError``. Comments, blank lines, and the
    original ordering are preserved. Values are written unquoted for empty input
    and quoted (double) otherwise to keep parseability consistent.
    """
    if not isinstance(changes, dict):
        raise ValueError("changes must be a mapping of key to value")

    if not ENV_PATH.exists():
        raise FileNotFoundError(f"Environment file not found: {ENV_PATH}")

    text = ENV_PATH.read_text(encoding="utf-8")

    # Backup the current .env before applying changes so a later value can be
    # restored (the backup holds the pre-write state; it is overwritten on every
    # subsequent write, so it always reflects "the state before the last save").
    backup_path = ENV_PATH.with_name(ENV_PATH.name + ".bak")
    backup_path.write_text(text, encoding="utf-8")

    entries, lines = _parse_dotenv(text)

    known_keys = {e.key for e in entries}
    unknown = [k for k in changes if k not in known_keys]
    if unknown:
        raise ValueError(f"Unknown environment keys: {', '.join(sorted(unknown))}")

    by_key = {e.key: e for e in entries}
    for key, value in changes.items():
        # Always write the value as-is (the UI edits the bare, unquoted value).
        # Values that need quoting (spaces or special chars) are quoted here so
        # the resulting .env remains parseable by dotenv on the next boot.
        entry = by_key[key]
        clean: str = value
        needs_quote = bool(clean) and not re.fullmatch(r"[A-Za-z0-9_.\-:/%]+", clean)
        if needs_quote:
            clean = f'"{clean}"'

        # Preserve original key indentation; write `KEY = VALUE`.
        indent = entry.raw[: len(entry.raw) - len(entry.raw.lstrip())]
        entry.raw = f"{indent}{key} = {clean}"

    # Rebuild the file: iterate original lines, swapping in updated assignment lines.
    # We need to replace lines that were assignments and are present in changes.
    updated_keys = set(changes.keys())
    out: list[str] = []
    i = 0
    for line in lines:
        if _COMMENT_RE.match(line):
            out.append(line)
            continue
        m = _ASSIGN_RE.match(line)
        if m and m.group(1) in updated_keys:
            out.append(by_key[m.group(1)].raw)
            updated_keys.remove(m.group(1))
        else:
            out.append(line)
    # Any updated key not found in a raw line (shouldn't happen for known keys) —
    # defensively append at the end to avoid dropping data.
    for key in updated_keys:
        out.append(f"{key} = {changes[key]}")

    ENV_PATH.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
