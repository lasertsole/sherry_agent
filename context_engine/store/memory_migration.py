"""One-shot import of the legacy Markdown memory files into SQLite.

Before the memory store moved to SQLite, the three targets lived in
``workspace/memory/{MEMORY,USER,FACTS}.md`` as ``§``-delimited entries. This
module imports them once, atomically, and renames each source file to
``*.md.bak`` so a human can still inspect (or restore) the old text.

Idempotency: any existing ``memory_entries`` row — archived included — means the
import already ran, so the files are left alone. That covers the partial-failure
case where the transaction committed but a rename failed: the next boot sees the
rows and never re-imports duplicates.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from loguru import logger

from config import MEMORY_DIR
from context_engine.store.memory_entry_repository import (
    MEMORY_TARGETS,
    MemoryEntryRepository,
    memory_entry_repo,
)

#: Same delimiter as ``agent.tools.memory.ENTRY_DELIMITER``; duplicated because
#: context_engine must not import agent (the package-import contract).
ENTRY_DELIMITER = "\n§\n"

_FILE_BY_TARGET: dict[str, str] = {
    "memory": "MEMORY.md",
    "user": "USER.md",
    "facts": "FACTS.md",
}
_HEADERS: frozenset[str] = frozenset({"# MEMORY", "# USER", "# FACTS"})
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ENTRY_TYPE_BY_TARGET: dict[str, str] = {
    "memory": "project",
    "user": "user",
    "facts": "system",
}


def split_legacy_memory_content(raw: str) -> list[str]:
    if not raw.strip():
        return []
    # Older installs may carry the template's purpose comment in a legacy file
    # (older releases seeded it). That comment is documentation, not an entry —
    # without this strip the import would create a junk memory row.
    raw = _HTML_COMMENT_RE.sub("", raw).strip()
    if not raw:
        return []
    parts = [part.strip() for part in raw.split(ENTRY_DELIMITER)]
    # An old file may carry an H1 title as its first §-segment; it is a header,
    # never an entry.
    if parts and parts[0].upper() in _HEADERS:
        parts.pop(0)
    # Older still: a bare title line followed directly by entries.
    entries = [part for part in parts if part]
    if entries and entries[0].upper() in _HEADERS:
        entries.pop(0)
    return list(dict.fromkeys(entry for entry in entries if entry))


def migrate_legacy_memory_files(
    repo: MemoryEntryRepository | None = None,
    *,
    skip_in_tests: bool = True,
) -> int:
    """Import the legacy Markdown files once. Returns the number of entries added.

    All inserts run in one transaction. The source files are renamed only after
    the commit; a rename failure is logged and the files are left in place —
    the next boot skips the import because rows now exist.

    ``skip_in_tests`` keeps a test suite that calls ``agent_core.init()`` from
    renaming the developer's real ``workspace/memory/*.md`` files; the
    migration tests pass ``False`` (they point ``MEMORY_DIR`` at a tmp dir).
    """
    if skip_in_tests and os.environ.get("PYTEST_CURRENT_TEST"):
        return 0
    repository = repo or memory_entry_repo
    try:
        if repository.count_entries() > 0:
            return 0
    except Exception:  # noqa: BLE001 - never block boot on the migration probe
        logger.exception("memory migration: could not read memory_entries; skipping")
        return 0

    sources: list[tuple[str, Path, int]] = []
    for target in MEMORY_TARGETS:
        path = MEMORY_DIR / _FILE_BY_TARGET[target]
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("memory migration: cannot read {}: {}", path, exc)
            continue
        entries = split_legacy_memory_content(raw)
        if not entries:
            continue
        try:
            mtime_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            mtime_ms = 0
        sources.append((target, path, mtime_ms))

    if not sources:
        return 0

    added = 0
    try:
        with repository.transaction():
            for target, _path, mtime_ms in sources:
                path = MEMORY_DIR / _FILE_BY_TARGET[target]
                entries = split_legacy_memory_content(path.read_text(encoding="utf-8"))
                for entry in entries:
                    repository.add_entry(
                        target,
                        entry,
                        entry_type=_ENTRY_TYPE_BY_TARGET[target],
                        origin_session=None,
                        created_at=mtime_ms,
                    )
                    added += 1
    except Exception:  # noqa: BLE001 - the transaction rolled back; retry next boot
        logger.exception("memory migration: import failed; legacy files left untouched")
        return 0

    for _target, path, _mtime_ms in sources:
        backup = path.with_name(path.name + ".bak")
        try:
            os.replace(path, backup)
        except OSError as exc:
            logger.warning(
                "memory migration: imported data but could not rename {} to {}: {}",
                path,
                backup,
                exc,
            )
    logger.info("memory migration: imported {} entries from legacy Markdown files", added)
    return added
