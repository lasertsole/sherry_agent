"""Read-only project file browsing: one directory level per call, one file preview.

Boundary: every path is resolved with :func:`agent.tools.pub_base.resolve_within`
against the SESSION's project directory (``PROJECT_DIR``, else the process
default). Escapes are refused outright — deliberately NOT routed through the
agent's ``resolve_external_path`` HITL flow: that flow is an interactive approval
for the agent, keyed per session and cumulative, while this is a human looking at
their own project.

Read side hardening:

- ``_open_no_follow`` closes the TOCTOU window between ``resolve()`` and ``open()``
  (the file browser is the first SERVER-side caller of it);
- responses never echo raw exceptions: ``PathOutOfBoundsError`` names the resolved
  path and the root, so failures become :func:`safe_error_detail` or a fixed
  message;
- size, entry-count, depth and encoding are bounded by :data:`FILE_BROWSER`.

The blocking filesystem work runs in a worker thread (``asyncio.to_thread`` at
the route layer), so a slow network mount cannot stall the event loop.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config.features import FILE_BROWSER
from pub.func.validator import is_safe_session_id
from agent.tools.pub_base import (
    PathOutOfBoundsError,
    _open_no_follow,
    resolve_within,
    safe_error_detail,
)
from runtime.session.project_dir import current_project_dir, project_dir_source

__all__ = [
    "FileBrowserError",
    "FileContent",
    "TreeLevel",
    "list_level",
    "read_file",
    "read_file_bytes",
]


class FileBrowserError(Exception):
    """A refusal with a client-safe message and an HTTP status to answer with."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class TreeLevel:
    """One directory level: its entries plus the truncation facts."""

    root: str
    path: str
    entries: list[dict]
    truncated: bool
    total: int


@dataclass(frozen=True)
class FileContent:
    """One file preview."""

    path: str
    content: str
    size: int
    truncated: bool


def _session_root(session_id: str) -> Path:
    """The session's read boundary, or a refusal when it is unknown."""
    if not session_id or not is_safe_session_id(session_id):
        raise FileBrowserError("invalid session_id")
    root = current_project_dir(session_id)
    if project_dir_source(session_id) == "default":
        # Unbound: the process default (the repository itself). Browsing it is
        # allowed for API parity but the UI shows an empty state instead — the
        # response carries the fact so a client can do the same.
        logger.debug(
            "project files: session {} is unbound; serving the process default {}", session_id, root
        )
    return root


def _skipper():
    skip_dirs = FILE_BROWSER["skip_dirs"]
    skip_suffixes = FILE_BROWSER["skip_suffixes"]
    return skip_dirs, skip_suffixes


def list_level(session_id: str, rel_path: str = "") -> TreeLevel:
    """List one level of the session's project tree (blocking: use a thread)."""
    root = _session_root(session_id)
    rel = (rel_path or "").strip().lstrip("/")
    if rel.count("/") + 1 > FILE_BROWSER["max_tree_depth"]:
        raise FileBrowserError(
            f"path is deeper than the {FILE_BROWSER['max_tree_depth']} level limit"
        )

    try:
        target = resolve_within(root, rel) if rel else root
    except PathOutOfBoundsError:
        raise FileBrowserError("path escapes the project directory") from None
    except OSError as exc:
        raise FileBrowserError(safe_error_detail(exc)) from None

    if not target.is_dir():
        raise FileBrowserError("path is not a directory")

    skip_dirs, skip_suffixes = _skipper()
    max_entries = FILE_BROWSER["max_entries_per_level"]

    entries: list[dict] = []
    try:
        # Dirs first, then files, each alphabetically — the same ordering the
        # skills tree uses, so both trees read identically.
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            name = child.name
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir and name in skip_dirs:
                continue
            if not is_dir and child.suffix in skip_suffixes:
                continue
            if is_dir:
                entries.append({"name": name, "type": "dir"})
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                entries.append({"name": name, "type": "file", "size": size})
    except OSError as exc:
        raise FileBrowserError(safe_error_detail(exc)) from None

    total = len(entries)
    truncated = total > max_entries
    return TreeLevel(
        root=str(root),
        path=rel,
        entries=entries[:max_entries],
        truncated=truncated,
        total=total,
    )


def _resolve_file_target(session_id: str, rel_path: str) -> Path:
    """The shared prologue of both file readers: boundary + existence checks.

    Raises :class:`FileBrowserError` with the reader-agnostic refusals (400
    escape / 404 missing / 400 directory); size caps and decoding stay with the
    callers, which answer different media.
    """
    root = _session_root(session_id)
    rel = (rel_path or "").strip().lstrip("/")
    if not rel:
        raise FileBrowserError("path is required")

    try:
        target = resolve_within(root, rel)
    except PathOutOfBoundsError:
        raise FileBrowserError("path escapes the project directory") from None
    except OSError as exc:
        raise FileBrowserError(safe_error_detail(exc)) from None

    if not target.exists():
        raise FileBrowserError("file not found", status=404)
    if target.is_dir():
        raise FileBrowserError("path is a directory")

    try:
        # A liveness probe before the open: a file that vanished between
        # exists() and open() surfaces as a safe refusal, not a raw OSError.
        target.stat()
    except OSError as exc:
        raise FileBrowserError(safe_error_detail(exc)) from None
    return target


def _read_raw(target: Path) -> bytes:
    """Read the file's bytes through ``_open_no_follow`` (shared tail)."""
    try:
        fd = _open_no_follow(target, os.O_RDONLY)
        try:
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise FileBrowserError(safe_error_detail(exc)) from None


def read_file(session_id: str, rel_path: str) -> FileContent:
    """Read one file preview (blocking: use a thread).

    413 above ``max_read_bytes``, 415 for a file that is not UTF-8 text — never
    decoded with ``errors="replace"`` (that would render binary as mojibake).
    """
    target = _resolve_file_target(session_id, rel_path)
    raw = _read_raw(target)
    if len(raw) > FILE_BROWSER["max_read_bytes"]:
        raise FileBrowserError(
            f"file too large ({len(raw)} bytes, limit {FILE_BROWSER['max_read_bytes']})", status=413
        )

    try:
        text = raw.decode("utf-8-sig")  # strips a UTF-8 BOM, like read_file
    except UnicodeDecodeError:
        raise FileBrowserError("binary file (not UTF-8 text)", status=415) from None

    return FileContent(
        path=rel_path.strip().lstrip("/"), content=text, size=len(raw), truncated=False
    )


def read_file_bytes(session_id: str, rel_path: str) -> tuple[bytes, str]:
    """Read one file as raw bytes for binary rendering (blocking: use a thread).

    The blob sibling of :func:`read_file` — same boundary (``resolve_within`` +
    ``_open_no_follow``), same error protocol, but the size cap is the wider
    ``max_blob_bytes`` (rendered documents are bigger than text previews) and
    the decode step is replaced by a content-type guess: ``mimetypes`` on the
    file name, ``application/octet-stream`` when unknown.

    :returns: ``(data, mime)``.
    """
    target = _resolve_file_target(session_id, rel_path)
    data = _read_raw(target)
    if len(data) > FILE_BROWSER["max_blob_bytes"]:
        raise FileBrowserError(
            f"file too large ({len(data)} bytes, limit {FILE_BROWSER['max_blob_bytes']})",
            status=413,
        )
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return data, mime
