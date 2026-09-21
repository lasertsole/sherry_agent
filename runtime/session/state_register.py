import json
import sqlite3
import threading
from typing import Any, Protocol, runtime_checkable
from pathlib import Path
from loguru import logger
from config import SRC_DIR
from .core import SessionRegister


def _log_state_db_failure(operation: str, exc: BaseException, **context: object) -> None:
    """Classify a swallowed StateRegisterDB failure for observability (audit #68).

    The fail-safe contract is unchanged — callers still receive the method's
    default value. What changes is that a real fault is no longer confusable
    with "no data": database errors, corrupt JSON values and unexpected faults
    each get a distinct, greppable message at ERROR level.
    """
    details = ", ".join(f"{key}={value}" for key, value in context.items())
    if isinstance(exc, sqlite3.Error):
        logger.error("state_register_db {}: database error ({}): {}", operation, details, exc)
    elif isinstance(exc, json.JSONDecodeError):
        logger.error("state_register_db {}: corrupt JSON value ({}): {}", operation, details, exc)
    else:
        logger.opt(exception=exc).error(
            "state_register_db {}: unexpected error ({})", operation, details
        )


@runtime_checkable
class StateRegisterProtocol(Protocol):
    """Full contract shared by the in-memory and DB-backed state registers."""

    def set_state(self, session_id: str, key: str, value: Any) -> bool: ...
    def get_state(self, session_id: str, key: str, default: Any = None) -> Any: ...
    def get_all_states(self, session_id: str) -> dict[str, Any]: ...
    def delete_state(self, session_id: str, key: str) -> bool: ...
    def clear_session(self, session_id: str) -> bool: ...
    def has_session(self, session_id: str) -> bool: ...
    def has_key(self, session_id: str, key: str) -> bool: ...
    def update_states(self, session_id: str, states: dict[str, Any]) -> bool: ...


class StateRegisterMeM(SessionRegister):
    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.Lock()
        self._states = {}
        self._initialized = True

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        try:
            with self._lock:
                self._states.setdefault(session_id, {})[key] = value
            return True
        except Exception:
            logger.exception(f"set_state failed: session_id={session_id}, key={key}")
        return False

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        try:
            with self._lock:
                if session_id not in self._states:
                    return default

                return self._states[session_id].get(key, default)
        except Exception:
            logger.exception(f"get_state failed: session_id={session_id}, key={key}")
        return default

    def get_all_states(self, session_id: str) -> dict[str, Any]:
        try:
            with self._lock:
                # Snapshot: caller mutations must not reach shared state (audit #13).
                return dict(self._states.get(session_id, {}))
        except Exception:
            logger.exception(f"get_all_states failed: session_id={session_id}")
        return {}

    def delete_state(self, session_id: str, key: str) -> bool:
        try:
            with self._lock:
                if session_id not in self._states:
                    return False

                if key in self._states[session_id]:
                    del self._states[session_id][key]
                    return True
        except Exception:
            logger.exception(f"delete_state failed: session_id={session_id}, key={key}")
        return False

    def clear_session(self, session_id: str) -> bool:
        try:
            with self._lock:
                if session_id in self._states:
                    self._states.pop(session_id, None)
                    return True
        except Exception:
            logger.exception(f"clear_session failed: session_id={session_id}")
        return False

    def has_session(self, session_id: str) -> bool:
        try:
            with self._lock:
                return session_id in self._states
        except Exception:
            logger.exception(f"has_session failed: session_id={session_id}")
        return False

    def has_key(self, session_id: str, key: str) -> bool:
        try:
            with self._lock:
                if session_id not in self._states:
                    return False
                return key in self._states[session_id]
        except Exception:
            logger.exception(f"has_key failed: session_id={session_id}, key={key}")
        return False

    def update_states(self, session_id: str, states: dict[str, Any]) -> bool:
        try:
            with self._lock:
                self._states.setdefault(session_id, {}).update(states)
            return True
        except Exception:
            logger.exception(f"update_states failed: session_id={session_id}")
        return False


