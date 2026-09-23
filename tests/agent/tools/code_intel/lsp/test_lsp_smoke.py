"""Real smoke test: drive an actual basedpyright language server end to end.

Skipped when no Python language server is resolvable (e.g. a minimal CI image
without the dev dependency). Verifies a real JSON-RPC handshake, a real
``textDocument/definition`` / ``references`` response, real diagnostics, and that
the server process is reaped (no orphan).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp.client import LSPClient
from agent.tools.code_intel.lsp.protocol import path_to_uri
from agent.tools.code_intel.lsp.resolver import resolve_lsp_server

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

_GOOD = "def helper(x):\n    return x + 1\n\ndef caller(y):\n    return helper(y)\n"
_BAD = "def oops(:\n    return\n"


def _make_client(repo: Path) -> LSPClient:
    binary = resolve_lsp_server("python")
    if not binary:
        pytest.skip("no Python LSP server resolvable in this environment")
    return LSPClient(
        "python",
        str(repo),
        [binary, "--stdio"],
        request_timeout_s=45.0,
        start_timeout_s=30.0,
        max_file_bytes=1_000_000,
        max_opened_files=8,
    )


def _assert_reaped(pid: int | None) -> None:
    assert pid is not None
    if sys.platform == "win32":
        return
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _request(client: LSPClient, method: str, params: dict, attempts: int = 3) -> dict:
    """Send a request, retrying on a synthetic timeout (PRoot wakes wait early)."""
    response: dict = {}
    for _ in range(attempts):
        response = client.request(method, params)
        if "error" not in response:
            return response
    return response


def test_real_definition_and_references(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    good = repo / "m.py"
    good.write_text(_GOOD, encoding="utf-8")
    client = _make_client(repo)
    pid: int | None = None
    try:
        assert client.start() is None
        pid = client.pid
        assert client.alive and pid is not None

        assert client.open_file(str(good)) is None

        definition = _request(
            client, "textDocument/definition", client.position_params(str(good), 5, 12)
        )
        assert "error" not in definition, definition
        assert definition.get("result"), definition

        references_params = client.position_params(str(good), 1, 5)
        references_params["context"] = {"includeDeclaration": True}
        references = _request(client, "textDocument/references", references_params)
        assert "error" not in references, references
        assert references.get("result"), references
    finally:
        client.shutdown()

    assert not client.alive and client.pid is None
    _assert_reaped(pid)


def test_real_diagnostics_on_broken_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    bad = repo / "bad.py"
    bad.write_text(_BAD, encoding="utf-8")
    client = _make_client(repo)
    pid: int | None = None
    try:
        assert client.start() is None
        pid = client.pid
        assert client.alive and pid is not None

        assert client.open_file(str(bad)) is None
        diagnostics = client.wait_diagnostics(path_to_uri(str(bad)), 45.0)
        if not diagnostics:
            diagnostics = client.wait_diagnostics(path_to_uri(str(bad)), 45.0)
        assert diagnostics, "basedpyright published no diagnostics for the broken file"
    finally:
        client.shutdown()

    assert not client.alive and client.pid is None
    _assert_reaped(pid)
