"""Ordered, pluggable wrapper chain for the compiled agent graph.

``agent.core.built_agent`` compiles the LangGraph graph and then wraps it
with stream-level guards (repetition detection, context-window budget). This
module owns that chain as a mutable registry so callers and tests can insert,
remove, or replace wrappers without editing ``built_agent``.

The registry is ordered innermost-first: the first factory receives the raw
compiled graph, the next receives that wrapper, and so on. The defaults
reproduce the historical hardcoded chain in ``agent/core.py``:

1. ``RepetitionGuardWrapper(phantom_stream_guard=True)``
2. ``ContextLimitGuardWrapper(context_window=main_llm_max_tokens)``
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph.state import CompiledStateGraph

from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
from .context_limit import ContextLimitGuardWrapper
from .repetition_guard import RepetitionGuardWrapper

# One wrapper step: receives the current graph, returns a wrapped graph.
GraphWrapperFactory = Callable[[CompiledStateGraph], CompiledStateGraph]


def _default_repetition_guard(inner: CompiledStateGraph) -> CompiledStateGraph:
    """Innermost default: stream-level repetition interception."""
    return RepetitionGuardWrapper(inner, phantom_stream_guard=True)


def _default_context_limit(inner: CompiledStateGraph) -> CompiledStateGraph:
    """Outermost default: context-window guard over the repetition guard."""
    return ContextLimitGuardWrapper(inner, context_window=main_llm_max_tokens)


_DEFAULT_GRAPH_WRAPPER_FACTORIES: tuple[GraphWrapperFactory, ...] = (
    _default_repetition_guard,
    _default_context_limit,
)

_GRAPH_WRAPPER_FACTORIES: list[GraphWrapperFactory] = list(_DEFAULT_GRAPH_WRAPPER_FACTORIES)


def register_graph_wrapper(factory: GraphWrapperFactory, position: int | None = None) -> None:
    """Register ``factory`` in the chain.

    ``position=None`` appends, so the new wrapper becomes the outermost one.
    An integer follows ``list.insert`` semantics (``0`` = innermost, wrapping
    the raw compiled graph directly).
    """
    if position is None:
        _GRAPH_WRAPPER_FACTORIES.append(factory)
        return
    _GRAPH_WRAPPER_FACTORIES.insert(position, factory)


def unregister_graph_wrapper(factory: GraphWrapperFactory) -> None:
    """Remove ``factory`` by identity; a non-registered factory is a no-op."""
    for index, registered in enumerate(_GRAPH_WRAPPER_FACTORIES):
        if registered is factory:
            del _GRAPH_WRAPPER_FACTORIES[index]
            return


def apply_graph_wrappers(inner: CompiledStateGraph) -> CompiledStateGraph:
    """Wrap ``inner`` with every registered factory, innermost first."""
    wrapped = inner
    for factory in _GRAPH_WRAPPER_FACTORIES:
        wrapped = factory(wrapped)
    return wrapped


def reset_graph_wrappers() -> None:
    """Restore the default factories (test isolation / explicit opt-out)."""
    _GRAPH_WRAPPER_FACTORIES[:] = _DEFAULT_GRAPH_WRAPPER_FACTORIES


__all__ = [
    "GraphWrapperFactory",
    "_DEFAULT_GRAPH_WRAPPER_FACTORIES",
    "_GRAPH_WRAPPER_FACTORIES",
    "_default_context_limit",
    "_default_repetition_guard",
    "apply_graph_wrappers",
    "register_graph_wrapper",
    "reset_graph_wrappers",
    "unregister_graph_wrapper",
]
