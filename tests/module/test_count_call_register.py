"""Module tests for runtime/count_call_register.py — CountCallRegister."""

import pytest
from unittest.mock import MagicMock
from runtime.core import Register
from runtime.count_call_register import CountCallRegister


class TestCountCallRegister:
    """Test the count-based callback trigger register."""

    @pytest.fixture
    def reg(self):
        """Fresh CountCallRegister instance."""
        if CountCallRegister in Register._instances:
            del Register._instances[CountCallRegister]
        r = CountCallRegister()
        yield r
        for sid in list(r.session_id_to_counter.keys()):
            r.clear_session(sid)

    # --- register ---

    def test_register_basic(self, reg):
        cb = MagicMock()
        assert reg.register("s1", "counter", cb) is True
        assert reg.session_id_to_counter["s1"]["counter"] == 0

    def test_register_duplicate_fails(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb)
        assert reg.register("s1", "counter", cb) is False

    def test_register_with_args(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, args={"key": "value"}, threshold=5)
        trigger = reg.session_id_to_trigger["s1"]["counter"]
        assert trigger.threshold == 5
        assert trigger.args == {"key": "value"}

    def test_register_execute_now(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, execute_now=True)
        cb.assert_called_once()

    # --- unregister ---

    def test_unregister(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb)
        assert reg.unregister("s1", "counter") is True
        assert "counter" not in reg.session_id_to_counter.get("s1", {})

    def test_unregister_nonexistent(self, reg):
        assert reg.unregister("s1", "missing") is False

    # --- increase ---

    def test_increase_below_threshold(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=3)
        assert reg.increase("s1", "counter") is True
        assert reg.session_id_to_counter["s1"]["counter"] == 1
        cb.assert_not_called()

    def test_increase_at_threshold_triggers(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=2)
        reg.increase("s1", "counter")  # count=1
        reg.increase("s1", "counter")  # count=2, triggers
        cb.assert_called_once()
        # Counter resets after trigger
        assert reg.session_id_to_counter["s1"]["counter"] == 0

    def test_increase_with_args(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=1, args={"x": 42})
        reg.increase("s1", "counter")
        cb.assert_called_once_with(x=42)

    def test_increase_unregistered_returns_false(self, reg):
        assert reg.increase("s1", "missing") is False

    def test_increase_resets_counter(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=3)
        reg.increase("s1", "counter")  # 1
        reg.increase("s1", "counter")  # 2
        reg.increase("s1", "counter")  # 3 -> triggers, resets to 0
        assert reg.session_id_to_counter["s1"]["counter"] == 0
        # Next cycle
        reg.increase("s1", "counter")  # 1 again
        assert reg.session_id_to_counter["s1"]["counter"] == 1

    # --- reset_count ---

    def test_reset_count(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=5)
        reg.increase("s1", "counter")
        reg.increase("s1", "counter")
        assert reg.session_id_to_counter["s1"]["counter"] == 2
        reg.reset_count("s1", "counter")
        assert reg.session_id_to_counter["s1"]["counter"] == 0

    def test_reset_count_nonexistent(self, reg):
        assert reg.reset_count("s1", "missing") is False

    # --- clear_session ---

    def test_clear_session(self, reg):
        cb = MagicMock()
        reg.register("s1", "counter", cb)
        reg.clear_session("s1")
        assert "s1" not in reg.session_id_to_counter
        assert "s1" not in reg.session_id_to_trigger

    # --- callback exception handling ---

    def test_callback_exception_doesnt_crash(self, reg):
        def bad_callback():
            raise ValueError("boom")
        reg.register("s1", "counter", bad_callback, threshold=1)
        # Should not raise
        assert reg.increase("s1", "counter") is True
