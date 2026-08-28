"""Lazy imports for the agent package.

Using ``__getattr__`` avoids triggering heavy dependency chains
(codeact -> langchain factory -> langgraph prebuilt) during test
collection or when only a specific submodule is needed.
"""

__all__ = [
    "codeact_agent",
    "built_agent",
    "get_agent_tools",
    "build_async_sqlite_checkpointer",
    "RepetitionGuardWrapper",
]


def __getattr__(name: str):
    if name == "codeact_agent":
        from .codeact import codeact_agent
        return codeact_agent
    if name in ("built_agent", "get_agent_tools"):
        from . import core
        return getattr(core, name)
    if name == "build_async_sqlite_checkpointer":
        from .checkpointer import build_async_sqlite_checkpointer
        return build_async_sqlite_checkpointer
    if name == "RepetitionGuardWrapper":
        from .repetition_guard_wrapper import RepetitionGuardWrapper
        return RepetitionGuardWrapper
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
