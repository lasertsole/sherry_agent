"""System folder listing for the project-directory picker (directories only).

Boundary: this is the one listing that deliberately looks OUTSIDE the session's
project root — choosing a new root cannot be done from inside the current one.
What it answers is deliberately narrow: the direct subdirectory NAMES of one
absolute path, never file names and never file contents (contrast
:mod:`server.service.project_files_service`, which serves the in-root tree and
previews). Auth, CORS and CSRF come from the same global middleware as every
other route, and the entry count is bounded by :data:`FILE_BROWSER`.

The blocking filesystem work runs in a worker thread (``asyncio.to_thread`` at
the route layer), so a slow network mount cannot stall the event loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config.features import FILE_BROWSER
from server.service.project_files_service import FileBrowserError


@dataclass(frozen=True)
class FolderLevel:
    """One directory level of the picker: where we are, how to go up, what is here."""

    path: str
    parent: str | None
    entries: list[dict[str, str]]
    truncated: bool
    total: int


def _is_dir(entry: os.DirEntry[str]) -> bool:
    """A subdirectory, following symlinks; an entry we cannot stat is skipped."""
    try:
        return entry.is_dir(follow_symlinks=True)
    except OSError:
        return False


def list_subdirectories(path: str | None = None) -> FolderLevel:
    """List the subdirectories of ``path`` (empty/None = the user's home).

    Raises :class:`FileBrowserError`: 404 for a directory that does not exist,
    400 for a relative path, a file, or a directory that cannot be read — the
    route maps those onto the response contract.

    ``parent`` is ``None`` at the filesystem root, so the UI's "go up" button has
    a defined stop. Entries are direct children only, sorted case-insensitively.
    """
    target = Path(path).expanduser() if path else Path.home()
    if not target.is_absolute():
        raise FileBrowserError("an absolute path is required")
    try:
        target = target.resolve(strict=True)
    except OSError:
        raise FileBrowserError(f"directory does not exist: {target}", 404) from None
    if not target.is_dir():
        raise FileBrowserError(f"not a directory: {target}")

    try:
        with os.scandir(target) as scan:
            names = sorted((entry.name for entry in scan if _is_dir(entry)), key=str.lower)
    except PermissionError:
        raise FileBrowserError(f"permission denied: {target}") from None
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise FileBrowserError(f"cannot list the directory: {detail}") from None

    cap = int(FILE_BROWSER["max_entries_per_level"])
    parent = target.parent
    return FolderLevel(
        path=str(target),
        parent=None if parent == target else str(parent),
        entries=[{"name": name, "path": str(target / name)} for name in names[:cap]],
        truncated=len(names) > cap,
        total=len(names),
    )
