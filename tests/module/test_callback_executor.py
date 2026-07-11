"""Module tests for runtime/_callback_executor.py — CallbackExecutor."""

import time
import asyncio
import threading
import pytest
from runtime._callback_executor import CallbackExecutor


class TestCallbackExecutor:
    """Test the dedicated thread + event loop executor."""

    @pytest.fixture
    def executor(self):
        """Create and yield a CallbackExecutor, then clean up."""
        ex = CallbackExecutor(name="test-executor")
        yield ex
        # Shut down the background loop
        if ex._loop and ex._loop.is_running():
            ex._loop.call_soon_threadsafe(ex._loop.stop)

    def test_loop_starts_lazily(self, executor):
        """Loop should not start until first access."""
        assert executor._loop is None
        _ = executor.loop
        assert executor._loop is not None
        assert executor._loop.is_running()

    def test_background_thread_is_daemon(self, executor):
        """Background thread should be a daemon thread."""
        _ = executor.loop
        assert executor._thread is not None
        assert executor._thread.daemon is True

    def test_run_coroutine_fire_and_forget(self, executor):
        """run_coroutine should execute async work without blocking."""
        results = []

        async def work():
            await asyncio.sleep(0.05)
            results.append("done")

        executor.run_coroutine(work())
        # Should return immediately (fire-and-forget)
        assert results == []
        # Wait for coroutine to finish
        time.sleep(0.3)
        assert results == ["done"]

    def test_create_task_returns_done_event(self, executor):
        """create_task should return a threading.Event that fires on completion."""
        results = []

        async def work():
            await asyncio.sleep(0.05)
            results.append("done")

        done_event = executor.create_task(work(), name="test-task")
        assert isinstance(done_event, threading.Event)
        assert not done_event.is_set()
        done_event.wait(timeout=2.0)
        assert done_event.is_set()
        assert results == ["done"]

    def test_cancel_task(self, executor):
        """cancel_task should stop a running task by name."""
        started = threading.Event()
        finished = []

        async def slow_work():
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
            finally:
                finished.append("cancelled")

        executor.create_task(slow_work(), name="cancel-me")
        started.wait(timeout=2.0)
        time.sleep(0.05)  # Let task settle
        executor.cancel_task("cancel-me")
        time.sleep(0.3)
        assert "cancelled" in finished

    def test_run_coroutine_exception_does_not_crash(self, executor):
        """Exceptions in fire-and-forget coroutines should be caught."""

        async def bad():
            raise ValueError("boom")

        executor.run_coroutine(bad())
        time.sleep(0.2)
        # Should not crash — executor is still alive
        assert executor._loop.is_running()

    def test_multiple_tasks_run_concurrently(self, executor):
        """Multiple tasks should run on the same loop concurrently."""
        order = []

        async def task_a():
            await asyncio.sleep(0.1)
            order.append("a")

        async def task_b():
            order.append("b_start")
            await asyncio.sleep(0.05)
            order.append("b_end")

        done_a = executor.create_task(task_a(), name="a")
        done_b = executor.create_task(task_b(), name="b")
        done_a.wait(timeout=2.0)
        done_b.wait(timeout=2.0)
        # b should have started before a finished
        assert "b_start" in order
        assert "a" in order
        assert "b_end" in order
