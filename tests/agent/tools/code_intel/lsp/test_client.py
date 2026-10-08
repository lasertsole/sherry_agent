"""Unit tests for the LSP JSON-RPC client against a hermetic fake server."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp.client import LSPClient
from agent.tools.code_intel.lsp.protocol import path_to_uri

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _client(
    fake: Path,
    cwd: Path,
    *,
    request_timeout_s: float = 10.0,
    max_file_bytes: int = 1_000_000,
) -> LSPClient:
    return LSPClient(
        "python",
        str(cwd),
        [str(fake), "--stdio"],
        request_timeout_s=request_timeout_s,
        start_timeout_s=15.0,
        max_file_bytes=max_file_bytes,
        max_opened_files=8,
    )


def _fixture(cwd: Path) -> Path:
    cwd.mkdir(parents=True, exist_ok=True)
    file = cwd / "m.py"
    file.write_text(
        "def helper(x):\n    return x + 1\n\ndef caller(y):\n    return helper(y)\n",
        encoding="utf-8",
    )
    return file


def _assert_reaped(pid: int | None) -> None:
    assert pid is not None
    if sys.platform == "win32":  # os.kill(pid, 0) is not available on Windows
        return
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


class TestHandshakeAndRequests:
    def test_start_handshake_and_definition(self, fake_lsp_server: Path, tmp_path: Path) -> None:
        file = _fixture(tmp_path / "repo")
        client = _client(fake_lsp_server, tmp_path)
        try:
            assert client.start() is None
            assert client.alive
            assert client.open_file(str(file)) is None
            response = client.request(
                "textDocument/definition", client.position_params(str(file), 5, 12)
            )
            assert response["result"][0]["uri"] == path_to_uri(str(file))
        finally:
            client.shutdown()

    def test_open_file_and_diagnostics(self, fake_lsp_server: Path, tmp_path: Path) -> None:
        file = _fixture(tmp_path / "repo")
        client = _client(fake_lsp_server, tmp_path)
        try:
            assert client.start() is None
            assert client.open_file(str(file)) is None
            diagnostics = client.wait_diagnostics(path_to_uri(str(file)), 5.0)
            assert diagnostics and diagnostics[0]["message"] == "fake diagnostic"
        finally:
            client.shutdown()

    def test_open_file_too_large_is_refused(self, fake_lsp_server: Path, tmp_path: Path) -> None:
        file = _fixture(tmp_path / "repo")
        client = _client(fake_lsp_server, tmp_path, max_file_bytes=5)
        try:
            assert client.start() is None
            error = client.open_file(str(file))
            assert error is not None and "too large" in error
        finally:
            client.shutdown()


class TestFailOpen:
    def test_request_timeout_returns_error(
        self, fake_lsp_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_LSP_MODE", "slow")
        file = _fixture(tmp_path / "repo")
        client = _client(fake_lsp_server, tmp_path, request_timeout_s=0.5)
        try:
            assert client.start() is None
            response = client.request(
                "textDocument/definition", client.position_params(str(file), 5, 12)
            )
            assert "error" in response
            assert "timed out" in response["error"]["message"]
        finally:
            client.shutdown()

    def test_error_response_is_passed_through(
        self, fake_lsp_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_LSP_MODE", "error")
        file = _fixture(tmp_path / "repo")
        client = _client(fake_lsp_server, tmp_path)
        try:
            assert client.start() is None
            response = client.request(
                "textDocument/definition", client.position_params(str(file), 5, 12)
            )
            assert response["error"]["message"] == "fake error"
        finally:
            client.shutdown()

    def test_request_after_shutdown_is_error(self, fake_lsp_server: Path, tmp_path: Path) -> None:
        file = _fixture(tmp_path / "repo")
        client = _client(fake_lsp_server, tmp_path)
        assert client.start() is None
        client.shutdown()
        response = client.request(
            "textDocument/definition", client.position_params(str(file), 5, 12)
        )
        assert "error" in response

    def test_force_kill_without_start_is_safe(self) -> None:
        client = LSPClient(
            "python",
            ".",
            ["/nonexistent"],
            request_timeout_s=1.0,
            start_timeout_s=1.0,
            max_file_bytes=1,
            max_opened_files=1,
        )
        client.force_kill()
        assert not client.alive


class TestProcessCleanup:
    def test_shutdown_reaps_process(self, fake_lsp_server: Path, tmp_path: Path) -> None:
        client = _client(fake_lsp_server, tmp_path)
        assert client.start() is None
        pid = client.pid
        assert pid is not None
        client.shutdown()
        assert not client.alive
        assert client.pid is None
        _assert_reaped(pid)

    def test_force_kill_reaps_process(self, fake_lsp_server: Path, tmp_path: Path) -> None:
        client = _client(fake_lsp_server, tmp_path)
        assert client.start() is None
        pid = client.pid
        client.force_kill()
        assert not client.alive
        _assert_reaped(pid)
