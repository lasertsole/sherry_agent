import asyncio
import inspect
from loguru import logger
from .core import Register
from ._callback_executor import CallbackExecutor
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field


class Timer(BaseModel):
    """定时器配置"""
    minutes: int = Field(ge=1, le=60)           # 倒计时分钟(1-60)
    callback: Callable                           # 触发回调
    args: dict[str, Any] = Field(default_factory=dict)
    task_name: Optional[str] = None              # 后台 task name, 用于查找/取消


class TimerRegister(Register):
    """
    倒计时注册类
    单位: 分钟, 范围: 1-60
    到达时间后触发回调, 触发后自动销毁

    所有定时器在独立的后台线程 event loop 中运行, 避免主 event loop
    阻塞时定时器无法调度。
    """
    def __init__(self):
        if self._initialized:
            return

        self.session_id_to_timers: dict[str, dict[str, Timer]] = {}
        self._executor = CallbackExecutor(name="timer-register-loop")

        self._initialized = True

    def register(self, session_id: str, name: str, callback: Callable, minutes: int = 1, args: dict[str, Any] | None = None) -> bool:
        """
        注册倒计时

        Args:
            session_id: 会话ID
            name: 定时器名称
            callback: 到时触发的回调函数
            minutes: 倒计时分钟数, 1-60
            args: 传递给回调的关键字参数

        Returns:
            是否注册成功
        """
        if not (1 <= minutes <= 60):
            logger.error(f"[timer_register] minutes must be between 1 and 60, got {minutes}")
            return False

        if name in self.session_id_to_timers.setdefault(session_id, {}):
            logger.warning(f"[timer_register] {name} is already registered in session {session_id}")
            return False

        args = args or {}

        timer = Timer(minutes=minutes, callback=callback, args=args)
        task_name = f"timer_{session_id}_{name}"
        timer.task_name = task_name
        self.session_id_to_timers[session_id][name] = timer

        # Start the timer coroutine on the background thread's event loop
        self._executor.create_task(
            self._run_timer(session_id, name, minutes, callback, args),
            name=task_name,
        )

        logger.info(f"[timer_register] registered timer '{name}' for session {session_id}, {minutes}min")
        return True

    def unregister(self, session_id: str, name: str) -> bool:
        """
        取消倒计时
        """
        timers = self.session_id_to_timers.get(session_id)
        if not timers or name not in timers:
            logger.warning(f"[timer_register] {name} is not registered in session {session_id}")
            return False

        timer = timers[name]
        if timer.task_name:
            self._executor.cancel_task(timer.task_name)

        del timers[name]
        logger.info(f"[timer_register] unregistered timer '{name}' for session {session_id}")
        return True

    async def _run_timer(self, session_id: str, name: str, minutes: int, callback: Callable, args: dict[str, Any]):
        """
        后台倒计时任务 (运行在后台线程的事件循环中)
        """
        try:
            await asyncio.sleep(minutes * 60)
            try:
                result = callback(**args)
                if inspect.iscoroutine(result):
                    self._executor.run_coroutine(result)
                logger.info(f"[timer_register] timer '{name}' triggered after {minutes}min for session {session_id}")
            except Exception:
                logger.exception(f"[timer_register] callback '{name}' failed for session {session_id}")
        except asyncio.CancelledError:
            logger.info(f"[timer_register] timer '{name}' cancelled for session {session_id}")
        finally:
            # 无论触发还是取消, 清理注册
            timers = self.session_id_to_timers.get(session_id)
            if timers and name in timers:
                del timers[name]

    def clear_session(self, session_id: str):
        """
        清理会话内所有倒计时
        """
        timers = self.session_id_to_timers.pop(session_id, {})
        for name, timer in timers.items():
            if timer.task_name:
                self._executor.cancel_task(timer.task_name)
        if timers:
            logger.info(f"[timer_register] cleared all timers for session {session_id}")


timer_register = TimerRegister()
