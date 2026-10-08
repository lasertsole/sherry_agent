"""The CDP transport: one WebSocket, JSON-RPC ids, flat-session event routing.

Chromium is launched with ``--remote-debugging-port=0``; the real port is
published in ``<user-data-dir>/DevToolsActivePort`` (line 1 = port, line 2 =
browser endpoint path). The browser-level WebSocket multiplexes every page in
one connection via ``Target.attachToTarget(flatten=True)`` — messages then carry
a ``sessionId``, which the manager maps back to its pages.

Everything here is deliberately protocol-thin: id correlation, one event
listener list, and a launcher. Page semantics live in ``browser_manager``.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

import websockets
from loguru import logger

__all__ = [
    "CdpConnection",
    "CdpError",
    "LaunchedBrowser",
    "launch_browser",
    "resolve_executable",
]

#: One CDP message may carry a full-page screenshot (bounded by config later).
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

#: Every one of these was measured to matter for screencast cadence: without the
#: anti-throttle set Chrome treats the window as backgrounded and the cadence
#: collapses (measured 2 frames / 8 s vs 40–60 fps). ``--disable-dev-shm-usage``
#: keeps large frames off /dev/shm.
CHROME_FLAGS: tuple[str, ...] = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-ipc-flooding-protection",
)

#: Playwright's cache layout; the glob keeps the version directory out of config.
_PLAYWRIGHT_GLOB = "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"
#: A distro Chrome install the repo has used historically.
_OPT_CHROME = "/opt/google/chrome/chrome"


class CdpError(RuntimeError):
    """A CDP call failed (protocol error, timeout, or a dead connection)."""


class CdpConnection:
    """One WebSocket to the browser endpoint; flat sessions on top.

    ``call`` correlates responses by id and raises :class:`CdpError` for
    protocol errors and timeouts. Every message that is not a response is handed
    to the registered listeners verbatim (``method`` + optional ``sessionId``).
    """

    def __init__(self, url: str, *, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._url = url
        self._max_message_bytes = max_message_bytes
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._reader: asyncio.Task | None = None

    @property
    def url(self) -> str:
        """The WebSocket URL this connection was opened against."""
        return self._url

    @property
    def connected(self) -> bool:
        """True while the socket is open and the reader task is alive."""
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def start(self, *, timeout: float = 10.0) -> None:
        """Open the socket and start dispatching (idempotent)."""
        if self.connected:
            return
        self._ws = await asyncio.wait_for(
            websockets.connect(self._url, max_size=self._max_message_bytes), timeout=timeout
        )
        self._reader = asyncio.create_task(self._read_loop())

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register an event listener (receives every non-response message)."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Drop a listener registered with :meth:`add_listener` (no-op if absent)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                call_id = message.get("id")
                future = self._pending.pop(call_id, None) if call_id is not None else None
                if future is not None and not future.done():
                    future.set_result(message)
                    continue
                for listener in list(self._listeners):
                    try:
                        listener(message)
                    except Exception:  # noqa: BLE001 - a bad listener must not kill the reader
                        logger.opt(exception=True).warning("CDP event listener failed")
        except websockets.ConnectionClosed:
            pass
        finally:
            # A dead socket must not leave callers awaiting forever.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("the CDP connection closed"))
            self._pending.clear()

    async def call(
        self,
        method: str,
        session: str | None = None,
        *,
        timeout: float | None = 20.0,
        **params: Any,
    ) -> dict[str, Any]:
        """Send one CDP command and return its ``result`` (flat session optional).

        :raises CdpError: protocol error, timeout or a closed connection.
        """
        if self._ws is None:
            raise CdpError("the CDP connection is not started")
        if not self.connected:
            raise CdpError("the CDP connection is closed")
        self._id += 1
        message: dict[str, Any] = {"id": self._id, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._id] = future
        try:
            await self._ws.send(json.dumps(message))
            outcome = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as error:
            self._pending.pop(self._id, None)
            raise CdpError(f"{method} timed out after {timeout}s") from error
        except websockets.ConnectionClosed as error:
            self._pending.pop(self._id, None)
            raise CdpError(f"{method}: the CDP connection closed") from error
        if "error" in outcome:
            detail = outcome["error"]
            text = detail.get("message", detail) if isinstance(detail, dict) else detail
            raise CdpError(f"{method}: {text}")
        return outcome.get("result", {})

    async def close(self) -> None:
        """Close the socket and stop the reader (safe to call twice)."""
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is fine
                logger.debug("CDP socket was already dead on close")
            self._ws = None


@dataclass(slots=True)
class LaunchedBrowser:
    """A running Chromium: its process, debug port and browser WebSocket URL."""

    pid: int
    port: int
    ws_url: str
    headless: bool
    #: True when Chrome's own sandbox had to be dropped (no usable userns).
    no_sandbox: bool = False


def resolve_executable(configured: str = "", candidates: tuple[str, ...] = ()) -> str:
    """Pick the Chromium binary to launch.

    Precedence: an explicit path, then ``/opt/google/chrome/chrome`` (a symlink
    into the playwright cache on this host), the playwright cache glob, then the
    configured bare names on PATH.

    :raises FileNotFoundError: nothing executable was found.
    """
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise FileNotFoundError(f"SHERRY_BROWSER_EXECUTABLE is not runnable: {configured}")
    if os.path.isfile(_OPT_CHROME) and os.access(_OPT_CHROME, os.X_OK):
        return _OPT_CHROME
    for candidate in sorted(glob.glob(os.path.expanduser(_PLAYWRIGHT_GLOB))):
        if os.access(candidate, os.X_OK):
            return candidate
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError("no Chromium found — set SHERRY_BROWSER_EXECUTABLE or install chromium")


def _read_http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    """GET a loopback JSON endpoint (the DevTools HTTP face)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback
        return json.loads(response.read().decode("utf-8"))


