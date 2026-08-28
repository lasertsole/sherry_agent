from venv import logger
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
            if subclass in cls._instances and cls._instances[subclass] is not None:
                instance = subclass()
                instance.clear_session(session_id)

def clear_all_register_sessions(session_id: str, clear_persistent_states: bool = False) -> None:
    Register.clear_all_register_sessions(session_id) 

    if clear_persistent_states:
        from runtime import state_register_db
        try:
            states = state_register_db.get_all_states(session_id)
            for key in states:
                _ = state_register_db.delete_state(session_id, key)
            logger.debug(f"Cleared {len(states)} state_register_db variable(s) for session_id={session_id}")
        except Exception:
            logger.exception(f"Failed to clear state_register_db for session_id={session_id}")