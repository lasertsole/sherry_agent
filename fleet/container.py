"""Docker/Podman CLI adapter for the container cell backend.

The manager never talks to the daemon API: it shells out to the same ``docker``
or ``podman`` binary an operator uses, so a cell is inspectable with the tools
they already have. Everything this module does is one argument vector — the
command construction is pure and unit-testable, and ``run`` is the only place
that touches a process.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: The API port the cell server listens on inside the container.
CELL_PORT = 8080
#: Container name prefix; the tenant id follows.
NAME_PREFIX = "sherry-cell-"
#: Network name prefix; the tenant id follows.
NETWORK_PREFIX = "sherry-cell-"
NETWORK_SUFFIX = "-net"
#: Labels the doctor and the upgrade path use to prove a container is ours.
LABEL_MANAGED = "sherry.fleet.managed"
LABEL_TENANT = "sherry.fleet.tenant"
LABEL_ATTEMPT = "sherry.fleet.attempt"
LABEL_IMAGE = "sherry.fleet.image"


class ContainerError(RuntimeError):
    """The container runtime refused a command or is not installed."""


def _tail(text: str, limit: int = 400) -> str:
    """The last *limit* characters of a command's stderr (one log line)."""
    collapsed = " ".join(text.split())
    return collapsed[-limit:] if len(collapsed) > limit else collapsed


@dataclass(frozen=True)
class ContainerSpec:
    """Everything one ``create`` needs; the command is derived from it."""

    name: str
    image: str
    data_dir: Path
    env_file: Path
    #: Bridge network name for publish mode; ``None`` means host networking.
    network: str | None = None
    publish: tuple[str, int] | None = None
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 1024
    #: ``uid:gid`` for the container process; ``None`` keeps the image default.
    user: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    #: Add ``host.docker.internal`` (the shared model daemons live on the host).
    host_gateway: bool = True

    @property
    def host_network(self) -> bool:
        """True when the cell shares the runtime's network namespace."""
        return self.network is None


