"""Lazy sync of workspace system files from the language template directory.

The ``workspace/`` root may not ship with its persona files (AGENTS.md,
SOUL.md, IDENTITY.md, USER.md) pre-created; they are instead copied in from
``workspace/template/<lang>/`` on first use. This keeps user-authored edits in
the workspace root authoritative — existing files are never overwritten — while
still guaranteeing that any task expecting a system file finds it present.
"""

from __future__ import annotations

import logging
import shutil

from config.path import WORKSPACE_DIR, resolve_workspace_template_dir
from workspace import ALL_SYSTEM_FILE_NAMES

logger = logging.getLogger(__name__)


def ensure_workspace_system_files(lang: str | None = None) -> list[str]:
    """Copy any missing workspace system files from the template directory.

    For each file name in ``ALL_SYSTEM_FILE_NAMES``, if it does not already
    exist under the workspace root, copy the matching template from the
    language directory resolved for ``lang``. Existing files are left
    untouched so user customisations are never overwritten (idempotent).

    Returns the list of files actually copied (empty when nothing was needed).
    """
    template_dir = resolve_workspace_template_dir(lang)
    copied: list[str] = []

    for name in ALL_SYSTEM_FILE_NAMES:
        target = WORKSPACE_DIR / name
        if target.exists():
            continue
        source = template_dir / name
        if not source.is_file():
            logger.warning(
                "Workspace system file %r missing and no template at %r",
                name,
                source,
            )
            continue
        try:
            shutil.copy2(source, target)
        except OSError:
            logger.exception(
                "Failed to copy workspace system file %r from %r to %r",
                name,
                source,
                target,
            )
            raise
        copied.append(name)
        logger.info("Copied workspace system file %r from %r", name, source)

    return copied
