"""Integration: ``built_agent`` builds through the pluggable wrapper registry.

Heavy dependencies are faked by the ``patched_agent_core`` fixture, so these
tests pin the seam where ``agent.core`` delegates to ``agent.graph_wrappers``.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from agent import core as agent_core
from agent.context_limit_guard_wrapper import ContextLimitGuardWrapper
from agent.graph_wrappers import (
    register_graph_wrapper,
    reset_graph_wrappers,
    unregister_graph_wrapper,
)
from agent.stream_repetition_guard_wrapper import RepetitionGuardWrapper

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_graph_wrappers()
    yield
    reset_graph_wrappers()


@pytest.mark.asyncio
async def test_built_agent_returns_default_wrapper_chain(
    patched_agent_core: tuple[Any, Any],
) -> None:
    _, fake_compiled_graph = patched_agent_core

    result = await agent_core.built_agent()

    assert isinstance(result, ContextLimitGuardWrapper)
    assert isinstance(result._inner, RepetitionGuardWrapper)
    assert result._inner._inner is fake_compiled_graph


@pytest.mark.asyncio
async def test_built_agent_honours_newly_registered_wrapper(
    patched_agent_core: tuple[Any, Any],
) -> None:
    _, fake_compiled_graph = patched_agent_core
    wrapped_inputs: list[Any] = []

    class _CustomWrapper:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

    def custom_factory(inner: Any) -> _CustomWrapper:
        wrapped_inputs.append(inner)
        return _CustomWrapper(inner)

    register_graph_wrapper(custom_factory)
    try:
        result = await agent_core.built_agent()
    finally:
        unregister_graph_wrapper(custom_factory)

    assert len(wrapped_inputs) == 1
    assert isinstance(wrapped_inputs[0], ContextLimitGuardWrapper)
    assert isinstance(result, _CustomWrapper)
    assert result.inner is wrapped_inputs[0]
    assert result.inner._inner._inner is fake_compiled_graph
