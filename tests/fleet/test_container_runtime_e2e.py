"""Opt-in smoke test: a real engine must accept the cell `create` vector.

CI has no container engine, so this is skipped unless the developer opts in:

    SHERRY_FLEET_CONTAINER_E2E=1 uv run --no-sync pytest tests/fleet/test_container_runtime_e2e.py

Two tests live here:

* the **create** vector — the engine itself validates the hardening flags, the
  bind mount, the labels and the env file, the parts a fake runtime cannot prove;
* a **full lifecycle smoke** — build a tiny stub image, create → start →
  `/health` → the cell writes through the mount → stop → rm, so the plumbing the
  manager depends on (host networking, env file, readiness, teardown) is
  exercised against the real engine rather than a fake.

Host networking is used because it needs no bridge rules, which is also the
WSL2 path.
"""

from __future__ import annotations

import json
import os
import socket
import textwrap
import time
import urllib.error
import urllib.request
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


_STUB = """
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._reply({"ready": True})
        elif self.path == "/auth/status":
            self._reply({"auth_enabled": False, "auth_required": False})
        else:
            self._reply({"path": self.path})

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


home = os.environ.get("SHERRY_HOME", "/app/data")
os.makedirs(home, exist_ok=True)
with open(os.path.join(home, "stub-started.txt"), "w", encoding="utf-8") as handle:
    handle.write("ready\\n")
HTTPServer((os.environ.get("API_HOST", "0.0.0.0"), int(os.environ["API_PORT"])), Handler).serve_forever()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_http(port: int, timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as reply:
                if reply.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.5)
    return False


def test_the_real_engine_runs_one_cell_lifecycle(tmp_path: Path):
    """Build a stub image and run create → start → health → mount → stop → rm."""
    runtime = _runtime()
    base = os.environ.get("SHERRY_FLEET_TEST_BASE_IMAGE", "python:3.13-slim")
    if not runtime.image_exists(base):
        pytest.skip(f"{base} is not present locally; pull it first")
    context = tmp_path / "context"
    context.mkdir()
    (context / "stub.py").write_text(textwrap.dedent(_STUB).lstrip(), encoding="utf-8")
    (context / "Dockerfile").write_text(
        f'FROM {base}\nCOPY stub.py /stub.py\nCMD ["python", "/stub.py"]\n', encoding="utf-8"
    )
    image = f"localhost/sherry-cell-e2e:{os.getpid()}"
    runtime.run("build", "-q", "-t", image, str(context), timeout=600)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    port = _free_port()
    env_file = data_dir / ".cell.env"
    env_file.write_text(
        "SHERRY_HOME=/app/data\n"
        "SHERRY_PROJECT_DIR=/app/data/workspace\n"
        "API_HOST=0.0.0.0\n"
        f"API_PORT={port}\n"
        "SHERRY_AUTH_ENABLED=false\n",
        encoding="utf-8",
    )
    spec = ContainerSpec(
        name=_PROBE,
        image=image,
        data_dir=data_dir,
        env_file=env_file,
        network=None,
        publish=None,
        labels={LABEL_MANAGED: "1", LABEL_TENANT: "e2e"},
    )
    runtime.remove(_PROBE)
    try:
        runtime.create(spec)
        runtime.start(_PROBE)
        assert _wait_http(port), runtime.logs(_PROBE)
        # The cell writes through the bind mount: the host tree sees the file.
        assert (data_dir / "stub-started.txt").read_text(encoding="utf-8") == "ready\n"
        runtime.stop(_PROBE)
        assert runtime.state(_PROBE) == "exited"
    finally:
        runtime.remove(_PROBE)
        runtime.run("rmi", "-f", image, check=False, timeout=120)