state_register_mem = StateRegisterMeM()


class StateRegisterDB(SessionRegister):
    """SQLite-backed state register.

    Connection strategy: one shared connection per process, opened lazily on
    first use and guarded by an ``RLock``. SQLite serializes writes regardless,
    the register only performs tiny key/value statements, and this mirrors the
    proven ``context_engine/store/db.py`` strategy — so a lock-guarded single
    connection avoids the per-operation connect churn (WAL/handle) without the
    multiplied handles of a per-thread pool, and keeps behaviour identical.
    """

    def __init__(self):
        db_path = (SRC_DIR / "data" / "state_register.db").resolve()
        if getattr(self, "db_path", None) != db_path:
            old_conn = getattr(self, "_conn", None)
            if old_conn is not None:
                try:
                    old_conn.close()
                except sqlite3.Error:  # noqa: S110
                    pass
            self._conn: sqlite3.Connection | None = None
            self.db_path: Path = db_path
        self._conn_lock = getattr(self, "_conn_lock", threading.RLock())

    def _init_db(self) -> sqlite3.Connection:
        """Create the schema on first use and return the shared connection."""
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS states (
                    session_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (session_id, key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS context_epoch (
                    session_id TEXT PRIMARY KEY,
                    baseline TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    baseline_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn
        return conn

    def ensure_initialized(self) -> sqlite3.Connection:
        """Materialize the connection/schema on first use (idempotent)."""
        with self._conn_lock:
            return self._init_db()

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        try:
            with self._conn_lock:
                conn = self._init_db()
                conn.execute(
                    "INSERT OR REPLACE INTO states (session_id, key, value) VALUES (?, ?, ?)",
                    (session_id, key, json.dumps(value)),
                )
                conn.commit()
            return True
        except Exception as exc:
            _log_state_db_failure("set_state", exc, session_id=session_id, key=key)
        return False

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        try:
            with self._conn_lock:
                row = (
                    self._init_db()
                    .execute(
                        "SELECT value FROM states WHERE session_id = ? AND key = ?",
                        (session_id, key),
                    )
                    .fetchone()
                )
            if row:
                return json.loads(row[0])
            return default
        except Exception as exc:
            _log_state_db_failure("get_state", exc, session_id=session_id, key=key)
        return default

    def get_all_states(self, session_id: str) -> dict[str, Any]:
        try:
            with self._conn_lock:
                rows = (
                    self._init_db()
                    .execute("SELECT key, value FROM states WHERE session_id = ?", (session_id,))
                    .fetchall()
                )
            return {row[0]: json.loads(row[1]) for row in rows}
        except Exception as exc:
            _log_state_db_failure("get_all_states", exc, session_id=session_id)
        return {}

    def delete_state(self, session_id: str, key: str) -> bool:
        try:
            with self._conn_lock:
                conn = self._init_db()
                affected = conn.execute(
                    "DELETE FROM states WHERE session_id = ? AND key = ?", (session_id, key)
                ).rowcount
                conn.commit()
            return affected > 0
        except Exception as exc:
            _log_state_db_failure("delete_state", exc, session_id=session_id, key=key)
        return False

    def get_all_session_ids(self) -> list[str]:
        """Return all distinct session_id values from the database."""
        try:
            with self._conn_lock:
                rows = self._init_db().execute("SELECT DISTINCT session_id FROM states").fetchall()
            return [row[0] for row in rows]
        except Exception as exc:
            _log_state_db_failure("get_all_session_ids", exc)
        return []

    # SessionRegister can't clear StateRegisterDB
    def clear_session(self, session_id: str) -> bool:
        return False

    def has_session(self, session_id: str) -> bool:
        try:
            with self._conn_lock:
                row = (
                    self._init_db()
                    .execute("SELECT 1 FROM states WHERE session_id = ? LIMIT 1", (session_id,))
                    .fetchone()
                )
            return row is not None
        except Exception as exc:
            _log_state_db_failure("has_session", exc, session_id=session_id)
        return False

    def has_key(self, session_id: str, key: str) -> bool:
        try:
            with self._conn_lock:
                row = (
                    self._init_db()
                    .execute(
                        "SELECT 1 FROM states WHERE session_id = ? AND key = ? LIMIT 1",
                        (session_id, key),
                    )
                    .fetchone()
                )
            return row is not None
        except Exception as exc:
            _log_state_db_failure("has_key", exc, session_id=session_id, key=key)
        return False

    def update_states(self, session_id: str, states: dict[str, Any]) -> bool:
        try:
            with self._conn_lock:
                conn = self._init_db()
                for key, value in states.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO states (session_id, key, value) VALUES (?, ?, ?)",
                        (session_id, key, json.dumps(value)),
                    )
                conn.commit()
            return True
        except Exception as exc:
            _log_state_db_failure("update_states", exc, session_id=session_id)
        return False


state_register_db = StateRegisterDB()

# Both implementations satisfy the shared register contract (static check).
_mem_conforms_to_protocol: StateRegisterProtocol = state_register_mem
_db_conforms_to_protocol: StateRegisterProtocol = state_register_db


class ContextEpoch:
    """System-context snapshot lifecycle (from opencode-dev).

    initialize → first baseline; prepare → reconcile/replace decision;
    replace → compaction rebuild; advance → snapshot-only update.
    """

    def __init__(self, db: StateRegisterDB):
        self._db = db

    def initialize(self, session_id: str, system_context: dict) -> None:
        import json
        from datetime import datetime

        baseline = json.dumps(system_context, ensure_ascii=False)
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        with sqlite3.connect(self._db.db_path) as conn:
            conn.execute(
                "INSERT INTO context_epoch (session_id, baseline, snapshot, baseline_seq, "
                "created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET baseline = excluded.baseline, "
                "snapshot = excluded.snapshot, updated_at = excluded.updated_at",
                (session_id, baseline, baseline, now, now),
            )
            conn.commit()

    def prepare(
        self,
        session_id: str,
        current_context: dict,
        latest_compaction_seq: int | None,
    ) -> tuple[dict, str]:
        """Load the epoch and decide: ok | reconcile | replace."""
        import json

        row = (
            sqlite3.connect(self._db.db_path)
            .execute(
                "SELECT baseline, snapshot, baseline_seq FROM context_epoch WHERE session_id = ?",
                (session_id,),
            )
            .fetchone()
        )
        if row is None:
            self.initialize(session_id, current_context)
            return current_context, "ok"

        baseline_raw, snapshot_raw, baseline_seq = row
        baseline = json.loads(baseline_raw)
        snapshot = json.loads(snapshot_raw)

        if latest_compaction_seq is not None and latest_compaction_seq > baseline_seq:
            self.replace(session_id, current_context, latest_compaction_seq)
            return current_context, "replace"

        current_json = json.dumps(current_context, ensure_ascii=False, sort_keys=True)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        if current_json != snapshot_json:
            self.advance(session_id, current_context)
            return current_context, "reconcile"

        _ = baseline
        return snapshot, "ok"

    def replace(self, session_id: str, new_context: dict, seq: int) -> None:
        import json
        from datetime import datetime

        ctx_json = json.dumps(new_context, ensure_ascii=False)
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        with sqlite3.connect(self._db.db_path) as conn:
            conn.execute(
                "UPDATE context_epoch SET baseline = ?, snapshot = ?, baseline_seq = ?, "
                "updated_at = ? WHERE session_id = ?",
                (ctx_json, ctx_json, seq, now, session_id),
            )
            conn.commit()

    def advance(self, session_id: str, new_snapshot: dict) -> None:
        import json
        from datetime import datetime

        snapshot_json = json.dumps(new_snapshot, ensure_ascii=False)
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        with sqlite3.connect(self._db.db_path) as conn:
            conn.execute(
                "UPDATE context_epoch SET snapshot = ?, updated_at = ? WHERE session_id = ?",
                (snapshot_json, now, session_id),
            )
            conn.commit()
