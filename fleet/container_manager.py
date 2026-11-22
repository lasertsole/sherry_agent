"""Container backend: one Docker/Podman container per tenant.

Same lifecycle surface as the bare-metal :class:`fleet.manager.FleetManager`
(create / start / stop / remove / backup / restore / supervise), but the cell
runs inside its own container: an isolated filesystem, an isolated network and
cgroup-bounded CPU, memory and pids. The Ingress stays unchanged — a cell is
still ``127.0.0.1:<port>``, whether the port is published from an isolated
bridge (``network_mode=publish``) or the container shares the host namespace
(``network_mode=host``, the WSL2 path where netavark cannot apply nft rules).

The data directory is the SAME host tree the process backend uses, bind-mounted
at ``/app/data``, so backup/restore stay file-level operations and an operator
can switch a tenant between backends by pointing either one at that tree.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

from .config import IngressConfig
from .container import (
    CELL_PORT,
    LABEL_ATTEMPT,
    LABEL_IMAGE,
    LABEL_MANAGED,
    LABEL_TENANT,
    NAME_PREFIX,
    NETWORK_PREFIX,
    NETWORK_SUFFIX,
    ContainerError,
    ContainerRuntime,
    ContainerSpec,
    default_user,
)
from .manager import FleetError, FleetManager, _tenant_id
from .store import FleetStore

#: Container stat name for the data directory (the plan's 0700 rule).
DATA_DIR_MODE = 0o700
#: ``--cap-drop=ALL`` reaches ``inspect`` EXPANDED on podman (docker normally
#: reports the literal ``ALL``), and the expanded set is engine/rootless
#: dependent (11 entries on podman 6 rootful here, 14 on docker). The check is
#: therefore an invariant subset every default set carries: without these six a
#: process cannot chown a file, switch uid/gid, signal another process or
#: chroot — i.e. the capabilities an escape needs first.
CORE_CAPABILITIES = frozenset({"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID", "KILL", "SYS_CHROOT"})


def all_capabilities_dropped(host_config: dict[str, Any]) -> tuple[bool, str]:
    """Whether the container runs with no meaningful capabilities."""
    added = [str(item).upper().removeprefix("CAP_") for item in (host_config.get("CapAdd") or [])]
    dropped = {
        str(item).upper().removeprefix("CAP_") for item in (host_config.get("CapDrop") or [])
    }
    detail = ",".join(sorted(dropped)) or "none"
    if added:
        return False, f"added {','.join(sorted(added))}"
    return "ALL" in dropped or CORE_CAPABILITIES <= dropped, detail


#: How long the cell may keep enforcing auth after the fleet flips the row.
_AUTH_CACHE_GRACE_S = 2.5


def network_mode(value: str) -> str:
    """Resolve ``auto`` to the mode this host can actually use.

    A Windows fleet host drives a runtime inside a VM; netavark there cannot
    apply the port-publish ruleset (the WSL2 kernel ships without nftables), so
    the cell shares the runtime's network namespace and listens on its assigned
    port directly. Linux/macOS hosts get the isolated bridge.
    """
    mode = (value or "auto").strip().lower()
    if mode in {"publish", "host"}:
        return mode
    return "host" if platform.system() == "Windows" else "publish"


class FleetContainerManager(FleetManager):
    """Create and supervise one container per tenant."""

    backend = "container"

    def __init__(
        self,
        store: FleetStore,
        config: IngressConfig,
        *,
        runtime: ContainerRuntime | None = None,
        image: str | None = None,
        network: str | None = None,
    ) -> None:
        super().__init__(store, config)
        self._runtime = runtime
        self.image = image or config.container_image
        self.network_mode = network_mode(network or config.container_network)
        self._user = default_user()

    @property
    def runtime(self) -> ContainerRuntime:
        """The engine, resolved lazily so ``list``/``doctor`` stay read-only."""
        if self._runtime is None:
            try:
                self._runtime = ContainerRuntime.resolve(self.config.container_runtime)
            except ContainerError as exc:
                raise FleetError(str(exc)) from exc
        return self._runtime

    # ── naming ──────────────────────────────────────────────────────────────

    @staticmethod
    def container_name(tenant: str) -> str:
        """The deterministic container name for one tenant."""
        return f"{NAME_PREFIX}{tenant}"

    @staticmethod
    def network_name(tenant: str) -> str:
        """The deterministic per-tenant bridge network name."""
        return f"{NETWORK_PREFIX}{tenant}{NETWORK_SUFFIX}"

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def create(
        self,
        tenant: str,
        *,
        port: int | None = None,
        start: bool = True,
        seed_config: bool = False,
        image: str | None = None,
    ) -> dict[str, Any]:
        """Create the tenant tree, container and registry row."""
        tenant = _tenant_id(tenant)
        if await self.store.get_cell(tenant) is not None:
            raise FleetError(f"tenant {tenant!r} already exists")
        chosen_image = image or self.image
        self._ensure_image(chosen_image)
        data_dir = self.prepare_tree(tenant, seed_config=seed_config)
        os.chmod(data_dir, DATA_DIR_MODE)
        async with self._port_guard():
            if await self.store.get_cell(tenant) is not None:
                raise FleetError(f"tenant {tenant!r} already exists")
            chosen = int(port) if port is not None else await self._pick_port()
            if await self._port_registered(chosen):
                raise FleetError(f"port {chosen} is already registered to a cell")
            await self.store.upsert_cell(
                tenant,
                host="127.0.0.1",
                port=chosen,
                gateway_token=secrets.token_urlsafe(32),
                runtime="container",
                image=chosen_image,
                container_name=self.container_name(tenant),
            )
            await self.store.set_process(tenant, pid=None, status="stopped", data_dir=str(data_dir))
        if start:
            try:
                await self.start(tenant)
            except FleetError:
                await self.remove(tenant)
                raise
        return await self._describe(tenant)

    async def start(self, tenant: str) -> dict[str, Any]:
        """Create (if needed) and start the container, then wait for /health."""
        tenant = _tenant_id(tenant)
        cell = await self.store.get_cell(tenant)
        if cell is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        if cell.get("runtime") != "container":
            raise FleetError(f"tenant {tenant!r} is not a container cell")
        name = str(cell.get("container_name") or self.container_name(tenant))
        state = self.runtime.state(name)
        if state == "running":
            raise FleetError(f"tenant {tenant!r} is already running")
        data_dir = self.data_dir(tenant)
        port = int(cell["port"])
        image = str(cell.get("image") or self.image)
        self._ensure_image(image)
        attempt = secrets.token_hex(8)
        if state is None:
            # A container that vanished (runtime pruned it) is recreated from the
            # registry, never adopted: its labels could belong to an old attempt.
            if self.network_mode == "publish":
                self.runtime.network_create(self.network_name(tenant))
            spec = self._spec(
                tenant,
                image=image,
                data_dir=data_dir,
                port=port,
                attempt=attempt,
            )
            env_file = self._write_env_file(data_dir, cell, port)
            try:
                self.runtime.create(spec)
            finally:
                env_file.unlink(missing_ok=True)
        await self.store.set_process(tenant, pid=None, status="starting", data_dir=str(data_dir))
        try:
            self.runtime.start(name)
        except ContainerError as exc:
            await self.store.set_process(tenant, pid=None, status="stopped", data_dir=str(data_dir))
            raise FleetError(f"could not start {tenant!r}: {exc}") from exc
        if not await self._wait_container_healthy(port, name, attempt):
            logs = self.runtime.logs(name)
            self.runtime.remove(name)
            await self.store.set_process(tenant, pid=None, status="stopped", data_dir=str(data_dir))
            raise FleetError(
                f"tenant {tenant!r} did not become healthy; container log tail: "
                f"{' '.join(logs.split())[-600:]}"
            )
        await self._disable_cell_auth(data_dir, port, str(cell["gateway_token"]))
        await self.store.set_process(
            tenant,
            pid=None,
            status="running",
            data_dir=str(data_dir),
            started_at=int(time.time()),
        )
        self._health_failures.pop(tenant, None)
        return await self._describe(tenant)

    async def stop(self, tenant: str) -> dict[str, Any]:
        """Stop the container; the data directory and network stay in place."""
        tenant = _tenant_id(tenant)
        cell = await self.store.get_cell(tenant)
        process = await self.store.get_process(tenant)
        if cell is None or process is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        name = str(cell.get("container_name") or self.container_name(tenant))
        if self.runtime.state(name) is not None:
            self.runtime.stop(name)
        await self.store.set_process(
            tenant, pid=None, status="stopped", data_dir=str(process["data_dir"])
        )
        self._health_failures.pop(tenant, None)
        return await self._describe(tenant)

    async def remove(self, tenant: str) -> None:
        """Remove the container, its network and the tenant's data directory."""
        tenant = _tenant_id(tenant)
        cell = await self.store.get_cell(tenant)
        if cell is not None:
            name = str(cell.get("container_name") or self.container_name(tenant))
            self.runtime.remove(name)
            if self.network_mode == "publish" or cell.get("runtime") == "container":
                self.runtime.network_remove(self.network_name(tenant))
        shutil.rmtree(self.data_dir(tenant), ignore_errors=True)
        await self.store.delete_process(tenant)
        await self.store.delete_cell(tenant)

    # ── upgrade ─────────────────────────────────────────────────────────────

    async def upgrade(self, tenant: str, image: str | None = None) -> dict[str, Any]:
        """Recreate a tenant's container on a new image, rolling back on failure."""
        tenant = _tenant_id(tenant)
        cell = await self.store.get_cell(tenant)
        if cell is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        if cell.get("runtime") != "container":
            raise FleetError(f"tenant {tenant!r} is not a container cell")
        previous_image = str(cell.get("image") or self.image)
        target_image = image or self.image
        if target_image == previous_image and self.runtime.image_exists(target_image):
            return await self._describe(tenant)
        self._ensure_image(target_image, force_pull=self.config.container_pull != "never")
        name = str(cell.get("container_name") or self.container_name(tenant))
        was_running = self.runtime.state(name) == "running"
        if was_running:
            await self.stop(tenant)
        self.runtime.remove(name)
        await self.store.upsert_cell(
            tenant,
            host=str(cell["host"]),
            port=int(cell["port"]),
            gateway_token=str(cell["gateway_token"]),
            runtime="container",
            image=target_image,
            container_name=name,
        )
        try:
            return await self.start(tenant)
        except FleetError as exc:
            logger.error("fleet: upgrade of {} to {} failed: {}", tenant, target_image, exc)
            await self.store.upsert_cell(
                tenant,
                host=str(cell["host"]),
                port=int(cell["port"]),
                gateway_token=str(cell["gateway_token"]),
                runtime="container",
                image=previous_image,
                container_name=name,
            )
            self.runtime.remove(name)
            try:
                result = await self.start(tenant)
            except FleetError as rollback_exc:
                raise FleetError(
                    f"upgrade of {tenant!r} to {target_image} failed ({exc}); rollback to "
                    f"{previous_image} also failed: {rollback_exc}"
                ) from exc
            result["rolled_back_from"] = target_image
            return result

    # ── supervision ─────────────────────────────────────────────────────────

    async def supervise_once(self) -> list[str]:
        """Restart container cells that stopped or stopped answering /health."""
        restarted: list[str] = []
        for cell in await self.store.list_cells():
            if cell.get("runtime") != "container":
                # A process cell registered in the same store is left to the
                # process supervisor; the CLI never mixes them in one manager.
                continue
            tenant = str(cell["tenant_id"])
            process = await self.store.get_process(tenant)
            if process is None or process.get("status") not in {"running", "starting"}:
                continue
            name = str(cell.get("container_name") or self.container_name(tenant))
            if self.runtime.state(name) != "running":
                logger.warning("fleet: {} container is not running; restarting", tenant)
                try:
                    await self.start(tenant)
                    restarted.append(tenant)
                except FleetError as exc:
                    logger.warning("fleet: restart of {} failed: {}", tenant, exc)
                continue
            if await self._health_ok(int(cell["port"])):
                self._health_failures.pop(tenant, None)
                continue
            failures = self._health_failures.get(tenant, 0) + 1
            self._health_failures[tenant] = failures
            if failures < 3:
                continue
            logger.warning("fleet: {} is unresponsive; restarting", tenant)
            self._health_failures.pop(tenant, None)
            try:
                await self.stop(tenant)
            except FleetError as exc:
                logger.warning("fleet: stop of hung {} failed: {}", tenant, exc)
            try:
                await self.start(tenant)
                restarted.append(tenant)
            except FleetError as exc:
                logger.warning("fleet: restart of {} failed: {}", tenant, exc)
        return restarted

    # ── reporting ───────────────────────────────────────────────────────────

    async def list(self) -> list[dict[str, Any]]:
        """List cells with backend, container state and process status."""
        result: list[dict[str, Any]] = []
        for cell in await self.store.list_cells():
            tenant = str(cell["tenant_id"])
            process = await self.store.get_process(tenant)
            backend = str(cell.get("runtime") or "process")
            entry: dict[str, Any] = {
                "tenant_id": tenant,
                "backend": backend,
                "host": cell["host"],
                "port": cell["port"],
                "status": (process or {}).get("status", "unknown"),
                "pid": (process or {}).get("pid"),
                "image": cell.get("image"),
                "container": cell.get("container_name"),
            }
            if backend == "container":
                try:
                    entry["container_state"] = self.runtime.state(
                        str(cell.get("container_name") or self.container_name(tenant))
                    )
                except FleetError:
                    entry["container_state"] = None
            result.append(entry)
        return result

    async def doctor(self, tenant: str | None = None) -> list[dict[str, Any]]:
        """Audit the container cells (existence, hardening, isolation, health)."""
        report: list[dict[str, Any]] = []
        wanted = _tenant_id(tenant) if tenant else None
        for cell in await self.store.list_cells():
            if cell.get("runtime") != "container":
                continue
            current = str(cell["tenant_id"])
            if wanted and current != wanted:
                continue
            report.append(await self._audit(current, cell))
        return report

    async def _audit(self, tenant: str, cell: dict[str, Any]) -> dict[str, Any]:
        name = str(cell.get("container_name") or self.container_name(tenant))
        checks: list[dict[str, Any]] = []

        def check(label: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": label, "ok": bool(ok), "detail": detail})

        state = self.runtime.state(name)
        check("container exists", state is not None, name)
        check("container running", state == "running", state or "absent")
        data = self._inspect(name) if state is not None else None
        if data is not None:
            host = data.get("HostConfig") or {}
            labels = (data.get("Config") or {}).get("Labels") or {}
            security = [str(item).lower() for item in (host.get("SecurityOpt") or [])]
            dropped, detail = all_capabilities_dropped(host)
            check("cap-drop=ALL", dropped, detail)
            check(
                "no-new-privileges",
                any("no-new-privileges" in item for item in security),
                ",".join(security) or "none",
            )
            check("memory limit", int(host.get("Memory") or 0) > 0, str(host.get("Memory") or 0))
            check("cpu limit", int(host.get("NanoCpus") or 0) > 0, str(host.get("NanoCpus") or 0))
            check(
                "pids limit",
                int(host.get("PidsLimit") or 0) > 0,
                str(host.get("PidsLimit") or 0),
            )
            check("managed label", str(labels.get(LABEL_MANAGED) or "") == "1", str(labels))
            check(
                "tenant label",
                str(labels.get(LABEL_TENANT) or "") == tenant,
                str(labels.get(LABEL_TENANT) or ""),
            )
            if self.network_mode == "publish":
                check(
                    "isolated network",
                    str(host.get("NetworkMode") or "") == self.network_name(tenant),
                    str(host.get("NetworkMode") or ""),
                )
                binding = (host.get("PortBindings") or {}).get(f"{CELL_PORT}/tcp") or []
                bound = binding[0] if binding else {}
                check(
                    "loopback publish",
                    str(bound.get("HostIp") or "") == "127.0.0.1"
                    and str(bound.get("HostPort") or "") == str(cell["port"]),
                    f"{bound.get('HostIp')}:{bound.get('HostPort')}",
                )
                attached = self._network_containers(self.network_name(tenant))
                check("single tenant network", attached <= 1, str(attached))
            else:
                check(
                    "host network",
                    str(host.get("NetworkMode") or "") == "host",
                    str(host.get("NetworkMode") or ""),
                )
        data_dir = self.data_dir(tenant)
        if os.name == "posix":
            mode = data_dir.stat().st_mode & 0o777 if data_dir.is_dir() else 0
            check("data directory 0700", mode == DATA_DIR_MODE, oct(mode))
        check("health endpoint", await self._health_ok(int(cell["port"])), str(cell["port"]))
        return {
            "tenant_id": tenant,
            "container": name,
            "ok": all(item["ok"] for item in checks),
            "checks": checks,
        }

    # ── internals ───────────────────────────────────────────────────────────

    def _spec(
        self,
        tenant: str,
        *,
        image: str,
        data_dir: Path,
        port: int,
        attempt: str,
    ) -> ContainerSpec:
        host_mode = self.network_mode == "host"
        return ContainerSpec(
            name=self.container_name(tenant),
            image=image,
            data_dir=data_dir,
            env_file=data_dir / ".cell.env",
            network=None if host_mode else self.network_name(tenant),
            publish=None if host_mode else ("127.0.0.1", port),
            memory=self.config.container_memory,
            cpus=self.config.container_cpus,
            pids_limit=self.config.container_pids_limit,
            user=self._user,
            labels={
                LABEL_MANAGED: "1",
                LABEL_TENANT: tenant,
                LABEL_ATTEMPT: attempt,
                LABEL_IMAGE: image,
            },
            host_gateway=True,
        )

    def _write_env_file(self, data_dir: Path, cell: dict[str, Any], port: int) -> Path:
        """Write the per-cell env file (mode 0600) that ``create`` consumes."""
        path = data_dir / ".cell.env"
        listen_port = str(port) if self.network_mode == "host" else str(CELL_PORT)
        lines = [
            "SHERRY_HOME=/app/data",
            "SHERRY_PROJECT_DIR=/app/data/workspace",
            f"SHERRY_GATEWAY_TOKEN={cell['gateway_token']}",
            "API_HOST=0.0.0.0",
            f"API_PORT={listen_port}",
            "SHERRY_AUTH_ENABLED=false",
            f"PUBLIC_BASE_URL={self.config.public_base_url}",
            "LOCAL_DAEMON_HOST=host.docker.internal",
            "SHERRY_DAEMON_HOME=/app/data/daemons",
            "SHERRY_DAEMON_CLIENT_ONLY=1",
            "PYTHONUNBUFFERED=1",
        ]
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def _ensure_image(self, image: str, *, force_pull: bool | None = None) -> None:
        """Honour the pull policy before a create/upgrade."""
        policy = self.config.container_pull
        pull = force_pull if force_pull is not None else policy == "always"
        if policy == "never" and not force_pull:
            if not self.runtime.image_exists(image):
                raise FleetError(f"image {image!r} is missing and pull policy is 'never'")
            return
        if pull or (policy == "missing" and not self.runtime.image_exists(image)):
            try:
                self.runtime.pull(image)
            except ContainerError as exc:
                raise FleetError(f"could not pull image {image!r}: {exc}") from exc

    async def _wait_container_healthy(self, port: int, name: str, attempt: str) -> bool:
        """Poll /health, then prove the answering container is THIS attempt."""
        deadline = time.monotonic() + self.config.health_timeout_s
        timeout = aiohttp.ClientTimeout(total=2)
        while time.monotonic() < deadline:
            if self.runtime.state(name) != "running":
                return False
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"http://127.0.0.1:{port}/health") as response:
                        if response.status == 200:
                            return self._attempt_matches(name, attempt)
            except (TimeoutError, aiohttp.ClientError):
                pass
            await asyncio.sleep(1.0)
        return False

    def _attempt_matches(self, name: str, attempt: str) -> bool:
        """The health that answered must come from the container we just made."""
        data = self._inspect(name)
        if data is None:
            return False
        labels = (data.get("Config") or {}).get("Labels") or {}
        return str(labels.get(LABEL_ATTEMPT) or "") == attempt

    def _inspect(self, name: str) -> dict[str, Any] | None:
        raw = self.runtime.inspect(name)
        if not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return parsed[0] if parsed else None
        return parsed if isinstance(parsed, dict) else None

    def _network_containers(self, name: str) -> int:
        result = self.runtime.run(
            "network", "inspect", name, "--format", "{{len .Containers}}", check=False, timeout=60
        )
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return 0

    async def _disable_cell_auth(self, data_dir: Path, port: int, token: str) -> None:
        """Make sure the cell never gates Ingress traffic with a login.

        A fresh cell seeds ``auth_settings.enabled=0`` from
        ``SHERRY_AUTH_ENABLED=false``, so this is a no-op in the normal case. A
        restored cell can carry an ENABLED login from its backup, and the
        Ingress arrives from the bridge gateway (not loopback), so the login
        gate would answer 401 to every proxied request. There is no
        password-free API to switch it off, and the fleet owns the data
        directory — so the row is flipped at rest and the cell's 2 s state cache
        is given time to expire.
        """
        status = await self._auth_status(port, token)
        if status is None or not status.get("auth_required"):
            return
        database = data_dir / "src" / "data" / "auth.db"
        if not database.is_file():
            raise FleetError(
                f"{data_dir} reports login protection but has no auth.db; "
                "restore it again or clear the login in the cell"
            )
        try:
            with sqlite3.connect(database, timeout=10) as connection:
                connection.execute(
                    "UPDATE auth_settings SET enabled = 0, updated_at = ?",
                    (int(time.time() * 1000),),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise FleetError(f"could not disable the cell login at rest: {exc}") from exc
        logger.warning(
            "fleet: {} carried an enabled login; disabled it at rest for container mode", data_dir
        )
        await asyncio.sleep(_AUTH_CACHE_GRACE_S)
        status = await self._auth_status(port, token)
        if status is not None and status.get("auth_required"):
            raise FleetError(
                f"cell at {data_dir} still requires login after the fleet disabled it; "
                "a container cell cannot serve Ingress traffic behind a login"
            )

    async def _auth_status(self, port: int, token: str) -> dict[str, Any] | None:
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/auth/status", headers={"token": token}
                ) as response:
                    if response.status != 200:
                        return None
                    payload = await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _describe(self, tenant: str) -> dict[str, Any]:
        cell = await self.store.get_cell(tenant)
        process = await self.store.get_process(tenant)
        if cell is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        name = str(cell.get("container_name") or self.container_name(tenant))
        return {
            "tenant_id": tenant,
            "backend": "container",
            "host": cell["host"],
            "port": int(cell["port"]),
            "data_dir": str(self.data_dir(tenant)),
            "image": cell.get("image"),
            "container": name,
            "container_state": self.runtime.state(name),
            "network_mode": self.network_mode,
            "status": (process or {}).get("status", "unknown"),
            "pid": (process or {}).get("pid"),
        }
