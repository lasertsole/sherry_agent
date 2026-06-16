import inspect
import asyncio
import threading
from loguru import logger
from .core import Register

from typing import Callable, Any
from pydantic import BaseModel, Field

class Trigger(BaseModel):
    threshold: int = 1
    callback: Callable
    args: dict[str, Any] = Field(default_factory=dict)
    reset_when_trigger: bool = True

class _CallbackExecutor:
    """Dedicated thread with a persistent event loop for running async callbacks.

    Avoids creating a new thread + loop per callback, while keeping the
    loop alive so cached async clients (httpx, AsyncOpenAI) don't get
    "Event loop is closed" errors on GC.
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_event = threading.Event()

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
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="count-register-callback")
            self._thread.start()
            self._start_event.wait(timeout=5)

    def run(self, coro):
        """Submit an async callback to the background event loop (fire-and-forget).

        The coroutine runs on the dedicated background loop via
        call_soon_threadsafe.  No result is waited on — the callback
        executes asynchronously.
        """
        self._ensure_running()

        async def _wrapped():
            try:
                await coro
            except Exception:
                logger.exception("Async callback failed in background executor")

        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(_wrapped()))

class CountRegister(Register):
    """
    统计注册类
    """
    def __init__(self):
        if self._initialized:
            return

        self.session_id_to_counter: dict[str, dict[str, int]] = {}
        self.session_id_to_trigger: dict[str, dict[str, Trigger]] = {}
        self._callback_executor = _CallbackExecutor()

        self._initialized = True

    def register(self, session_id: str, name: str, callback: Callable, threshold: int = 1, args: dict[str, Any] = None)-> bool:
        """
        注册统计函数
        """
        if args is None:
            args = {}

        if name in self.session_id_to_counter.setdefault(session_id, {}):
            logger.info(f"{name} is already registered for session {session_id}")
            return False

        self.session_id_to_counter.setdefault(session_id, {})[name] = 0
        self.session_id_to_trigger.setdefault(session_id, {})[name] = Trigger(threshold = threshold, callback = callback, args = args)

        return True

    def unregister(self, session_id: str, name: str)-> bool:
        """
        取消注册
        """
        if name not in self.session_id_to_counter.setdefault(session_id, {}):
            logger.error(f"{name} is not registered for session {session_id}")
            return False

        del self.session_id_to_counter.setdefault(session_id, {})[name]
        del self.session_id_to_trigger.setdefault(session_id, {})[name]

        return True


    def increase(self, session_id: str, name: str)-> bool:
        """
        增加统计值
        """
        if name not in self.session_id_to_counter.setdefault(session_id, {}):
            logger.error(f"{name} is not registered")
            return False

        now_counter:int = self.session_id_to_counter.setdefault(session_id, {})[name] + 1

        trigger: Trigger = self.session_id_to_trigger.setdefault(session_id, {})[name]
        threshold: int = trigger.threshold

        if now_counter >= threshold:
            callback: Callable = trigger.callback
            args: dict[str, Any] = trigger.args

            try:
                result = callback(**args)
                if inspect.iscoroutine(result):
                    self._callback_executor.run(result)
            except Exception:
                logger.exception(f"Callback '{name}' failed for session {session_id}")

            if trigger.reset_when_trigger:
                now_counter = 0

        self.session_id_to_counter.setdefault(session_id, {})[name] = now_counter

        return True
    
    def clear_session(self, session_id: str):
        del self.session_id_to_counter[session_id]
        del self.session_id_to_trigger[session_id]

count_register = CountRegister()
