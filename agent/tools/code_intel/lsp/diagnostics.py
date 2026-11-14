"""Process-level registry of ``textDocument/publishDiagnostics`` results.

The LSP client already caches each file's last published diagnostics, but that
cache is private to one client. Two features need a process-wide view:

* **passive injection** — a ``<system-reminder source="lsp_diagnostics">`` on
  the next turn's first model call, so an edit that introduces an error is
  visible to the agent immediately instead of only when it calls
  ``lsp_diagnostics``;
* **auto-diagnostics evidence** — the finish-time regression gate replays the
  diagnostics rows the edit path wrote.

Delivery is per SESSION: two sessions can share one language server and one
file, and each must see the error once. A new publish resets every session's
delivered mark for that file (the diagnostics changed), while an unchanged
republish (many servers publish on every keystroke) resets only when the error
set actually differs — a byte-identical republish must not re-inject.

Fail-open by construction: every function swallows its own errors and a broken
entry is simply skipped.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from .protocol import uri_to_path

__all__ = [
    "clear_client",
    "mark_delivered",
    "publish",
    "reset_for_tests",
    "snapshot",
    "undelivered_errors",
]


@dataclass
class _Entry:
    """One file's last published diagnostics."""

    path: str
    client_key: str
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0
    delivered_sessions: set[str] = field(default_factory=set)

    def errors(self) -> list[dict[str, Any]]:
        """Severity-1 diagnostics only (LSP: 1 = Error)."""
        return [
            item
            for item in self.diagnostics
            if isinstance(item, dict) and item.get("severity") == 1
        ]


_LOCK = threading.RLock()
_ENTRIES: dict[str, _Entry] = {}


def _fingerprint(diagnostics: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    """Stable identity of a diagnostic list, for republish change detection."""
    return tuple(
        sorted(
            (str(item.get("message")), str(item.get("source")), str(item.get("code")))
            for item in diagnostics
            if isinstance(item, dict)
        )
    )


def publish(client_key: str, uri: str, diagnostics: list) -> None:
    """Record one publish; reset delivery only when the content changed."""
    try:
        path = uri_to_path(uri)
        if not path:
            return
        clean = [item for item in diagnostics if isinstance(item, dict)]
        with _LOCK:
            entry = _ENTRIES.get(path)
            changed = entry is None or _fingerprint(entry.diagnostics) != _fingerprint(clean)
            if entry is None:
                entry = _Entry(path=path, client_key=client_key)
            entry.client_key = client_key or entry.client_key
            entry.diagnostics = clean
            entry.updated_at = time.time()
            if changed:
                entry.delivered_sessions.clear()
            _ENTRIES[path] = entry
    except Exception:  # noqa: BLE001 - an observer must never break the reader thread
        logger.opt(exception=True).debug("LSP diagnostics registry publish failed")


def clear_client(client_key: str) -> None:
    """Drop every file published by one client (its server is gone)."""
    if not client_key:
        return
    with _LOCK:
        for path in [path for path, entry in _ENTRIES.items() if entry.client_key == client_key]:
            _ENTRIES.pop(path, None)


def _is_under(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def undelivered_errors(
    session_id: str,
    root: Path,
    *,
    max_files: int,
    max_errors: int,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Files under *root* with undelivered errors for *session_id*.

    Newest publish first, then capped by files and total entries. A file that
    no longer exists is skipped (its diagnostics are moot).
    """
    if not session_id or max_files <= 0 or max_errors <= 0:
        return []
    try:
        with _LOCK:
            candidates: list[tuple[float, str, list[dict[str, Any]]]] = []
            for entry in _ENTRIES.values():
                if session_id in entry.delivered_sessions:
                    continue
                if not _is_under(entry.path, root):
                    continue
                if not Path(entry.path).is_file():
                    continue
                errors = entry.errors()
                if errors:
                    candidates.append((entry.updated_at, entry.path, errors))
            candidates.sort(key=lambda item: item[0], reverse=True)
            result: list[tuple[str, list[dict[str, Any]]]] = []
            remaining = max_errors
            for _updated, path, errors in candidates[:max_files]:
                if remaining <= 0:
                    break
                take = errors[:remaining]
                result.append((path, take))
                remaining -= len(take)
            return result
    except Exception:  # noqa: BLE001 - injection is advisory
        logger.opt(exception=True).debug("LSP diagnostics registry read failed")
        return []


def mark_delivered(session_id: str, paths: list[str]) -> None:
    """Mark *paths*' current diagnostics as seen by *session_id*."""
    if not session_id:
        return
    with _LOCK:
        for path in paths:
            entry = _ENTRIES.get(path)
            if entry is not None:
                entry.delivered_sessions.add(session_id)


def snapshot() -> list[dict[str, Any]]:
    """Test/diagnostic view of the registry (absolute paths included)."""
    with _LOCK:
        return [
            {
                "path": entry.path,
                "client_key": entry.client_key,
                "errors": len(entry.errors()),
                "total": len(entry.diagnostics),
                "delivered_sessions": sorted(entry.delivered_sessions),
            }
            for entry in _ENTRIES.values()
        ]


def reset_for_tests() -> None:
    """Clear the registry (test teardown hook)."""
    with _LOCK:
        _ENTRIES.clear()
