"""Central account and cell registry for the Ingress.

The Ingress is the only process that knows usernames and tenants. Each cell has
its own ``SHERRY_HOME`` and is reached over loopback, so the cell's single-user
auth is not in the browser request path.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

from server.utils.password import hash_password, password_policy_error, verify_password

_USER_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    tenant_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
)"""

_USERS_DDL = f"CREATE TABLE IF NOT EXISTS users {_USER_COLUMNS}"
# Used only to rebuild a legacy ``users`` table whose ``tenant_id`` was UNIQUE.
_USERS_REBUILD_DDL = f"CREATE TABLE users_rebuild {_USER_COLUMNS}"

_USER_TENANTS_DDL = """
CREATE TABLE IF NOT EXISTS user_tenants (
    user_id INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, tenant_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
)
"""

_USER_TENANTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_user_tenants_tenant ON user_tenants(tenant_id)"
)

#: Every legacy account becomes an owner of its original tenant. ``OR IGNORE``
#: keeps memberships added after the migration (and their default flag).
_BACKFILL_USER_TENANTS = """
INSERT OR IGNORE INTO user_tenants(user_id, tenant_id, role, is_default, created_at)
SELECT id, tenant_id, 'owner', 1, created_at FROM users
"""

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS refresh_tokens (
        jti TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cells (
        tenant_id TEXT PRIMARY KEY,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        gateway_token TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cell_processes (
        tenant_id TEXT PRIMARY KEY,
        pid INTEGER,
        status TEXT NOT NULL,
        data_dir TEXT NOT NULL,
        started_at INTEGER,
        updated_at INTEGER NOT NULL
    )
    """,
)


def _public_user(row: Any, tenant_id: str | None = None) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "role": str(row["role"]),
        "tenant_id": str(tenant_id if tenant_id is not None else row["tenant_id"]),
    }


async def _tenant_id_has_unique_index(db: aiosqlite.Connection) -> bool:
    """True for the pre-multi-tenant schema (``users.tenant_id UNIQUE``)."""
    indexes = await db.execute_fetchall("PRAGMA index_list('users')")
    for index in indexes:
        if not int(index["unique"]):
            continue
        name = str(index["name"]).replace('"', '""')
        columns = await db.execute_fetchall(f'PRAGMA index_info("{name}")')
        if [str(column["name"]) for column in columns] == ["tenant_id"]:
            return True
    return False


async def _rebuild_users_without_tenant_unique(db: aiosqlite.Connection) -> None:
    """Table-swap a legacy users table (SQLite cannot drop a UNIQUE constraint).

    ``users.tenant_id`` stays as the legacy/default column; the constraint that
    made one tenant exclusive to one account is what disappears.
    """
    await db.execute("DROP TABLE IF EXISTS users_rebuild")
    await db.execute(_USERS_REBUILD_DDL)
    await db.execute(
        "INSERT INTO users_rebuild(id, username, password_hash, role, tenant_id, created_at) "
        "SELECT id, username, password_hash, role, tenant_id, created_at FROM users"
    )
    await db.execute("DROP TABLE users")
    await db.execute("ALTER TABLE users_rebuild RENAME TO users")


class FleetStore:
    """Small SQLite store; one short connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._ready = False

    async def _connect(self) -> aiosqlite.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path, timeout=10.0)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 10000")
        await db.execute("PRAGMA journal_mode = WAL")
        return db

    async def init(self) -> None:
        """Create the current schema once, migrating the legacy users table."""
        if self._ready:
            return
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(_USERS_DDL)
            migrated = await _tenant_id_has_unique_index(db)
            if migrated:
                await _rebuild_users_without_tenant_unique(db)
            await db.execute(_USER_TENANTS_DDL)
            for ddl in _TABLES:
                await db.execute(ddl)
            await db.execute(_USER_TENANTS_INDEX)
            if migrated:
                # Only the one-time legacy migration seeds memberships; running
                # it on every boot would resurrect a membership an admin removed.
                await db.execute(_BACKFILL_USER_TENANTS)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        self._ready = True

    def backup_database(self, destination: Path) -> dict[str, Any]:
        """Online backup of the central users/cells DB (admin CLI)."""
        target = Path(destination)
        if target.suffix == "" or target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target = target / "fleet.db"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.path, timeout=10) as source, sqlite3.connect(target) as sink:
                source.backup(sink)
        except sqlite3.Error as exc:
            raise RuntimeError(f"fleet backup failed: {exc}") from exc
        return {"backup": str(target), "bytes": target.stat().st_size}

    async def create_user(
        self,
        username: str,
        password: str,
        *,
        tenant_id: str,
        role: str = "user",
    ) -> dict[str, Any]:
        """Create one account and its first tenant membership.

        ``tenant_id`` is no longer exclusive: several accounts may share the
        same cell (the openclaw trust-domain model). Only the username is
        unique.
        """
        name = username.strip()
        tenant = tenant_id.strip()
        if not name or not tenant:
            raise ValueError("username and tenant_id are required")
        problem = password_policy_error(password)
        if problem:
            raise ValueError(problem)
        now = int(time.time())
        db = await self._connect()
        row = None
        try:
            cursor = await db.execute(
                "INSERT INTO users(username, password_hash, role, tenant_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, hash_password(password), role, tenant, now),
            )
            user_id = int(cursor.lastrowid or 0)
            if not user_id:
                raise RuntimeError("user insert returned no id")
            await db.execute(
                "INSERT INTO user_tenants(user_id, tenant_id, role, is_default, created_at) "
                "VALUES (?, ?, 'owner', 1, ?)",
                (user_id, tenant, now),
            )
            await db.commit()
            row = await (
                await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            ).fetchone()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ValueError("username already exists") from exc
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        if row is None:
            raise RuntimeError("user insert returned no row")
        return _public_user(row)

    async def list_user_tenants(self, user_id: int) -> list[dict[str, Any]]:
        """Every tenant this account belongs to, default first."""
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT tenant_id, role, is_default FROM user_tenants "
                    "WHERE user_id = ? ORDER BY is_default DESC, created_at ASC, tenant_id ASC",
                    (user_id,),
                )
            ).fetchall()
        finally:
            await db.close()
        return [
            {
                "tenant_id": str(row["tenant_id"]),
                "role": str(row["role"]),
                "is_default": bool(row["is_default"]),
            }
            for row in rows
        ]

    async def get_default_tenant(self, user_id: int) -> str | None:
        """The account's default tenant, or None when it has no membership."""
        rows = await self.list_user_tenants(user_id)
        return str(rows[0]["tenant_id"]) if rows else None

    async def user_has_tenant(self, user_id: int, tenant_id: str) -> bool:
        """True when the account is still authorized for that tenant."""
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT 1 FROM user_tenants WHERE user_id = ? AND tenant_id = ?",
                    (user_id, tenant_id),
                )
            ).fetchone()
        finally:
            await db.close()
        return row is not None

    async def add_user_tenant(
        self,
        user_id: int,
        tenant_id: str,
        *,
        role: str = "member",
        is_default: bool = False,
    ) -> None:
        """Authorize one account for another cell (idempotent upsert)."""
        tenant = tenant_id.strip()
        if not tenant:
            raise ValueError("tenant_id is required")
        now = int(time.time())
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            user = await (
                await db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,))
            ).fetchone()
            if user is None:
                raise ValueError("unknown user")
            has_default = await (
                await db.execute(
                    "SELECT 1 FROM user_tenants WHERE user_id = ? AND is_default = 1",
                    (user_id,),
                )
            ).fetchone()
            make_default = bool(is_default) or has_default is None
            if make_default:
                await db.execute(
                    "UPDATE user_tenants SET is_default = 0 WHERE user_id = ?", (user_id,)
                )
                await db.execute(
                    "INSERT INTO user_tenants(user_id, tenant_id, role, is_default, created_at) "
                    "VALUES (?, ?, ?, 1, ?) ON CONFLICT(user_id, tenant_id) DO UPDATE SET "
                    "role = excluded.role, is_default = 1",
                    (user_id, tenant, role, now),
                )
                await db.execute("UPDATE users SET tenant_id = ? WHERE id = ?", (tenant, user_id))
            else:
                await db.execute(
                    "INSERT INTO user_tenants(user_id, tenant_id, role, is_default, created_at) "
                    "VALUES (?, ?, ?, 0, ?) ON CONFLICT(user_id, tenant_id) DO UPDATE SET "
                    "role = excluded.role",
                    (user_id, tenant, role, now),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def remove_user_tenant(self, user_id: int, tenant_id: str) -> bool:
        """Revoke one membership, promoting the next oldest when it was default."""
        tenant = tenant_id.strip()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT is_default FROM user_tenants WHERE user_id = ? AND tenant_id = ?",
                    (user_id, tenant),
                )
            ).fetchone()
            if row is None:
                await db.commit()
                return False
            await db.execute(
                "DELETE FROM user_tenants WHERE user_id = ? AND tenant_id = ?",
                (user_id, tenant),
            )
            if bool(row["is_default"]):
                successor = await (
                    await db.execute(
                        "SELECT tenant_id FROM user_tenants WHERE user_id = ? "
                        "ORDER BY created_at ASC, tenant_id ASC LIMIT 1",
                        (user_id,),
                    )
                ).fetchone()
                if successor is not None:
                    next_tenant = str(successor["tenant_id"])
                    await db.execute(
                        "UPDATE user_tenants SET is_default = 1 "
                        "WHERE user_id = ? AND tenant_id = ?",
                        (user_id, next_tenant),
                    )
                    await db.execute(
                        "UPDATE users SET tenant_id = ? WHERE id = ?", (next_tenant, user_id)
                    )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Look up an account by username (case-insensitive)."""
        db = await self._connect()
        try:
            row = await (
                await db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),))
            ).fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Look up an account by numeric id."""
        db = await self._connect()
        try:
            row = await (
                await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            ).fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None

    def verify_password(self, password: str, stored_hash: str) -> bool:
        """Verify a password against a stored scrypt hash."""
        return verify_password(password, stored_hash)

    async def save_refresh(self, jti: str, user_id: int, expires_at: int) -> None:
        """Persist one refresh-token id (rotation/revocation state)."""
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO refresh_tokens(jti, user_id, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (jti, user_id, expires_at, int(time.time())),
            )
            await db.commit()
        finally:
            await db.close()

    async def refresh_active(self, jti: str, user_id: int) -> bool:
        """True when this refresh id is unrevoked and unexpired."""
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT 1 FROM refresh_tokens WHERE jti = ? AND user_id = ? "
                    "AND revoked = 0 AND expires_at > ?",
                    (jti, user_id, int(time.time())),
                )
            ).fetchone()
        finally:
            await db.close()
        return row is not None

    async def revoke_refresh(self, jti: str) -> None:
        """Revoke one refresh-token id."""
        db = await self._connect()
        try:
            await db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE jti = ?", (jti,))
            await db.commit()
        finally:
            await db.close()

    async def revoke_user_refresh_tokens(self, user_id: int) -> None:
        """Revoke every refresh token of one account (password change)."""
        db = await self._connect()
        try:
            await db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
            await db.commit()
        finally:
            await db.close()

    async def set_password_hash(self, user_id: int, password_hash: str) -> None:
        """Replace one account's password hash."""
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def upsert_cell(
        self,
        tenant_id: str,
        *,
        host: str,
        port: int,
        gateway_token: str,
    ) -> None:
        """Register or update a tenant cell endpoint."""
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO cells(tenant_id, host, port, gateway_token, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET host = excluded.host, "
                "port = excluded.port, gateway_token = excluded.gateway_token, "
                "updated_at = excluded.updated_at",
                (tenant_id.strip(), host, int(port), gateway_token, int(time.time())),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_cell(self, tenant_id: str) -> dict[str, Any] | None:
        """Resolve one tenant's cell (including its gateway token)."""
        db = await self._connect()
        try:
            row = await (
                await db.execute("SELECT * FROM cells WHERE tenant_id = ?", (tenant_id,))
            ).fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None

    async def list_cells(self) -> list[dict[str, Any]]:
        """List tenant endpoints without their gateway tokens."""
        db = await self._connect()
        try:
            rows = await (await db.execute("SELECT tenant_id, host, port FROM cells")).fetchall()
        finally:
            await db.close()
        return [dict(row) for row in rows]

    async def delete_cell(self, tenant_id: str) -> None:
        """Remove one tenant from the registry."""
        db = await self._connect()
        try:
            await db.execute("DELETE FROM cells WHERE tenant_id = ?", (tenant_id,))
            await db.commit()
        finally:
            await db.close()

    async def set_process(
        self,
        tenant_id: str,
        *,
        pid: int | None,
        status: str,
        data_dir: str,
        started_at: int | None = None,
    ) -> None:
        """Record the cell supervisor state."""
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO cell_processes(tenant_id, pid, status, data_dir, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET "
                "pid = excluded.pid, status = excluded.status, data_dir = excluded.data_dir, "
                "started_at = excluded.started_at, updated_at = excluded.updated_at",
                (tenant_id, pid, status, data_dir, started_at, int(time.time())),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_process(self, tenant_id: str) -> dict[str, Any] | None:
        """Read one cell's supervisor state."""
        db = await self._connect()
        try:
            row = await (
                await db.execute("SELECT * FROM cell_processes WHERE tenant_id = ?", (tenant_id,))
            ).fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None

    async def delete_process(self, tenant_id: str) -> None:
        """Drop one cell's supervisor state."""
        db = await self._connect()
        try:
            await db.execute("DELETE FROM cell_processes WHERE tenant_id = ?", (tenant_id,))
            await db.commit()
        finally:
            await db.close()
