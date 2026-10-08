"""Real smoke test: drive an actual basedpyright language server end to end.

Skipped when no Python language server is resolvable (e.g. a minimal CI image
without the dev dependency). Verifies a real JSON-RPC handshake, a real
``textDocument/definition`` / ``references`` response, real diagnostics, and that
the server process is reaped (no orphan).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp import build_lsp_tools
from agent.tools.code_intel.lsp import resolver as resolver_mod
from agent.tools.code_intel.lsp.client import LSPClient
from agent.tools.code_intel.lsp.protocol import path_to_uri
from agent.tools.code_intel.lsp.resolver import resolve_lsp_server
from config.features import LSP

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


def _real_tools(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Wire the real tools at the resolved basedpyright binary for *repo*."""
    binary = resolve_lsp_server("python")
    if not binary:
        pytest.skip("no Python LSP server resolvable in this environment")
    monkeypatch.setitem(LSP, "lsp_python_server", binary)
    monkeypatch.setitem(LSP, "lsp_request_timeout_s", 45.0)
    monkeypatch.setitem(LSP, "lsp_server_start_timeout_s", 45.0)
    monkeypatch.setitem(LSP, "lsp_diagnostics_timeout_s", 60.0)
    monkeypatch.setenv("SHERRY_LSP_ROOT", str(repo))
    resolver_mod._clear_cache_for_tests()
    return {tool.name: tool for tool in build_lsp_tools("smoke")}


def test_real_diagnostics_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    bad = repo / "bad.py"
    bad.write_text(_BAD, encoding="utf-8")
    tools = _real_tools(repo, monkeypatch)

    payload = json.loads(tools["lsp_diagnostics"]._run(str(bad)))
    assert payload["count"] > 0, payload
    assert payload["timed_out"] is False
    assert any(item["severity"] in ("error", "warning") for item in payload["diagnostics"])


def test_real_rename_tool_previews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    good = repo / "m.py"
    good.write_text(_GOOD, encoding="utf-8")
    tools = _real_tools(repo, monkeypatch)

    payload = json.loads(tools["lsp_rename"]._run(str(good), 1, 5, "renamed"))
    assert payload["applied"] is False
    assert payload["count"] >= 1, payload
    assert payload["edit_count"] >= 1, payload
    assert good.read_text(encoding="utf-8") == _GOOD


def test_real_status_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    tools = _real_tools(repo, monkeypatch)

    payload = json.loads(tools["lsp_status"]._run())
    by_language = {server["language"]: server for server in payload["servers"]}
    assert by_language["python"]["status"] == "available"
    assert payload["count"] == 10
    assert payload["active_servers"] == []
