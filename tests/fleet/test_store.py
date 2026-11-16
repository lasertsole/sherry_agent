"""Central Ingress account/cell store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fleet.store import FleetStore

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.mark.asyncio
async def test_account_and_cell_round_trip(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()

    user = await store.create_user("alice", "password123", tenant_id="alice")

    assert user == {"id": 1, "username": "alice", "role": "user", "tenant_id": "alice"}
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
async def test_duplicate_username_or_tenant_is_refused(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="tenant-a")

    with pytest.raises(ValueError, match="already exists"):
        await store.create_user("ALICE", "password123", tenant_id="tenant-b")
    with pytest.raises(ValueError, match="already exists"):
        await store.create_user("bob", "password123", tenant_id="tenant-a")


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
