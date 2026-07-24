import pytest
from future_subagent.registry.terminal_gen import TerminalGenerationTracker, get_terminal_gen_tracker


class TestTerminalGenerationTracker:
    def test_register_and_check_current(self):
        tracker = TerminalGenerationTracker()
        tracker.register_expected("r1", 3)
        assert tracker.is_callback_current("r1", 3) is True
        assert tracker.is_callback_current("r1", 4) is True
        assert tracker.is_callback_current("r1", 2) is False

    def test_is_older_equivalent(self):
        tracker = TerminalGenerationTracker()
        tracker.register_expected("r1", 5)
        assert tracker.is_older_equivalent("r1", 4) is True
        assert tracker.is_older_equivalent("r1", 5) is False
        assert tracker.is_older_equivalent("r1", 6) is False

    def test_no_registered_always_current(self):
        tracker = TerminalGenerationTracker()
        assert tracker.is_callback_current("r1", 0) is True
        assert tracker.is_callback_current("r1", 100) is True

    def test_no_registered_not_older(self):
        tracker = TerminalGenerationTracker()
        assert tracker.is_older_equivalent("r1", 0) is False

    def test_retire(self):
        tracker = TerminalGenerationTracker()
        tracker.register_expected("r1", 3)
        tracker.retire("r1")
        assert tracker.is_callback_current("r1", 2) is True
        assert tracker.is_older_equivalent("r1", 2) is False

    def test_retire_nonexistent(self):
        tracker = TerminalGenerationTracker()
        tracker.retire("nonexistent")

    def test_multiple_runs_independent(self):
        tracker = TerminalGenerationTracker()
        tracker.register_expected("r1", 1)
        tracker.register_expected("r2", 5)
        assert tracker.is_callback_current("r1", 1) is True
        assert tracker.is_callback_current("r2", 4) is False
        assert tracker.is_callback_current("r2", 5) is True

    def test_register_overwrites(self):
        tracker = TerminalGenerationTracker()
        tracker.register_expected("r1", 2)
        tracker.register_expected("r1", 5)
        assert tracker.is_callback_current("r1", 3) is False
        assert tracker.is_callback_current("r1", 5) is True


class TestGetTerminalGenTracker:
    def test_singleton(self):
        t1 = get_terminal_gen_tracker()
        t2 = get_terminal_gen_tracker()
        assert t1 is t2
