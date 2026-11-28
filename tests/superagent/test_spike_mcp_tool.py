"""P-Spike 2: an ``orchestrator_*`` MCP tool reaches a Sherry subagent.

The plan requires the super-agent MCP server to be configured through
``sherry.jsonc`` (`mcp.servers`) rather than by editing ``build_main_tools()``.
This spike verifies the whole contract with a fake MCP client: settings spec →
``build_mcp_tools()`` → main tool face → subagent tool face.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

import agent.tools as agent_tools
from agent.tools import mcp_plugin
from agent.tools.subagent.spawn import child_agent_factory
from agent.tools.subagent.types.functional_role import FunctionalRole

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _FakeClient:
    """MultiServerMCPClient stand-in: records specs, returns one named tool."""

    configs: list[dict[str, Any]] = []

    def __init__(self, servers: dict[str, Any]):
        ((name, spec),) = servers.items()
        self.name = name
        type(self).configs.append({name: spec})

    async def get_tools(self) -> list[Any]:
        return [types.SimpleNamespace(name=f"mcp__{self.name}__probe", metadata={})]


@pytest.fixture()
def fake_mcp_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.configs = []
    package = types.ModuleType("langchain_mcp_adapters")
    submodule = types.ModuleType("langchain_mcp_adapters.client")
    submodule.MultiServerMCPClient = _FakeClient
    package.client = submodule
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", package)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", submodule)
    return _FakeClient


@pytest.fixture()
def settings_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate discovery and declare one super-agent server in sherry.jsonc."""
    monkeypatch.setattr(mcp_plugin, "MCP_SERVERS_DIR", tmp_path / "mcp_servers")
    monkeypatch.setattr(mcp_plugin, "_BUNDLED_CONFIG", tmp_path / "bundled.json")
    monkeypatch.setattr(mcp_plugin, "SHERRY_CONFIG_PATH", tmp_path / "sherry.jsonc")
    monkeypatch.setattr(
        mcp_plugin,
        "load_sherry_settings",
        lambda: {"mcp.servers": {"superagent": {"command": "stub", "args": []}}},
    )


def test_sherry_jsonc_server_builds_a_tool(
    settings_server: None, fake_mcp_client: type[_FakeClient]
) -> None:
    tools = mcp_plugin.build_mcp_tools()

    assert [tool.name for tool in tools] == ["mcp__superagent__probe"]
    assert fake_mcp_client.configs[0]["superagent"]["command"] == "stub"


def test_mcp_tool_is_visible_in_the_subagent_tool_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No build_main_tools() surgery: the child inherits the main MCP tool."""
    mcp_tool = types.SimpleNamespace(name="mcp__superagent__probe", metadata={})
    monkeypatch.setattr(agent_tools, "build_main_tools", lambda: [mcp_tool])

    child_tools = child_agent_factory._assemble_child_tools(
        None,
        [],
        [],
        [],
        FunctionalRole.GENERAL,
        "superagent-spike",
    )

    names = [getattr(tool, "name", None) for tool in child_tools]
    assert "mcp__superagent__probe" in names
