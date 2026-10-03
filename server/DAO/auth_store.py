"""Storage for the login feature: users, the runtime switch and the JWT blacklist.

Three tables in ``src/data/auth.db`` (schema per the plan's §10):

* ``auth_users`` — one row per account; the password column stores the scrypt
  string, never a plaintext or reversible form.
* ``auth_settings`` — a single row (``CHECK (id = 1)``) carrying the runtime
  switch the account menu writes. Absent row = disabled.
* ``auth_jwt_blacklist`` — revoked ``jti``s (logout, refresh rotation), kept
  only until the token would have expired anyway (``expires_at`` in JWT
  seconds, the same unit as the ``exp`` claim it mirrors).

The connection/schema lifecycle is the shared :class:`BaseSQLiteRepository`
skeleton; this module only owns the DDL and the queries.
"""

from __future__ import annotations

import asyncio
import time

from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository
from config.path import SRC_DIR

__all__ = [
    "add_jwt_to_blacklist",
    "cleanup_expired_blacklist",
    "count_users",
    "create_user",
    "delete_user",
    "ensure_db",
    "get_auth_settings",
    "get_first_user",
    "get_user_by_id",
    "get_user_by_username",
    "is_jwt_blacklisted",
    "set_auth_enabled",
    "update_password",
    "update_username",
]

_DB_DIR = SRC_DIR / "data"
_DB_PATH = _DB_DIR / "auth.db"

# Every connection waits for a contended lock instead of failing instantly.
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

# How long a non-owning event loop waits for the owner's schema init.
_INIT_WAIT_TIMEOUT_S = 30.0

_USERS_TABLE = "auth_users"
_BLACKLIST_TABLE = "auth_jwt_blacklist"
_SETTINGS_TABLE = "auth_settings"

_CREATE_USERS_SQL = f"""
CREATE TABLE IF NOT EXISTS {_USERS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

_CREATE_BLACKLIST_SQL = f"""
CREATE TABLE IF NOT EXISTS {_BLACKLIST_TABLE} (
    jti TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)
