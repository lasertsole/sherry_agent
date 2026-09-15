"""Shared fixtures for tests/server: simulate the boot-time runtime-hook assembly.

``server/__main__.py`` registers the server-owned callbacks into
``runtime.hooks`` at boot and the agent layer resolves them at call time.
Server tests that drive the real services — e.g. the ``auto_turn_inflight``
signal of ``agent.tools.subagent.registry.session_state`` — need that same
assembly, so the real auto-turn module is registered as the
``AUTO_TURN_MODULE`` hook for every test here and removed again on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from runtime import hooks


@pytest.fixture(autouse=True)
def _auto_turn_module_hook() -> Iterator[None]:
    """Register the real auto-turn module as the runtime hook (server assembled)."""
    from server.service import auto_turn as auto_turn_module

    hooks.register(hooks.AUTO_TURN_MODULE, lambda: auto_turn_module)
    yield
    hooks.unregister(hooks.AUTO_TURN_MODULE)
