"""Lazy imports for the agent package.

Using ``__getattr__`` avoids triggering heavy dependency chains
(langchain factory -> langgraph prebuilt) during test
collection or when only a specific submodule is needed.
"""

from typing import Any

__all__ = [
    "built_agent",
    "get_agent_tools",
    "build_async_sqlite_checkpointer",
    "ContextLimitGuardWrapper",
    "RepetitionGuardWrapper",
]


def __getattr__(name: str) -> Any:
    if name in ("built_agent", "get_agent_tools"):
        from . import core

        return getattr(core, name)
    if name == "build_async_sqlite_checkpointer":
        from .checkpointer import build_async_sqlite_checkpointer

        return build_async_sqlite_checkpointer
    if name == "ContextLimitGuardWrapper":
        from .wrapper.context_limit import ContextLimitGuardWrapper

        return ContextLimitGuardWrapper
    if name == "RepetitionGuardWrapper":
        from .wrapper.repetition_guard import RepetitionGuardWrapper

        return RepetitionGuardWrapper
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
