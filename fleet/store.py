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

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        tenant_id TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL
    )
    """,
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


def _public_user(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "role": str(row["role"]),
        "tenant_id": str(row["tenant_id"]),
    }


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
        """Create the current schema once."""
        if self._ready:
            return
        db = await self._connect()
        try:
            for ddl in _SCHEMA:
                await db.execute(ddl)
            await db.commit()
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
        """Create one account; violates a unique username/tenant or policy."""
        name = username.strip()
        tenant = tenant_id.strip()
        if not name or not tenant:
            raise ValueError("username and tenant_id are required")
        problem = password_policy_error(password)
        if problem:
            raise ValueError(problem)
        db = await self._connect()
        try:
            cursor = await db.execute(
                "INSERT INTO users(username, password_hash, role, tenant_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, hash_password(password), role, tenant, int(time.time())),
            )
            await db.commit()
            row = await (
                await db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,))
            ).fetchone()
        except aiosqlite.IntegrityError as exc:
            raise ValueError("username or tenant_id already exists") from exc
        finally:
            await db.close()
        if row is None:
            raise RuntimeError("user insert returned no row")
        return _public_user(row)

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
                "started_at = exited.started_at, updated_at = excluded.updated_at".replace(
                    "exited", "excluded"
                ),
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
