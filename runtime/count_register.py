from logging import getLogger
from typing import Callable, Any
from pydantic import BaseModel, Field

logger = getLogger(__name__)

class Trigger(BaseModel):
    threshold: int = 1
    callback: Callable
    args: list[Any] = Field(default_factory=list)
    reset_when_trigger: bool = True

class CountRegister:
    """
    统计注册类
    """
    _counter: dict[str, int] = {}
    _trigger: dict[str, Trigger] = {}

    def register(self, name: str, callback: Callable, threshold: int = 1)-> bool:
        """
        注册统计函数
        """
        if name in self._counter:
            logger.error(f"{name} is already registered")
            return False

        self._counter[name] = 0
        self._trigger[name] = Trigger(threshold = threshold, callback = callback)

        return True

    def unregister(self, name: str)-> bool:
        """
        取消注册
        """
        if name not in self._counter:
            logger.error(f"{name} is not registered")
            return False

        del self._counter[name]
        del self._trigger[name]

        return True


    def increase(self, name: str)-> bool:
        """
        增加统计值
        """
        if name not in self._counter:
            logger.error(f"{name} is not registered")
            return False

        now_counter:int = self._counter[name] + 1

        trigger: Trigger = self._trigger[name]
        threshold: int = trigger.threshold

        if now_counter >= threshold:
            callback: Callable = trigger.callback
            args: list[Any] = trigger.args

            callback(*args)

            if trigger.reset_when_trigger:
                now_counter = 0

        self._counter[name] = now_counter

        return True