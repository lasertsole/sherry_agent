"""Per-tenant process supervisor for the bare-metal deployment.

Each cell is a normal ``python -m server`` process with its own
``SHERRY_HOME``/``SHERRY_PROJECT_DIR`` and a loopback port. The manager owns
only the lifecycle (start/health/stop/remove); the Ingress owns auth/routing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from loguru import logger
from typing import Any
from collections.abc import Sequence

import aiohttp

from config import APP_HOME, ROOT_DIR

from .config import IngressConfig
from .store import FleetStore

try:  # POSIX only; the fleet targets Linux bare metal.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PORT_START = 20100
_PORT_END = 20299


class FleetError(RuntimeError):
    """A tenant could not be created/started/stopped."""


def _tenant_id(value: str) -> str:
    tenant = value.strip()
    if not _TENANT_RE.match(tenant):
        raise FleetError("tenant must be 1-64 chars: letters, digits, dot, dash, underscore")
    return tenant


class FleetManager:
    """Create and supervise one Sherry process per tenant."""

    def __init__(
        self,
        store: FleetStore,
        config: IngressConfig,
        *,
        server_command: Sequence[str] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.server_command = list(server_command) if server_command else None
        #: Serialises port pick + registry write inside this process; the
        #: ``flock`` in ``_port_guard`` does the same across CLI processes.
        self._create_lock = asyncio.Lock()
        self._health_failures: dict[str, int] = {}

    def data_dir(self, tenant: str) -> Path:
        """The tenant's SHERRY_HOME."""
        return self.config.fleet_root / _tenant_id(tenant)

    async def create(
        self,
        tenant: str,
        *,
        port: int | None = None,
        start: bool = True,
        seed_config: bool = False,
    ) -> dict[str, Any]:
        """Create the tenant tree, registry row and optional process."""
        tenant = _tenant_id(tenant)
        if await self.store.get_cell(tenant) is not None:
            raise FleetError(f"tenant {tenant!r} already exists")
        data_dir = self.data_dir(tenant)
        workspace = data_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)
        config_file = data_dir / "sherry.jsonc"
        if not config_file.exists():
            config_file.write_text(
                json.dumps({"project_dir": str(workspace)}) + "\n",
                encoding="utf-8",
            )
        if seed_config:
            self._seed_config(data_dir)
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
            )
            await self.store.set_process(tenant, pid=None, status="stopped", data_dir=str(data_dir))
        if start:
            await self.start(tenant)
        return await self._describe(tenant)

    async def start(self, tenant: str) -> dict[str, Any]:
        """Start one cell and wait for its /health."""
        tenant = _tenant_id(tenant)
        cell = await self.store.get_cell(tenant)
        if cell is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        process = await self.store.get_process(tenant)
        if (
            process is not None
            and process.get("pid")
            and self._pid_alive(int(process["pid"]))
            and self._pid_is_cell(int(process["pid"]), data_dir=self.data_dir(tenant))
        ):
            raise FleetError(f"tenant {tenant!r} is already running")
        data_dir = self.data_dir(tenant)
        log_path = data_dir / "logs" / "cell.log"
        env = os.environ.copy()
        env.update(
            {
                "SHERRY_HOME": str(data_dir),
                "SHERRY_PROJECT_DIR": str(data_dir / "workspace"),
                "API_HOST": "127.0.0.1",
                "API_PORT": str(int(cell["port"])),
                "SHERRY_GATEWAY_TOKEN": str(cell["gateway_token"]),
                "PUBLIC_BASE_URL": self.config.public_base_url,
                "SHERRY_DAEMON_HOME": str(self.config.fleet_root / "daemons"),
                "SHERRY_DAEMON_CLIENT_ONLY": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = self._limited_command(self.server_command or [sys.executable, "-m", "server"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log(log_path)
        with log_path.open("ab") as log:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(ROOT_DIR),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
            except Exception as exc:
                raise FleetError(f"could not start {tenant!r}: {exc}") from exc
        await self.store.set_process(
            tenant,
            pid=proc.pid,
            status="starting",
            data_dir=str(data_dir),
            started_at=int(time.time()),
        )
        if not await self._wait_healthy(int(cell["port"]), proc.pid):
            self._terminate(proc.pid)
            self._reap(proc.pid)
            await self.store.set_process(tenant, pid=None, status="stopped", data_dir=str(data_dir))
            raise FleetError(f"tenant {tenant!r} did not become healthy; see {log_path}")
        await self.store.set_process(
            tenant,
            pid=proc.pid,
            status="running",
            data_dir=str(data_dir),
            started_at=int(time.time()),
        )
        self._health_failures.pop(tenant, None)
        return await self._describe(tenant)

    async def stop(self, tenant: str) -> dict[str, Any]:
        """Stop one cell (SIGTERM, then SIGKILL after a bounded wait)."""
        tenant = _tenant_id(tenant)
        process = await self.store.get_process(tenant)
        if process is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        pid = int(process["pid"]) if process.get("pid") else None
        if pid and self._pid_alive(pid) and self._pid_is_cell(pid, data_dir=self.data_dir(tenant)):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and self._pid_alive(pid):
                await asyncio.sleep(0.2)
            if self._pid_alive(pid) and self._pid_is_cell(pid, data_dir=self.data_dir(tenant)):
                self._terminate(pid)
        if pid:
            self._reap(pid)
        await self.store.set_process(
            tenant, pid=None, status="stopped", data_dir=str(process["data_dir"])
        )
        self._health_failures.pop(tenant, None)
        return await self._describe(tenant)

    async def remove(self, tenant: str) -> None:
        """Stop a cell and delete its data directory."""
        tenant = _tenant_id(tenant)
        process = await self.store.get_process(tenant)
        if process is not None:
            await self.stop(tenant)
        data_dir = self.data_dir(tenant)
        shutil.rmtree(data_dir, ignore_errors=True)
        await self.store.delete_process(tenant)
        await self.store.delete_cell(tenant)

    def backup(self, tenant: str, dest: Path) -> dict[str, Any]:
        """Copy one cell's DBs (online sqlite backup) and files to *dest*."""
        tenant = _tenant_id(tenant)
        source = self.data_dir(tenant)
        if not source.is_dir():
            raise FleetError(f"tenant {tenant!r} has no data directory")
        target = Path(dest)
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if relative.parts and relative.parts[0] == "logs":
                continue
            if path.name.endswith(("-wal", "-shm")):
                continue
            out = target / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".db":
                try:
                    with sqlite3.connect(path, timeout=10) as src, sqlite3.connect(out) as dst:
                        src.backup(dst)
                except sqlite3.Error as exc:
                    raise FleetError(f"sqlite backup failed for {relative}: {exc}") from exc
            else:
                shutil.copy2(path, out)
            copied += 1
        return {"tenant_id": tenant, "backup": str(target), "files": copied}

    async def restore(self, tenant: str, source: Path) -> dict[str, Any]:
        """Replace a cell's data tree from a backup, then restart if it was live."""
        tenant = _tenant_id(tenant)
        backup = Path(source)
        if not backup.is_dir():
            raise FleetError(f"backup directory not found: {backup}")
        process = await self.store.get_process(tenant)
        was_running = bool(
            process
            and process.get("pid")
            and self._pid_alive(int(process["pid"]))
            and self._pid_is_cell(int(process["pid"]), data_dir=self.data_dir(tenant))
        )
        if process is not None:
            await self.stop(tenant)
        data_dir = self.data_dir(tenant)
        staging = data_dir.with_name(data_dir.name + ".restore")
        previous = data_dir.with_name(data_dir.name + ".pre-restore")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(backup, staging)
        if data_dir.exists():
            shutil.rmtree(previous, ignore_errors=True)
            data_dir.rename(previous)
        staging.rename(data_dir)
        if was_running:
            await self.start(tenant)
        return {"tenant_id": tenant, "restored": str(data_dir), "previous": str(previous)}

    def _rotate_log(self, path: Path) -> None:
        """Keep one bounded previous log via copy-truncate.

        The running cell inherited an O_APPEND fd, so truncating in place is
        safe: its next write lands at the new end instead of into the copy.
        Copy-truncate can lose a few interleaved lines while copying, which is
        the trade-off for rotating a log this process does not own.
        """
        try:
            if not path.is_file() or path.stat().st_size <= self.config.log_max_bytes:
                return
            previous = path.with_name(path.name + ".1")
            with path.open("rb") as source, previous.open("wb") as target:
                shutil.copyfileobj(source, target)
            path.write_bytes(b"")
        except OSError:
            logger.debug("fleet: log rotation skipped for {}", path, exc_info=True)

    async def supervise_once(self) -> list[str]:
        """Restart cells whose recorded process vanished or changed identity."""
        restarted: list[str] = []
        for cell in await self.store.list_cells():
            tenant = str(cell["tenant_id"])
            process = await self.store.get_process(tenant)
            if process is None or process.get("status") not in {"running", "starting"}:
                continue
            # Rotate even while healthy: a long-lived cell would otherwise grow
            # its log until the next restart.
            data_dir = process.get("data_dir")
            if data_dir:
                self._rotate_log(Path(str(data_dir)) / "logs" / "cell.log")
            pid = process.get("pid")
            if (
                pid
                and self._pid_alive(int(pid))
                and self._pid_is_cell(int(pid), data_dir=self.data_dir(tenant))
            ):
                if process.get("status") != "running" or await self._health_ok(int(cell["port"])):
                    self._health_failures.pop(tenant, None)
                    continue
                # A live process can still be wedged. Three consecutive failed
                # probes (one tick apart) is long enough to ride out a pause,
                # short enough to recover a hung cell without an operator.
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
            if pid:
                # A crashed cell may still be an unreaped zombie (kill(pid, 0)
                # succeeds until it is collected), so collect it before the
                # replacement takes the port.
                self._reap(int(pid))
            try:
                await self.start(tenant)
                restarted.append(tenant)
            except FleetError as exc:
                logger.warning("fleet: restart of {} failed: {}", tenant, exc)
        return restarted

    async def supervise_forever(self, interval_s: float | None = None) -> None:
        """Poll cell health until cancelled (Ingress startup task)."""
        interval = self.config.supervise_interval_s if interval_s is None else interval_s
        while True:
            try:
                await self.supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a supervisor tick must not die
                logger.exception("fleet: supervisor tick failed")
            await asyncio.sleep(max(1.0, interval))

    async def list(self) -> list[dict[str, Any]]:
        """List tenant cells with their supervisor state."""
        result: list[dict[str, Any]] = []
        for cell in await self.store.list_cells():
            process = await self.store.get_process(str(cell["tenant_id"]))
            result.append(
                {
                    "tenant_id": cell["tenant_id"],
                    "host": cell["host"],
                    "port": cell["port"],
                    "status": (process or {}).get("status", "unknown"),
                    "pid": (process or {}).get("pid"),
                }
            )
        return result

    async def _describe(self, tenant: str) -> dict[str, Any]:
        cell = await self.store.get_cell(tenant)
        process = await self.store.get_process(tenant)
        if cell is None:
            raise FleetError(f"unknown tenant {tenant!r}")
        return {
            "tenant_id": tenant,
            "host": cell["host"],
            "port": int(cell["port"]),
            "data_dir": str(self.data_dir(tenant)),
            "status": (process or {}).get("status", "unknown"),
            "pid": (process or {}).get("pid"),
        }

    def _limited_command(self, command: list[str]) -> list[str]:
        """Apply per-cell ``ulimit`` bounds through a small exec wrapper.

        CPU quotas are a cgroup/systemd concern; address space and open files
        are the limits a runaway cell hits first and ``ulimit`` needs no
        privileges. ``exec`` keeps the recorded PID equal to the server PID.
        """
        limits: list[str] = []
        if self.config.cell_memory_mb > 0:
            limits.append(f"ulimit -v {self.config.cell_memory_mb * 1024}")
        if self.config.cell_nofile > 0:
            limits.append(f"ulimit -n {self.config.cell_nofile}")
        if not limits:
            return command
        return ["/bin/sh", "-c", "; ".join(limits) + '; exec "$@"', "sherry-cell", *command]

    @asynccontextmanager
    async def _port_guard(self):
        """Serialise port selection + registration across processes.

        The registry row is the authority, but a second CLI process can pick
        the same free port between the socket probe and the INSERT. A
        fleet-root ``flock`` closes that window on the same host.
        """
        async with self._create_lock:
            handle = None
            try:
                if _fcntl is not None:
                    lock_path = self.config.fleet_root / ".ports.lock"
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    handle = lock_path.open("a+")
                    await asyncio.to_thread(_fcntl.flock, handle.fileno(), _fcntl.LOCK_EX)
                yield
            finally:
                if handle is not None and _fcntl is not None:
                    try:
                        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                    finally:
                        handle.close()

    async def _port_registered(self, port: int) -> bool:
        return any(int(cell["port"]) == port for cell in await self.store.list_cells())

    async def _pick_port(self) -> int:
        used = {int(cell["port"]) for cell in await self.store.list_cells()}
        for port in range(_PORT_START, _PORT_END + 1):
            if port in used:
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            finally:
                sock.close()
            return port
        raise FleetError("no free tenant port in 20100-20299")

    async def _health_ok(self, port: int) -> bool:
        """One short readiness probe for the supervisor."""
        timeout = aiohttp.ClientTimeout(total=2)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as response:
                    return response.status == 200
        except (TimeoutError, aiohttp.ClientError):
            return False

    async def _wait_healthy(self, port: int, pid: int) -> bool:
        deadline = time.monotonic() + self.config.health_timeout_s
        timeout = aiohttp.ClientTimeout(total=2)
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return False
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"http://127.0.0.1:{port}/health") as response:
                        if response.status == 200:
                            return True
            except (TimeoutError, aiohttp.ClientError):
                continue
            await asyncio.sleep(1.0)
        return False

    @staticmethod
    def _pid_is_cell(pid: int, *, data_dir: Path) -> bool:
        """Verify the PID still carries this cell's SHERRY_HOME.

        A stored PID can be reused by an unrelated process after a crash. A
        bare existence check would then SIGTERM the wrong program, so a PID we
        cannot positively associate with the cell is treated as foreign.
        """
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil is a project dependency
            return False
        try:
            process = psutil.Process(pid)
            return str(process.environ().get("SHERRY_HOME", "")) == str(data_dir)
        except (psutil.Error, OSError):
            return False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _terminate(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    @staticmethod
    def _reap(pid: int) -> None:
        """Collect a dead child so a restart does not leave zombies behind."""
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    @staticmethod
    def _seed_config(data_dir: Path) -> None:
        for name in (".models", ".network"):
            source = APP_HOME / name
            target = data_dir / name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
