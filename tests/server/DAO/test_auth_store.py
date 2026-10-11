"""The auth store: the single account, the runtime switch and the JWT blacklist.

Contract under test: ONE account row exists at most (password-only login — the
schema pins ``id = 1``, so a second INSERT cannot create a second account), a
password change updates that row in place, the settings row is a singleton that
reads as "disabled" when absent (fresh install), the boot seed writes it only
once (the menu owns it afterwards), blacklist entries expire out of the table at
their own ``exp``, and a username-era database is migrated at init.
"""

from __future__ import annotations

import time

import pytest

from server.DAO import auth_store

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_set_and_read_the_account(isolated_auth_store) -> None:
    assert await auth_store.get_account() is None
    assert await auth_store.account_exists() is False

    created = await auth_store.set_account_password("scrypt$stored")

    assert created["id"] == 1
    assert created["password_hash"] == "scrypt$stored"
    assert created["created_at"] > 0
    assert await auth_store.account_exists() is True
    assert (await auth_store.get_account())["password_hash"] == "scrypt$stored"


@pytest.mark.asyncio
async def test_a_second_write_updates_the_row_in_place(isolated_auth_store) -> None:
    """The schema pins id = 1: the account can never multiply."""
    first = await auth_store.set_account_password("h1")
    second = await auth_store.set_account_password("h2")

    assert second["id"] == first["id"]
    assert second["password_hash"] == "h2"
    # A password change keeps the "created" stamp — only "updated" moves.
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


@pytest.mark.asyncio
async def test_delete_account(isolated_auth_store) -> None:
    await auth_store.set_account_password("h")

    assert await auth_store.account_exists() is True
    assert await auth_store.delete_account() is True
    assert await auth_store.account_exists() is False
    assert await auth_store.get_account() is None
    assert await auth_store.delete_account() is False


@pytest.mark.asyncio
async def test_a_username_era_database_is_migrated(isolated_auth_store, tmp_path) -> None:
    """A legacy ``auth_users`` row (lowest id wins) becomes the account, and the
    legacy table is dropped — the password keeps working across the upgrade."""
    import sqlite3

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.execute(
        "CREATE TABLE auth_users (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,"
        " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    con.execute(
        "INSERT INTO auth_users (username, password_hash, created_at, updated_at)"
        " VALUES ('admin', 'scrypt$old', 111, 222)"
    )
    con.commit()
    con.close()

    # Point the store at the legacy file and let its init run the migration.
    import asyncio

    from server.DAO import auth_store as store

    isolated_auth_store.write_bytes(legacy.read_bytes())
    store._initialized = False
    store._init_loop = None
    store._init_lock = asyncio.Lock()
    store._sync_tables_ready = False

    account = await store.get_account()
    assert account is not None and account["password_hash"] == "scrypt$old"

    con = sqlite3.connect(isolated_auth_store)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "auth_users" not in tables
    assert "auth_account" in tables


@pytest.mark.asyncio
async def test_settings_default_to_disabled_and_flip(isolated_auth_store) -> None:
    assert await auth_store.get_auth_settings() == {"enabled": False, "updated_at": 0}

    await auth_store.set_auth_enabled(True)
    assert (await auth_store.get_auth_settings())["enabled"] is True

    await auth_store.set_auth_enabled(False)
    assert (await auth_store.get_auth_settings())["enabled"] is False


@pytest.mark.asyncio
async def test_the_boot_seed_writes_once_then_leaves_the_switch_alone(isolated_auth_store) -> None:
    assert await auth_store.seed_auth_enabled_once(True) is True
    assert (await auth_store.get_auth_settings())["enabled"] is True

    # The menu turns it off; a later boot must NOT re-enable it.
    await auth_store.set_auth_enabled(False)
    assert await auth_store.seed_auth_enabled_once(True) is False
    assert (await auth_store.get_auth_settings())["enabled"] is False


@pytest.mark.asyncio
async def test_blacklist_add_and_check(isolated_auth_store) -> None:
    expiry = int(time.time()) + 600

    assert await auth_store.is_jwt_blacklisted("jti-1") is False
    await auth_store.add_jwt_to_blacklist("jti-1", expiry)
    assert await auth_store.is_jwt_blacklisted("jti-1") is True

    # Adding the same jti twice is a no-op, not an error (logout twice).
    await auth_store.add_jwt_to_blacklist("jti-1", expiry)
    assert await auth_store.is_jwt_blacklisted("jti-1") is True

    # A token without a jti is never acceptable.
    assert await auth_store.is_jwt_blacklisted("") is True


@pytest.mark.asyncio
async def test_cleanup_drops_only_expired_entries(isolated_auth_store) -> None:
    now = int(time.time())
    await auth_store.add_jwt_to_blacklist("expired", now - 5)
    await auth_store.add_jwt_to_blacklist("live", now + 600)

    removed = await auth_store.cleanup_expired_blacklist()

    assert removed == 1
    assert await auth_store.is_jwt_blacklisted("expired") is False
    assert await auth_store.is_jwt_blacklisted("live") is True
