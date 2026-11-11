"""Shared lifecycle for the local GGUF model daemons.

Every daemon (STT, embed, reranker, local chat) is a detached child process that
loads one model once, answers loopback HTTP JSON and exits after an idle
period. This module owns the client half of that contract — pid file, detached
spawn, readiness wait, kill — so the lifecycle exists once instead of once per
model family. The daemons themselves stay thin: a model loader and a handler.

The readiness predicate is deliberately strict: ``/healthy`` must answer
``{"ready": true}``. A daemon whose process is alive but whose model is still
loading is NOT ready, and a caller that spawned it waits (bounded by
``ready_wait_s``) instead of sending a request that would time out.

Spawn is packaging-aware: a checkout runs ``python -c "from <module> import
main; main()"``, while a PyInstaller build re-executes the packaged backend
with ``--sherry-daemon <module>`` (``packaging/backend_entry.py`` dispatches it)
because a frozen executable cannot run ``-c``. ``--sherry-home`` and
``--sherry-desktop`` are propagated so the child resolves the same writable home
and packaged mode as its parent.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from config import APP_HOME, INTERPRETER_PATH, ROOT_DIR
from config.path import SHERRY_DESKTOP


@dataclass(frozen=True)
class DaemonSpec:
    """One daemon's identity and client-side budgets."""

    name: str
    host: str
    port: int
    pid_path: Path
    import_fn: str
    ready_wait_s: float = 8.0
    liveness_timeout_s: float = 1.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/healthy"


class DaemonError(RuntimeError):
    """The daemon answered with an error (or not at all) for one request."""


class ReadyState(StrEnum):
    """Outcome of a readiness attempt — callers branch on WHY it is not ready."""

    READY = "ready"
    #: A daemon process exists (just spawned, or recorded from an earlier call)
    #: but its model is still loading.
    WARMING = "warming"
    #: No daemon process exists and one could not be started (bad interpreter,
    #: permissions, a dead packaging entry point). A client may fall back to its
    #: in-process path when this is the answer.
    UNAVAILABLE = "unavailable"


_SPAWN_LOCK = threading.Lock()
_ATEXIT_SPECS: set[str] = set()


def pid_alive(pid: int) -> bool:
    """True when *pid* names a live process this user may signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user — it is alive.
        return True
    except OSError:
        return False
    return True


def recorded_pid(spec: DaemonSpec) -> int | None:
    """The pid recorded for *spec*, or None when absent/unreadable."""
    try:
        raw = spec.pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def is_ready(spec: DaemonSpec) -> bool:
    """True only when the daemon answers 200 and reports ``ready: true``."""
    try:
        response = requests.get(spec.health_url, timeout=spec.liveness_timeout_s)
    except Exception:  # noqa: BLE001 - any transport failure means "not ready"
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return bool(isinstance(payload, dict) and payload.get("ready") is True)


def _child_command(import_fn: str) -> list[str]:
    """The argv that starts *import_fn*'s daemon in this environment."""
    command = [str(INTERPRETER_PATH)]
    if getattr(sys, "frozen", False):
        command += ["--sherry-daemon", import_fn]
    else:
        command += ["-c", f"from {import_fn} import main; main()"]
    # The child resolves APP_HOME from argv at import time (config/_home.py);
    # without it a packaged daemon would fall back to the read-only code root.
    command += ["--sherry-home", str(APP_HOME)]
    if SHERRY_DESKTOP:
        command += ["--sherry-desktop"]
    return command


def _write_pid(spec: DaemonSpec, pid: int) -> None:
    spec.pid_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = spec.pid_path.with_name(f"{spec.pid_path.name}.tmp")
    temporary.write_text(str(pid), encoding="utf-8")
    os.replace(temporary, spec.pid_path)