class ContainerRuntime:
    """One container engine binary (``docker`` or ``podman``)."""

    def __init__(self, name: str, *, binary: str | None = None, timeout_s: float = 180.0) -> None:
        self.name = name
        self.binary = binary or name
        self.timeout_s = timeout_s

    # ── discovery ───────────────────────────────────────────────────────────

    @classmethod
    def resolve(cls, preference: str = "auto") -> ContainerRuntime:
        """Pick the configured engine; ``auto`` prefers docker, then podman."""
        candidates = ("docker", "podman") if preference == "auto" else (preference,)
        for candidate in candidates:
            path = shutil.which(candidate)
            if path:
                return cls(candidate, binary=path)
        raise ContainerError(
            f"no container runtime found (wanted {preference}); install docker or podman "
            "or set SHERRY_FLEET_CONTAINER_RUNTIME"
        )

    def version(self) -> str:
        """The engine's short version line (doctor output)."""
        result = self.run("--version", timeout=30)
        return result.stdout.strip() or result.stderr.strip()

    # ── process plumbing ────────────────────────────────────────────────────

    def run(
        self,
        *args: str,
        timeout: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one engine command, raising a readable error on failure."""
        command = [self.binary, *args]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.timeout_s,
                check=False,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainerError(f"{self.name} {args[0]}: {exc}") from exc
        if check and result.returncode != 0:
            raise ContainerError(
                f"{self.name} {args[0]} failed ({result.returncode}): {_tail(result.stderr)}"
            )
        return result

    # ── images ──────────────────────────────────────────────────────────────

    def image_exists(self, image: str) -> bool:
        """Whether the image is already in the local store."""
        return self.run("image", "inspect", image, check=False, timeout=60).returncode == 0

    def pull(self, image: str, *, timeout: float | None = 900) -> None:
        """Pull one image (the create/upgrade pull policies gate this)."""
        self.run("pull", image, timeout=timeout)

    # ── networks ────────────────────────────────────────────────────────────

    def network_exists(self, name: str) -> bool:
        """Whether a network with this name exists."""
        return self.run("network", "inspect", name, check=False, timeout=60).returncode == 0

    def network_create(self, name: str) -> None:
        """Create the tenant's bridge network (idempotent)."""
        if self.network_exists(name):
            return
        self.run("network", "create", name, timeout=120)

    def network_remove(self, name: str) -> None:
        """Remove the tenant's bridge network, ignoring a missing one."""
        self.run("network", "rm", "-f", name, check=False, timeout=120)

    # ── containers ──────────────────────────────────────────────────────────

    def exists(self, name: str) -> bool:
        """Whether a container with this name exists (any state)."""
        return self.run("container", "inspect", name, check=False, timeout=60).returncode == 0

    def state(self, name: str) -> str | None:
        """``running`` / ``exited`` / ``created`` …, or None when absent."""
        result = self.run(
            "container", "inspect", "--format", "{{.State.Status}}", name, check=False, timeout=60
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def inspect(self, name: str) -> str:
        """The raw JSON of one container (doctor and upgrade read it)."""
        return self.run("container", "inspect", name, check=False, timeout=60).stdout

    def create_args(self, spec: ContainerSpec) -> list[str]:
        """The full ``create`` argument vector for one cell."""
        args = [
            "create",
            "--name",
            spec.name,
            "--init",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            spec.memory,
            "--cpus",
            spec.cpus,
            "--pids-limit",
            str(spec.pids_limit),
            "--restart",
            "unless-stopped",
        ]
        if spec.user:
            args += ["--user", spec.user]
        if spec.network:
            args += ["--network", spec.network]
        else:
            args += ["--network", "host"]
        if spec.publish:
            address, port = spec.publish
            args += ["-p", f"{address}:{port}:{CELL_PORT}"]
        args += ["--volume", f"{spec.data_dir}:/app/data"]
        if spec.host_gateway:
            args += ["--add-host", "host.docker.internal:host-gateway"]
        args += ["--env-file", str(spec.env_file)]
        for key, value in sorted(spec.labels.items()):
            args += ["--label", f"{key}={value}"]
        args.append(spec.image)
        return args

    def create(self, spec: ContainerSpec) -> None:
        """Create (never start) one cell container from the spec."""
        self.run(*self.create_args(spec), timeout=120)

    def start(self, name: str) -> None:
        """Start a created or stopped container."""
        self.run("start", name, timeout=180)

    def stop(self, name: str, *, timeout_s: int = 10) -> None:
        """Stop a container gracefully, then let the engine kill it."""
        self.run("stop", "-t", str(timeout_s), name, check=False, timeout=timeout_s + 60)

    def remove(self, name: str) -> None:
        """Force-remove a container (missing is not an error)."""
        self.run("container", "rm", "-f", name, check=False, timeout=120)

    def kill(self, name: str) -> None:
        """Send SIGKILL through the engine (best-effort)."""
        self.run("kill", name, check=False, timeout=60)

    def logs(self, name: str, *, tail: int = 120) -> str:
        """The container's last log lines, stdout and stderr concatenated."""
        result = self.run("logs", "--tail", str(tail), name, check=False, timeout=60)
        return (result.stdout or "") + (result.stderr or "")


def default_user() -> str | None:
    """The ``uid:gid`` a cell should run as, or None to keep the image's user.

    The data directory is bind-mounted from the host, so a container running as
    a different uid cannot write it. Reusing the host uid keeps the 0700 data
    directory private without a chown. A Windows host has no POSIX uid and the
    runtime maps bind mounts itself, so the image's own user stays in place.
    """
    if platform.system() == "Windows":
        return None
    try:
        return f"{os.getuid()}:{os.getgid()}"
    except AttributeError:  # pragma: no cover - non-POSIX without Windows
        return None
