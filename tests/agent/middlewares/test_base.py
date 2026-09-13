"""TDD tests for `agent/middlewares/base.py` shared helpers (audit 1.1.8/1.1.9/1.1.10).

* ``require_session_id`` — deduplicates the 7 per-middleware session-id
  extraction variants while preserving each caller's exact error message.
* ``args_hash`` — deduplicates the HITL/guardrails argument-hash copies
  (audit 1.1.8): md5 of ``json.dumps(args, sort_keys=True, default=str)``
  with a ``str(args)`` fallback.
* ``BeforeAgentHooksMixin`` / ``AfterAgentHooksMixin`` — auto-bridge for
  middlewares implementing sync ``_before_agent_impl`` /
  ``_after_agent_impl`` (audit 1.1.9); the four langchain hooks delegate to
  the impls and return None.
"""

import pytest

from agent.middlewares.base import (
    AfterAgentHooksMixin,
    BeforeAgentHooksMixin,
    args_hash,
    require_session_id,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class TestRequireSessionId:
    def test_returns_session_id(self):
        assert require_session_id({"session_id": "s1"}, "Not pass session_id") == "s1"

    def test_blank_raises_with_exact_message(self):
        with pytest.raises(RuntimeError, match="Not pass session_id"):
            require_session_id({"session_id": ""}, "Not pass session_id")

    def test_whitespace_only_raises(self):
        with pytest.raises(RuntimeError, match="session_id is required"):
            require_session_id({"session_id": "   "}, "X: session_id is required")

    def test_missing_key_raises(self):
        with pytest.raises(RuntimeError, match="Not pass session_id"):
            require_session_id({}, "Not pass session_id")

    def test_owner_prefixed_message_preserved(self):
        """The 3 prefixed variants keep their exact per-middleware messages."""
        with pytest.raises(RuntimeError, match="HeartbeatStaleness: session_id is required"):
            require_session_id({"session_id": ""}, "HeartbeatStaleness: session_id is required")


class TestArgsHash:
    def test_deterministic(self):
        assert args_hash({"a": 1}) == args_hash({"a": 1})

    def test_order_insensitive(self):
        assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})

    def test_differs_for_different_args(self):
        assert args_hash({"a": 1}) != args_hash({"a": 2})

    def test_non_serializable_values_fall_back_to_str(self):
        class _Unserializable:
            def __str__(self):
                return "UNSERIALIZABLE_OBJECT"

        assert args_hash({"x": _Unserializable()}) == args_hash({"x": _Unserializable()})
        # Non-serializable objects fall back to str(), producing a distinct
        # hash from a plain string "UNSERIALIZABLE" (which would be the
        # result of str() on the class name, not the same as the object's str).
        # The fallback uses str() which for a custom object includes the
        # class name, distinct from the bare string "UNSERIALIZABLE".
        assert args_hash({"x": _Unserializable()}) != args_hash({"x": "UNSERIALIZABLE"})

    def test_md5_hex_length(self):
        assert len(args_hash({"a": 1})) == 32


class _Recorder:
    def __init__(self) -> None:
        self.before_calls = 0
        self.after_calls = 0

    def _before_agent_impl(self, state) -> None:
        self.before_calls += 1

    def _after_agent_impl(self, state) -> None:
        self.after_calls += 1


class TestSyncAsyncHookMixins:
    def test_before_mixin_bridges_sync_and_async(self):
        class _MW(BeforeAgentHooksMixin):
            def __init__(self):
                self.rec = _Recorder()

            def _before_agent_impl(self, state) -> None:
                self.rec.before_calls += 1

        mw = _MW()
        assert mw.before_agent({"session_id": "s1"}, runtime=None) is None
        assert mw.rec.before_calls == 1

        import asyncio

        asyncio.run(mw.abefore_agent({"session_id": "s1"}, runtime=None))
        assert mw.rec.before_calls == 2

    def test_after_mixin_bridges_sync_and_async(self):
        class _MW(AfterAgentHooksMixin):
            def __init__(self):
                self.rec = _Recorder()

            def _after_agent_impl(self, state) -> None:
                self.rec.after_calls += 1

        mw = _MW()
        assert mw.after_agent({}, runtime=None) is None

        import asyncio

        asyncio.run(mw.aafter_agent({}, runtime=None))
        assert mw.rec.after_calls == 2

    def test_both_mixins_combined(self):
        class _MW(BeforeAgentHooksMixin, AfterAgentHooksMixin):
            def __init__(self):
                self.rec = _Recorder()

            def _before_agent_impl(self, state) -> None:
                self.rec.before_calls += 1

            def _after_agent_impl(self, state) -> None:
                self.rec.after_calls += 1

        mw = _MW()
        mw.before_agent({}, runtime=None)
        mw.after_agent({}, runtime=None)
        assert mw.rec.before_calls == 1
        assert mw.rec.after_calls == 1
