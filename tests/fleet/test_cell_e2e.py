"""End-to-end: a real spawned cell is health-checked, proxied and restarted."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from fleet.config import IngressConfig
from fleet.ingress import create_app
from fleet.manager import FleetManager
from fleet.store import FleetStore

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

_STUB = Path(__file__).with_name("cell_stub.py")


def _config(tmp_path: Path) -> IngressConfig:
    return IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
        fleet_root=tmp_path / "cells",
        public_base_url="http://127.0.0.1:8081",
        health_timeout_s=90,
        supervise=False,
        supervise_interval_s=1,
    )


async def _wait_dead(manager: FleetManager, pid: int, timeout_s: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        manager._reap(pid)
        if not manager._pid_alive(pid):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"pid {pid} did not exit")


@pytest.mark.asyncio
async def test_a_real_cell_is_spawned_proxied_restarted_and_stopped(tmp_path: Path):
    config = _config(tmp_path)
    store = FleetStore(config.db_path)
    await store.init()
    manager = FleetManager(store, config, server_command=[sys.executable, str(_STUB)])
    await manager.create("alice", start=False)

    started = await manager.start("alice")
    pid = int(started["pid"])
    try:
        assert started["status"] == "running"
        assert manager._pid_is_cell(pid, data_dir=manager.data_dir("alice"))
        assert (manager.data_dir("alice") / "logs" / "cell.log").exists()

        await store.create_user("alice", "password123", tenant_id="alice")
        client = TestClient(TestServer(await create_app(config, store, manager)))
        await client.start_server()
        try:
            assert (await client.get("/sessions")).status == 401
            login = await client.post(
                "/auth/login", json={"username": "alice", "password": "password123"}
            )
            assert login.status == 200
            proxied = await client.get("/sessions")
            assert proxied.status == 200
            assert (await proxied.json())["tenant"] == "alice"

            ws = await client.ws_connect("/sessions/agent/ws?session_id=s")
            await ws.send_str("ping")
            message = await ws.receive(timeout=5)
            assert message.type == WSMsgType.TEXT
            assert message.data == "echo:ping"
            await ws.close()

            os.kill(pid, signal.SIGKILL)
            await _wait_dead(manager, pid)

            assert await manager.supervise_once() == ["alice"]
            process = await manager.store.get_process("alice")
            new_pid = int(process["pid"])
            assert new_pid != pid
            assert process["status"] == "running"
            assert manager._pid_is_cell(new_pid, data_dir=manager.data_dir("alice"))
        finally:
            await client.close()
    finally:
        await manager.stop("alice")

    process = await manager.store.get_process("alice")
    assert process["status"] == "stopped"
    assert process["pid"] is None


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_real_sherry_cell_boots_and_serves_through_the_ingress(tmp_path: Path):
    """The actual `python -m server` cell, not the stub: manager env + proxy."""
    config = _config(tmp_path)
    store = FleetStore(config.db_path)
    await store.init()
    manager = FleetManager(store, config, server_command=[sys.executable, "-m", "server"])
    await manager.create("alice", start=False)

    started = await manager.start("alice")
    pid = int(started["pid"])
    assert started["status"] == "running"
    assert manager._pid_is_cell(pid, data_dir=manager.data_dir("alice"))
    try:
        await store.create_user("alice", "password123", tenant_id="alice")
        client = TestClient(TestServer(await create_app(config, store, manager)))
        await client.start_server()
        try:
            login = await client.post(
                "/auth/login", json={"username": "alice", "password": "password123"}
            )
            assert login.status == 200
            heartbeat = await client.get("/heartbeat")
            assert heartbeat.status == 200
            assert isinstance(await heartbeat.json(), dict)
        finally:
            await client.close()
    finally:
        await manager.stop("alice")

    process = await manager.store.get_process("alice")
    assert process["status"] == "stopped"
    assert process["pid"] is None
