"""Regression pins for the container backend's user-visible contract.

These are not unit tests of one function: they pin the decisions an operator
depends on and that a refactor could silently change —

* the `create` vector keeps EVERY isolation flag (a dropped `--cap-drop=ALL` or
  `--security-opt` is a silent security regression, not a cosmetic one);
* the host-network fallback listens on the assigned port (publish mode listens
  on 8080), because the Ingress reaches a cell at `127.0.0.1:<port>` in both;
* a registered container cell resolves to the container manager without the
  caller repeating `--backend`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet import __main__ as cli
from fleet.config import IngressConfig
from fleet.container import ContainerRuntime, ContainerSpec
from fleet.container_manager import FleetContainerManager
from fleet.manager import FleetManager
from fleet.store import FleetStore
from tests.fleet.test_container_manager import FakeRuntime

pytestmark = [pytest.mark.regression, pytest.mark.timeout(60)]


def _spec(tmp_path: Path, *, network: str | None, publish: tuple[str, int] | None) -> ContainerSpec:
    data_dir = tmp_path / "cells" / "alice"
    data_dir.mkdir(parents=True)
    env_file = data_dir / ".cell.env"
    env_file.write_text("SHERRY_HOME=/app/data\n", encoding="utf-8")
    return ContainerSpec(
        name="sherry-cell-alice",
        image="sherry-agent:dev",
        data_dir=data_dir,
        env_file=env_file,
        network=network,
        publish=publish,
        labels={"sherry.fleet.tenant": "alice"},
    )


def _args(spec: ContainerSpec) -> list[str]:
    return ContainerRuntime("podman", binary="podman").create_args(spec)


def test_the_publish_create_vector_keeps_every_isolation_flag(tmp_path: Path):
    spec = _spec(tmp_path, network="sherry-cell-alice-net", publish=("127.0.0.1", 20100))

    args = _args(spec)

    assert args == [
        "create",
        "--name",
        "sherry-cell-alice",
        "--init",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--pids-limit",
        "1024",
        "--restart",
        "unless-stopped",
        "--network",
        "sherry-cell-alice-net",
        "-p",
        "127.0.0.1:20100:8080",
        "--volume",
        f"{spec.data_dir}:/app/data",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--env-file",
        str(spec.env_file),
        "--label",
        "sherry.fleet.tenant=alice",
        "sherry-agent:dev",
    ]


def test_host_mode_swaps_the_publish_for_the_shared_namespace(tmp_path: Path):
    spec = _spec(tmp_path, network=None, publish=None)

    args = _args(spec)

    assert args[args.index("--network") + 1] == "host"
    assert "-p" not in args
    # The mount and the hardening flags are unchanged in host mode.
    assert "--cap-drop=ALL" in args
    assert "--security-opt" in args
    assert f"{spec.data_dir}:/app/data" in args


@pytest.mark.asyncio
async def test_a_registered_container_cell_resolves_without_repeating_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = IngressConfig(
        host="127.0.0.1",
        port=8081,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
        fleet_root=tmp_path / "cells",
        public_base_url="http://127.0.0.1:8081",
    )
    store = FleetStore(config.db_path)
    await store.init()
    await store.upsert_cell(
        "alice",
        host="127.0.0.1",
        port=20100,
        gateway_token="gw-1",
        runtime="container",
        image="sherry-agent:dev",
        container_name="sherry-cell-alice",
    )
    await store.upsert_cell("bob", host="127.0.0.1", port=20101, gateway_token="gw-2")
    monkeypatch.setattr(cli, "load_config", lambda: config)

    container_manager = await cli._manager_for("alice")
    process_manager = await cli._manager_for("bob")

    assert isinstance(container_manager, FleetContainerManager)
    assert not isinstance(process_manager, FleetContainerManager)
    assert isinstance(process_manager, FleetManager)


@pytest.mark.asyncio
async def test_host_mode_env_file_listens_on_the_assigned_port(tmp_path: Path):
    config = IngressConfig(
        host="127.0.0.1",
        port=8081,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
        fleet_root=tmp_path / "cells",
        public_base_url="http://127.0.0.1:8081",
        container_image="sherry-agent:dev",
    )
    store = FleetStore(config.db_path)
    await store.init()
    runtime = FakeRuntime()
    runtime.images.add("sherry-agent:dev")
    manager = FleetContainerManager(
        store, config, runtime=runtime, image="sherry-agent:dev", network="host"
    )
    await manager.create("alice", start=False)
    cell = await store.get_cell("alice")
    assert cell is not None

    env_file = manager._write_env_file(manager.data_dir("alice"), cell, 20100)

    values = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert values["API_PORT"] == "20100"
    assert values["SHERRY_HOME"] == "/app/data"
    assert values["SHERRY_AUTH_ENABLED"] == "false"
    assert values["LOCAL_DAEMON_HOST"] == "host.docker.internal"
