"""Install a minimal ``deepagents`` stub before importing vendored LDW modules.

LDW imports ``deepagents`` only for its sandbox/leaf-construction surfaces. Sherry
uses LDW's DAG/journal primitives with its own LangGraph leaf runnables, so those
surfaces are never called. The stub keeps the import graph resolvable without
pulling the real deepagents dependency into every Sherry install.
"""

from __future__ import annotations

import sys
import types


class _AutoStubModule(types.ModuleType):
    """Module whose missing attributes resolve to harmless empty classes."""

    def __getattr__(self, name: str):
        cls = type(name, (), {})
        setattr(self, name, cls)
        return cls


def _module(name: str, *, package: bool = False) -> _AutoStubModule:
    module = _AutoStubModule(name)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def install_deepagents_stub() -> None:
    """Idempotently install the stub modules LDW imports at module import time."""
    if "deepagents" in sys.modules:
        return
    deepagents = _module("deepagents", package=True)
    backends = _module("deepagents.backends", package=True)
    _module("deepagents.backends.protocol", package=True)
    _module("deepagents.backends.composite", package=True)
    _module("deepagents.backends.state", package=True)
    # Attribute access on the package modules still resolves to auto-classes;
    # create_deep_agent is never called by Sherry.
    deepagents.backends = backends
