"""Shared fixtures for tests/workspace/*.

``workspace.prompt_builder`` reads todos, taskflow rows, memory,
knowledge and continuity through the runtime prompt data provider
(``agent.core.init()`` registers it at server boot). Registering the real
agent-side provider for the whole directory keeps every existing monkeypatch of
the underlying store modules effective — the provider imports them call-time —
and exercises the same assembly path production uses. The unregistered
degradation path is covered explicitly by the prompt-builder tests.
"""

import pytest


@pytest.fixture(autouse=True)
def _register_prompt_data_provider():
    from agent.prompt_data_provider import AgentPromptDataProvider
    from runtime import data_provider

    data_provider.set_prompt_data_provider(AgentPromptDataProvider())
    yield
    data_provider.clear_prompt_data_provider()
