"""The auth store: the single account, the runtime switch and the JWT blacklist.

Contract under test: ONE account row exists at most (password-only login — the
schema pins ``id = 1``, so a second INSERT cannot create a second account), a
password change updates that row in place, the settings row is a singleton that
reads as "disabled" when absent (fresh install), the boot seed writes it only
once (the menu owns it afterwards), and blacklist entries expire out of the table at
their own ``exp``.
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
