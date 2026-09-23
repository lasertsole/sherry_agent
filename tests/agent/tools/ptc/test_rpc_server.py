"""Unit tests for the PTC TCP RPC server."""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest

from agent.tools.ptc.rpc_server import PTCCallBudgetExceeded, PtcRpcServer

pytestmark = [pytest.mark.unit]


class _FakeTool:
    def __init__(self, result=None, error=None) -> None:
        self.name = "echo"
        self._result = result if result is not None else {"ok": True}
        self._error = error
        self.calls = 0
        self.seen_sessions: list[str] = []

    async def ainvoke(self, args, config=None):
        self.calls += 1
        configurable = (config or {}).get("configurable", {})
        self.seen_sessions.append(configurable.get("session_id", ""))
        if self._error is not None:
            raise self._error
        return {"echo": args, "result": self._result}


def _start_server(server: PtcRpcServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _roundtrip(port: int, requests: list[dict]) -> list[dict]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    buffer = b""
    responses: list[dict] = []
    try:
        for request in requests:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    raise AssertionError("server closed before responding")
                buffer += chunk
            line, buffer = buffer.split(b"\n", 1)
            responses.append(json.loads(line.decode("utf-8")))
    finally:
        sock.close()
    return responses


def test_binds_loopback_only() -> None:
    server = PtcRpcServer({}, None, 50, "child:sess")
    host, port = server.start()
    try:
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        server.stop()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_refuses_non_loopback_host(host: str) -> None:
    with pytest.raises(ValueError):
        PtcRpcServer({}, None, 50, "child:sess", host=host)


def test_json_protocol_roundtrip() -> None:
    tool = _FakeTool(result="hello")
    server = PtcRpcServer({"echo": tool}, None, 50, "child:sess")
    _host, port = server.start()
    thread = _start_server(server)
    try:
        responses = _roundtrip(port, [{"tool": "echo", "args": {"x": 1}}])
        assert responses[0]["ok"] is True
        assert responses[0]["result"]["echo"] == {"x": 1}
        assert tool.calls == 1
    finally:
        server.stop()
        thread.join(timeout=2)


def test_unknown_tool_returns_error_without_consuming_budget() -> None:
    server = PtcRpcServer({"echo": _FakeTool()}, None, 1, "child:sess")
    _host, port = server.start()
    thread = _start_server(server)
    try:
        responses = _roundtrip(port, [{"tool": "nope", "args": {}}])
        assert responses[0]["ok"] is False
        assert "unknown tool" in responses[0]["error"]
        assert server.calls_made == 0
    finally:
        server.stop()
        thread.join(timeout=2)


def test_tool_error_is_returned_not_raised() -> None:
    tool = _FakeTool(error=ValueError("boom"))
    server = PtcRpcServer({"echo": tool}, None, 50, "child:sess")
    _host, port = server.start()
    thread = _start_server(server)
    try:
        responses = _roundtrip(port, [{"tool": "echo", "args": {}}])
        assert responses[0]["ok"] is False
        assert "ValueError: boom" in responses[0]["error"]
    finally:
        server.stop()
        thread.join(timeout=2)


def test_call_budget_exceeded_terminates() -> None:
    tool = _FakeTool()
    server = PtcRpcServer({"echo": tool}, None, 2, "child:sess")
    _host, port = server.start()
    thread = _start_server(server)
    try:
        responses = _roundtrip(
            port,
            [
                {"tool": "echo", "args": {}},
                {"tool": "echo", "args": {}},
                {"tool": "echo", "args": {}},
            ],
        )
        assert responses[0]["ok"] is True
        assert responses[1]["ok"] is True
        assert responses[2]["ok"] is False
        assert responses[2]["code"] == "PTCCallBudgetExceeded"
        assert server.calls_made == 2
        assert server.budget_exhausted is True
        with pytest.raises(PTCCallBudgetExceeded):
            server._check_budget()
    finally:
        server.stop()
        thread.join(timeout=2)


def test_invalid_json_line_returns_error() -> None:
    server = PtcRpcServer({"echo": _FakeTool()}, None, 50, "child:sess")
    _host, port = server.start()
    thread = _start_server(server)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(b"not json\n")
            raw = sock.recv(65536)
        finally:
            sock.close()
        response = json.loads(raw.decode("utf-8").strip())
        assert response["ok"] is False
        assert "invalid request" in response["error"]
    finally:
        server.stop()
        thread.join(timeout=2)


def test_reconnect_after_disconnect() -> None:
    server = PtcRpcServer({"echo": _FakeTool()}, None, 50, "child:sess")
    _host, port = server.start()
    thread = _start_server(server)
    try:
        first = _roundtrip(port, [{"tool": "echo", "args": {"n": 1}}])
        second = _roundtrip(port, [{"tool": "echo", "args": {"n": 2}}])
        assert first[0]["ok"] and second[0]["ok"]
        assert second[0]["result"]["echo"] == {"n": 2}
    finally:
        server.stop()
        thread.join(timeout=2)


def test_dispatch_uses_parent_event_loop_and_child_session() -> None:
    async def _main() -> None:
        loop = asyncio.get_running_loop()
        tool = _FakeTool()
        server = PtcRpcServer({"echo": tool}, loop, 50, "agent:x:subagent:y")
        _host, port = server.start()
        thread = _start_server(server)
        try:
            responses = await asyncio.to_thread(
                _roundtrip, port, [{"tool": "echo", "args": {"a": 1}}]
            )
        finally:
            server.stop()
            thread.join(timeout=2)
        assert responses[0]["ok"] is True
        # Exactly the child session reaches the tool — never the parent.
        assert tool.seen_sessions == ["agent:x:subagent:y"]

    asyncio.run(_main())


def test_stop_unblocks_accept_loop() -> None:
    server = PtcRpcServer({}, None, 50, "child:sess")
    server.start()
    thread = _start_server(server)
    server.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
