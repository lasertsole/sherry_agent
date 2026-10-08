"""Localhost TCP RPC server for PTC tool-call dispatch.

The PTC child process calls Sherry tools by sending newline-delimited JSON
requests to this server over a loopback TCP socket. The server runs in a
dedicated thread; each request is dispatched onto the *parent* asyncio event
loop via :func:`asyncio.run_coroutine_threadsafe`, so a blocking child never
blocks the parent's async work.

Protocol (one JSON object per line)::

    request  {"tool": "read_file", "args": {"file_path": "x"}, "token": "<one-time token>"}
    response {"ok": true,  "result": ...}
    response {"ok": false, "error": "..."}

Safety invariants:

- Binds ``127.0.0.1`` only; an explicit ``0.0.0.0`` / ``::`` host is refused.
- When a per-run ``token`` is configured, every request frame must carry it
  (the child reads it from ``SHERRY_PTC_RPC_TOKEN`` in its own environment). A
  missing or wrong token makes the server drop the connection immediately,
  without sending a response, so a stray local process cannot learn whether
  the guess was close. Comparisons are constant-time.
- Every dispatched tool runs with the child's ``session_id`` in the runnable
  config, so tools resolve their caller against the child session — the
  parent session is never exposed.
- A hard per-script call budget; exceeding it raises
  :class:`PTCCallBudgetExceeded`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import socket
import threading
from typing import Any

from loguru import logger


class PtcCallBudgetExceededError(Exception):
    """The PTC script exceeded its per-script tool-call budget."""


#: Spec-facing/legacy name for :class:`PtcCallBudgetExceededError` (N818 prefers
#: an ``Error`` suffix on exception classes).
PTCCallBudgetExceeded = PtcCallBudgetExceededError


#: Hosts that would expose the RPC listener beyond the local machine.
_FORBIDDEN_HOSTS = frozenset({"0.0.0.0", "::", ""})


class PtcRpcServer:
    """TCP RPC listener that dispatches PTC tool calls onto the parent loop."""

    def __init__(
        self,
        tools_map: dict[str, Any],
        loop: asyncio.AbstractEventLoop | None,
        max_calls: int,
        session_id: str,
        *,
        host: str = "127.0.0.1",
        tool_call_timeout: float = 120.0,
        token: str | None = None,
    ) -> None:
        if host in _FORBIDDEN_HOSTS:
            raise ValueError(f"PTC RPC must bind loopback only; refusing host {host!r}")
        self._tools_map = tools_map
        self._loop = loop
        self._max_calls = max_calls
        self._session_id = session_id
        self._host = host
        self._tool_call_timeout = tool_call_timeout
        # One-time, in-memory token. The runner always supplies a fresh value;
        # ``None`` disables authentication and exists only for lower-level unit
        # tests that exercise the protocol without the runner's handshake.
        self._token = token

        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._calls_made = 0
        self._budget_exhausted = False

    # ── introspection (tests / runner) ───────────────────────────────────
    @property
    def calls_made(self) -> int:
        return self._calls_made

    @property
    def budget_exhausted(self) -> bool:
        return self._budget_exhausted

    @property
    def host(self) -> str:
        return self._host

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> tuple[str, int]:
        """Bind an ephemeral loopback port and start listening.

        Returns the ``(host, port)`` the child must connect to.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        sock.listen(1)
        # Bounded accept so ``stop()`` can never leave the thread stuck.
        sock.settimeout(0.5)
        self._sock = sock
        bound_host, bound_port = sock.getsockname()
        # Defensive: never hand the child a non-loopback address.
        if bound_host in _FORBIDDEN_HOSTS:
            sock.close()
            self._sock = None
            raise RuntimeError(f"PTC RPC bound a non-loopback address {bound_host!r}")
        return str(bound_host), int(bound_port)

    def serve_forever(self) -> None:
        """Accept clients (one at a time) until :meth:`stop`.

        A client disconnect is not fatal — the loop returns to ``accept`` so a
        reconnecting child (or a fresh child in the same script run) is served.
        """
        if self._sock is None:
            raise RuntimeError("PtcRpcServer.start() must be called before serve_forever()")
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                self._serve_client(conn)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("PTC RPC client loop ended: {}", exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self) -> None:
        """Signal the accept loop to stop and close the listener."""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None

    # ── request handling ─────────────────────────────────────────────────
    def _serve_client(self, conn: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                response = self._handle_line(line)
                if response is None:
                    # Auth rejection: drop the connection without a response so
                    # a stray local client learns nothing from the failure.
                    return
                try:
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except OSError:
                    return

    def _authenticated(self, token: object) -> bool:
        """Constant-time check of the per-run token (no token ⇒ open)."""
        if self._token is None:
            return True
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            logger.warning("PTC RPC rejected a connection with a missing or invalid token")
            return False
        return True

    def _handle_line(self, line: bytes) -> dict[str, Any] | None:
        """Parse and dispatch one request; ``None`` means "reject and close"."""
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("payload must be an object")
            tool_name = request["tool"]
            args = request.get("args") or {}
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError) as exc:
            return {"ok": False, "error": f"invalid request: {exc}"}
        if not self._authenticated(request.get("token")):
            return None
        if not isinstance(tool_name, str) or not isinstance(args, dict):
            return {"ok": False, "error": "invalid request: tool must be str, args must be object"}
        try:
            return self._dispatch(tool_name, args)
        except PTCCallBudgetExceeded as exc:
            return {"ok": False, "error": str(exc), "code": "PTCCallBudgetExceeded"}

    def _dispatch(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._tools_map:
            return {"ok": False, "error": f"unknown tool: {tool_name}"}
        self._check_budget()
        self._calls_made += 1
        tool = self._tools_map[tool_name]
        try:
            result = self._invoke(tool, args)
            return {"ok": True, "result": result}
        except Exception as exc:  # tool errors are returned to the child, not raised
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _check_budget(self) -> None:
        if self._calls_made >= self._max_calls:
            self._budget_exhausted = True
            raise PTCCallBudgetExceeded(
                f"PTCCallBudgetExceeded: a PTC script may make at most {self._max_calls} tool calls"
            )

    def _invoke(self, tool: Any, args: dict[str, Any]) -> Any:
        """Run the tool on the parent loop (or inline when no loop is running)."""
        coro = tool.ainvoke(
            args,
            config={"configurable": {"session_id": self._session_id}},
        )
        loop = self._loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return future.result(timeout=self._tool_call_timeout)
            except TimeoutError:
                future.cancel()
                raise TimeoutError(
                    f"tool call timed out after {self._tool_call_timeout}s"
                ) from None
        # No parent loop (unit tests / synchronous callers): drain inline.
        return asyncio.run(coro)
