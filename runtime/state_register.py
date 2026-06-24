import json
import sqlite3
from typing import Any
from pathlib import Path
from loguru import logger
from config import SRC_DIR
from runtime import Register


class StateRegisterMeM(Register):
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

state_register_mem = StateRegisterMeM()

class StateRegisterDB(Register):
    def __init__(self):
        self.db_path: Path = (SRC_DIR / "data" / "state_register.db").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS states (
                    session_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (session_id, key)
                )
            """)
            conn.commit()

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO states (session_id, key, value) VALUES (?, ?, ?)",
                    (session_id, key, json.dumps(value))
                )
                conn.commit()
            return True
        except Exception:
            logger.exception(f"set_state_db failed: session_id={session_id}, key={key}")
        return False

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT value FROM states WHERE session_id = ? AND key = ?",
                    (session_id, key)
                )
                row = cursor.fetchone()

            if row:
                return json.loads(row[0])
            return default
        except Exception:
            logger.exception(f"get_state_db failed: session_id={session_id}, key={key}")
        return default

    def get_all_states(self, session_id: str) -> dict[str, Any]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT key, value FROM states WHERE session_id = ?",
                    (session_id,)
                )
                rows = cursor.fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}
        except Exception:
            logger.exception(f"get_all_states_db failed: session_id={session_id}")
        return {}

    def delete_state(self, session_id: str, key: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM states WHERE session_id = ? AND key = ?",
                    (session_id, key)
                )
                affected = cursor.rowcount
                conn.commit()
            return affected > 0
        except Exception:
            logger.exception(f"delete_state_db failed: session_id={session_id}, key={key}")
        return False

    def clear_session(self, session_id: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM states WHERE session_id = ?", (session_id,))
                conn.commit()
            return True
        except Exception:
            logger.exception(f"clear_session_db failed: session_id={session_id}")
        return False

    def has_session(self, session_id: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM states WHERE session_id = ? LIMIT 1",
                    (session_id,)
                )
                result = cursor.fetchone() is not None
            return result
        except Exception:
            logger.exception(f"has_session_db failed: session_id={session_id}")
        return False

    def has_key(self, session_id: str, key: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM states WHERE session_id = ? AND key = ? LIMIT 1",
                    (session_id, key)
                )
                result = cursor.fetchone() is not None
            return result
        except Exception:
            logger.exception(f"has_key_db failed: session_id={session_id}, key={key}")
        return False

    def update_states(self, session_id: str, states: dict[str, Any]) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                for key, value in states.items():
                    cursor.execute(
                        "INSERT OR REPLACE INTO states (session_id, key, value) VALUES (?, ?, ?)",
                        (session_id, key, json.dumps(value))
                    )
                conn.commit()
            return True
        except Exception:
            logger.exception(f"update_states_db failed: session_id={session_id}")
        return False


state_register_db = StateRegisterDB()