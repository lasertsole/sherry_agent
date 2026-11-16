"""Fleet manager: tenant tree, process start/stop and registry state."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from fleet import manager as manager_mod
from fleet.config import IngressConfig
from fleet.manager import FleetError, FleetManager
from fleet.store import FleetStore

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _config(tmp_path: Path) -> IngressConfig:
    return IngressConfig(
        host="127.0.0.1",
        port=8081,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
        static_dir=None,
        fleet_root=tmp_path / "cells",
        public_base_url="http://127.0.0.1:8081",
        health_timeout_s=1,
    )


async def _manager(tmp_path: Path) -> FleetManager:
    config = _config(tmp_path)
    store = FleetStore(config.db_path)
    await store.init()
    return FleetManager(store, config)


@pytest.mark.asyncio
async def test_create_builds_the_tenant_tree_without_starting(tmp_path: Path):
    manager = await _manager(tmp_path)

    result = await manager.create("alice", start=False)

    assert result["status"] == "stopped"
    assert result["port"] >= 20100
    data_dir = tmp_path / "cells" / "alice"
    assert (data_dir / "workspace").is_dir()
    assert json.loads((data_dir / "sherry.jsonc").read_text())["project_dir"] == str(
        data_dir / "workspace"
    )
    assert await manager.store.get_cell("alice") is not None
    with pytest.raises(FleetError, match="already exists"):
        await manager.create("alice", start=False)


@pytest.mark.asyncio
async def test_start_records_the_pid_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = await _manager(tmp_path)
    await manager.create("alice", start=False)
    captured: dict = {}

    class _Proc:
        pid = 4242

    def _popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return _Proc()

    async def _healthy(_self, _port, _pid):
        return True

    monkeypatch.setattr(manager_mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(FleetManager, "_wait_healthy", _healthy)

    result = await manager.start("alice")

    assert result["status"] == "running"
    assert result["pid"] == 4242
    env = captured["env"]
    assert env["SHERRY_HOME"] == str(tmp_path / "cells" / "alice")
    assert env["SHERRY_PROJECT_DIR"] == str(tmp_path / "cells" / "alice" / "workspace")
    assert env["API_PORT"] == str(result["port"])
    assert env["PUBLIC_BASE_URL"] == "http://127.0.0.1:8081"
    assert captured["command"][-2:] == ["-m", "server"]


@pytest.mark.asyncio
async def test_stop_and_remove_clear_the_registry_and_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = await _manager(tmp_path)
    await manager.create("alice", start=False)
    data_dir = tmp_path / "cells" / "alice"
    killed: list[tuple[int, int]] = []

    monkeypatch.setattr(manager_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    checks = {"n": 0}

    def _alive(_pid: int) -> bool:
        checks["n"] += 1
        return checks["n"] == 1

    monkeypatch.setattr(FleetManager, "_pid_alive", staticmethod(_alive))
    monkeypatch.setattr(FleetManager, "_pid_is_cell", staticmethod(lambda _pid, **_kw: True))
    await manager.store.set_process(
        "alice", pid=4242, status="running", data_dir=str(data_dir), started_at=1
    )

    stopped = await manager.stop("alice")

    assert stopped["status"] == "stopped"
    assert stopped["pid"] is None
    assert any(sig == manager_mod.signal.SIGTERM for _pid, sig in killed)

    await manager.remove("alice")
    assert not data_dir.exists()
    assert await manager.store.get_cell("alice") is None
    assert await manager.store.get_process("alice") is None


@pytest.mark.asyncio
async def test_seed_config_copies_the_host_model_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = await _manager(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    (host / ".models").write_text("model-config\n", encoding="utf-8")
    monkeypatch.setattr(manager_mod, "APP_HOME", host)

    await manager.create("alice", start=False, seed_config=True)

    assert (tmp_path / "cells" / "alice" / ".models").read_text() == "model-config\n"


@pytest.mark.asyncio
async def test_stop_never_signals_a_reused_foreign_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = await _manager(tmp_path)
    await manager.create("alice", start=False)
    data_dir = tmp_path / "cells" / "alice"
    killed: list[tuple[int, int]] = []

    monkeypatch.setattr(manager_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(FleetManager, "_pid_alive", staticmethod(lambda _pid: True))
    monkeypatch.setattr(FleetManager, "_pid_is_cell", staticmethod(lambda _pid, **_kw: False))
    await manager.store.set_process(
        "alice", pid=4242, status="running", data_dir=str(data_dir), started_at=1
    )

    stopped = await manager.stop("alice")

    assert killed == []
    assert stopped["status"] == "stopped"


@pytest.mark.asyncio
async def test_backup_and_restore_are_database_consistent(tmp_path: Path):
    manager = await _manager(tmp_path)
    await manager.create("alice", start=False)
    data_dir = tmp_path / "cells" / "alice"
    (data_dir / "workspace" / "note.txt").write_text("original\n", encoding="utf-8")
    db_path = data_dir / "src" / "data" / "state.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE items (name TEXT)")
        db.execute("INSERT INTO items VALUES ('one')")
    (data_dir / "logs").mkdir(exist_ok=True)
    (data_dir / "logs" / "cell.log").write_text("noise\n", encoding="utf-8")
    backup_dir = tmp_path / "backup"

    result = manager.backup("alice", backup_dir)

    assert result["files"] >= 3
    assert (backup_dir / "workspace" / "note.txt").read_text() == "original\n"
    assert not (backup_dir / "logs" / "cell.log").exists()
    with sqlite3.connect(backup_dir / "src" / "data" / "state.db") as db:
        assert db.execute("SELECT name FROM items").fetchone() == ("one",)

    (data_dir / "workspace" / "note.txt").write_text("changed\n", encoding="utf-8")
    restored = await manager.restore("alice", backup_dir)

    assert (data_dir / "workspace" / "note.txt").read_text() == "original\n"
    assert Path(restored["previous"]).is_dir()


@pytest.mark.asyncio
async def test_supervisor_restarts_a_cell_whose_process_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = await _manager(tmp_path)
    await manager.create("alice", start=False)
    await manager.store.set_process(
        "alice", pid=4242, status="running", data_dir=str(manager.data_dir("alice")), started_at=1
    )
    restarted: list[str] = []

    async def _start(_self, tenant: str) -> dict:
        restarted.append(tenant)
        return {}

    monkeypatch.setattr(FleetManager, "_pid_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(FleetManager, "start", _start)

    assert await manager.supervise_once() == ["alice"]
    assert restarted == ["alice"]


def test_logs_rotate_to_one_previous_file(tmp_path: Path):
    from dataclasses import replace

    config = replace(_config(tmp_path), log_max_bytes=10)
    manager = FleetManager(FleetStore(config.db_path), config)
    log = tmp_path / "cell.log"
    log.write_text("x" * 11, encoding="utf-8")

    manager._rotate_log(log)

    assert log.exists()
    assert log.read_bytes() == b""
    assert (tmp_path / "cell.log.1").read_text() == "x" * 11


@pytest.mark.asyncio
async def test_concurrent_creates_never_share_a_port(tmp_path: Path):
    manager = await _manager(tmp_path)

    results = await asyncio.gather(*(manager.create(f"tenant{i}", start=False) for i in range(6)))

    assert len({result["port"] for result in results}) == 6


def test_cell_resource_limits_wrap_the_command(tmp_path: Path):
    from dataclasses import replace

    config = replace(_config(tmp_path), cell_memory_mb=512, cell_nofile=1024)
    manager = FleetManager(FleetStore(config.db_path), config)

    command = manager._limited_command(["python3", "-m", "server"])

    assert command[:4] == [
        "/bin/sh",
        "-c",
        'ulimit -v 524288; ulimit -n 1024; exec "$@"',
        "sherry-cell",
    ]
    assert command[4:] == ["python3", "-m", "server"]
    assert FleetManager(FleetStore(tmp_path / "plain.db"), _config(tmp_path))._limited_command(
        ["python3"]
    ) == ["python3"]


@pytest.mark.asyncio
async def test_supervisor_restarts_a_hung_cell_after_three_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = await _manager(tmp_path)
    await manager.create("alice", start=False)
    await manager.store.set_process(
        "alice", pid=4242, status="running", data_dir=str(manager.data_dir("alice")), started_at=1
    )
    calls: list[str] = []

    async def _unhealthy(_self, _port: int) -> bool:
        return False

    async def _stop(_self, _tenant: str) -> dict:
        calls.append("stop")
        return {}

    async def _start(_self, _tenant: str) -> dict:
        calls.append("start")
        return {}

    monkeypatch.setattr(FleetManager, "_pid_alive", staticmethod(lambda _pid: True))
    monkeypatch.setattr(FleetManager, "_pid_is_cell", staticmethod(lambda _pid, **_kw: True))
    monkeypatch.setattr(FleetManager, "_health_ok", _unhealthy)
    monkeypatch.setattr(FleetManager, "stop", _stop)
    monkeypatch.setattr(FleetManager, "start", _start)

    assert await manager.supervise_once() == []
    assert await manager.supervise_once() == []
    assert calls == []
    assert await manager.supervise_once() == ["alice"]
    assert calls == ["stop", "start"]
