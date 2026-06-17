import inspect
from loguru import logger
from .core import Register
from typing import Callable, Any
from pydantic import BaseModel, Field
from ._callback_executor import CallbackExecutor as _CallbackExecutor


class Trigger(BaseModel):
    threshold: int = 1
    callback: Callable
    args: dict[str, Any] = Field(default_factory=dict)

class CountRegister(Register):
    """
    Count register for tracking and triggering callbacks
    """
    def __init__(self):
        if self._initialized:
            return

        self.session_id_to_counter: dict[str, dict[str, int]] = {}
        self.session_id_to_trigger: dict[str, dict[str, Trigger]] = {}
        self._callback_executor = _CallbackExecutor()

        self._initialized = True

    def register(self, session_id: str, name: str, callback: Callable, threshold: int = 1, args: dict[str, Any] = None, execute_now: bool = False)-> bool:
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
            logger.info(f"{name} is already registered for session {session_id}")
            return False

        self.session_id_to_counter.setdefault(session_id, {})[name] = 0
        self.session_id_to_trigger.setdefault(session_id, {})[name] = Trigger(threshold = threshold, callback = callback, args = args)

        # Execute immediately if requested
        if execute_now:
            try:
                result = callback(**args)
                if inspect.iscoroutine(result):
                    self._callback_executor.run_coroutine(result)
                logger.info(f"[count_register] execute_now: callback '{name}' triggered immediately for session {session_id}")
            except Exception:
                logger.exception(f"[count_register] execute_now: callback '{name}' failed for session {session_id}")

        return True

    def unregister(self, session_id: str, name: str)-> bool:
        """
        Unregister a counter
        """
        if name not in self.session_id_to_counter.setdefault(session_id, {}):
            logger.error(f"{name} is not registered for session {session_id}")
            return False

        del self.session_id_to_counter.setdefault(session_id, {})[name]
        del self.session_id_to_trigger.setdefault(session_id, {})[name]

        return True


    def increase(self, session_id: str, name: str)-> bool:
        """
        Increase counter value
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
                    self._callback_executor.run_coroutine(result)
            except Exception:
                logger.exception(f"Callback '{name}' failed for session {session_id}")

            now_counter = 0

        self.session_id_to_counter.setdefault(session_id, {})[name] = now_counter

        return True

    def reset_count(self, session_id: str, name: str) -> bool:
        """
        Reset counter to zero
        """
        if name not in self.session_id_to_counter.get(session_id, {}):
            logger.error(f"{name} is not registered for session {session_id}")
            return False

        self.session_id_to_counter[session_id][name] = 0
        return True

    def clear_session(self, session_id: str):
        del self.session_id_to_counter[session_id]
        del self.session_id_to_trigger[session_id]

count_register = CountRegister()