"""

_CREATE_SETTINGS_SQL = f"""
CREATE TABLE IF NOT EXISTS {_SETTINGS_TABLE} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
)
"""

_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS idx_jwt_blacklist_expires ON {_BLACKLIST_TABLE}(expires_at)",
)


class _AuthRepository(BaseSQLiteRepository):
    """Base-class wiring for the auth store: DDL hooks + WAL warning label."""

    wal_label = "auth db"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_CREATE_USERS_SQL, _CREATE_BLACKLIST_SQL, _CREATE_SETTINGS_SQL)

    def _index_ddls(self) -> tuple[str, ...]:
        return _INDEX_DDL


# Once-per-process schema-init state the base reads/writes through this module
# namespace (declared here so tests can monkeypatch it, like the other stores).
_init_loop: asyncio.AbstractEventLoop | None = None
_initialized = False
_init_lock = asyncio.Lock()
_sync_tables_ready = False

# The base reads/writes the module-level init state through this namespace.
_repository = _AuthRepository(globals())

_connect = _repository.connect
ensure_db = _repository.ensure_db
_ensure_tables_sync = _repository.ensure_tables_sync


def _now_ms() -> int:
    """Wall-clock milliseconds — the unit the user/settings timestamps use."""
    return int(time.time() * 1000)


def _row_to_user(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "username": str(row[1]),
        "password_hash": str(row[2]),
        "created_at": int(row[3]),
        "updated_at": int(row[4]),
    }


_USER_COLUMNS = "id, username, password_hash, created_at, updated_at"


# ── users ────────────────────────────────────────────────────────────────────


async def create_user(username: str, password_hash: str) -> dict:
    """Insert a user and return the stored row (raises on a duplicate name)."""
    now = _now_ms()
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"INSERT INTO {_USERS_TABLE} (username, password_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (username, password_hash, now, now),
        )
        await db.commit()
        user_id = int(cursor.lastrowid or 0)
    created = await get_user_by_id(user_id)
    assert created is not None
    return created


async def get_user_by_username(username: str) -> dict | None:
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"SELECT {_USER_COLUMNS} FROM {_USERS_TABLE} WHERE username = ?", (username,)
        )
        return _row_to_user(await cursor.fetchone())


async def get_user_by_id(user_id: int) -> dict | None:
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"SELECT {_USER_COLUMNS} FROM {_USERS_TABLE} WHERE id = ?", (int(user_id),)
        )
        return _row_to_user(await cursor.fetchone())


async def update_password(user_id: int, new_password_hash: str) -> bool:
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {_USERS_TABLE} SET password_hash = ?, updated_at = ? WHERE id = ?",
            (new_password_hash, _now_ms(), int(user_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_username(user_id: int, new_username: str) -> bool:
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {_USERS_TABLE} SET username = ?, updated_at = ? WHERE id = ?",
            (new_username, _now_ms(), int(user_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_user(user_id: int) -> bool:
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"DELETE FROM {_USERS_TABLE} WHERE id = ?", (int(user_id),))
        await db.commit()
        return cursor.rowcount > 0


async def get_first_user() -> dict | None:
    """The lowest-id user — the single account this product supports."""
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"SELECT {_USER_COLUMNS} FROM {_USERS_TABLE} ORDER BY id LIMIT 1")
        return _row_to_user(await cursor.fetchone())


async def count_users() -> int:
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"SELECT COUNT(*) FROM {_USERS_TABLE}")
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


# ── runtime switch ───────────────────────────────────────────────────────────


async def get_auth_settings() -> dict:
    """The single settings row; an absent row reads as disabled."""
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"SELECT enabled, updated_at FROM {_SETTINGS_TABLE} WHERE id = 1")
        row = await cursor.fetchone()
    if row is None:
        return {"enabled": False, "updated_at": 0}
    return {"enabled": bool(row[0]), "updated_at": int(row[1])}


async def set_auth_enabled(enabled: bool) -> None:
    """Upsert the single settings row."""
    await ensure_db()
    async with _connect() as db:
        await db.execute(
            f"INSERT INTO {_SETTINGS_TABLE} (id, enabled, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled,"
            " updated_at = excluded.updated_at",
            (1 if enabled else 0, _now_ms()),
        )
        await db.commit()


async def seed_auth_enabled_once(enabled: bool) -> bool:
    """Write the settings row only when none exists yet (boot seed).

    ``SHERRY_AUTH_ENABLED`` is a *seed*, not a runtime override: a deployment
    that wants protection at first boot sets it, and the menu keeps owning the
    switch afterwards. Returns whether the row was written.
    """
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"SELECT COUNT(*) FROM {_SETTINGS_TABLE}")
        row = await cursor.fetchone()
        if row and int(row[0]) > 0:
            return False
        await db.execute(
            f"INSERT INTO {_SETTINGS_TABLE} (id, enabled, updated_at) VALUES (1, ?, ?)",
            (1 if enabled else 0, _now_ms()),
        )
        await db.commit()
        return True


# ── JWT blacklist ────────────────────────────────────────────────────────────


async def add_jwt_to_blacklist(jti: str, expires_at: int) -> None:
    """Revoke ``jti``; ``expires_at`` is the JWT ``exp`` (seconds)."""
    if not jti:
        return
    await ensure_db()
    async with _connect() as db:
        await db.execute(
            f"INSERT INTO {_BLACKLIST_TABLE} (jti, expires_at, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(jti) DO NOTHING",
            (jti, int(expires_at), int(time.time())),
        )
        await db.commit()


async def is_jwt_blacklisted(jti: str) -> bool:
    if not jti:
        return True  # A token without a jti is never acceptable.
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"SELECT 1 FROM {_BLACKLIST_TABLE} WHERE jti = ? LIMIT 1", (jti,))
        return await cursor.fetchone() is not None


async def cleanup_expired_blacklist() -> int:
    """Drop entries whose token has expired anyway; returns the row count."""
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM {_BLACKLIST_TABLE} WHERE expires_at <= ?", (int(time.time()),)
        )
        await db.commit()
        return cursor.rowcount
