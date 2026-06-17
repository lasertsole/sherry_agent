from typing import Any
from loguru import logger
from runtime import Register


class StateRegister(Register):
    def __init__(self):
        self._states = {}

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        try:
            if session_id not in self._states:
                self._states[session_id] = {}

            self._states[session_id][key] = value
            return True
        except Exception:
            logger.exception(f"set_state failed: session_id={session_id}, key={key}")
        return False

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        try:
            if session_id not in self._states:
                return default

            return self._states[session_id].get(key, default)
        except Exception:
            logger.exception(f"get_state failed: session_id={session_id}, key={key}")
        return default

    def get_all_states(self, session_id: str) -> dict[str, Any]:
        try:
            return self._states.get(session_id, {})
        except Exception:
            logger.exception(f"get_all_states failed: session_id={session_id}")
        return {}

    def delete_state(self, session_id: str, key: str) -> bool:
        try:
            if session_id not in self._states:
                logger.warning(f"session_id {session_id} not found")
                return False

            if key in self._states[session_id]:
                del self._states[session_id][key]
                return True

            logger.warning(f"key {key} not found in session_id {session_id}")
        except Exception:
            logger.exception(f"delete_state failed: session_id={session_id}, key={key}")
        return False

    def clear_session(self, session_id: str) -> bool:
        try:
            if session_id in self._states:
                del self._states[session_id]
                return True

            logger.warning(f"session_id {session_id} not found")
        except Exception:
            logger.exception(f"clear_session failed: session_id={session_id}")
        return False

    def has_session(self, session_id: str) -> bool:
        try:
            return session_id in self._states
        except Exception:
            logger.exception(f"has_session failed: session_id={session_id}")
        return False

    def has_key(self, session_id: str, key: str) -> bool:
        try:
            if session_id not in self._states:
                return False
            return key in self._states[session_id]
        except Exception:
            logger.exception(f"has_key failed: session_id={session_id}, key={key}")
        return False

    def update_states(self, session_id: str, states: dict[str, Any]) -> bool:
        try:
            if session_id not in self._states:
                self._states[session_id] = {}

            self._states[session_id].update(states)
            return True
        except Exception:
            logger.exception(f"update_states failed: session_id={session_id}")
        return False

state_register = StateRegister()