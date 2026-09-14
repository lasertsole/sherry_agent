"""Module tests for runtime/state_register.py — StateRegisterMeM and StateRegisterDB."""

import sqlite3

import pytest
from runtime.core import Register
from runtime.state_register import StateRegisterMeM


pytestmark = [pytest.mark.module]


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


class TestStateRegisterDBFailures:
    """Audit #68: DB faults are logged and distinguishable from "no data"."""

    @pytest.fixture
    def db_reg(self, tmp_path, monkeypatch):
        import runtime.state_register as state_register_mod

        monkeypatch.setattr(state_register_mod, "SRC_DIR", tmp_path)
        return state_register_mod.StateRegisterDB()

    @pytest.fixture
    def error_logs(self):
        from loguru import logger

        records: list[str] = []
        sink_id = logger.add(lambda m: records.append(str(m)), level="ERROR")
        try:
            yield records
        finally:
            logger.remove(sink_id)

    def test_missing_key_and_session_are_silent(self, db_reg, error_logs):
        """Absent data is a normal path: default value, no error log."""
        assert db_reg.get_state("s", "k", "fallback") == "fallback"
        assert db_reg.has_session("s") is False
        assert db_reg.has_key("s", "k") is False
        assert db_reg.delete_state("s", "k") is False
        assert db_reg.get_all_states("s") == {}
        assert db_reg.get_all_session_ids() == []
        assert error_logs == []

    def test_database_error_returns_defaults_and_logs(self, db_reg, error_logs, monkeypatch):
        """Every method keeps its default return, but the fault is visible."""

        def _broken_connect(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(sqlite3, "connect", _broken_connect)

        assert db_reg.set_state("s", "k", 1) is False
        assert db_reg.get_state("s", "k", "fallback") == "fallback"
        assert db_reg.get_all_states("s") == {}
        assert db_reg.delete_state("s", "k") is False
        assert db_reg.get_all_session_ids() == []
        assert db_reg.has_session("s") is False
        assert db_reg.has_key("s", "k") is False
        assert db_reg.update_states("s", {"k": 2}) is False

        assert len(error_logs) == 8
        assert all("database error" in record for record in error_logs)
        assert any("get_state" in record for record in error_logs)
        assert any("update_states" in record for record in error_logs)

    def test_corrupt_json_value_is_logged_separately(self, db_reg, error_logs):
        """A corrupt stored value gets its own classification, not a DB error."""
        conn = sqlite3.connect(db_reg.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO states (session_id, key, value) VALUES (?, ?, ?)",
                ("s", "k", "{not json"),
            )
            conn.commit()
        finally:
            conn.close()

        assert db_reg.get_state("s", "k", "fallback") == "fallback"
        assert any("corrupt JSON" in record for record in error_logs)
        assert not any("database error" in record for record in error_logs)
