"""MCP foldering: the drop folder, the two drop shapes, and error isolation.

The loader merges a read-only bundled default with the user's
``APP_HOME/mcp_servers/`` folder (user overrides bundled on a name clash),
resolving ``$here/`` PER ENTRY — a self-contained subdirectory keeps its
``server.py`` beside its ``config.json``. Every entry parses in isolation and
``build_mcp_tools`` connects per server, so one bad file or one dead server
never takes the others down.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import agent.tools.mcp_plugin as mcp_plugin

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


@pytest.fixture()
def isolated_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Repoint the bundled config and the user folder at a throwaway tree."""
    bundled = tmp_path / "plugins" / "mcp_server"
    bundled.mkdir(parents=True)
    user = tmp_path / "home" / "mcp_servers"
    user.mkdir(parents=True)
    monkeypatch.setattr(mcp_plugin, "_BUNDLED_CONFIG", bundled / "config.json")
    monkeypatch.setattr(mcp_plugin, "PLUGINS_PATH", tmp_path / "plugins")
    monkeypatch.setattr(mcp_plugin, "MCP_SERVERS_DIR", user)
    return bundled, user


def _write(path: Path, payload: dict | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _names(entries) -> list[str]:
    return [entry.name for entry in entries]


# ── discovery ────────────────────────────────────────────────────────────────


def test_a_bundled_config_lists_its_servers(isolated_sources):
    bundled, _ = isolated_sources
    _write(bundled / "config.json", {"github": {"command": "npx", "args": ["-y", "gh-mcp"]}})

    entries = mcp_plugin.scan_mcp_servers()

    assert _names(entries) == ["github"]
    assert entries[0].source == "bundled"
    assert entries[0].error is None and entries[0].spec is not None


def test_a_missing_bundled_config_is_empty_not_created(isolated_sources):
    bundled, _ = isolated_sources

    entries = mcp_plugin.scan_mcp_servers()

    assert entries == []
    assert not (bundled / "config.json").exists(), "a read-only install tree must not be written"


def test_both_drop_shapes_are_discovered(isolated_sources):
    _, user = isolated_sources
    _write(user / "flat.json", {"command": "npx", "args": []})
    _write(user / "selfcontained" / "config.json", {"command": "python", "args": ["$here/s.py"]})
    # Noise: not a config in either shape — silently skipped.
    (user / "README.md").write_text("docs", encoding="utf-8")
    (user / "empty-dir").mkdir()

    entries = mcp_plugin.scan_mcp_servers()

    assert _names(entries) == ["flat", "selfcontained"]
    assert all(entry.source == "user" for entry in entries)


def test_a_bad_json_entry_is_isolated_and_marked(isolated_sources):
    _, user = isolated_sources
    _write(user / "broken.json", "{not json")
    _write(user / "good.json", {"command": "npx", "args": []})

    entries = mcp_plugin.scan_mcp_servers()

    by_name = {entry.name: entry for entry in entries}
    assert by_name["broken"].error is not None and by_name["broken"].spec is None
    assert by_name["good"].error is None


def test_a_non_object_json_body_is_an_error_too(isolated_sources):
    _, user = isolated_sources
    _write(user / "array.json", ["not", "an", "object"])

    entries = mcp_plugin.scan_mcp_servers()

    assert entries[0].error is not None
    assert entries[0].spec is None


def test_the_user_folder_overrides_a_bundled_same_name(isolated_sources):
    bundled, user = isolated_sources
    _write(
        bundled / "config.json",
        {"shared": {"command": "bundled-cmd"}, "only-bundled": {"command": "b2"}},
    )
    _write(user / "shared.json", {"command": "user-cmd"})

    servers = mcp_plugin._load_config()

    assert servers["shared"]["command"] == "user-cmd"
    assert servers["only-bundled"]["command"] == "b2"


# ── placeholders ─────────────────────────────────────────────────────────────


def test_here_resolves_per_entry_in_the_subdirectory_shape(isolated_sources):
    _, user = isolated_sources
    _write(user / "tool" / "config.json", {"command": "python", "args": ["$here/server.py"]})

    servers = mcp_plugin._load_config()

    resolved = servers["tool"]["args"][0]
    assert resolved == (user / "tool" / "server.py").as_posix()


def test_here_resolves_to_the_files_parent_in_the_flat_shape(isolated_sources):
    _, user = isolated_sources
    _write(user / "flat.json", {"command": "python", "args": ["$here/s.py"]})

    servers = mcp_plugin._load_config()

    assert servers["flat"]["args"][0] == (user / "s.py").as_posix()


def test_the_bundled_here_keeps_the_legacy_plugins_dir(isolated_sources):
    bundled, _ = isolated_sources
    _write(bundled / "config.json", {"legacy": {"command": "python", "args": ["$here/s.py"]}})

    servers = mcp_plugin._load_config()

    assert servers["legacy"]["args"][0] == (bundled / "s.py").as_posix()


def test_the_sys_executable_placeholder_resolves(isolated_sources):
    _, user = isolated_sources
    _write(user / "py.json", {"command": "$sys.executable", "args": ["-m", "server"]})

    servers = mcp_plugin._load_config()

    assert servers["py"]["command"] == sys.executable


# ── tool building (per-server clients, isolated failures) ────────────────────


class _FakeClient:
    """MultiServerMCPClient stand-in: records its config, fails on demand."""

    fail_for: set[str] = set()
    configs: list[dict] = []

    def __init__(self, servers: dict):
        ((name, spec),) = servers.items()
        self.name = name
        type(self).configs.append({name: spec})

    async def get_tools(self):
        if self.name in type(self).fail_for:
            raise RuntimeError(f"{self.name} refused to start")
        tool = types.SimpleNamespace(name=f"mcp__{self.name}__probe")
        return [tool]


@pytest.fixture()
def fake_mcp_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.fail_for = set()
    _FakeClient.configs = []
    package = types.ModuleType("langchain_mcp_adapters")
    submodule = types.ModuleType("langchain_mcp_adapters.client")
    submodule.MultiServerMCPClient = _FakeClient
    package.client = submodule
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", package)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", submodule)
    return _FakeClient


def test_build_mcp_tools_connects_one_client_per_server(isolated_sources, fake_mcp_client):
    _, user = isolated_sources
    _write(user / "a.json", {"command": "a"})
    _write(user / "b.json", {"command": "b"})

    tools = mcp_plugin.build_mcp_tools()

    assert [t.name for t in tools] == ["mcp__a__probe", "mcp__b__probe"]
    assert [list(c)[0] for c in fake_mcp_client.configs] == ["a", "b"]


def test_one_dead_server_does_not_stop_the_others(isolated_sources, fake_mcp_client):
    _, user = isolated_sources
    _write(user / "dead.json", {"command": "dead"})
    _write(user / "alive.json", {"command": "alive"})
    fake_mcp_client.fail_for = {"dead"}

    tools = mcp_plugin.build_mcp_tools()

    assert [t.name for t in tools] == ["mcp__alive__probe"], "the dead server is skipped alone"


def test_an_empty_discovery_skips_the_client_import(isolated_sources):
    """No servers at all: the (heavy) adapters package is never imported."""
    isolated = mcp_plugin.scan_mcp_servers()
    assert isolated == []
    assert "langchain_mcp_adapters" not in sys.modules
    assert mcp_plugin.build_mcp_tools() == []
    assert "langchain_mcp_adapters" not in sys.modules
