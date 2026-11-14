"""Edit-time LSP sync, diagnostics generations and the ContentModified retry.

These pin the contracts the edit tools and the passive reminder depend on:
didOpen first / didChange+didSave afterwards, a publish generation that can be
waited on, the diagnostics observer, and a bounded retry for ``-32801``.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp.client import LSPClient
from agent.tools.code_intel.lsp.protocol import path_to_uri
from config.features import LSP

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _client(**kwargs) -> tuple[LSPClient, list[tuple[str, dict]]]:
    client = LSPClient(
        "python",
        "/tmp",
        ["fake"],
        request_timeout_s=1.0,
        start_timeout_s=1.0,
        max_file_bytes=100_000,
        max_opened_files=4,
        **kwargs,
    )
    client._closed = False
    # ``alive`` reads the subprocess; a pollable stand-in enables sync without
    # spawning a server.
    client._proc = types.SimpleNamespace(poll=lambda: None, pid=1)
    sent: list[tuple[str, dict]] = []
    client.notify = lambda method, params: sent.append((method, params)) or True  # type: ignore[method-assign]
    return client, sent


def test_first_sync_opens_then_subsequent_syncs_change(tmp_path: Path):
    client, sent = _client()
    target = tmp_path / "a.py"

    client.sync_document(str(target), "x = 1\n")
    client.sync_document(str(target), "x = 2\n")

    assert [method for method, _params in sent] == [
        "textDocument/didOpen",
        "textDocument/didSave",
        "textDocument/didChange",
        "textDocument/didSave",
    ]
    opened = sent[0][1]["textDocument"]
    assert opened["text"] == "x = 1\n" and opened["version"] == 1
    changed = sent[2][1]
    assert changed["textDocument"]["version"] == 2
    assert changed["contentChanges"] == [{"text": "x = 2\n"}]
    assert sent[3][1]["text"] == "x = 2\n"


def test_sync_skips_a_dead_client(tmp_path: Path):
    client, sent = _client()
    client._proc = None  # alive is False before start

    client.sync_document(str(tmp_path / "a.py"), "x = 1\n")

    assert sent == []


def test_refresh_from_disk_reads_the_current_bytes(tmp_path: Path):
    client, sent = _client()
    target = tmp_path / "a.py"
    target.write_text("fresh = True\n", encoding="utf-8")

    assert client.refresh_from_disk(str(target)) == (None, True)
    assert sent[0][1]["textDocument"]["text"] == "fresh = True\n"
    # A second refresh with identical bytes neither notifies nor waits.
    assert client.refresh_from_disk(str(target)) == (None, False)
    assert len(sent) == 2  # didOpen + didSave from the first refresh only


def test_refresh_from_disk_reports_an_oversized_file(tmp_path: Path):
    client, sent = _client()
    client._max_file_bytes = 4
    target = tmp_path / "a.py"
    target.write_text("too large", encoding="utf-8")

    error, changed = client.refresh_from_disk(str(target))
    assert "too large" in (error or "")
    assert changed is False
    assert sent == []


def test_publish_bumps_the_generation_and_feeds_the_sink():
    published: list[tuple[str, str, list]] = []
    client, _sent = _client(
        client_key="python:/tmp", diagnostics_sink=lambda *a: published.append(a)
    )
    uri = path_to_uri("/tmp/a.py")

    assert client.diagnostics_generation(uri) == 0
    client._dispatch(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": [{"message": "boom", "severity": 1}]},
        }
    )

    assert client.diagnostics_generation(uri) == 1
    assert client.wait_diagnostics_since(uri, 0, 0.2) == [{"message": "boom", "severity": 1}]
    assert published == [("python:/tmp", uri, [{"message": "boom", "severity": 1}])]


def test_wait_since_times_out_without_a_new_publish():
    client, _sent = _client()
    uri = path_to_uri("/tmp/a.py")
    client._dispatch(
        {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}}
    )
    generation = client.diagnostics_generation(uri)

    assert client.wait_diagnostics_since(uri, generation, 0.1) is None


def test_content_modified_is_retried_with_backoff(monkeypatch):
    client, _sent = _client()
    monkeypatch.setitem(LSP, "lsp_content_modified_max_retries", 3)
    monkeypatch.setitem(LSP, "lsp_content_modified_backoff_s", 0.0)
    responses = iter(
        [
            {"error": {"code": -32801, "message": "content modified"}},
            {"error": {"code": -32801, "message": "content modified"}},
            {"result": {"ok": True}},
        ]
    )
    calls: list[str] = []

    def _once(method, params, timeout=None):
        calls.append(method)
        return next(responses)

    monkeypatch.setattr(client, "_request_once", _once)

    assert client.request("textDocument/hover", {}) == {"result": {"ok": True}}
    assert len(calls) == 3


def test_other_errors_are_not_retried(monkeypatch):
    client, _sent = _client()
    calls: list[str] = []

    def _once(method, params, timeout=None):
        calls.append(method)
        return {"error": {"code": -32601, "message": "method not found"}}

    monkeypatch.setattr(client, "_request_once", _once)

    assert client.request("x", {})["error"]["code"] == -32601
    assert len(calls) == 1


def test_retries_are_bounded(monkeypatch):
    client, _sent = _client()
    monkeypatch.setitem(LSP, "lsp_content_modified_max_retries", 2)
    monkeypatch.setitem(LSP, "lsp_content_modified_backoff_s", 0.0)
    calls: list[str] = []

    def _once(method, params, timeout=None):
        calls.append(method)
        return {"error": {"code": -32801, "message": "content modified"}}

    monkeypatch.setattr(client, "_request_once", _once)

    assert client.request("x", {})["error"]["code"] == -32801
    assert len(calls) == 3  # 1 attempt + 2 retries
