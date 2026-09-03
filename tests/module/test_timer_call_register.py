"""Module tests for runtime/timer_call_register.py — TimerCallRegister."""

import asyncio
import time
from collections.abc import Generator

import pytest
from unittest.mock import MagicMock
from runtime.core import Register
from runtime.timer_call_register import TimerCallRegister


def _settle(seconds: float = 0.5) -> None:
    """Give the background timer loop time to process queued cancels/creates."""
    time.sleep(seconds)


def _wait_done(task: asyncio.Task[None], timeout: float = 3.0) -> None:
    """Poll until a background task is done — cancellation is async, never assume timing."""
    deadline = time.monotonic() + timeout
    while not task.done():
        if time.monotonic() > deadline:
            pytest.fail(f"task '{task.get_name()}' not done after {timeout}s")
        time.sleep(0.05)


class TestTimerCallRegister:
    """Test the countdown timer register (audit #8: per-generation task names)."""

    @pytest.fixture
    def reg(self) -> Generator[TimerCallRegister, None, None]:
        """Fresh TimerCallRegister instance; its loop is stopped afterwards."""
        if TimerCallRegister in Register._instances:
            del Register._instances[TimerCallRegister]
        r = TimerCallRegister()
        yield r
        for sid in list(r.session_id_to_timers.keys()):
            r.clear_session(sid)
        _settle(0.3)
        loop = r._executor._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    def _live_timer_tasks(
        self, reg: TimerCallRegister, session_id: str, name: str = ""
    ) -> list[asyncio.Task[None]]:
        """Snapshot of non-done tasks for one timer (or all timers of a session)."""
        loop = reg._executor._loop
        prefix = f"timer_{session_id}_{name}" if name else f"timer_{session_id}_"
        return [
            t
            for t in list(asyncio.all_tasks(loop))
            if t.get_name().startswith(prefix) and not t.done()
        ]

    def _wait_live(
        self,
        reg: TimerCallRegister,
        session_id: str,
        name: str = "",
        count: int = 1,
        timeout: float = 3.0,
    ) -> list[asyncio.Task[None]]:
        """Poll until exactly `count` live tasks exist for one timer (or all
        timers of a session) — task creation/cancellation are async, never
        assume timing."""
        deadline = time.monotonic() + timeout
        live: list[asyncio.Task[None]] = []
        while True:
            live = self._live_timer_tasks(reg, session_id, name)
            if len(live) == count:
                return live
            if time.monotonic() > deadline:
                pytest.fail(
                    f"expected {count} live task(s) for timer '{session_id}/{name}', "
                    f"got {len(live)} after {timeout}s"
                )
            time.sleep(0.05)

    # --- register ---

    def test_register_creates_background_task(self, reg: TimerCallRegister):
        cb = MagicMock()
        assert reg.register("s1", "t1", cb) is True
        timer = reg.session_id_to_timers["s1"]["t1"]
        assert timer.task_name and timer.task_name.startswith("timer_s1_t1_")
        live = self._wait_live(reg, "s1", "t1")
        assert live[0].get_name() == timer.task_name

    def test_register_duplicate_fails(self, reg: TimerCallRegister):
        cb = MagicMock()
        assert reg.register("s1", "t1", cb) is True
        assert reg.register("s1", "t1", cb) is False

    def test_register_invalid_minutes(self, reg: TimerCallRegister):
        assert reg.register("s1", "t1", MagicMock(), minutes=0) is False
        assert reg.register("s1", "t1", MagicMock(), minutes=61) is False

    def test_register_execute_now(self, reg: TimerCallRegister):
        cb = MagicMock()
        assert reg.register("s1", "t1", cb, execute_now=True) is True
        cb.assert_called_once()

    # --- unregister ---

    def test_unregister_cancels_task(self, reg: TimerCallRegister):
        cb = MagicMock()
        reg.register("s1", "t1", cb)
        task = self._wait_live(reg, "s1", "t1")[0]
        assert reg.unregister("s1", "t1") is True
        _wait_done(task)
        assert "t1" not in reg.session_id_to_timers.get("s1", {})
        assert self._live_timer_tasks(reg, "s1", "t1") == []

    def test_unregister_nonexistent(self, reg: TimerCallRegister):
        assert reg.unregister("s1", "missing") is False

    # --- reset_timer (audit #8 core) ---

    def test_reset_cancels_old_generation_task(self, reg: TimerCallRegister):
        cb = MagicMock()
        reg.register("s1", "t1", cb)
        old_tasks = self._wait_live(reg, "s1", "t1")
        old_name = old_tasks[0].get_name()

        assert reg.reset_timer("s1", "t1") is True
        _wait_done(old_tasks[0])

        # Old generation is gone, exactly one live task remains
        live = self._wait_live(reg, "s1", "t1")
        # New generation got a FRESH unique name — never reusable (audit #8)
        assert live[0].get_name() != old_name
        assert live[0].get_name() == reg.session_id_to_timers["s1"]["t1"].task_name

    def test_reset_twice_only_newest_generation_survives(self, reg: TimerCallRegister):
        cb = MagicMock()
        reg.register("s1", "t1", cb)
        gen1 = self._wait_live(reg, "s1", "t1")

        assert reg.reset_timer("s1", "t1") is True
        _wait_done(gen1[0])
        gen2 = self._wait_live(reg, "s1", "t1")

        assert reg.reset_timer("s1", "t1") is True
        _wait_done(gen2[0])
        gen3 = self._wait_live(reg, "s1", "t1")

        assert len(gen3) == 1
        assert gen1[0].done()
        # Generation 2 must also be gone — this is the leak #8 describes
        assert gen2[0].done()

    def test_reset_nonexistent(self, reg: TimerCallRegister):
        assert reg.reset_timer("s1", "missing") is False

    # --- clear_session ---

    def test_clear_session_cancels_all_timers(self, reg: TimerCallRegister):
        cb = MagicMock()
        reg.register("s1", "t1", cb)
        reg.register("s1", "t2", cb)
        tasks = self._wait_live(reg, "s1", count=2)

        reg.clear_session("s1")
        for t in tasks:
            _wait_done(t)

        assert self._live_timer_tasks(reg, "s1") == []
        assert "s1" not in reg.session_id_to_timers

    # --- unregister + register same name (generation independence) ---

    def test_unregister_then_register_same_name(self, reg: TimerCallRegister):
        cb = MagicMock()
        reg.register("s1", "t1", cb)
        first = self._wait_live(reg, "s1", "t1")

        assert reg.unregister("s1", "t1") is True
        assert reg.register("s1", "t1", cb) is True
        _wait_done(first[0])

        live = self._wait_live(reg, "s1", "t1")
        # The re-registered generation must survive and own a fresh name
        assert live[0].get_name() != first[0].get_name()
        assert first[0].done()
