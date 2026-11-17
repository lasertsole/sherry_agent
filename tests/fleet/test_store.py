"""Central Ingress account/cell store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fleet.store import FleetStore
from server.utils.password import hash_password

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.mark.asyncio
async def test_account_and_cell_round_trip(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()

    user = await store.create_user("alice", "password123", tenant_id="alice")

    assert user == {"id": 1, "username": "alice", "role": "user", "tenant_id": "alice"}
    assert await store.list_user_tenants(1) == [
        {"tenant_id": "alice", "role": "owner", "is_default": True}
    ]
    stored = await store.get_user_by_username("ALICE")
    assert stored is not None
    assert store.verify_password("password123", stored["password_hash"])
    assert not store.verify_password("wrong", stored["password_hash"])

    await store.upsert_cell("alice", host="127.0.0.1", port=20100, gateway_token="gw-1")
    cell = await store.get_cell("alice")
    assert cell is not None
    assert cell["port"] == 20100
    assert cell["gateway_token"] == "gw-1"
    assert await store.list_cells() == [{"tenant_id": "alice", "host": "127.0.0.1", "port": 20100}]


@pytest.mark.asyncio
async def test_a_tenant_is_shared_but_the_username_stays_unique(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="tenant-a")

    with pytest.raises(ValueError, match="username already exists"):
        await store.create_user("ALICE", "password123", tenant_id="tenant-b")

    bob = await store.create_user("bob", "password456", tenant_id="tenant-a")
    assert bob["tenant_id"] == "tenant-a"
    assert await store.list_user_tenants(bob["id"]) == [
        {"tenant_id": "tenant-a", "role": "owner", "is_default": True}
    ]


@pytest.mark.asyncio
async def test_membership_add_switch_and_remove(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    alice = await store.create_user("alice", "password123", tenant_id="tenant-a")

    await store.add_user_tenant(alice["id"], "tenant-b", role="member")
    rows = await store.list_user_tenants(alice["id"])
    assert rows == [
        {"tenant_id": "tenant-a", "role": "owner", "is_default": True},
        {"tenant_id": "tenant-b", "role": "member", "is_default": False},
    ]
    assert await store.get_default_tenant(alice["id"]) == "tenant-a"
    assert await store.user_has_tenant(alice["id"], "tenant-b") is True

    await store.add_user_tenant(alice["id"], "tenant-b", is_default=True)
    rows = await store.list_user_tenants(alice["id"])
    assert rows == [
        {"tenant_id": "tenant-b", "role": "member", "is_default": True},
        {"tenant_id": "tenant-a", "role": "owner", "is_default": False},
    ]
    assert await store.get_default_tenant(alice["id"]) == "tenant-b"
    updated = await store.get_user_by_id(alice["id"])
    assert updated is not None and updated["tenant_id"] == "tenant-b"

    assert await store.remove_user_tenant(alice["id"], "tenant-b") is True
    assert await store.get_default_tenant(alice["id"]) == "tenant-a"
    assert await store.remove_user_tenant(alice["id"], "tenant-b") is False

    assert await store.remove_user_tenant(alice["id"], "tenant-a") is True
    assert await store.list_user_tenants(alice["id"]) == []
    assert await store.get_default_tenant(alice["id"]) is None


@pytest.mark.asyncio
async def test_legacy_users_table_is_rebuilt_once(tmp_path: Path):
    path = tmp_path / "fleet.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                tenant_id TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO users(username, password_hash, role, tenant_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice", hash_password("password123"), "user", "tenant-a", 1),
        )

    store = FleetStore(path)
    await store.init()
    await store.init()

    alice = await store.get_user_by_username("alice")
    assert alice is not None and alice["id"] == 1
    assert await store.list_user_tenants(1) == [
        {"tenant_id": "tenant-a", "role": "owner", "is_default": True}
    ]
    bob = await store.create_user("bob", "password123", tenant_id="tenant-a")
    assert bob["id"] == 2
    assert await store.remove_user_tenant(1, "tenant-a") is True
    restarted = FleetStore(path)
    await restarted.init()
    assert await restarted.list_user_tenants(1) == []
    with sqlite3.connect(path) as db:
        indexes = db.execute("PRAGMA index_list('users')").fetchall()
    tenant_unique = False
    for index in indexes:
        if not index[2]:
            continue
        columns = [
            row[2] for row in sqlite3.connect(path).execute(f'PRAGMA index_info("{index[1]}")')
        ]
        if columns == ["tenant_id"]:
            tenant_unique = True
    assert tenant_unique is False


@pytest.mark.asyncio
async def test_refresh_tokens_can_be_revoked(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    user = await store.create_user("alice", "password123", tenant_id="alice")

    await store.save_refresh("jti-1", user["id"], 2**31)
    assert await store.refresh_active("jti-1", user["id"]) is True

    await store.revoke_refresh("jti-1")
    assert await store.refresh_active("jti-1", user["id"]) is False


@pytest.mark.asyncio
async def test_backup_database_uses_the_online_backup_api(tmp_path: Path):
    path = tmp_path / "fleet.db"
    store = FleetStore(path)
    await store.init()
    await store.create_user("alice", "password123", tenant_id="alice")

    result = store.backup_database(tmp_path / "backup")

    assert result["bytes"] > 0
    with sqlite3.connect(tmp_path / "backup" / "fleet.db") as db:
        assert db.execute("SELECT username FROM users").fetchone() == ("alice",)
