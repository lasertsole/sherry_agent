"""Unit tests for ``server/service/network_config.py`` — the ``.network`` file.

The Tavily / SearXNG search backends, the browser agent's switches and the
LangSmith tracing settings left ``.env`` / ``sherry.jsonc`` for one gitignored
JSON5 file. The service: shows the browser / tracing groups in the panel's
grouped shape, WITHHOLDS the search keys (the 模型联网 tab owns them and reads
them from its state payload), and accepts the panel's writes to all of them.
"""

import json5
import pytest

from server.service import network_config as nc

SAMPLE_NETWORK = """// 网络服务配置
{
  // 联网搜索
  "TAVILY_API_KEY": "",
  "SEARXNG_URL": "http://localhost:8888",
  "SEARXNG_VERIFY_TLS": true,
  // 浏览器代理
  "SHERRY_BROWSER_AGENT_ENABLED": 1,
  "SHERRY_BROWSER_HEADLESS": "",
  // 追踪
  "LANGSMITH_TRACING_V2": false,
  "LANGSMITH_PROJECT": "EMA_AI_agent"
}
"""


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def network_file(tmp_path, monkeypatch):
    path = tmp_path / ".network"
    path.write_text(SAMPLE_NETWORK, encoding="utf-8")
    monkeypatch.setattr(nc, "NETWORK_CONFIG_PATH", path)
    return path


class TestReadNetworkConfig:
    def test_shows_the_browser_and_tracing_groups_in_order(self, network_file):
        payload = nc.read_network_config()

        assert [g["name"] for g in payload["groups"]] == ["SHERRY_BROWSER", "LANGSMITH"]
        assert [g["kind"] for g in payload["groups"]] == ["plain", "plain"]
        browser = {e["key"]: e["value"] for e in payload["groups"][0]["entries"]}
        assert browser == {"SHERRY_BROWSER_AGENT_ENABLED": "1", "SHERRY_BROWSER_HEADLESS": ""}

    def test_withholds_the_search_keys_from_the_payload(self, network_file):
        keys = {e["key"] for g in nc.read_network_config()["groups"] for e in g["entries"]}

        assert "TAVILY_API_KEY" not in keys
        assert not any(k.startswith("SEARXNG_") for k in keys)

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nc, "NETWORK_CONFIG_PATH", tmp_path / "missing")

        with pytest.raises(FileNotFoundError):
            nc.read_network_config()


class TestNetworkFileValues:
    def test_includes_the_withheld_search_keys(self, network_file):
        values = nc.network_file_values()

        assert values["SEARXNG_URL"] == "http://localhost:8888"
        assert values["SEARXNG_VERIFY_TLS"] == "true"
        assert values["TAVILY_API_KEY"] == ""
        assert values["LANGSMITH_PROJECT"] == "EMA_AI_agent"

    def test_missing_file_yields_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nc, "NETWORK_CONFIG_PATH", tmp_path / "missing")

        assert nc.network_file_values() == {}


class TestWriteNetworkConfig:
    def test_accepts_the_search_keys_and_keeps_comments(self, network_file):
        nc.write_network_config(
            {"TAVILY_API_KEY": "tvly-new", "SEARXNG_URL": "http://localhost:9999"}
        )

        text = network_file.read_text(encoding="utf-8")
        assert "// 网络服务配置" in text
        assert "// 联网搜索" in text
        assert '"TAVILY_API_KEY": "tvly-new"' in text
        assert '"SEARXNG_URL": "http://localhost:9999"' in text
        assert (network_file.parent / ".network.bak").read_text(encoding="utf-8") == SAMPLE_NETWORK

    def test_coerces_booleans_and_ints(self, network_file):
        nc.write_network_config(
            {"SEARXNG_VERIFY_TLS": "false", "SHERRY_BROWSER_AGENT_ENABLED": "0"}
        )

        data = json5.loads(network_file.read_text(encoding="utf-8"))
        assert data["SEARXNG_VERIFY_TLS"] is False
        assert data["SHERRY_BROWSER_AGENT_ENABLED"] == 0

    def test_rejects_unknown_keys(self, network_file):
        with pytest.raises(ValueError, match="Unknown network config keys"):
            nc.write_network_config({"NOPE": "1"})

    def test_rejects_a_model_key(self, network_file):
        # The model keys belong to .models — the wrong endpoint must refuse them.
        with pytest.raises(ValueError, match="Unknown network config keys"):
            nc.write_network_config({"MAIN_LLM_API_KEY": "sk-x"})
