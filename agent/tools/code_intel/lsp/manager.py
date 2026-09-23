"""Process-level LSP server manager — lazy start, bounded concurrency, idle reaping.

A language server is a heavy resident subprocess (basedpyright / rust-analyzer are
hundreds of MB), so this manager enforces the J10 resource constraints:

* **Lazy start** — a server is spawned only on the first request for its
  ``(language, cwd)``, never at import or tool-build time.
* **Bounded concurrency** — at most ``lsp_max_concurrent_servers`` live servers;
  acquiring one more evicts the least-recently-used server first.
* **Idle shutdown** — a daemon sweeper reaps servers idle for
  ``lsp_idle_shutdown_s`` (disabled when ``<= 0``).
* **Explicit + guaranteed shutdown** — :meth:`shutdown_all` and an ``atexit``
  hook reap every child; a server is never left orphaned.

Clients are shared per ``(language, cwd)`` rather than per subagent session: the
cost of a second server for the same project outweighs the isolation benefit.
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path

from config.features import LSP

from .client import LSPClient

__all__ = ["LspServerManager", "get_manager"]


class LspServerManager:
    """Owns and bounds all LSP subprocesses in this process."""

    def __init__(
        self,
        *,
        max_servers: int | None = None,
        idle_shutdown_s: float | None = None,
    ) -> None:
        self._clients: dict[str, LSPClient] = {}
        self._lock = threading.RLock()
        self._max_servers = max_servers
        self._idle_shutdown_s = idle_shutdown_s
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()

    # ── public API ───────────────────────────────────────────────────────────

    def acquire(self, language: str, cwd: str, binary_path: str) -> tuple[LSPClient | None, str]:
        """Return a started client for ``(language, cwd)``, starting one if needed.

        On failure returns ``(None, error_message)``; the caller degrades to the
        fallback message.
        """
        spec = LSP["lsp_supported_servers"].get(language)
        if spec is None:
            return None, f"no LSP server is configured for language '{language}'"

        resolved_cwd = str(Path(cwd).resolve())
        key = f"{language}:{resolved_cwd}"
        with self._lock:
            existing = self._clients.get(key)
            if existing is not None:
                if existing.alive:
                    existing.touch()
                    return existing, ""
                existing.force_kill()
                self._clients.pop(key, None)

            self._evict_lru_locked()

            command = [binary_path, *spec["command"][1:]]
            client = LSPClient(
                language,
                resolved_cwd,
                command,
                request_timeout_s=LSP["lsp_request_timeout_s"],
                start_timeout_s=LSP["lsp_server_start_timeout_s"],
                max_file_bytes=LSP["lsp_max_file_bytes"],
                max_opened_files=LSP["lsp_max_opened_files"],
            )
            error = client.start()
            if error:
                return None, error
            self._clients[key] = client

        self._ensure_sweeper()
        return client, ""

    def shutdown(self, language: str, cwd: str) -> None:
        """Shut down one ``(language, cwd)`` server if it exists."""
        key = f"{language}:{Path(cwd).resolve()}"
        with self._lock:
            client = self._clients.pop(key, None)
        if client is not None:
            client.shutdown()

    def shutdown_all(self) -> None:
        """Shut down every managed server (safe to call repeatedly)."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001 - teardown must never raise
                client.force_kill()

    def sweep_idle(self) -> list[str]:
        """Reap servers idle beyond ``lsp_idle_shutdown_s``; returns reaped keys."""
        idle = self._idle_limit()
        if idle <= 0:
            return []
        now = time.time()
        reaped: list[str] = []
        with self._lock:
            for key, client in list(self._clients.items()):
                if not client.alive or now - client.last_used > idle:
                    self._clients.pop(key, None)
                    client.shutdown()
                    reaped.append(key)
        return reaped

    def active_count(self) -> int:
        """Number of currently managed (possibly dead) clients."""
        with self._lock:
            return len(self._clients)

    def stats(self) -> list[dict]:
        """Snapshot of managed servers (language, pid, alive) for diagnostics."""
        with self._lock:
            return [
                {
                    "key": key,
                    "language": client.language,
                    "pid": client.pid,
                    "alive": client.alive,
                    "idle_s": round(time.time() - client.last_used, 3),
                }
                for key, client in self._clients.items()
            ]

    def _clear_for_tests(self) -> None:
        """Stop the sweeper and reap every server (test teardown hook)."""
        self._stop.set()
        self.shutdown_all()
        with self._lock:
            self._sweeper = None
            self._stop = threading.Event()

    # ── internals ────────────────────────────────────────────────────────────

    def _idle_limit(self) -> float:
        if self._idle_shutdown_s is not None:
            return self._idle_shutdown_s
        return LSP["lsp_idle_shutdown_s"]

    def _max_limit(self) -> int:
        if self._max_servers is not None:
            return self._max_servers
        return LSP["lsp_max_concurrent_servers"]

    def _evict_lru_locked(self) -> None:
        """Evict least-recently-used servers while at the concurrency limit."""
        limit = self._max_limit()
        while len(self._clients) >= limit:
            oldest_key = min(self._clients, key=lambda key: self._clients[key].last_used)
            oldest = self._clients.pop(oldest_key)
            oldest.shutdown()

    def _ensure_sweeper(self) -> None:
        idle = self._idle_limit()
        if idle <= 0:
            return
        with self._lock:
            if self._sweeper is not None and self._sweeper.is_alive():
                return
            self._stop.clear()
            interval = max(min(idle / 2.0, 60.0), 5.0)
            self._sweeper = threading.Thread(
                target=self._sweep_loop, args=(interval,), name="lsp-idle-sweeper", daemon=True
            )
            self._sweeper.start()

    def _sweep_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.sweep_idle()
            except Exception:  # noqa: BLE001 - a sweeper failure must not kill the process
                continue


_manager = LspServerManager()
atexit.register(_manager.shutdown_all)


def get_manager() -> LspServerManager:
    """Return the process-level LSP server manager singleton."""
    return _manager
