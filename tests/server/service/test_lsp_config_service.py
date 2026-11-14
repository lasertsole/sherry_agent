"""LSP configuration service: merged read, project edit/upload/delete, test probe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import server.service.lsp_config_service as service

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(service, "session_root", lambda _sid: tmp_path)
    monkeypatch.setattr(
        service,
        "resolve_lsp_server",
        lambda language, cwd, servers: None if language == "typescript" else "/fake/bin",
    )
    return tmp_path


def _payload(command: str = "elixir-ls") -> dict:
    return {
        "servers": {
            "elixir": {
                "command": [command],
                "extensions": [".ex"],
                "language_id": "elixir",
            }
        },
        "enable": ["elixir"],
    }


def test_get_config_reports_sources_and_installation(root: Path):
    payload = service.get_config("s1")

    assert payload["success"] is True
    by_language = {entry["language"]: entry for entry in payload["servers"]}
    assert by_language["python"]["source"] == "builtin"
    assert by_language["python"]["installed"] is True
    assert by_language["python"]["binary_path"] == "/fake/bin"
    assert by_language["typescript"]["installed"] is False
    assert payload["project"] == {"servers": {}, "enable": []}


def test_put_project_writes_and_reflects_the_project_source(root: Path):
    result = service.put_project("s1", _payload())
    assert result["success"] is True

    config = service.get_config("s1")
    by_language = {entry["language"]: entry for entry in config["servers"]}
    assert by_language["elixir"]["source"] == "project"
    assert by_language["elixir"]["enabled"] is True
    assert json.loads((root / ".sherry" / "lsp.json").read_text(encoding="utf-8"))["servers"][
        "elixir"
    ]["command"] == ["elixir-ls"]


def test_put_project_refuses_an_unsafe_command(root: Path):
    result = service.put_project("s1", _payload("/usr/bin/elixir-ls"))

    assert result["success"] is False
    assert "bare executable" in result["message"] or "metacharacter" in result["message"]
    assert not (root / ".sherry" / "lsp.json").exists()


def test_get_project_reports_the_editor_payload(root: Path):
    service.put_project("s1", _payload())

    payload = service.get_project("s1")

    assert payload["exists"] is True
    assert payload["servers"]["elixir"]["command"] == ["elixir-ls"]
    assert payload["enable"] == ["elixir"]


def test_upload_project_accepts_json_text(root: Path):
    result = service.upload_project("s1", json.dumps(_payload()))

    assert result["success"] is True
    assert service.get_project("s1")["servers"]["elixir"]["extensions"] == [".ex"]


def test_upload_project_refuses_bad_json(root: Path):
    result = service.upload_project("s1", "{not json")

    assert result["success"] is False
    assert "invalid JSON" in result["message"]


def test_delete_language_removes_only_the_project_entry(root: Path):
    service.put_project("s1", _payload())

    result = service.delete_language("s1", "elixir")

    assert result["success"] is True
    assert result["deleted"] == "elixir"
    assert "elixir" not in service.get_project("s1")["servers"]


def test_delete_unknown_language_is_refused(root: Path):
    result = service.delete_language("s1", "elixir")

    assert result["success"] is False
    assert "not a project-level entry" in result["message"]


def test_test_server_runs_a_real_initialize_handshake(root: Path, monkeypatch):
    calls: dict = {}

    class _FakeClient:
        def __init__(self, language, cwd, command, **kwargs):
            calls.update(language=language, cwd=cwd, command=command)

        def start(self):
            return None

        def shutdown(self):
            calls["shutdown"] = True

    monkeypatch.setattr(service, "LSPClient", _FakeClient)
    service.put_project("s1", _payload())

    result = service.test_server("s1", "elixir")

    assert result["ok"] is True
    assert calls["command"] == ["/fake/bin"]
    assert calls["cwd"] == str(root)
    assert calls["shutdown"] is True


def test_test_server_reports_a_start_failure(root: Path, monkeypatch):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return "initialize failed"

        def shutdown(self):
            pass

    monkeypatch.setattr(service, "LSPClient", _FakeClient)
    service.put_project("s1", _payload())

    result = service.test_server("s1", "elixir")

    assert result["ok"] is False
    assert "initialize failed" in result["message"]


def test_test_server_reports_a_missing_binary(root: Path):
    result = service.test_server("s1", "typescript")

    assert result["ok"] is False
    assert result["installed"] is False
    assert result["message"]
