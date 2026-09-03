"""Module tests for runtime/count_call_register.py — CountCallRegister."""

import sys
import threading

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

    # --- concurrency / atomicity (audit #9) ---

    def test_increase_concurrent_no_lost_counts(self, reg):
        """Parallel increase() calls must not lose increments (audit #9 RMW race).

        8 threads x 250 increments on threshold=10 -> exactly 200 callback
        fires and a final counter of 0. Without the lock, the read-modify-write
        race loses updates and the callback fires fewer times. The shrunken
        switch interval forces frequent thread preemptions to amplify
        interleavings inside the race window.
        """
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=10)

        threads_count, per_thread = 8, 250  # 2000 increments total
        barrier = threading.Barrier(threads_count)

        def worker():
            barrier.wait()  # maximize contention on the first increments
            for _ in range(per_thread):
                reg.increase("s1", "counter")

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        old_switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(old_switch_interval)

        total = threads_count * per_thread
        assert cb.call_count == total // 10
        assert reg.session_id_to_counter["s1"]["counter"] == total % 10

    def test_increase_overflow_carried_not_discarded(self, reg):
        """Hitting the threshold must carry the overshoot, not hard-reset to 0 (audit #9).

        The public API keeps the counter below threshold, so a counter past
        the threshold is injected directly (legacy state / defensive path).
        Audit scenario: count=5, threshold=3 -> next cycle must start at 2.
        """
        cb = MagicMock()
        reg.register("s1", "counter", cb, threshold=3)
        reg.session_id_to_counter["s1"]["counter"] = 4

        assert reg.increase("s1", "counter") is True
        cb.assert_called_once()
        # 5 >= 3 fires, and 5 % 3 = 2 is carried into the next cycle.
        assert reg.session_id_to_counter["s1"]["counter"] == 2

    def test_callback_runs_outside_lock(self, reg):
        """Callbacks must fire with the register lock released.

        A callback that re-enters increase() (or any locked API) would
        deadlock if the lock were still held during the callback. acquire()
        with a timeout proves the lock is free; it can never hang the test.
        """
        lock_acquirable: list[bool] = []
        called: list[bool] = []

        def cb():
            called.append(True)
            got = reg._lock.acquire(timeout=2)
            lock_acquirable.append(got)
            if got:
                reg._lock.release()

        reg.register("s1", "counter", cb, threshold=1)
        reg.increase("s1", "counter")
        assert called == [True]
        assert lock_acquirable == [True]
