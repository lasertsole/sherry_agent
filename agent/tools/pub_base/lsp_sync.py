"""Edit-time LSP document sync + debounced auto-diagnostics.

A file-writing tool must not leave the language server analyzing the PREVIOUS
bytes: the next ``lsp_diagnostics`` call would otherwise report errors that were
already fixed (or miss the ones just introduced). Two fail-open halves live here:

* :func:`sync_edited_file` — push the new full text to an ALREADY-ACQUIRED
  server (``didChange`` + ``didSave``; ``didOpen`` when the server has not seen
  the file). No server is started: the LSP layer stays lazy.
* :func:`schedule_auto_diagnostics` — after a short debounce, run one diagnostics
  pass in a background thread and append an evidence row
  (``kind="lsp_diagnostics"``, ``auto=True``). The finish-time regression gate
  replays those rows, so a type error introduced by an edit is confirmed as
  ``[broken]`` instead of living only in a tool result nobody reread.

Both paths are advisory: a missing server, an unreadable file, a dead pipe or a
ledger failure only logs. The edit itself already succeeded.
"""

from __future__ import annotations

import atexit
import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from agent.tools.code_intel.lsp.config_merge import effective_lsp_servers
from agent.tools.code_intel.lsp.manager import get_manager
from agent.tools.code_intel.lsp.protocol import (
    detect_language,
    format_diagnostic,
    path_to_uri,
)
from config.features import LSP
from config.path import resolve_default_project_dir

from .evidence_ledger import EvidenceLedger, normalize_evidence_path
from .path_utils import session_workspace_root

__all__ = [
    "schedule_auto_diagnostics",
    "shutdown_auto_diagnostics",
    "sync_edited_file",
    "sync_edited_path",
]

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lsp-auto-diagnostics")
_LOCK = threading.RLock()
_TIMERS: dict[tuple[str, str], threading.Timer] = {}
#: Insertion-ordered recent files per session; the cap evicts the OLDEST so a
#: long session can still auto-check a newly edited file.
_SESSION_FILES: dict[str, dict[str, None]] = defaultdict(dict)
_STOP = threading.Event()


def _lsp_root(session_id: str = "") -> Path:
    """The session's project root (the same acquisition key the tools use)."""
    override = os.environ.get("SHERRY_LSP_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return session_workspace_root(session_id) or resolve_default_project_dir()


def _client_for(file_path: str, session_id: str = ""):
    """The live client that owns *file_path*'s language, or ``(language, None)``."""
    root = _lsp_root(session_id)
    language = detect_language(file_path, effective_lsp_servers(root).servers)
    if not language:
        return None, None
    return language, get_manager().peek(language, str(root))


def sync_edited_file(file_path: str, content: str, session_id: str = "") -> bool:
    """Push an edited file's full content to its already-acquired server.

    Returns True when a live client accepted the sync. Never starts a server and
    never raises.
    """
    try:
        _language, client = _client_for(file_path, session_id)
        if client is None:
            return False
        client.sync_document(file_path, content)
        if session_id:
            schedule_auto_diagnostics(file_path, session_id)
        return True
    except Exception:  # noqa: BLE001 - the edit already succeeded
        logger.opt(exception=True).debug("LSP edit sync failed (fail-open)")
        return False


def sync_edited_path(file_path: str, session_id: str = "") -> bool:
    """Read the file and :func:`sync_edited_file` it — only when a client exists.

    The read is skipped entirely when no server is acquired, so an edit in a
    project without a running language server pays nothing.
    """
    try:
        _language, client = _client_for(file_path, session_id)
        if client is None:
            return False
        raw = Path(file_path).read_bytes()
        if len(raw) > int(LSP["lsp_max_file_bytes"]):
            return False
        client.sync_document(file_path, raw.decode("utf-8", "replace"))
        if session_id:
            schedule_auto_diagnostics(file_path, session_id)
        return True
    except Exception:  # noqa: BLE001 - the edit already succeeded
        logger.opt(exception=True).debug("LSP path sync failed (fail-open)")
        return False


def schedule_auto_diagnostics(file_path: str, session_id: str) -> None:
    """Debounce then background-check one edited file (fail-open)."""
    if not LSP["lsp_auto_diagnostics_enabled"] or not session_id:
        return
    key = (session_id, file_path)
    with _LOCK:
        files = _SESSION_FILES[session_id]
        cap = max(1, int(LSP["lsp_auto_diagnostics_max_files"]))
        if file_path not in files:
            while len(files) >= cap:
                files.pop(next(iter(files)))
            files[file_path] = None
        previous = _TIMERS.pop(key, None)
        if previous is not None:
            previous.cancel()
        timer = threading.Timer(
            max(0.0, float(LSP["lsp_auto_diagnostics_debounce_s"])),
            _submit,
            args=(file_path, session_id),
        )
        timer.daemon = True
        _TIMERS[key] = timer
        timer.start()


def _submit(file_path: str, session_id: str) -> None:
    with _LOCK:
        _TIMERS.pop((session_id, file_path), None)
    if _STOP.is_set():
        return
    try:
        _EXECUTOR.submit(_run_once, file_path, session_id)
    except RuntimeError:  # pragma: no cover - interpreter shutdown
        pass


def _run_once(file_path: str, session_id: str) -> None:
    """One diagnostics pass -> one evidence row (no row when nothing published)."""
    try:
        _language, client = _client_for(file_path, session_id)
        if client is None:
            return
        try:
            raw = Path(file_path).read_bytes()
        except OSError:
            return
        if len(raw) > int(LSP["lsp_max_file_bytes"]):
            return
        uri = path_to_uri(file_path)
        cached_before = client.cached_diagnostics(uri)
        generation = client.diagnostics_generation(uri)
        changed = client.sync_document(file_path, raw.decode("utf-8", "replace"))
        if changed or cached_before is None or client.diagnostics_pending(uri):
            published = client.wait_diagnostics_since(
                uri,
                generation,
                max(0.1, float(LSP["lsp_auto_diagnostics_timeout_s"])),
            )
        else:
            published = cached_before
        if published is None:
            return
        diagnostics = [
            formatted for item in published if (formatted := format_diagnostic(item)) is not None
        ]
        errors = [item for item in diagnostics if item["severity"] == "error"]
        relative = normalize_evidence_path(str(file_path), session_id)
        summary = "; ".join(
            f"{item['message']} (line {item['range']['start_line']})" for item in errors[:5]
        )
        EvidenceLedger.for_session(session_id).append(
            kind="lsp_diagnostics",
            command=f"lsp_diagnostics {relative}",
            file_path=relative,
            status="failed" if errors else "passed",
            exit_code=1 if errors else 0,
            output_snippet=summary[:500],
            auto=True,
            modified_files=[relative],
        )
    except Exception as exc:  # noqa: BLE001 - advisory only
        logger.debug("auto diagnostics failed (fail-open): {}", exc)


def shutdown_auto_diagnostics() -> None:
    """Cancel pending timers and stop accepting new work (process teardown)."""
    _STOP.set()
    with _LOCK:
        for timer in _TIMERS.values():
            timer.cancel()
        _TIMERS.clear()
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)


atexit.register(shutdown_auto_diagnostics)
