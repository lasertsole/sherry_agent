import asyncio
import threading
from loguru import logger


class CallbackExecutor:
    """Dedicated thread with a persistent event loop for running async callbacks.

    Avoids creating a new thread + loop per callback, while keeping the
    loop alive so cached async clients (httpx, AsyncOpenAI) don't get
    "Event loop is closed" errors on GC.
    """

    def __init__(self, name: str = "callback-executor"):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_event = threading.Event()
        self._name = name

    def _run_loop(self):
        """Thread target: create and run a persistent event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._start_event.set()
        self._loop.run_forever()

    def _ensure_running(self):
        """Lazy-start the background thread and loop."""
        if self._thread is None or not self._thread.is_alive():
            self._start_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name=self._name
            )
            self._thread.start()
            self._start_event.wait(timeout=5)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self._ensure_running()
        return self._loop

    def run_coroutine(self, coro, timeout: float = 60 * 60):
        """Submit an async callback to the background event loop (fire-and-forget).

        The coroutine runs on the dedicated background loop via
        call_soon_threadsafe.  No result is waited on — the callback
        executes asynchronously.  A timeout timer is registered on the
        background loop via call_later; if the callback does not complete
        within the given time its task is cancelled.
        """
        loop = self.loop

        async def _wrapped():
            try:
                await coro
            except asyncio.CancelledError:
                logger.warning("Async callback timed out after {}s", timeout)
            except Exception:
                logger.exception("Async callback failed in background executor")

        def _schedule():
            task = asyncio.create_task(_wrapped())
            loop.call_later(timeout, lambda: task.cancel() if not task.done() else None)

        loop.call_soon_threadsafe(_schedule, *())

    def create_task(self, coro, name: str = None, timeout: float = 60 * 60):
        """Create a task on the background event loop.

        Returns a threading.Event that is set when the task completes
        (or is cancelled/timed out).  A timeout timer is registered on the
        background loop via call_later; if the coroutine does not complete
        within the given time its task is cancelled.
        """
        loop = self.loop
        done = threading.Event()

        async def _wrapped():
            try:
                await coro
            except asyncio.CancelledError:
                logger.warning("Task '{}' cancelled (timeout after {}s)", name, timeout)
            finally:
                done.set()

        def _schedule():
            task = asyncio.create_task(_wrapped(), name=name)
            loop.call_later(timeout, lambda: task.cancel() if not task.done() else None)

        loop.call_soon_threadsafe(_schedule, *())
        return done

    def cancel_task(self, name: str):
        """Cancel a task on the background loop by name."""
        loop = self.loop

        def _cancel():
            for t in asyncio.all_tasks(loop):
                if t.get_name() == name and not t.done():
                    t.cancel()
                    return

        loop.call_soon_threadsafe(_cancel)
