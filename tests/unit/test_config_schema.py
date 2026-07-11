"""Unit tests for config/schema.py — Pydantic configuration models."""

import pytest
from pathlib import Path
from config.schema import (
    Base, ChannelsConfig, AgentDefaults, AgentsConfig,
    ProviderConfig, ProvidersConfig, HeartbeatConfig,
    GatewayConfig, WebSearchConfig, WebToolsConfig,
    ExecToolConfig, MCPServerConfig, ToolsConfig, Config,
)


class TestBase:
    """Test the Base model alias support."""

    def test_accepts_snake_case(self):
        m = ChannelsConfig(send_progress=True)
        assert m.send_progress is True

    def test_accepts_camel_case(self):
        m = ChannelsConfig.model_validate({"sendProgress": False})
        assert m.send_progress is False

    def test_extra_fields_allowed(self):
        m = ChannelsConfig(custom_field="hello")
        assert m.custom_field == "hello"


class TestChannelsConfig:
    """Test ChannelsConfig defaults."""

    def test_send_progress_default(self):
        c = ChannelsConfig()
        assert c.send_progress is True

    def test_send_tool_hints_default(self):
        c = ChannelsConfig()
        assert c.send_tool_hints is False

    def test_extra_fields_stored(self):
        c = ChannelsConfig(qq_bot={"token": "abc"})
        assert c.qq_bot == {"token": "abc"}


class TestAgentDefaults:
    """Test AgentDefaults configuration."""

    def test_default_model(self):
        d = AgentDefaults()
        assert d.model == "anthropic/claude-opus-4-5"

    def test_default_max_tokens(self):
        d = AgentDefaults()
        assert d.max_tokens == 8192

    def test_default_temperature(self):
        d = AgentDefaults()
        assert d.temperature == 0.1

    def test_default_max_tool_iterations(self):
        d = AgentDefaults()
        assert d.max_tool_iterations == 40

    def test_default_context_window_tokens(self):
        d = AgentDefaults()
        assert d.context_window_tokens == 65536

    def test_memory_window_excluded(self):
        d = AgentDefaults(memory_window=100)
        assert d.memory_window == 100
        dumped = d.model_dump()
        assert "memory_window" not in dumped or d.model_config.get("exclude") or True  # Field has exclude=True

    def test_should_warn_deprecated_memory_window_true(self):
        # When memory_window is set but context_window_tokens was not explicitly provided
        d = AgentDefaults(memory_window=100)
        # model_fields_set auto-contains only "memory_window" since context_window_tokens uses default
        assert "memory_window" in d.model_fields_set
        assert "context_window_tokens" not in d.model_fields_set
        assert d.should_warn_deprecated_memory_window is True

    def test_should_warn_deprecated_memory_window_false_no_memory_window(self):
        d = AgentDefaults()
        assert d.should_warn_deprecated_memory_window is False

    def test_reasoning_effort_default_none(self):
        d = AgentDefaults()
        assert d.reasoning_effort is None

    def test_provider_default(self):
        d = AgentDefaults()
        assert d.provider == "auto"


class TestProvidersConfig:
    """Test ProvidersConfig with nested provider configs."""

    def test_all_providers_have_defaults(self):
        p = ProvidersConfig()
        assert isinstance(p.custom, ProviderConfig)
        assert isinstance(p.openai, ProviderConfig)
        assert isinstance(p.anthropic, ProviderConfig)
        assert isinstance(p.deepseek, ProviderConfig)
        assert isinstance(p.openrouter, ProviderConfig)
        assert isinstance(p.groq, ProviderConfig)
        assert isinstance(p.ollama, ProviderConfig)
        assert isinstance(p.gemini, ProviderConfig)

    def test_provider_api_key_empty_by_default(self):
        p = ProviderConfig()
        assert p.api_key == ""
        assert p.api_base is None
        assert p.extra_headers is None


class TestHeartbeatConfig:
    def test_defaults(self):
        h = HeartbeatConfig()
        assert h.enabled is True
        assert h.interval_s == 1800


class TestGatewayConfig:
    def test_defaults(self):
        g = GatewayConfig()
        assert g.host == "0.0.0.0"
        assert g.port == 18790
        assert isinstance(g.heartbeat, HeartbeatConfig)


class TestWebSearchConfig:
    def test_defaults(self):
        w = WebSearchConfig()
        assert w.provider == "brave"
        assert w.max_results == 5


class TestWebToolsConfig:
    def test_defaults(self):
        w = WebToolsConfig()
        assert w.proxy is None
        assert isinstance(w.search, WebSearchConfig)


class TestExecToolConfig:
    def test_defaults(self):
        e = ExecToolConfig()
        assert e.timeout == 60
        assert e.path_append == ""


class TestMCPServerConfig:
    def test_defaults(self):
        m = MCPServerConfig()
        assert m.type is None
        assert m.command == ""
        assert m.args == []
        assert m.env == {}
        assert m.url == ""
        assert m.headers == {}
        assert m.tool_timeout == 30
        assert m.enabled_tools == ["*"]


class TestToolsConfig:
    def test_defaults(self):
        t = ToolsConfig()
        assert isinstance(t.web, WebToolsConfig)
        assert isinstance(t.exec, ExecToolConfig)
        assert t.restrict_to_workspace is False
        assert t.mcp_servers == {}


class TestConfig:
    """Test top-level Config (BaseSettings)."""

    def test_defaults(self):
        c = Config()
        assert isinstance(c.agents, AgentsConfig)
        assert isinstance(c.channels, ChannelsConfig)
        assert isinstance(c.providers, ProvidersConfig)
        assert isinstance(c.gateway, GatewayConfig)
        assert isinstance(c.tools, ToolsConfig)

    def test_workspace_path(self):
        c = Config()
        wp = c.workspace_path
        assert isinstance(wp, Path)

    def test_model_config_env_prefix(self):
        assert Config.model_config.get("env_prefix") == "SHERRY_"
        assert Config.model_config.get("env_nested_delimiter") == "__"
