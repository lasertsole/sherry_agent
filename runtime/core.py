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
    def session_end(self, session_id: str):
        pass