"""Unit tests for runtime.data_provider — the prompt data provider registry.

Covers the register/get contract (identity, last-writer-wins override),
missing-provider -> ``None`` degradation, and clear() cleanup used for test
isolation. The agent-side implementation is only checked for interface shape
(method presence); its forwarding behavior is covered by the workspace and
context_engine consumer suites.
"""

import pytest

from runtime import data_provider

pytestmark = [pytest.mark.unit]

_PROVIDER_METHODS = (
    "get_todos",
    "requester_session_key",
    "get_active_flows",
    "step_status",
    "steps_summary",
    "format_memory_for_system_prompt",
    "build_todolist_knowledge_block",
    "build_continuity_prompt",
    "build_system_prompt",
)


class _StubProvider:
    """Minimal stand-in carrying the registry identity."""

    def get_todos(self, session_id):
        return [{"content": session_id}]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts and ends with no registered provider."""
    data_provider.clear_prompt_data_provider()
    yield
    data_provider.clear_prompt_data_provider()


class TestMissingProvider:
    def test_unregistered_provider_is_none(self):
        assert data_provider.get_prompt_data_provider() is None

    def test_clear_without_registration_is_noop(self):
        data_provider.clear_prompt_data_provider()

        assert data_provider.get_prompt_data_provider() is None


class TestRegisterResolve:
    def test_set_then_get_returns_same_object(self):
        stub = _StubProvider()

        data_provider.set_prompt_data_provider(stub)

        assert data_provider.get_prompt_data_provider() is stub

    def test_re_registration_is_last_writer_wins(self):
        first = _StubProvider()
        second = _StubProvider()

        data_provider.set_prompt_data_provider(first)
        data_provider.set_prompt_data_provider(second)

        assert data_provider.get_prompt_data_provider() is second

    def test_clear_drops_registration(self):
        data_provider.set_prompt_data_provider(_StubProvider())

        data_provider.clear_prompt_data_provider()

        assert data_provider.get_prompt_data_provider() is None


class TestAgentProviderShape:
    def test_agent_provider_implements_every_protocol_method(self):
        from agent.prompt_data_provider import AgentPromptDataProvider

        provider = AgentPromptDataProvider()

        missing = [
            name for name in _PROVIDER_METHODS if not callable(getattr(provider, name, None))
        ]
        assert missing == []
