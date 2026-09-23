"""Shared fixtures for the LSP tests: a hermetic fake language server + isolation.

``fake_lsp_server`` writes a stdio JSON-RPC server to ``tmp_path`` and marks it
executable. ``point_to_fake`` rewires the LSP config so the resolver discovers the
fake binary (explicit-absolute tier) instead of a real language server. Every test
gets a clean resolver cache and a reaped manager on the way in and out.
"""

from __future__ import annotations

import stat
from pathlib import Path
from collections.abc import Callable

import pytest

from agent.tools.code_intel.lsp import resolver as resolver_mod
from agent.tools.code_intel.lsp.manager import get_manager
from config.features import LSP

# A minimal, deterministic LSP server. ``FAKE_LSP_MODE`` selects:
#   ok (default) | slow (never answer requests → client timeout) | error (answer with an error)
FAKE_LSP_SERVER = r"""#!/usr/bin/env python3
import json
import os
import sys

MODE = os.environ.get("FAKE_LSP_MODE", "ok")


def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            break
        key, _, value = stripped.partition(b":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    body = sys.stdin.buffer.read(length)
    try:
        return json.loads(body)
    except Exception:
        return None


def send(message):
    data = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def result(mid, value):
    send({"jsonrpc": "2.0", "id": mid, "result": value})


def fakerange():
    return {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}}


while True:
    msg = read_msg()
    if msg is None:
        break
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        result(mid, {"capabilities": {}})
    elif method in ("initialized", "exit"):
        if method == "exit":
            break
    elif method == "shutdown":
        result(mid, None)
    elif method == "textDocument/didOpen":
        uri = msg["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": uri,
                         "diagnostics": [{"message": "fake diagnostic", "severity": 1}]}})
    elif mid is None:
        pass
    elif MODE == "slow":
        pass
    elif MODE == "error":
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "fake error"}})
    elif method == "textDocument/definition":
        result(mid, [{"uri": msg["params"]["textDocument"]["uri"], "range": fakerange()}])
    elif method == "textDocument/references":
        uri = msg["params"]["textDocument"]["uri"]
        result(mid, [{"uri": uri, "range": fakerange()}, {"uri": uri, "range": fakerange()}])
    elif method == "workspace/symbol":
        result(mid, [{"name": "fakesym", "kind": 12,
                      "location": {"uri": "file:///fixture/m.py", "range": fakerange()}}])
    elif method == "textDocument/prepareCallHierarchy":
        uri = msg["params"]["textDocument"]["uri"]
        result(mid, [{"name": "fakefn", "kind": 12, "uri": uri, "range": fakerange(),
                      "selectionRange": fakerange()}])
    elif method == "callHierarchy/incomingCalls":
        item = msg["params"]["item"]
        result(mid, [{"from": {"name": "callerfn", "kind": 12, "uri": item.get("uri"),
                               "range": fakerange()},
                      "fromRanges": [fakerange()]}])
    elif method == "callHierarchy/outgoingCalls":
        item = msg["params"]["item"]
        result(mid, [{"to": {"name": "calleefn", "kind": 12, "uri": item.get("uri"),
                             "range": fakerange()}, "fromRanges": []}])
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
"""

_SERVER_FIELDS = {
    "python": "lsp_python_server",
    "typescript": "lsp_typescript_server",
    "rust": "lsp_rust_server",
    "go": "lsp_go_server",
}

_EXTENSIONS = {
    "python": ".py",
    "typescript": ".ts",
    "rust": ".rs",
    "go": ".go",
}


@pytest.fixture(autouse=True)
def _clean_lsp_state() -> None:
    """Reset the resolver cache and reap every managed server around each test."""
    resolver_mod._clear_cache_for_tests()
    get_manager()._clear_for_tests()
    yield
    get_manager()._clear_for_tests()
    resolver_mod._clear_cache_for_tests()


@pytest.fixture()
def fake_lsp_server(tmp_path: Path) -> Path:
    """Write the fake LSP server script and return its executable path."""
    script = tmp_path / "fake_lsp_server.py"
    script.write_text(FAKE_LSP_SERVER, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture()
def point_to_fake(monkeypatch: pytest.MonkeyPatch, fake_lsp_server: Path) -> Callable[[str], Path]:
    """Return a function that rewires the LSP config to the fake server."""

    def _point(language: str = "python") -> Path:
        monkeypatch.setitem(LSP, _SERVER_FIELDS[language], str(fake_lsp_server))
        monkeypatch.setitem(
            LSP["lsp_supported_servers"],
            language,
            {
                "command": [str(fake_lsp_server), "--stdio"],
                "extensions": [_EXTENSIONS[language]],
                "local_install": None,
            },
        )
        resolver_mod._clear_cache_for_tests()
        return fake_lsp_server

    return _point


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny Python fixture project for path-based tool calls."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(
        "def helper(x):\n    return x + 1\n\ndef caller(y):\n    return helper(y)\n",
        encoding="utf-8",
    )
    return repo
