"""Single registry of durable key/value entries (prototype ``src/kv.py``)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class KVEntry[T]:
    """One typed KV key with a stable family prefix."""

    family: str
    key: str
    type: type[T]

    @property
    def storage_key(self) -> str:
        """Return the persisted key."""
        return f"{self.family}:{self.key}"

    def dynamic(self, *parts: object) -> str:
        """Return a namespaced key for per-entity values."""
        return ":".join((self.family, self.key, *(str(part) for part in parts)))


class KV:
    """Typed accessors for the single ``kv`` table."""

    SCHEMA = KVEntry("schema", "schema-version", int)
    SETTINGS_REVISION = KVEntry("settings", "settings-revision", int)
    PROMPT_REVISION = KVEntry("prompts", "prompt-revision", int)
    MASTER_ID = KVEntry("main", "master-id", str)

    @staticmethod
    def get[T](conn: sqlite3.Connection, entry: KVEntry[T]) -> T | None:
        """Read a registered entry."""
        return KV.get_raw(conn, entry.storage_key, entry.type)

    @staticmethod
    def set[T](conn: sqlite3.Connection, entry: KVEntry[T], value: T) -> None:
        """Write a registered entry."""
        KV.set_raw(conn, entry.storage_key, value)

    @staticmethod
    def get_raw[T](conn: sqlite3.Connection, storage_key: str, type_: type[T]) -> T | None:
        """Read a dynamic key with an explicit type."""
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (storage_key,)).fetchone()
        if row is None or row[0] is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, type_) else None

    @staticmethod
    def set_raw(conn: sqlite3.Connection, storage_key: str, value: object) -> None:
        """Write a dynamic key."""
        conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (storage_key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
