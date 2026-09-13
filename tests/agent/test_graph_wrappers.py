"""Tests for the pluggable graph-wrapper chain (``agent.wrapper.registry``).

The registry is process-global state, so every test runs under the autouse
``_reset_registry`` fixture. Order-sensitive tests first strip the default
factories through the public ``unregister_graph_wrapper`` API.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from agent import core as agent_core
from agent.wrapper import registry as gw
from agent.wrapper.context_limit import ContextLimitGuardWrapper
from agent.wrapper.repetition_guard import RepetitionGuardWrapper

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


class _DummyGraph:
    """Stand-in for a compiled graph; wrapper constructors never call into it."""


class _Recorder:
    """Factory result that remembers the graph it wrapped."""

    def __init__(self, tag: str, inner: Any) -> None:
        self.tag = tag
        self.inner = inner


def _recording_factory(tag: str, calls: list[str]) -> Callable[[Any], _Recorder]:
    def factory(inner: Any) -> _Recorder:
        calls.append(tag)
        return _Recorder(tag, inner)

    return factory


def _clear_registry() -> None:
    for factory in list(gw._GRAPH_WRAPPER_FACTORIES):
        gw.unregister_graph_wrapper(factory)


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:
    gw.reset_graph_wrappers()
    yield
    gw.reset_graph_wrappers()


class TestDefaultRegistry:
    def test_default_registry_contains_two_factories_in_order(self) -> None:
        assert len(gw._GRAPH_WRAPPER_FACTORIES) == 2
        assert gw._GRAPH_WRAPPER_FACTORIES[0] is gw._default_repetition_guard
        assert gw._GRAPH_WRAPPER_FACTORIES[1] is gw._default_context_limit

    def test_reset_restores_defaults_after_mutation(self) -> None:
        gw.register_graph_wrapper(_recording_factory("extra", []))
        assert len(gw._GRAPH_WRAPPER_FACTORIES) == 3
        gw.reset_graph_wrappers()
        assert gw._GRAPH_WRAPPER_FACTORIES == [
            gw._default_repetition_guard,
            gw._default_context_limit,
        ]


class TestRegisterGraphWrapper:
    def test_register_appends_and_apply_chains_in_order(self) -> None:
        _clear_registry()
        calls: list[str] = []
        gw.register_graph_wrapper(_recording_factory("a", calls))
        gw.register_graph_wrapper(_recording_factory("b", calls))

        inner = _DummyGraph()
        result = gw.apply_graph_wrappers(inner)

        assert calls == ["a", "b"]
        assert isinstance(result, _Recorder)
        assert result.tag == "b"
        assert isinstance(result.inner, _Recorder)
        assert result.inner.tag == "a"
        assert result.inner.inner is inner

    def test_register_at_position_inserts_first(self) -> None:
        _clear_registry()
        calls: list[str] = []
        gw.register_graph_wrapper(_recording_factory("a", calls))
        gw.register_graph_wrapper(_recording_factory("first", calls), position=0)

        gw.apply_graph_wrappers(_DummyGraph())

        assert calls == ["first", "a"]


class TestUnregisterGraphWrapper:
    def test_unregister_removes_only_the_identical_factory(self) -> None:
        _clear_registry()
        calls: list[str] = []
        factory = _recording_factory("a", calls)
        other = _recording_factory("b", calls)
        gw.register_graph_wrapper(factory)
        gw.register_graph_wrapper(other)

        gw.unregister_graph_wrapper(lambda *_: None)
        assert len(gw._GRAPH_WRAPPER_FACTORIES) == 2

        gw.unregister_graph_wrapper(other)
        assert list(gw._GRAPH_WRAPPER_FACTORIES) == [factory]

    def test_unregister_default_shrinks_registry(self) -> None:
        gw.unregister_graph_wrapper(gw._default_context_limit)
        assert gw._GRAPH_WRAPPER_FACTORIES == [gw._default_repetition_guard]


class TestApplyDefaults:
    def test_apply_wraps_dummy_with_real_default_wrappers(self) -> None:
        inner = _DummyGraph()
        result = gw.apply_graph_wrappers(inner)

        assert isinstance(result, ContextLimitGuardWrapper)
        assert isinstance(result._inner, RepetitionGuardWrapper)
        assert result._inner._inner is inner
        assert result._inner._phantom_stream_guard is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_built_agent_applies_registered_wrapper(patched_agent_core: tuple[Any, Any]) -> None:
    _, fake_compiled_graph = patched_agent_core
    wrapped_inputs: list[Any] = []

    class _CustomWrapper:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

    def custom_factory(inner: Any) -> _CustomWrapper:
        wrapped_inputs.append(inner)
        return _CustomWrapper(inner)

    gw.register_graph_wrapper(custom_factory)
    try:
        result = await agent_core.built_agent()
    finally:
        gw.unregister_graph_wrapper(custom_factory)

    assert len(wrapped_inputs) == 1
    assert isinstance(wrapped_inputs[0], ContextLimitGuardWrapper)
    assert isinstance(result, _CustomWrapper)
    assert result.inner is wrapped_inputs[0]
    assert isinstance(result.inner._inner, RepetitionGuardWrapper)
    assert result.inner._inner._inner is fake_compiled_graph
