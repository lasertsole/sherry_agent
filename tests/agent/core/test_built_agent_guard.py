"""Unit contract for the ``built_agent()`` MAX_TOKEN gate.

``built_agent`` refuses to assemble the graph whenever either resolved MAX_TOKEN
value falls below 128K; the heavy build path itself is faked by the shared
``patched_agent_core`` fixture.
"""

from typing import Any

import pytest

from agent import core as agent_core
from config.features import LLM_CLIENT_DEFAULTS, TokenGuardError

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.mark.asyncio
async def test_built_agent_rejects_main_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "65536")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")

    with pytest.raises(TokenGuardError, match="MAIN_LLM_MAX_TOKEN"):
        await agent_core.built_agent()


@pytest.mark.asyncio
async def test_built_agent_rejects_unset_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIN_LLM_MAX_TOKEN", raising=False)
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")

    with pytest.raises(TokenGuardError, match="MAIN_LLM_MAX_TOKEN is not set"):
        await agent_core.built_agent()


@pytest.mark.asyncio
async def test_built_agent_rejects_unset_auxiliary_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.delenv("AUXILIARY_LLM_MAX_TOKEN", raising=False)

    with pytest.raises(TokenGuardError, match="AUXILIARY_LLM_MAX_TOKEN"):
        await agent_core.built_agent()

    assert LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] < 131_072


@pytest.mark.asyncio
async def test_built_agent_builds_with_valid_env(
    monkeypatch: pytest.MonkeyPatch,
    patched_agent_core: tuple[Any, Any],
) -> None:
    agent_core_mod, fake_compiled_graph = patched_agent_core
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")

    result = await agent_core_mod.built_agent(force_rebuild=True)

    assert result._inner._inner is fake_compiled_graph
