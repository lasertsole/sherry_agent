"""Unit test for runtime/core.py — Register ABC and clear_all_register_sessions."""

import pytest
from abc import ABC, abstractmethod
from runtime.core import Register, clear_all_register_sessions


class ConcreteRegister(Register):
    """Concrete implementation for testing the Register ABC."""

    def __init__(self):
        if self._initialized:
            return
        self._cleared_sessions = []
        self._initialized = True

    def clear_session(self, session_id: str):
        self._cleared_sessions.append(session_id)


class TestRegisterSingleton:
    """Test Register singleton pattern."""

    def test_same_instance(self):
        a = ConcreteRegister()
        b = ConcreteRegister()
        assert a is b

    def test_initialized_flag(self):
        reg = ConcreteRegister()
        assert reg._initialized is True

    def test_different_subclasses_are_different_singletons(self):
        class AnotherRegister(Register):
            def __init__(self):
                if self._initialized:
                    return
                self.data = []
                self._initialized = True
            def clear_session(self, session_id):
                pass

        a = ConcreteRegister()
        b = AnotherRegister()
        assert a is not b


class TestClearAllRegisterSessions:
    """Test the clear_all_register_sessions classmethod."""

    def setup_method(self):
        """Ensure ConcreteRegister is registered."""
        self.reg = ConcreteRegister()
        self.reg._cleared_sessions.clear()

    def test_clears_all_subclass_instances(self):
        ConcreteRegister._instances[ConcreteRegister] = self.reg
        clear_all_register_sessions("test-session")
        assert "test-session" in self.reg._cleared_sessions

    def test_clears_multiple_sessions(self):
        ConcreteRegister._instances[ConcreteRegister] = self.reg
        clear_all_register_sessions("session-a")
        clear_all_register_sessions("session-b")
        assert "session-a" in self.reg._cleared_sessions
        assert "session-b" in self.reg._cleared_sessions


class TestModuleLevelFunction:
    """Test the module-level clear_all_register_sessions function."""

    def test_calls_class_method(self):
        reg = ConcreteRegister()
        reg._cleared_sessions.clear()
        ConcreteRegister._instances[ConcreteRegister] = reg
        clear_all_register_sessions("mod-test")
        assert "mod-test" in reg._cleared_sessions
