"""LSP JSON-RPC client over stdio.

One client owns exactly one language-server subprocess. Messages are framed with
``Content-Length`` headers; a daemon reader thread dispatches responses to their
waiting request ids and buffers ``textDocument/publishDiagnostics`` notifications.
Every blocking wait is bounded by a timeout, and :meth:`LSPClient.shutdown` /
:meth:`LSPClient.force_kill` guarantee the child is reaped — a client never
leaves an orphan process behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from collections.abc import Callable
from typing import IO

from loguru import logger

from config.features import LSP

from .protocol import language_id, path_to_uri, to_position

__all__ = ["LSPClient"]

_TIMEOUT_ERROR_CODE = -32000
_CLOSED_ERROR_CODE = -32001
_CONTENT_MODIFIED_CODE = -32801

_CLIENT_CAPABILITIES: dict = {
    "textDocument": {
        "definition": {},
        "references": {},
        "callHierarchy": {},
        "publishDiagnostics": {},
        "rename": {"prepareSupport": False},
        "formatting": {},
        "rangeFormatting": {},
        "hover": {},
    },
    "workspace": {"symbol": {}},
}


class LSPClient:
    """Synchronous LSP client (call from a worker thread, not the event loop)."""

    def __init__(
        self,
        language: str,
        cwd: str,
        command: list[str],
        *,
        request_timeout_s: float,
        start_timeout_s: float,
        max_file_bytes: int,
        max_opened_files: int,
        client_key: str = "",
        diagnostics_sink: Callable[[str, str, list], None] | None = None,
    ) -> None:
        self.language = language
        self.cwd = cwd
        self._command = command
        self._request_timeout_s = request_timeout_s
        self._start_timeout_s = start_timeout_s
        self._max_file_bytes = max_file_bytes
        self._max_opened_files = max_opened_files
        self.client_key = client_key
        self._diagnostics_sink = diagnostics_sink

        self._proc: subprocess.Popen[bytes] | None = None
        self._stdin: IO[bytes] | None = None
        self._stdout: IO[bytes] | None = None
        self._reader: threading.Thread | None = None

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._events: dict[int, threading.Event] = {}
        self._responses: dict[int, dict] = {}
        self._diagnostics: dict[str, list] = {}
        self._diagnostics_seq: dict[str, int] = {}
        #: URIs whose current server version has not produced a publish yet. A
        #: didChange sent by an edit must not let the next diagnostics call
        #: return the PREVIOUS publish as if it described the new bytes.
        self._diagnostics_pending: set[str] = set()
        self._opened: dict[str, int] = {}
        #: URI -> sha256 of the last content this server was told about. Lets a
        #: no-op refresh skip the notification AND the diagnostics wait.
        self._synced_hashes: dict[str, str] = {}
        self._closed = True
        self.last_used = time.time()

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        """True while the server subprocess is running."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        """PID of the server subprocess, or ``None`` before start."""
        return self._proc.pid if self._proc is not None else None

    def touch(self) -> None:
        """Mark the client as recently used (idle-sweeper bookkeeping)."""
        self.last_used = time.time()

    def start(self) -> str | None:
        """Spawn the server and complete the initialize handshake.

        Returns ``None`` on success, or a human-readable error message (the
        child process is reaped before returning on any failure).
        """
        if self.alive:
            return None
        # Lazy: importing pub_base pulls the runtime/robyn chain at import time.
        from agent.tools.pub_base.env_scrub import scrub_env

        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.cwd,
                env=scrub_env(os.environ.copy()),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._proc = None
            return f"failed to start LSP server {' '.join(self._command)}: {exc}"

        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop, name=f"lsp-reader-{self.language}", daemon=True
        )
        self._reader.start()

        root_uri = path_to_uri(self.cwd)
        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": _CLIENT_CAPABILITIES,
            "workspaceFolders": [{"uri": root_uri, "name": Path(self.cwd).name or "root"}],
        }
        response = self.request("initialize", params, timeout=self._start_timeout_s)
        if "error" in response or "result" not in response:
            error = (response.get("error") or {}).get("message", "no initialize result")
            self.force_kill()
            return f"LSP initialize failed for {self.language}: {error}"

        self.notify("initialized", {})
        self.touch()
        return None

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Ask the server to shut down, then force-reap it if it lingers."""
        if self._proc is None:
            return
        if self.alive:
            self.request("shutdown", None, timeout=timeout_s)
            self.notify("exit", None)
        self.force_kill()

    def force_kill(self) -> None:
        """Close stdin, terminate, then kill — never leaves an orphan process."""
        proc = self._proc
        if proc is None:
            return
        stdin = self._stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass  # noqa: S110 - best-effort pipe cleanup
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # noqa: S110 - process already unreapable; nothing left to do
        self._proc = None
        self._stdin = None
        self._stdout = None
        self._closed = True
        reader = self._reader
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    # ── requests ─────────────────────────────────────────────────────────────

    def notify(self, method: str, params: dict | None) -> bool:
        """Send a JSON-RPC notification (no response expected)."""
        return self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict | None, timeout: float | None = None) -> dict:
        """Send a JSON-RPC request, retrying ``ContentModified`` with backoff.

        A language server answers ``-32801`` while it reindexes after an edit
        (rust-analyzer is the common case); failing the tool then would report a
        transient state as a hard error. Retries are bounded by
        ``lsp_content_modified_max_retries`` with an exponential backoff capped
        at that many doublings of ``lsp_content_modified_backoff_s``.
        """
        max_retries = max(0, int(LSP["lsp_content_modified_max_retries"]))
        backoff = max(0.0, float(LSP["lsp_content_modified_backoff_s"]))
        attempt = 0
        while True:
            response = self._request_once(method, params, timeout)
            error = response.get("error")
            if (
                isinstance(error, dict)
                and error.get("code") == _CONTENT_MODIFIED_CODE
                and attempt < max_retries
            ):
                attempt += 1
                logger.debug(
                    "LSP {} answered ContentModified; retry {}/{}",
                    self.language,
                    attempt,
                    max_retries,
                )
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            return response

    def _request_once(self, method: str, params: dict | None, timeout: float | None = None) -> dict:
        """One attempt: send a request and await its response.

        Returns the raw response dict (``result`` or ``error``). A timeout or a
        dead server returns a synthetic error response instead of raising.
        """
        effective_timeout = self._request_timeout_s if timeout is None else timeout
        with self._state_lock:
            if self._closed or not self.alive:
                return _error(_CLOSED_ERROR_CODE, "LSP server is not running")
            self._next_id += 1
            request_id = self._next_id
            event = threading.Event()
            self._events[request_id] = event

        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        if not self._send(message):
            with self._state_lock:
                self._events.pop(request_id, None)
            return _error(_CLOSED_ERROR_CODE, "LSP server pipe is closed")

        if not event.wait(effective_timeout):
            with self._state_lock:
                self._events.pop(request_id, None)
                self._responses.pop(request_id, None)
            return _error(
                _TIMEOUT_ERROR_CODE,
                f"LSP request '{method}' timed out after {effective_timeout}s",
            )

        with self._state_lock:
            self._events.pop(request_id, None)
            response = self._responses.pop(request_id, None)
        if response is None:
            return _error(_CLOSED_ERROR_CODE, "LSP server closed before responding")
        return response

    def open_file(self, file_path: str) -> str | None:
        """Read and ``didOpen`` a file (bounded); returns an error string or ``None``."""
        try:
            raw = Path(file_path).read_bytes()
        except OSError as exc:
            return f"cannot read {file_path}: {exc}"
        if len(raw) > self._max_file_bytes:
            return f"file too large to open: {len(raw)} bytes > {self._max_file_bytes}"
        uri = path_to_uri(file_path)
        with self._state_lock:
            if uri in self._opened:
                return None
        text = raw.decode("utf-8", "replace")
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id(self.language),
                    "version": 1,
                    "text": text,
                }
            },
        )
        with self._state_lock:
            self._opened[uri] = 1
            self._synced_hashes[uri] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self._diagnostics_pending.add(uri)
            victims = self._pop_overflow_opened_locked()
        for victim in victims:
            self.notify("textDocument/didClose", {"textDocument": {"uri": victim}})
        return None

    def sync_document(self, file_path: str, content: str) -> bool:
        """Push an edited file's FULL content to the server (didChange/didSave).

        Returns True when a notification was actually sent. Identical content is
        a no-op: a repeated diagnostics call must not depend on the server
        republishing an unchanged file just to answer.

        Used after a file-writing tool succeeds. The server may not have the
        file open yet (it was acquired for another file), so the first sync is a
        ``didOpen``; later ones bump the version and send ``didChange`` with the
        whole text, then ``didSave``. Full-content sync avoids range math and
        keeps the server's view exactly equal to what is on disk. Fail-open: a
        dead pipe makes the notifications no-ops.
        """
        if not self.alive:
            return False
        raw = content.encode("utf-8", "replace")
        if len(raw) > self._max_file_bytes:
            return False
        uri = path_to_uri(file_path)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._state_lock:
            version = self._opened.get(uri)
            if version is not None and self._synced_hashes.get(uri) == digest:
                return False
            opened = version is None
            if opened:
                self._opened[uri] = 1
                victims = self._pop_overflow_opened_locked()
            else:
                new_version = int(version) + 1
                self._opened[uri] = new_version
                victims = []
            self._synced_hashes[uri] = digest
            self._diagnostics_pending.add(uri)
        if opened:
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id(self.language),
                        "version": 1,
                        "text": content,
                    }
                },
            )
        else:
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": new_version},
                    "contentChanges": [{"text": content}],
                },
            )
        self.notify(
            "textDocument/didSave",
            {"textDocument": {"uri": uri}, "text": content},
        )
        for victim in victims:
            self._synced_hashes.pop(victim, None)
            self._diagnostics_pending.discard(victim)
            self.notify("textDocument/didClose", {"textDocument": {"uri": victim}})
        return True

    def refresh_from_disk(self, file_path: str) -> tuple[str | None, bool]:
        """Sync the server's view to the file's CURRENT bytes (bounded).

        Returns ``(error, changed)``: an error string for an unreadable/oversized
        file, else ``None``; ``changed`` says whether the server received a new
        version (False means the server's view is already current). The
        diagnostics tool calls this before waiting: a file edited by a tool that
        predates the edit-sync hook must still be analyzed as it is now.
        """
        try:
            raw = Path(file_path).read_bytes()
        except OSError as exc:
            return f"cannot read {file_path}: {exc}", False
        if len(raw) > self._max_file_bytes:
            return f"file too large to sync: {len(raw)} bytes > {self._max_file_bytes}", False
        return None, self.sync_document(file_path, raw.decode("utf-8", "replace"))

    def diagnostics_generation(self, uri: str) -> int:
        """A monotonic counter of publishes for *uri* (0 = never published)."""
        with self._state_lock:
            return self._diagnostics_seq.get(uri, 0)

    def diagnostics_pending(self, uri: str) -> bool:
        """Whether a sent version is still waiting for its publish."""
        with self._state_lock:
            return uri in self._diagnostics_pending

    def wait_diagnostics_since(self, uri: str, generation: int, timeout_s: float) -> list | None:
        """Wait for a publish NEWER than *generation*; None on timeout.

        A plain ``wait_diagnostics`` returns an existing cache immediately, which
        is stale after an edit. Comparing generations makes "the server has
        re-analyzed this version" observable even when the diagnostics are
        identical to the previous round.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._state_lock:
                if self._diagnostics_seq.get(uri, 0) > generation:
                    return list(self._diagnostics.get(uri, []))
            time.sleep(0.05)
        return None

    def position_params(self, file_path: str, line: int, character: int) -> dict:
        """Build the ``TextDocumentPositionParams`` for a 1-based position."""
        return {
            "textDocument": {"uri": path_to_uri(file_path)},
            "position": to_position(line, character),
        }

    def wait_diagnostics(self, uri: str, timeout_s: float) -> list:
        """Poll for ``publishDiagnostics`` on *uri* until *timeout_s* elapses."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._state_lock:
                if uri in self._diagnostics:
                    return list(self._diagnostics[uri])
            time.sleep(0.05)
        return []

    def cached_diagnostics(self, uri: str) -> list | None:
        """Return the last published diagnostics for *uri*, or ``None`` if none arrived."""
        with self._state_lock:
            if uri not in self._diagnostics:
                return None
            return list(self._diagnostics[uri])

    # ── internals ────────────────────────────────────────────────────────────

    def _pop_overflow_opened_locked(self) -> list[str]:
        victims: list[str] = []
        while len(self._opened) > self._max_opened_files:
            oldest = next(iter(self._opened))
            del self._opened[oldest]
            victims.append(oldest)
        return victims

    def _send(self, message: dict) -> bool:
        stdin = self._stdin
        if stdin is None:
            return False
        body = json.dumps(message).encode("utf-8")
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            try:
                stdin.write(framed)
                stdin.flush()
            except (OSError, ValueError):
                return False
        return True

    def _read_loop(self) -> None:
        try:
            while True:
                message = self._read_message()
                if message is None:
                    break
                self._dispatch(message)
        except (OSError, ValueError):
            pass  # noqa: S110 - pipe closed during teardown
        finally:
            self._closed = True
            with self._state_lock:
                pending = list(self._events.values())
            for event in pending:
                event.set()

    def _read_message(self) -> dict | None:
        stdout = self._stdout
        if stdout is None:
            return None
        headers: dict[str, str] = {}
        while True:
            line = stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("ascii", "replace").partition(":")
            headers[key.strip().lower()] = value.strip()
        length_raw = headers.get("content-length")
        if length_raw is None:
            return None
        try:
            length = int(length_raw)
        except ValueError:
            return None
        body = stdout.read(length)
        if not body:
            return None
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _dispatch(self, message: dict) -> None:
        message_id = message.get("id")
        method = message.get("method")
        is_response = message_id is not None and ("result" in message or "error" in message)
        reply: dict | None = None
        published: tuple[str, list] | None = None
        with self._state_lock:
            if is_response:
                self._responses[message_id] = message
                event = self._events.get(message_id)
            elif method:
                event = None
                if method == "textDocument/publishDiagnostics":
                    params = message.get("params") or {}
                    uri = params.get("uri")
                    if uri:
                        diagnostics = params.get("diagnostics") or []
                        self._diagnostics[uri] = diagnostics
                        self._diagnostics_seq[uri] = self._diagnostics_seq.get(uri, 0) + 1
                        self._diagnostics_pending.discard(uri)
                        published = (uri, diagnostics)
                elif message_id is not None:
                    # Server→client request: answer with an empty result so the
                    # server is never left waiting on us.
                    reply = {"jsonrpc": "2.0", "id": message_id, "result": None}
            else:
                event = None
        if event is not None:
            event.set()
        if published is not None and self._diagnostics_sink is not None:
            uri, diagnostics = published
            try:
                self._diagnostics_sink(self.client_key, uri, diagnostics)
            except Exception:  # noqa: BLE001 - an observer must not break the client
                logger.opt(exception=True).debug("LSP diagnostics sink failed")
        if reply is not None:
            self._send(reply)


def _error(code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
