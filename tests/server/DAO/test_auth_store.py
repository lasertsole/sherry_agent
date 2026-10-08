"""The auth store: users, the runtime switch and the JWT blacklist.

Contract under test: users are unique and updatable, the settings row is a
singleton that reads as "disabled" when absent (fresh install), the boot seed
writes it only once (the menu owns it afterwards), and blacklist entries expire
out of the table at their own ``exp``.
"""

from __future__ import annotations

import time

import pytest

from server.DAO import auth_store

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_create_and_read_a_user(isolated_auth_store) -> None:
    created = await auth_store.create_user("admin", "scrypt$stored")

    assert created["id"] > 0
    assert created["username"] == "admin"
    assert created["password_hash"] == "scrypt$stored"
    assert created["created_at"] > 0

    by_name = await auth_store.get_user_by_username("admin")
    by_id = await auth_store.get_user_by_id(created["id"])
    assert by_name == by_id
    assert by_name is not None and by_name["username"] == "admin"
    assert await auth_store.get_user_by_username("nobody") is None


@pytest.mark.asyncio
async def test_duplicate_usernames_are_refused(isolated_auth_store) -> None:
    await auth_store.create_user("admin", "h1")

    with pytest.raises(Exception):
        await auth_store.create_user("admin", "h2")


@pytest.mark.asyncio
async def test_update_password_and_username(isolated_auth_store) -> None:
    user = await auth_store.create_user("admin", "h1")

    assert await auth_store.update_password(user["id"], "h2") is True
    assert await auth_store.update_username(user["id"], "root") is True

    refreshed = await auth_store.get_user_by_id(user["id"])
    assert refreshed is not None
    assert refreshed["password_hash"] == "h2"
    assert refreshed["username"] == "root"
    assert refreshed["updated_at"] >= refreshed["created_at"]


@pytest.mark.asyncio
async def test_delete_user_and_count(isolated_auth_store) -> None:
    user = await auth_store.create_user("admin", "h")

    assert await auth_store.count_users() == 1
    assert await auth_store.delete_user(user["id"]) is True
    assert await auth_store.count_users() == 0
    assert await auth_store.get_user_by_id(user["id"]) is None
    assert await auth_store.delete_user(user["id"]) is False


@pytest.mark.asyncio
async def test_first_user_is_the_single_account(isolated_auth_store) -> None:
    assert await auth_store.get_first_user() is None

    first = await auth_store.create_user("admin", "h1")
    assert (await auth_store.get_first_user())["id"] == first["id"]


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
