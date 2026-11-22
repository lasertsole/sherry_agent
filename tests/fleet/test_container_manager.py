"""Container cell backend: command shape, lifecycle, upgrade and doctor."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from fleet.config import IngressConfig
from fleet.container import ContainerError, ContainerRuntime
from fleet.container_manager import FleetContainerManager
from fleet.manager import FleetError
from fleet.store import FleetStore

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class FakeRuntime:
    """Records every engine call and models just enough container state."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.images: set[str] = set()
        self.networks: set[str] = set()
        self.containers: dict[str, dict[str, Any]] = {}
        self.env_files: dict[str, str] = {}
        self.env_modes: dict[str, int] = {}
        self.logs_text = "cell boom"
        self.fail_start_image: str | None = None

    def _record(self, *args: str) -> None:
        self.calls.append(args)

    def version(self) -> str:
        return "fake 1.0"

    def image_exists(self, image: str) -> bool:
        return image in self.images

    def pull(self, image: str, *, timeout: float | None = None) -> None:
        self._record("pull", image)
        self.images.add(image)

    def network_exists(self, name: str) -> bool:
        return name in self.networks

    def network_create(self, name: str) -> None:
        self._record("network", "create", name)
        self.networks.add(name)

    def network_remove(self, name: str) -> None:
        self._record("network", "rm", name)
        self.networks.discard(name)

    def exists(self, name: str) -> bool:
        return name in self.containers

    def state(self, name: str) -> str | None:
        entry = self.containers.get(name)
        return str(entry["state"]) if entry else None

    def inspect(self, name: str) -> str:
        entry = self.containers.get(name)
        if entry is None:
            return ""
        return json.dumps(
            [
                {
                    "State": {"Status": entry["state"]},
                    "Config": {"Labels": entry["labels"], "Image": entry["image"]},
                    "HostConfig": entry["host"],
                }
            ]
        )

    def create_args(self, spec) -> list[str]:
        return ContainerRuntime("fake", binary="fake").create_args(spec)

    def create(self, spec) -> None:
        self._record("create", *self.create_args(spec))
        self.env_files[spec.name] = spec.env_file.read_text(encoding="utf-8")
        self.env_modes[spec.name] = stat.S_IMODE(spec.env_file.stat().st_mode)
        port_bindings = {}
        if spec.publish:
            address, port = spec.publish
            port_bindings = {"8080/tcp": [{"HostIp": address, "HostPort": str(port)}]}
        self.containers[spec.name] = {
            "state": "created",
            "image": spec.image,
            "labels": dict(spec.labels),
            "host": {
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": 4 * 1024**3,
                "NanoCpus": 2 * 10**9,
                "PidsLimit": 1024,
                "NetworkMode": spec.network or "host",
                "PortBindings": port_bindings,
            },
        }

    def start(self, name: str) -> None:
        self._record("start", name)
        entry = self.containers[name]
        if self.fail_start_image and entry["image"] == self.fail_start_image:
            raise ContainerError("engine refused to start the new image")
        entry["state"] = "running"

    def stop(self, name: str, *, timeout_s: int = 10) -> None:
        self._record("stop", name)
        if name in self.containers:
            self.containers[name]["state"] = "exited"

    def remove(self, name: str) -> None:
        self._record("container", "rm", name)
        self.containers.pop(name, None)

    def kill(self, name: str) -> None:
        self._record("kill", name)

    def logs(self, name: str, *, tail: int = 120) -> str:
        return self.logs_text

    def run(self, *args: str, timeout: float | None = None, check: bool = True):
        self._record(*args)
        return _Completed(stdout="1")


class _Completed:
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


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
        container_image="sherry:test",
    )


