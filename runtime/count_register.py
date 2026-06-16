from loguru import logger
from .core import Register
from typing import Callable, Any
from pydantic import BaseModel, Field


class Trigger(BaseModel):
    threshold: int = 1
    callback: Callable
    args: list[Any] = Field(default_factory=list)
    reset_when_trigger: bool = True

class CountRegister(Register):
    """
    统计注册类
    """
    def __init__(self):
        if self._initialized:
            return

        self.session_id_to_counter: dict[str, dict[str, int]] = {}
        self.session_id_to_trigger: dict[str, dict[str, Trigger]] = {}

        self._initialized = True

    def register(self, session_id: str, name: str, callback: Callable, threshold: int = 1)-> bool:
        """
        注册统计函数
        """
        if name in self.session_id_to_counter:
            logger.error(f"{name} is already registered")
            return False

        self.session_id_to_counter.setdefault(session_id, {})[name] = 0
        self.session_id_to_trigger.setdefault(session_id, {})[name] = Trigger(threshold = threshold, callback = callback)

        return True

    def unregister(self, session_id: str, name: str)-> bool:
        """
        取消注册
        """
        if name not in self.session_id_to_counter:
            logger.error(f"{name} is not registered")
            return False

        del self.session_id_to_counter.setdefault(session_id, {})[name]
        del self.session_id_to_trigger.setdefault(session_id, {})[name]

        return True


    def increase(self, session_id: str, name: str)-> bool:
        """
        增加统计值
        """
        if name not in self.session_id_to_counter:
            logger.error(f"{name} is not registered")
            return False

        now_counter:int = self.session_id_to_counter.setdefault(session_id, {})[name] + 1

        trigger: Trigger = self.session_id_to_trigger.setdefault(session_id, {})[name]
        threshold: int = trigger.threshold

        if now_counter >= threshold:
            callback: Callable = trigger.callback
            args: list[Any] = trigger.args

            callback(*args)

            if trigger.reset_when_trigger:
                now_counter = 0

        self.session_id_to_counter.setdefault(session_id, {})[name] = now_counter

        return True
    
    def clear_session(self, session_id: str):
        del self.session_id_to_counter[session_id]
        del self.session_id_to_trigger[session_id]

count_register = CountRegister()