def spawn(
    spec: DaemonSpec,
    *,
    extra_env: dict[str, str] | None = None,
    log_path: Path | None = None,
) -> bool:
    """Start the daemon when no live recorded pid exists.

    Returns True when this call started a process, False when a live recorded
    pid already covers the daemon (still warming) or the spawn failed. ``log_path``
    appends the child's stdout/stderr to a file (diagnosable); without it the
    output is discarded, matching the STT daemon's console-free behaviour.
    """
    with _SPAWN_LOCK:
        recorded = recorded_pid(spec)
        if recorded is not None and pid_alive(recorded):
            logger.debug("{} daemon already running (pid={}).", spec.name, recorded)
            return False

        command = _child_command(spec.import_fn)
        environment = os.environ.copy()
        if extra_env:
            environment.update(extra_env)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        stream = None
        try:
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                stream = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - fd for the child
                stdout = stream
                stderr = subprocess.STDOUT
            else:
                stdout = subprocess.DEVNULL
                stderr = subprocess.DEVNULL
            proc = subprocess.Popen(
                command,
                cwd=str(ROOT_DIR),
                env=environment,
                stdout=stdout,
                stderr=stderr,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except Exception as error:  # noqa: BLE001 - a daemon is best-effort
            logger.exception("{} daemon spawn failed: {}", spec.name, error)
            # A stale pid file would otherwise make the caller believe a daemon
            # exists and wait the full model budget for nothing.
            spec.pid_path.unlink(missing_ok=True)
            return False
        finally:
            if stream is not None:
                stream.close()

        _write_pid(spec, proc.pid)
        logger.info("{} daemon spawning (pid={}, port={}).", spec.name, proc.pid, spec.port)
        return True


def wait_until_ready(spec: DaemonSpec, timeout_s: float | None = None) -> bool:
    """Poll ``/healthy`` until ready, the budget expires, or the process dies."""
    budget = spec.ready_wait_s if timeout_s is None else timeout_s
    deadline = time.monotonic() + max(0.0, budget)
    while True:
        if is_ready(spec):
            return True
        pid = recorded_pid(spec)
        if pid is not None and not pid_alive(pid):
            # A model that crashes during load (missing weights, bad file)
            # must not make every caller wait the full cold-start budget.
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def ensure_ready(
    spec: DaemonSpec,
    *,
    extra_env: dict[str, str] | None = None,
    log_path: Path | None = None,
    ready_wait_s: float | None = None,
) -> ReadyState:
    """Make the daemon ready, spawning it once when needed.

    Registers an atexit kill only when THIS process started the daemon, so a
    client that joined an existing daemon never tears it down for its owner.
    ``UNAVAILABLE`` means no process exists (the spawn failed); ``WARMING``
    means a process exists but the model is not loaded within the budget.
    """
    if is_ready(spec):
        return ReadyState.READY
    started = spawn(spec, extra_env=extra_env, log_path=log_path)
    if started:
        register_atexit_kill(spec)
    if not started and recorded_pid(spec) is None:
        # The spawn itself failed and no earlier process exists: waiting the
        # model budget would only delay a caller's fallback.
        logger.warning("{} daemon could not be started.", spec.name)
        return ReadyState.UNAVAILABLE
    if wait_until_ready(spec, ready_wait_s):
        return ReadyState.READY
    logger.warning(
        "{} daemon did not become ready within {}s (pid file {}).",
        spec.name,
        spec.ready_wait_s if ready_wait_s is None else ready_wait_s,
        spec.pid_path,
    )
    pid = recorded_pid(spec)
    if pid is not None and not pid_alive(pid):
        # It crashed while loading (bad model path, missing flag, import
        # error). Reporting WARMING would make every later call wait the full
        # budget again; UNAVAILABLE lets the client fall back once.
        return ReadyState.UNAVAILABLE
    return ReadyState.WARMING if (started or pid is not None) else ReadyState.UNAVAILABLE


def _reap_child(pid: int) -> None:
    """Reap a direct child killed by :func:`kill` (no-op for a foreign pid).

    Without this, a SIGTERM'd daemon stays a zombie: ``os.kill(pid, 0)`` keeps
    answering "alive" and the kill loop waits its full budget for a process the
    kernel already stopped.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _pid_is_daemon(spec: DaemonSpec, pid: int) -> bool:
    """True when *pid*'s command line names this daemon's import target.

    A pid file can outlive its daemon (idle exit is ``os._exit``), and a later
    process may reuse the number. Killing on the number alone would signal an
    unrelated program at atexit, so the command line is checked when ``psutil``
    is importable; a pid we cannot inspect is treated as foreign (leave it
    alone — the idle watchdog retires it on its own).
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a project dependency
        return True
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return False
    return spec.import_fn in " ".join(cmdline)


def kill(spec: DaemonSpec) -> None:
    """Terminate the recorded daemon (SIGTERM, then SIGKILL) and drop the pid file."""
    pid = recorded_pid(spec)
    if pid is None:
        return
    if pid == os.getpid():
        # A corrupted pid file must never make the client kill itself.
        spec.pid_path.unlink(missing_ok=True)
        return
    if pid_alive(pid) and not _pid_is_daemon(spec, pid):
        logger.warning(
            "{} daemon pid {} does not match the expected command line; leaving it alone.",
            spec.name,
            pid,
        )
        spec.pid_path.unlink(missing_ok=True)
        return
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:  # noqa: BLE001 - already gone; the wait below settles it
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _reap_child(pid)
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        if pid_alive(pid) and hasattr(signal, "SIGKILL"):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:  # noqa: BLE001
                pass
            _reap_child(pid)
    spec.pid_path.unlink(missing_ok=True)


def register_atexit_kill(spec: DaemonSpec) -> None:
    """Kill *spec*'s daemon when this process exits (idempotent per pid file)."""
    key = str(spec.pid_path)
    if key in _ATEXIT_SPECS:
        return
    _ATEXIT_SPECS.add(key)
    atexit.register(kill, spec)


def request_json(spec: DaemonSpec, path: str, payload: dict[str, Any], *, timeout_s: float) -> Any:
    """POST JSON to the daemon and return the decoded body.

    Raises :class:`DaemonError` with the daemon's bounded error detail on any
    transport/HTTP/JSON failure, so callers can fail open with one message.
    """
    try:
        response = requests.post(f"{spec.base_url}{path}", json=payload, timeout=timeout_s)
    except requests.RequestException as error:
        raise DaemonError(f"{spec.name} daemon unreachable: {error}") from error
    if response.status_code != 200:
        detail = response.text.strip()[:400] or f"HTTP {response.status_code}"
        raise DaemonError(f"{spec.name} daemon error: {detail}")
    try:
        return response.json()
    except ValueError as error:
        raise DaemonError(f"{spec.name} daemon returned a non-JSON body") from error


def send_json(handler: Any, status: int, payload: dict[str, Any]) -> None:
    """Write one JSON response through an ``http.server`` handler."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class IdleWatchdog:
    """Exit the daemon process after *timeout_s* without a touched request.

    Daemons are caches: a model that has not been used for the idle window is
    worth zero memory, and the next client call cold-starts it again. The exit
    is ``os._exit`` on purpose — the HTTP server thread is blocked in
    ``serve_forever`` and a graceful shutdown would need a second thread to
    join the first.
    """

    def __init__(self, timeout_s: float, on_idle: Any, *, interval_s: float = 5.0) -> None:
        self._timeout_s = timeout_s
        self._on_idle = on_idle
        self._interval_s = max(0.5, min(interval_s, timeout_s / 2 if timeout_s > 0 else 1.0))
        self._last_touch = time.monotonic()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def touch(self) -> None:
        with self._lock:
            self._last_touch = time.monotonic()

    def stop(self) -> None:
        """Stop the watcher without calling the idle callback."""
        self._stop.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="daemon-idle-watchdog", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            if self._stop.wait(self._interval_s):
                return
            with self._lock:
                idle = time.monotonic() - self._last_touch
            if idle >= self._timeout_s:
                logger.info("daemon idle for {:.0f}s; exiting", idle)
                self._on_idle()
                return