async def _manager(
    tmp_path: Path,
    *,
    network: str = "publish",
    image: str = "sherry:test",
) -> tuple[FleetContainerManager, FakeRuntime]:
    config = _config(tmp_path)
    store = FleetStore(config.db_path)
    await store.init()
    runtime = FakeRuntime()
    runtime.images.add(image)
    manager = FleetContainerManager(store, config, runtime=runtime, image=image, network=network)
    return manager, runtime


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the three probes that would otherwise need a live HTTP cell."""

    async def _wait(_self, _port: int, _name: str, _attempt: str) -> bool:
        return True

    async def _health(_self, _port: int) -> bool:
        return True

    async def _auth(_self, _data_dir: Path, _port: int, _token: str) -> None:
        return None

    monkeypatch.setattr(FleetContainerManager, "_wait_container_healthy", _wait)
    monkeypatch.setattr(FleetContainerManager, "_health_ok", _health)
    monkeypatch.setattr(FleetContainerManager, "_disable_cell_auth", _auth)


@pytest.mark.asyncio
async def test_create_registers_a_container_cell_without_starting(tmp_path: Path):
    manager, runtime = await _manager(tmp_path)

    result = await manager.create("alice", start=False)

    assert result["backend"] == "container"
    assert result["image"] == "sherry:test"
    assert result["container"] == "sherry-cell-alice"
    # No container exists until the first start: create only prepares the tree.
    assert result["container_state"] is None
    cell = await manager.store.get_cell("alice")
    assert cell is not None
    assert cell["runtime"] == "container"
    assert cell["image"] == "sherry:test"
    assert cell["container_name"] == "sherry-cell-alice"
    assert (tmp_path / "cells" / "alice" / "workspace").is_dir()
    assert runtime.calls == []
    with pytest.raises(FleetError, match="already exists"):
        await manager.create("alice", start=False)


@pytest.mark.asyncio
async def test_start_builds_the_hardened_create_command(tmp_path: Path, healthy: None):
    manager, runtime = await _manager(tmp_path)
    await manager.create("alice", start=False)

    result = await manager.start("alice")

    assert result["status"] == "running"
    create = next(call for call in runtime.calls if call[0] == "create")
    joined = " ".join(create)
    assert "--cap-drop=ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--memory 4g" in joined
    assert "--cpus 2" in joined
    assert "--pids-limit 1024" in joined
    assert "--restart unless-stopped" in joined
    assert "--network sherry-cell-alice-net" in joined
    assert f"-p 127.0.0.1:{result['port']}:8080" in joined
    assert "--volume" in joined and ":/app/data" in joined
    assert "--add-host host.docker.internal:host-gateway" in joined
    assert "--label sherry.fleet.tenant=alice" in joined
    assert "--label sherry.fleet.managed=1" in joined
    assert "sherry:test" == create[-1]
    env = runtime.env_files["sherry-cell-alice"]
    assert "SHERRY_HOME=/app/data" in env
    assert "SHERRY_PROJECT_DIR=/app/data/workspace" in env
    assert f"SHERRY_GATEWAY_TOKEN={await _token(manager)}" in env
    assert "API_PORT=8080" in env
    assert "SHERRY_AUTH_ENABLED=false" in env
    assert "LOCAL_DAEMON_HOST=host.docker.internal" in env
    assert "SHERRY_DAEMON_CLIENT_ONLY=1" in env
    if os.name == "posix":
        assert runtime.env_modes["sherry-cell-alice"] == 0o600


@pytest.mark.asyncio
async def test_host_network_mode_omits_publish_and_listens_on_the_assigned_port(
    tmp_path: Path, healthy: None
):
    manager, runtime = await _manager(tmp_path, network="host")
    await manager.create("bob", start=False)

    result = await manager.start("bob")

    create = next(call for call in runtime.calls if call[0] == "create")
    joined = " ".join(create)
    assert "--network host" in joined
    assert " -p " not in f" {joined} "
    env = runtime.env_files["sherry-cell-bob"]
    assert f"API_PORT={result['port']}" in env
    assert "API_PORT=8080" not in env


@pytest.mark.asyncio
async def test_a_failed_health_check_removes_the_container_and_marks_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager, runtime = await _manager(tmp_path)
    await manager.create("alice", start=False)

    async def _unhealthy(_self, _port: int, _name: str, _attempt: str) -> bool:
        return False

    monkeypatch.setattr(FleetContainerManager, "_wait_container_healthy", _unhealthy)

    with pytest.raises(FleetError, match="did not become healthy") as failure:
        await manager.start("alice")

    assert "cell boom" in str(failure.value)
    assert runtime.containers == {}
    process = await manager.store.get_process("alice")
    assert process is not None and process["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_and_remove_tear_down_container_network_and_data(tmp_path: Path, healthy: None):
    manager, runtime = await _manager(tmp_path)
    await manager.create("alice")

    stopped = await manager.stop("alice")
    assert stopped["status"] == "stopped"
    assert runtime.state("sherry-cell-alice") == "exited"

    await manager.remove("alice")

    assert "sherry-cell-alice" not in runtime.containers
    assert "sherry-cell-alice-net" not in runtime.networks
    assert not (tmp_path / "cells" / "alice").exists()
    assert await manager.store.get_cell("alice") is None


@pytest.mark.asyncio
async def test_upgrade_switches_the_image_and_rolls_back_on_failure(tmp_path: Path, healthy: None):
    manager, runtime = await _manager(tmp_path)
    await manager.create("alice")
    runtime.images.add("sherry:next")

    upgraded = await manager.upgrade("alice", "sherry:next")
    assert upgraded["image"] == "sherry:next"
    assert runtime.containers["sherry-cell-alice"]["image"] == "sherry:next"

    runtime.images.add("sherry:broken")
    runtime.fail_start_image = "sherry:broken"

    rolled_back = await manager.upgrade("alice", "sherry:broken")

    assert rolled_back["image"] == "sherry:next"
    assert rolled_back["rolled_back_from"] == "sherry:broken"
    assert runtime.containers["sherry-cell-alice"]["image"] == "sherry:next"
    cell = await manager.store.get_cell("alice")
    assert cell is not None and cell["image"] == "sherry:next"


@pytest.mark.asyncio
async def test_doctor_flags_a_weakened_container(tmp_path: Path, healthy: None):
    manager, runtime = await _manager(tmp_path)
    await manager.create("alice")

    report = (await manager.doctor())[0]
    assert report["ok"] is True, report

    runtime.containers["sherry-cell-alice"]["host"]["CapDrop"] = []

    weakened = (await manager.doctor("alice"))[0]
    assert weakened["ok"] is False
    failed = {item["check"] for item in weakened["checks"] if not item["ok"]}
    assert failed == {"cap-drop=ALL"}

    missing = await manager.doctor("nobody")
    assert missing == []


@pytest.mark.asyncio
async def test_supervision_restarts_an_exited_container(tmp_path: Path, healthy: None):
    manager, runtime = await _manager(tmp_path)
    await manager.create("alice")

    runtime.containers["sherry-cell-alice"]["state"] = "exited"

    restarted = await manager.supervise_once()

    assert restarted == ["alice"]
    assert runtime.state("sherry-cell-alice") == "running"


@pytest.mark.asyncio
async def test_list_reports_the_backend_and_the_engine_state(tmp_path: Path, healthy: None):
    manager, _runtime = await _manager(tmp_path)
    await manager.create("alice")

    rows = await manager.list()

    assert rows == [
        {
            "tenant_id": "alice",
            "backend": "container",
            "host": "127.0.0.1",
            "port": rows[0]["port"],
            "status": "running",
            "pid": None,
            "image": "sherry:test",
            "container": "sherry-cell-alice",
            "container_state": "running",
        }
    ]


@pytest.mark.asyncio
async def test_disable_cell_auth_flips_a_restored_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _config(tmp_path)
    manager = FleetContainerManager(FleetStore(config.db_path), config, runtime=FakeRuntime())
    data_dir = tmp_path / "cells" / "alice"
    database = data_dir / "src" / "data" / "auth.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE auth_settings ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO auth_settings VALUES (1, 1, 0)")

    replies = iter(
        [
            {"auth_required": True, "auth_enabled": True},
            {"auth_required": False, "auth_enabled": False},
        ]
    )

    async def _status(_self, _port: int, _token: str) -> dict[str, Any]:
        return next(replies)

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(FleetContainerManager, "_auth_status", _status)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    await manager._disable_cell_auth(data_dir, 20100, "gw-token")

    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT enabled FROM auth_settings").fetchone()
    assert row == (0,)


async def _token(manager: FleetContainerManager) -> str:
    cell = await manager.store.get_cell("alice")
    assert cell is not None
    return str(cell["gateway_token"])
