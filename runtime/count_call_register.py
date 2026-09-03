import inspect
import threading
from loguru import logger
from .core import Register
from typing import Callable, Any
from pydantic import BaseModel, Field
from ._callback_executor import CallbackExecutor as _CallbackExecutor


class Trigger(BaseModel):
    threshold: int = 1
    callback: Callable
    args: dict[str, Any] = Field(default_factory=dict)


class CountCallRegister(Register):
    """
    Count register for tracking and triggering callbacks
    """

    def __init__(self):
        if self._initialized:
            return

        self.session_id_to_counter: dict[str, dict[str, int]] = {}
        self.session_id_to_trigger: dict[str, dict[str, Trigger]] = {}
        self._callback_executor = _CallbackExecutor()

        # Guards the counter value lifecycle (read-modify-write in increase(),
        # value writes in reset_count) so concurrent callers cannot lose
        # increments. Callbacks are fired OUTSIDE this lock — a callback that
        # re-enters increase() must not deadlock (see test_callback_runs_outside_lock).
        self._lock: threading.Lock = threading.Lock()

        self._initialized = True

    def register(
        self,
        session_id: str,
        name: str,
        callback: Callable,
        threshold: int = 1,
        args: dict[str, Any] = None,
        execute_now: bool = False,
    ) -> bool:
        """
        Register a counter with callback

        Args:
            session_id: session ID
            name: counter name
            callback: callback function to trigger on threshold
            threshold: count threshold to trigger callback
            args: keyword arguments to pass to callback
            execute_now: if True, immediately increase count and check threshold upon registration

        Returns:
            whether registration succeeded
        """
        if args is None:
            args = {}

        if name in self.session_id_to_counter.setdefault(session_id, {}):
            logger.debug(f"{name} is already registered for session {session_id}")
            return False

        self.session_id_to_counter.setdefault(session_id, {})[name] = 0
        self.session_id_to_trigger.setdefault(session_id, {})[name] = Trigger(
            threshold=threshold, callback=callback, args=args
        )

        # Execute immediately if requested
        if execute_now:
            try:
                result = callback(**args)
                if inspect.iscoroutine(result):
                    self._callback_executor.run_coroutine(result)
                logger.debug(
                    f"[count_call_register] execute_now: callback '{name}' triggered immediately for session {session_id}"
                )
            except Exception:
                logger.exception(
                    f"[count_call_register] execute_now: callback '{name}' failed for session {session_id}"
                )

        return True

    def unregister(self, session_id: str, name: str) -> bool:
        """
        Unregister a counter
        """
        if name not in self.session_id_to_counter.setdefault(session_id, {}):
            logger.error(f"{name} is not registered for session {session_id}")
            return False

        del self.session_id_to_counter.setdefault(session_id, {})[name]
        del self.session_id_to_trigger.setdefault(session_id, {})[name]

        return True

    def increase(self, session_id: str, name: str) -> bool:
        """
        Increase counter value
        """
        # Snapshot of a threshold-hit callback, fired after the lock is
        # released so a re-entrant increase() from inside the callback
        # cannot deadlock on a lock its own caller still holds.
        pending: tuple[Callable[..., Any], dict[str, Any]] | None = None

        with self._lock:
            counters: dict[str, int] = self.session_id_to_counter.setdefault(session_id, {})
            if name not in counters:
                logger.error(f"{name} is not registered")
                return False

            # Read-modify-write is atomic under the lock: without it, two
            # threads can read the same value, both increment, and one
            # update is silently lost (audit #9).
            now_counter: int = counters[name] + 1

            trigger: Trigger = self.session_id_to_trigger.setdefault(session_id, {})[name]
            threshold: int = trigger.threshold

            if now_counter >= threshold:
                # Carry the overshoot into the next cycle instead of
                # hard-resetting to 0, which would silently discard it
                # (audit #9: count=5, threshold=3 -> keep 2, not 0).
                now_counter %= threshold
                pending = (trigger.callback, trigger.args)

            counters[name] = now_counter

        if pending is not None:
            callback, args = pending
            try:
                result = callback(**args)
                if inspect.iscoroutine(result):
                    self._callback_executor.run_coroutine(result)
            except Exception:
                logger.exception(f"Callback '{name}' failed for session {session_id}")

        return True

    def reset_count(self, session_id: str, name: str) -> bool:
        """
        Reset counter to zero
        """
        # Same lock as increase(): an unlocked writer here could interleave
        # with increase()'s locked read-modify-write and resurrect a stale value.
        with self._lock:
            counters: dict[str, int] = self.session_id_to_counter.get(session_id, {})
            if name not in counters:
                logger.error(f"{name} is not registered for session {session_id}")
                return False

            counters[name] = 0
        return True

    def clear_session(self, session_id: str):
        self.session_id_to_counter.pop(session_id, None)
        self.session_id_to_trigger.pop(session_id, None)


count_call_register = CountCallRegister()
