import asyncio
import logging
from typing import Coroutine
from threading import Thread, Lock
_logger = logging.getLogger(__name__)

class AsyncTaskQueue:
    """Async task queue for background operations."""
    _instance = None
    _lock = Lock()  # tread safe lock

    def __new__(cls):
        """single instance"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)

                # ensure init process only invoke once
                cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._queue: asyncio.Queue[Coroutine] = asyncio.Queue()
        self._event_loop = asyncio.new_event_loop()

    def start(self) -> None:
        """Start the background worker."""
        if not self._event_loop.is_running():
            self._event_loop.call_soon(asyncio.create_task, self._worker())
            self._event_loop.run_forever()

    async def _worker(self) -> None:
        """Process tasks from the queue."""
        while True:
            core: Coroutine = await self._queue.get()
            try:
                await core
            except Exception as e:
                _logger.error(f"Task failed: {e}", exc_info=True)
            finally:
                self._queue.task_done()

    def add_task(self, task: Coroutine) -> None:
        asyncio.run_coroutine_threadsafe(self._queue.put(task), self._event_loop)

async_task_queue = AsyncTaskQueue()
_async_task_thread: Thread = Thread(target=lambda: async_task_queue.start(), daemon=True)
_async_task_thread.start()