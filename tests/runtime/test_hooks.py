"""Unit tests for runtime.hooks — the process-level callback registry.

Covers the register/resolve contract (identity, last-writer-wins override,
missing name -> None), unregister/clear cleanup used for test isolation, and
the documented hook-name constants the server assembly and the agent call
sites share.
"""

import asyncio

import pytest

from runtime import hooks

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts and ends with an empty registry."""
    hooks.clear()
    yield
    hooks.clear()


class TestResolveMissing:
    def test_unregistered_name_resolves_to_none(self):
        assert hooks.resolve("no_such_hook") is None

    def test_resolve_after_unregister_is_none(self):
        hooks.register("some_hook", lambda: 1)
        hooks.unregister("some_hook")
        assert hooks.resolve("some_hook") is None

    def test_unregister_unknown_name_is_noop(self):
        hooks.register("kept", lambda: 1)
        hooks.unregister("never_registered")
        assert hooks.resolve("kept") is not None


class TestRegisterResolve:
    def test_register_then_resolve_returns_same_callable(self):
        def impl(value: int) -> int:
            return value + 1

        hooks.register("adder", impl)

        resolved = hooks.resolve("adder")
        assert resolved is impl
        assert resolved is not None
        assert resolved(41) == 42

    def test_re_registration_is_last_writer_wins(self):
        hooks.register("hook", lambda: "first")
        hooks.register("hook", lambda: "second")

        resolved = hooks.resolve("hook")
        assert resolved is not None
        assert resolved() == "second"

    def test_async_callable_resolves_and_awaits(self):
        async def impl() -> str:
            return "done"

        hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, impl)

        resolved = hooks.resolve(hooks.MAYBE_TRIGGER_AUTO_TURN)
        assert resolved is not None
        assert asyncio.run(resolved()) == "done"

    def test_clear_drops_every_registration(self):
        hooks.register("a", lambda: 1)
        hooks.register("b", lambda: 2)

        hooks.clear()

        assert hooks.resolve("a") is None
        assert hooks.resolve("b") is None


class TestHookNames:
    def test_documented_names_are_stable(self):
        # The server assembly registers under these exact names; the agent
        # call sites resolve the same constants.
        assert hooks.MAYBE_TRIGGER_AUTO_TURN == "maybe_trigger_auto_turn"
        assert hooks.AUTO_TURN_MODULE == "auto_turn_module"
        assert hooks.WS_ACTIVE_TASKS == "ws_active_tasks"
