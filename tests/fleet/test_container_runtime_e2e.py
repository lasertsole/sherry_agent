"""Opt-in smoke test: a real engine must accept the cell `create` vector.

CI has no container engine, so this is skipped unless the developer opts in:

    SHERRY_FLEET_CONTAINER_E2E=1 uv run --no-sync pytest tests/fleet/test_container_runtime_e2e.py

It CREATES one container (never starts it) so the engine itself validates the
hardening flags, the bind mount, the labels and the env file — the parts a fake
runtime cannot prove. Host networking is used because it needs no bridge rules,
which is also the WSL2 path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fleet.container import (
    LABEL_MANAGED,
    LABEL_TENANT,
    ContainerRuntime,
    ContainerSpec,
)
from fleet.container_manager import all_capabilities_dropped

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

_PROBE = "sherry-e2e-probe"


def _runtime() -> ContainerRuntime:
    if os.environ.get("SHERRY_FLEET_CONTAINER_E2E") != "1":
        pytest.skip("set SHERRY_FLEET_CONTAINER_E2E=1 to exercise a real engine")
    return ContainerRuntime.resolve(os.environ.get("SHERRY_FLEET_CONTAINER_RUNTIME", "auto"))


def test_the_engine_accepts_the_cell_create_vector(tmp_path: Path):
    runtime = _runtime()
    image = os.environ.get("SHERRY_FLEET_TEST_IMAGE", "python:3.13-slim")
    if not runtime.image_exists(image):
        pytest.skip(f"{image} is not present locally; pull it first")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_file = data_dir / ".cell.env"
    env_file.write_text("API_PORT=8080\nSHERRY_HOME=/app/data\n", encoding="utf-8")
    spec = ContainerSpec(
        name=_PROBE,
        image=image,
        data_dir=data_dir,
        env_file=env_file,
        network=None,
        publish=None,
        labels={LABEL_MANAGED: "1", LABEL_TENANT: "probe"},
    )
    runtime.remove(_PROBE)
    try:
        runtime.create(spec)
        assert runtime.state(_PROBE) == "created"
        inspected = json.loads(runtime.inspect(_PROBE))[0]
        host = inspected["HostConfig"]
        labels = inspected["Config"]["Labels"]
        # podman reports the expanded default set, docker reports "ALL".
        assert all_capabilities_dropped(host)[0], host["CapDrop"]
        assert any("no-new-privileges" in item for item in host["SecurityOpt"])
        assert host["Memory"] == 4 * 1024**3
        assert host["PidsLimit"] == 1024
        assert host["NetworkMode"] == "host"
        assert labels[LABEL_TENANT] == "probe"
        mounts = inspected["Mounts"]
        assert any(mount["Destination"] == "/app/data" for mount in mounts)
    finally:
        runtime.remove(_PROBE)