async def _wait_for_debug_port(user_data_dir: str, timeout_s: float) -> tuple[int, str]:
    """Wait for ``DevToolsActivePort`` and return ``(port, browser_ws_url)``.

    The port file can appear a moment before the HTTP face accepts connections,
    so a refused read is "not ready yet", not a failure.

    :raises TimeoutError: the browser never came up within ``timeout_s``.
    """
    port_file = Path(user_data_dir) / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_file.exists():
            lines = port_file.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                port = int(lines[0].strip())
                version: dict[str, Any] | None
                try:
                    version = await asyncio.to_thread(
                        _read_http_json, f"http://127.0.0.1:{port}/json/version"
                    )
                except Exception:  # noqa: BLE001 - a refused read just means "not yet"
                    version = None
                if version and version.get("webSocketDebuggerUrl"):
                    return port, str(version["webSocketDebuggerUrl"])
        await asyncio.sleep(0.05)
    raise TimeoutError(f"the browser did not publish a debug port within {timeout_s}s")


def _kill_quietly(process: Any) -> None:
    """Kill a child that may already be gone (never raises)."""
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def launch_browser(
    *,
    executable: str,
    headless: bool,
    user_data_dir: str,
    timeout_s: float,
) -> LaunchedBrowser:
    """Start Chromium and return the running process plus its browser WS URL.

    Two attempts, in this order:

    1. the standard flag set — Chrome keeps its own renderer sandbox;
    2. the same set plus ``--no-sandbox`` — hosts without unprivileged user
       namespaces (this repo's PRoot distro among them) abort at startup with
       ``FATAL: No usable sandbox!``, and the retry is the same AUTO-degrade
       pattern the tool sandbox backends use. The trade is logged.

    The debug port binds loopback only (``--remote-debugging-port=0`` on the
    default address) and is never reported outside this process (C3).
    """
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    port_file = Path(user_data_dir) / "DevToolsActivePort"
    for attempt_flags in ((), ("--no-sandbox",)):
        port_file.unlink(missing_ok=True)
        argv = [
            executable,
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir}",
            *CHROME_FLAGS,
            *attempt_flags,
        ]
        if headless:
            argv.append("--headless=new")
        argv.append("about:blank")
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port, ws_url = await _wait_for_debug_port(user_data_dir, timeout_s)
        except TimeoutError:
            _kill_quietly(process)
            if attempt_flags:
                raise
            logger.warning(
                "Browser did not start without --no-sandbox (hosts without user "
                "namespaces need it); retrying with it enabled"
            )
            continue
        logger.info(
            "Browser launched: pid={} port={} headless={} no_sandbox={}",
            process.pid,
            port,
            headless,
            bool(attempt_flags),
        )
        return LaunchedBrowser(
            pid=process.pid,
            port=port,
            ws_url=ws_url,
            headless=headless,
            no_sandbox=bool(attempt_flags),
        )
    raise AssertionError("unreachable")  # the second attempt either returns or raises
