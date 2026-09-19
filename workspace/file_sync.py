"""Lazy sync of workspace system files from the language template directory.

The ``workspace/`` root may not ship with its persona files (AGENTS.md,
SOUL.md, USER.md) pre-created; they are instead copied in from
``workspace/template/<lang>/`` on first use. Memory system files
(``MEMORY_SYSTEM_FILE_NAMES``, e.g. FACTS.md) are copied from the same
template directory into ``workspace/memory/``. User-authored edits stay
authoritative — existing files are never overwritten — while any task
expecting a system file still finds it present.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from config.path import MEMORY_DIR, WORKSPACE_DIR, resolve_workspace_template_dir
from workspace import ALL_SYSTEM_FILE_NAMES, MEMORY_SYSTEM_FILE_NAMES

logger = logging.getLogger(__name__)


def _memory_file_has_content(path: Path) -> bool:
    """True when a memory file exists with non-whitespace content.

    A file created empty (first-boot ``touch()``) counts as missing so the
    template's purpose comment can still land; authored content is never
    overwritten.
    """
    if not path.exists():
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _copy_template_file(name: str, source: Path, target: Path) -> bool:
    """Copy one template file into place. Returns True when it was copied."""
    if not source.is_file():
        logger.warning(
            "Workspace system file %r missing and no template at %r",
            name,
            source,
        )
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError:
        logger.exception(
            "Failed to copy workspace system file %r from %r to %r",
            name,
            source,
            target,
        )
        raise
    logger.info("Copied workspace system file %r from %r", name, source)
    return True


def ensure_workspace_system_files(lang: str | None = None) -> list[str]:
    """Copy any missing workspace system files from the template directory.

    For each file name in ``ALL_SYSTEM_FILE_NAMES``, if it does not already
    exist under the workspace root, copy the matching template from the
    language directory resolved for ``lang``. For each name in
    ``MEMORY_SYSTEM_FILE_NAMES``, do the same under ``workspace/memory/`` —
    there an empty file also counts as missing. Existing files are left
    untouched so user customisations are never overwritten (idempotent).

    Returns the list of files actually copied (empty when nothing was needed);
    memory files are reported as ``memory/<name>``.
    """
    template_dir = resolve_workspace_template_dir(lang)
    copied: list[str] = []

    for name in ALL_SYSTEM_FILE_NAMES:
        target = WORKSPACE_DIR / name
        if target.exists():
            continue
        if _copy_template_file(name, template_dir / name, target):
            copied.append(name)

    for name in MEMORY_SYSTEM_FILE_NAMES:
        target = MEMORY_DIR / name
        if _memory_file_has_content(target):
            continue
        if _copy_template_file(name, template_dir / name, target):
            copied.append(f"memory/{name}")

    return copied
