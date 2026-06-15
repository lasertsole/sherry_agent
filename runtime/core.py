from typing import final
from abc import ABC, abstractmethod

class Register(ABC):
    _instances = {}

    def __new__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__new__(cls)
            instance._initialized = False
            cls._instances[cls] = instance
        return cls._instances[cls]

    @abstractmethod
    def clear_session(self, session_id: str):
        pass

    @classmethod
    @final
    def clear_all_register_sessions(cls, session_id: str) -> None:
        for subclass in cls.__subclasses__():
            if subclass._instance is not None:
                instance = subclass()
                instance.clear_session(session_id)

def clear_all_register_sessions(session_id: str)->None:
    Register.clear_all_register_sessions(session_id)