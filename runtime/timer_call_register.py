import asyncio
import inspect
from loguru import logger
from .core import Register
from typing import Callable, Any
from pydantic import BaseModel, Field
from ._callback_executor import CallbackExecutor


class Timer(BaseModel):
    """Timer configuration"""
    minutes: int = Field(ge=1, le=60)           # 倒计时分钟(1-60)
    callback: Callable                           # 触发回调
    args: dict[str, Any] = Field(default_factory=dict)
    task_name: str | None = None              # 后台 task name, 用于查找/取消


class TimerCallRegister(Register):
    """
    Countdown register class
    Unit: minutes, range: 1-60
    Triggers callback when time is up, auto-destroys after trigger

    All timers run in a separate background thread event loop to avoid
    timer scheduling issues when main event loop is blocked.
    """
    def __init__(self):
        if self._initialized:
            return

        self.session_id_to_timers: dict[str, dict[str, Timer]] = {}
        self._executor = CallbackExecutor(name="timer-register-loop")

        self._initialized = True

    def register(self, session_id: str, name: str, callback: Callable, args: dict[str, Any] | None = None, minutes: int = 15, execute_now: bool = False) -> bool:
        """
        Register a countdown timer

        Args:
            session_id: session ID
            name: timer name
            callback: callback function to trigger
            minutes: countdown minutes, 1-60
            args: keyword arguments to pass to callback
            execute_now: if True, trigger callback immediately upon registration

        Returns:
            whether registration succeeded
        """
        if not (1 <= minutes <= 60):
            logger.error(f"[timer_call_register] minutes must be between 1 and 60, got {minutes}")
            return False

        if name in self.session_id_to_timers.setdefault(session_id, {}):
            logger.warning(f"[timer_call_register] {name} is already registered in session {session_id}")
            return False

        args = args or {}

        timer = Timer(minutes=minutes, callback=callback, args=args)
        task_name = f"timer_{session_id}_{name}"
        timer.task_name = task_name
        self.session_id_to_timers[session_id][name] = timer

        # Execute immediately if requested
        if execute_now:
            try:
                result = callback(**args)
                if inspect.iscoroutine(result):
                    self._executor.run_coroutine(result)
                logger.debug(f"[timer_call_register] execute_now: timer '{name}' triggered immediately for session {session_id}")
            except Exception:
                logger.exception(f"[timer_call_register] execute_now: callback '{name}' failed for session {session_id}")

        # Start the timer coroutine on the background thread's event loop
        self._executor.create_task(
            self._run_timer(session_id, name, minutes, callback, args, timer),
            name=task_name,
        )

        logger.debug(f"[timer_call_register] registered timer '{name}' for session {session_id}, {minutes}min")
        return True

    def unregister(self, session_id: str, name: str) -> bool:
        """
        Cancel a countdown timer
        """
        timers = self.session_id_to_timers.get(session_id)
        if not timers or name not in timers:
            logger.warning(f"[timer_call_register] {name} is not registered in session {session_id}")
            return False

        timer = timers.pop(name, None)
        if timer is None:
            logger.warning(f"[timer_call_register] {name} already removed from session {session_id}")
            return False

        if timer.task_name:
            self._executor.cancel_task(timer.task_name)

        logger.debug(f"[timer_call_register] unregistered timer '{name}' for session {session_id}")
        return True

    async def _run_timer(self, session_id: str, name: str, minutes: int, callback: Callable, args: dict[str, Any], timer_obj: Timer):
        """
        Repeating countdown task (runs in background thread event loop).
        Loops forever until cancelled or unregistered.
        """
        while True:
            try:
                await asyncio.sleep(minutes * 60)
                try:
                    result = callback(**args)
                    if inspect.iscoroutine(result):
                        self._executor.run_coroutine(result)
                    logger.debug(f"[timer_call_register] timer '{name}' triggered after {minutes}min for session {session_id}")
                except Exception:
                    logger.exception(f"[timer_call_register] callback '{name}' failed for session {session_id}")
            except asyncio.CancelledError:
                logger.debug(f"[timer_call_register] timer '{name}' cancelled for session {session_id}")
                break

        # Clean up registration on cancel — only if still the same timer object
        timers = self.session_id_to_timers.get(session_id)
        if timers and name in timers and timers[name] is timer_obj:
            del timers[name]

    def reset_timer(self, session_id: str, name: str) -> bool:
        """
        Reset timer by cancelling current and re-registering

        Args:
            session_id: session ID
            name: timer name

        Returns:
            whether reset succeeded
        """
        timers = self.session_id_to_timers.get(session_id)
        if not timers or name not in timers:
            logger.warning(f"[timer_call_register] {name} is not registered in session {session_id}")
            return False

        old_timer = timers[name]
        minutes = old_timer.minutes
        callback = old_timer.callback
        args = old_timer.args

        if old_timer.task_name:
            self._executor.cancel_task(old_timer.task_name)

        del timers[name]

        new_timer = Timer(minutes=minutes, callback=callback, args=args)
        task_name = f"timer_{session_id}_{name}"
        new_timer.task_name = task_name
        timers[name] = new_timer

        self._executor.create_task(
            self._run_timer(session_id, name, minutes, callback, args, new_timer),
            name=task_name,
        )

        logger.debug(f"[timer_call_register] reset timer '{name}' for session {session_id}, {minutes}min")
        return True

    def clear_session(self, session_id: str):
        """
        Clear all timers for a session
        """
        timers = self.session_id_to_timers.pop(session_id, {})
        for name, timer in timers.items():
            if timer.task_name:
                self._executor.cancel_task(timer.task_name)
        if timers:
            logger.debug(f"[timer_call_register] cleared all timers for session {session_id}")


timer_call_register = TimerCallRegister()
