from typing import Any
from loguru import logger
from runtime import Register


class StateRegister(Register):
    def __init__(self):
        self._states = {}

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        if session_id not in self._states:
            self._states[session_id] = {}

            self._states[session_id][key] = value
        return True

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        if session_id not in self._states:
            return default

        return self._states[session_id].get(key, default)

    def get_all_states(self, session_id: str) -> dict[str, Any]:
        return self._states.get(session_id, {})

    def delete_state(self, session_id: str, key: str) -> bool:
        if session_id not in self._states:
            logger.warning(f"session_id {session_id} not found")
            return False

        if key in self._states[session_id]:
            del self._states[session_id][key]
            return True

        logger.warning(f"key {key} not found in session_id {session_id}")
        return False

    def clear_session(self, session_id: str) -> bool:
        if session_id in self._states:
            del self._states[session_id]
            return True

        logger.warning(f"session_id {session_id} not found")
        return False

    def has_session(self, session_id: str) -> bool:
        return session_id in self._states

    def has_key(self, session_id: str, key: str) -> bool:
        if session_id not in self._states:
            return False
        return key in self._states[session_id]

    def update_states(self, session_id: str, states: dict[str, Any]) -> bool:
        if session_id not in self._states:
            self._states[session_id] = {}

        self._states[session_id].update(states)
        return True

state_register = StateRegister()