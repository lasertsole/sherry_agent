"""The MCP discovery service: the read-only view behind ``GET /mcp/servers``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent.tools.mcp_plugin as mcp_plugin
from server.service import mcp_service

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


@pytest.fixture()
def isolated_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundled = tmp_path / "plugins" / "mcp_server"
    bundled.mkdir(parents=True)
    user = tmp_path / "home" / "mcp_servers"
    user.mkdir(parents=True)
    monkeypatch.setattr(mcp_plugin, "_BUNDLED_CONFIG", bundled / "config.json")
    monkeypatch.setattr(mcp_plugin, "PLUGINS_PATH", tmp_path / "plugins")
    monkeypatch.setattr(mcp_plugin, "MCP_SERVERS_DIR", user)
    monkeypatch.setattr(mcp_service, "MCP_SERVERS_DIR", user)
    return bundled, user


def test_the_snapshot_lists_both_sources_and_the_user_dir(isolated_sources):
    bundled, user = isolated_sources
    (bundled / "config.json").write_text(
        json.dumps({"bundled1": {"command": "b-cmd", "args": []}}), encoding="utf-8"
    )
    (user / "mine.json").write_text(json.dumps({"command": "u-cmd", "args": []}), encoding="utf-8")
    (user / "broken.json").write_text("{oops", encoding="utf-8")

    snapshot = mcp_service.mcp_servers_snapshot()

    assert snapshot["user_dir"] == str(user)
    by_name = {server["name"]: server for server in snapshot["servers"]}
    assert by_name["bundled1"]["source"] == "bundled"
    assert by_name["bundled1"]["command"] == "b-cmd"
    assert by_name["mine"]["source"] == "user"
    assert by_name["broken"]["has_error"] is True
    assert by_name["broken"]["error"]
    assert by_name["broken"]["command"] is None


def test_an_empty_install_answers_an_empty_list(isolated_sources):
    snapshot = mcp_service.mcp_servers_snapshot()

    assert snapshot["servers"] == []
    assert snapshot["user_dir"]


def test_the_route_answers_the_snapshot(monkeypatch: pytest.MonkeyPatch):
    """The route handler is a thin wrapper: the payload is the service's."""
    import asyncio

    from server.trigger.http import mcp as mcp_routes

    payload = {
        "servers": [
            {"name": "x", "source": "user", "command": None, "has_error": False, "error": None}
        ],
        "user_dir": "/tmp",
    }
    monkeypatch.setattr(mcp_service, "mcp_servers_snapshot", lambda: payload)

    response = asyncio.run(mcp_routes.list_mcp_servers_handler(None))

    assert response.status_code == 200
    body = str(response.description)
    assert '"user_dir"' in body and "/tmp" in body
