"""Unit tests for the PTC RPC one-time token handshake."""

from __future__ import annotations

import inspect
import json
import socket
import threading

import pytest

from agent.tools.ptc.rpc_server import PtcRpcServer
from agent.tools.ptc.stub_generator import PTC_RPC_TOKEN_ENV, generate_stub

pytestmark = [pytest.mark.unit]

_TOKEN = "unit-test-one-time-token"


class _FakeTool:
    name = "echo"

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, args, config=None):
        self.calls += 1
        return {"echo": args}


def _start(server: PtcRpcServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _send_raw(port: int, request: dict) -> bytes:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        return sock.recv(65536)
    finally:
        sock.close()


def test_correct_token_is_accepted() -> None:
    tool = _FakeTool()
    server = PtcRpcServer({"echo": tool}, None, 50, "child:sess", token=_TOKEN)
    _host, port = server.start()
    thread = _start(server)
    try:
        raw = _send_raw(port, {"tool": "echo", "args": {"x": 1}, "token": _TOKEN})
        response = json.loads(raw.decode("utf-8"))
        assert response["ok"] is True
        assert tool.calls == 1
    finally:
        server.stop()
        thread.join(timeout=2)


def test_missing_token_is_rejected_without_a_response() -> None:
    tool = _FakeTool()
    server = PtcRpcServer({"echo": tool}, None, 50, "child:sess", token=_TOKEN)
    _host, port = server.start()
    thread = _start(server)
    try:
        raw = _send_raw(port, {"tool": "echo", "args": {}})
        assert raw == b""
        assert tool.calls == 0
    finally:
        server.stop()
        thread.join(timeout=2)


def test_wrong_token_is_rejected_without_a_response() -> None:
    tool = _FakeTool()
    server = PtcRpcServer({"echo": tool}, None, 50, "child:sess", token=_TOKEN)
    _host, port = server.start()
    thread = _start(server)
    try:
        raw = _send_raw(port, {"tool": "echo", "args": {}, "token": "not-the-token"})
        assert raw == b""
        assert tool.calls == 0
    finally:
        server.stop()
        thread.join(timeout=2)


def test_tokenless_server_stays_open_for_low_level_protocol_tests() -> None:
    tool = _FakeTool()
    server = PtcRpcServer({"echo": tool}, None, 50, "child:sess")
    _host, port = server.start()
    thread = _start(server)
    try:
        raw = _send_raw(port, {"tool": "echo", "args": {}})
        response = json.loads(raw.decode("utf-8"))
        assert response["ok"] is True
    finally:
        server.stop()
        thread.join(timeout=2)


def test_stub_reads_the_token_env_var_and_never_embeds_a_value() -> None:
    source = generate_stub("127.0.0.1", 1, [])
    assert PTC_RPC_TOKEN_ENV in source
    assert '"token"' in source
    # The generator cannot bake a token in: it has no token parameter at all.
    assert "token" not in inspect.signature(generate_stub).parameters
