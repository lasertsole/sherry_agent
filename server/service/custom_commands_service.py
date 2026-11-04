"""User-defined slash commands (``/name``) for the composer.

Commands live in two flat Markdown trees:

* ``APP_HOME/commands/`` — user scope, every session;
* ``<project>/.sherry/commands/`` — workspace scope, the active project.

The file name is the command name (nested directories join with ``:``), the
frontmatter carries ``description`` / ``argument-hint``, and the body is the
prompt template. ``$ARGUMENTS`` is replaced on the CLIENT before the text is
sent; this module only reads and normalizes the files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from config import APP_HOME
from runtime.session.project_dir import current_project_dir
from skills.loader import parse_frontmatter

__all__ = ["list_custom_commands"]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]*$")
_MAX_COMMANDS = 200
_MAX_BODY_CHARS = 20_000
_COMMANDS_DIR = "commands"


def _read_one(path: Path, root: Path, source: str) -> dict[str, Any] | None:
    """Parse one command file; a malformed file is skipped, never fatal."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("custom commands: unreadable file skipped: {}", path)
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    meta = parse_frontmatter(text)
    if not isinstance(meta, dict):
        return None

    name = ":".join(path.relative_to(root).with_suffix("").parts)
    if not _NAME_RE.match(name):
        logger.debug("custom commands: invalid name skipped: {}", name)
        return None
    description = str(meta.get("description", "") or "").strip()
    if not description:
        # Description is the palette's only human-facing signal; a command
        # without one would render as a blank row.
        return None
    argument_hint = str(meta.get("argument-hint", meta.get("argument_hint", "")) or "").strip()
    body = parts[2].strip()[:_MAX_BODY_CHARS]
    return {
        "name": name,
        "description": description,
        "argument_hint": argument_hint,
        "body": body,
        "source": source,
    }


def list_custom_commands(session_id: str = "") -> list[dict[str, Any]]:
    """Every command visible to *session_id*, workspace overriding user scope."""
    roots: list[tuple[Path, str]] = [(Path(APP_HOME) / _COMMANDS_DIR, "user")]
    try:
        project = current_project_dir(session_id or None)
        roots.append((project / ".sherry" / _COMMANDS_DIR, "workspace"))
    except Exception:  # noqa: BLE001 - an unreadable project root is not fatal
        logger.debug("custom commands: project root unavailable", exc_info=True)

    found: dict[str, dict[str, Any]] = {}
    for root, source in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            item = _read_one(path, root, source)
            if item is not None:
                found[item["name"]] = item
            if len(found) >= _MAX_COMMANDS:
                break
    return [found[name] for name in sorted(found)]
