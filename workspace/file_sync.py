"""Lazy sync of workspace system files from the language template directory.

The ``workspace/`` root may not ship with its persona files (AGENTS.md,
SOUL.md, USER.md) pre-created; they are instead copied in from
``workspace/template/<lang>/`` on first use. User-authored edits stay
authoritative — existing files are never overwritten — while any task
expecting a system file still finds it present. Long-term memory is SQLite
now, so the legacy ``workspace/memory/*.md`` files are no longer seeded; the
one-shot migration still imports any that an older install left behind.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from config.path import WORKSPACE_DIR, resolve_workspace_template_dir
from loguru import logger

from workspace import ALL_SYSTEM_FILE_NAMES


def _copy_template_file(name: str, source: Path, target: Path) -> bool:
    """Copy one template file into place. Returns True when it was copied."""
    if not source.is_file():
        logger.warning(
            "Workspace system file {!r} missing and no template at {!r}",
            name,
            source,
        )
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError:
        logger.opt(exception=True).error(
            "Failed to copy workspace system file {!r} from {!r} to {!r}",
            name,
            source,
            target,
        )
        raise
    logger.info("Copied workspace system file {!r} from {!r}", name, source)
    return True


def ensure_workspace_system_files(lang: str | None = None) -> list[str]:
    """Copy any missing workspace system files from the template directory.

    For each file name in ``ALL_SYSTEM_FILE_NAMES``, if it does not already
    exist under the workspace root, copy the matching template from the
    language directory resolved for ``lang``. Existing files are left untouched
    so user customisations are never overwritten (idempotent).

    Returns the list of files actually copied (empty when nothing was needed).
    """
    # The packaged app's first run has no workspace root: the write path opens
    # ``workspace/<file>`` directly, so "no template got copied" must still
    # leave the root in place instead of surfacing a FileNotFoundError.
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    template_dir = resolve_workspace_template_dir(lang)
    copied: list[str] = []

    for name in ALL_SYSTEM_FILE_NAMES:
        target = WORKSPACE_DIR / name
        if target.exists():
            continue
        if _copy_template_file(name, template_dir / name, target):
            copied.append(name)

    return copied
