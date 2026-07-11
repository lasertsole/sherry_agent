"""Module tests for runtime/state_register.py — StateRegisterMeM and StateRegisterDB."""

import json
import pytest
from pathlib import Path
from runtime.core import Register
from runtime.state_register import StateRegisterMeM


class TestStateRegisterMeM:
    """Test in-memory state register."""

    @pytest.fixture
    def reg(self):
        """Fresh StateRegisterMeM with singleton reset."""
        if StateRegisterMeM in Register._instances:
            del Register._instances[StateRegisterMeM]
        r = StateRegisterMeM()
        yield r
        for sid in list(r._states.keys()):
            r.clear_session(sid)

    # --- set_state / get_state ---

    def test_set_get_basic(self, reg):
        assert reg.set_state("s1", "key", "value") is True
        assert reg.get_state("s1", "key") == "value"

    def test_get_default(self, reg):
        assert reg.get_state("s1", "missing") is None
        assert reg.get_state("s1", "missing", "fallback") == "fallback"

    def test_set_overwrite(self, reg):
        reg.set_state("s1", "k", "old")
        reg.set_state("s1", "k", "new")
        assert reg.get_state("s1", "k") == "new"

    def test_set_complex_value(self, reg):
        data = {"nested": [1, 2, 3]}
        reg.set_state("s1", "complex", data)
        assert reg.get_state("s1", "complex") == data

    # --- get_all_states ---

    def test_get_all_states(self, reg):
        reg.set_state("s1", "a", 1)
        reg.set_state("s1", "b", 2)
        all_states = reg.get_all_states("s1")
        assert all_states == {"a": 1, "b": 2}

    def test_get_all_states_empty_session(self, reg):
        assert reg.get_all_states("nonexistent") == {}

    # --- delete_state ---

    def test_delete_existing(self, reg):
        reg.set_state("s1", "k", "v")
        assert reg.delete_state("s1", "k") is True
        assert reg.get_state("s1", "k") is None

    def test_delete_nonexistent_key(self, reg):
        assert reg.delete_state("s1", "missing") is False

    def test_delete_nonexistent_session(self, reg):
        assert reg.delete_state("no_session", "k") is False

    # --- clear_session ---

    def test_clear_session(self, reg):
        reg.set_state("s1", "a", 1)
        reg.set_state("s1", "b", 2)
        assert reg.clear_session("s1") is True
        assert reg.get_all_states("s1") == {}

    def test_clear_nonexistent_session(self, reg):
        assert reg.clear_session("no_session") is False

    # --- has_session / has_key ---

    def test_has_session(self, reg):
        assert reg.has_session("s1") is False
        reg.set_state("s1", "k", "v")
        assert reg.has_session("s1") is True

    def test_has_key(self, reg):
        reg.set_state("s1", "k", "v")
        assert reg.has_key("s1", "k") is True
        assert reg.has_key("s1", "missing") is False

    def test_has_key_nonexistent_session(self, reg):
        assert reg.has_key("no_session", "k") is False

    # --- update_states ---

    def test_update_states(self, reg):
        reg.set_state("s1", "a", 1)
        reg.update_states("s1", {"a": 100, "b": 200})
        assert reg.get_state("s1", "a") == 100
        assert reg.get_state("s1", "b") == 200

    def test_update_creates_session(self, reg):
        reg.update_states("new_session", {"x": 1})
        assert reg.get_state("new_session", "x") == 1

    # --- Multi-session isolation ---

    def test_sessions_isolated(self, reg):
        reg.set_state("s1", "k", "v1")
        reg.set_state("s2", "k", "v2")
        assert reg.get_state("s1", "k") == "v1"
        assert reg.get_state("s2", "k") == "v2"

    def test_clear_one_session_preserves_others(self, reg):
        reg.set_state("s1", "k", "v1")
        reg.set_state("s2", "k", "v2")
        reg.clear_session("s1")
        assert reg.get_state("s1", "k") is None
        assert reg.get_state("s2", "k") == "v2"

    # --- Singleton init guard ---

    def test_init_guard(self, reg):
        """Second __init__ call should not reset _states."""
        reg.set_state("s1", "k", "v")
        reg.__init__()  # Should not wipe state
        assert reg.get_state("s1", "k") == "v"
