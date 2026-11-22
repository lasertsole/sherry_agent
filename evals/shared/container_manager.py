"""Per-suite container lifecycle over the shared harness primitives.

``external_bench`` owns the raw docker/podman calls; this module adds the two
shapes the new suites need on top:

* :class:`TaskContainer` — the create → bridge → remove lifecycle one task's
  image goes through (the deepswe suite inlines this; every new suite shares
  it), with work-root staging outside the checkout;
* :func:`compose_up` / :func:`compose_down` — the ``docker compose`` /
  ``podman compose`` pair WebArena / VisualWebArena need for their resident
  web applications (a compose project lives ACROSS tasks, so it is started
  once per run and torn down at the end).

Fail-open rules: a missing container binary degrades with a clear detail
string (the suite skips), and a compose project that is already running is a
no-op rather than an error.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals import external_bench

__all__ = ["TaskContainer", "compose_up", "compose_down", "compose_command"]

_WORK_ROOT_DEFAULT = Path(external_bench.__file__).parent / "results" / "_work"


def work_root(suite: str, results_dir: Path, override: str = "") -> Path:
    """Where task workdirs live for *suite* — OUTSIDE the sherry checkout.

    The agent's self-path guard refuses writes inside the product checkout
    (the same lesson the deepswe suite records); ``SHERRY_EVAL_WORK_ROOT``
    overrides the temp-dir default.
    """
    import tempfile

    del suite  # kept for call-site readability: one root per suite per run
    base = (
        Path(override) if override else Path(tempfile.gettempdir()) / "sherry_evals" / "bench-work"
    )
    return base / results_dir.name


@dataclass
class TaskContainer:
    """One task's container: extract /app to the host, start, expose a bridge."""

    suite: str
    task_id: str
    image: str
    host_workdir: Path
    container_workdir: str = "/app"
    network: str | None = None
    name: str = ""
    container: str = ""
    bridge: Any = None
    extra_env: dict[str, str] = field(default_factory=dict)

    def prepare(self) -> None:
        """Extract the image's workdir to the host and start the container."""
        self.host_workdir.mkdir(parents=True, exist_ok=True)
        external_bench.extract_workdir(
            self.image,
            self.container_workdir,
            self.host_workdir,
            name=external_bench.container_name(f"{self.suite}-extract", self.task_id),
        )
        self.container = external_bench.start_agent_container(
            self.image,
            self.name or external_bench.container_name(self.suite, self.task_id),
            host_workdir=self.host_workdir,
            workdir=self.container_workdir,
            network=self.network,
            env=self.extra_env or None,
        )

    def connect(self) -> Any:
        """Build (and return) the DockerBridge for this task's container."""
        self.bridge = external_bench.DockerBridge(
            self.container,
            host_workdir=self.host_workdir,
            container_workdir=(
                self.container_workdir if external_bench.container_bin() == "podman" else None
            ),
        )
        external_bench.install_terminal_bridge(self.bridge)
        return self.bridge

    def cleanup(self, *, keep_workdir: bool = False) -> None:
        """Remove the container and (unless kept) the staged workdir."""
        if self.container:
            external_bench.remove_container(self.container)
            self.container = ""
        if not keep_workdir:
            shutil.rmtree(self.host_workdir, ignore_errors=True)


def compose_command() -> list[str]:
    """``docker compose`` / ``podman compose`` as an argv prefix, or [] when
    neither is available (the caller skips with the detail)."""
    binary = external_bench.container_bin()
    probe = external_bench.run([binary, "compose", "version"], timeout=30.0)
    if not probe.ok:
        return []
    return [binary, "compose"]


def compose_up(
    compose_file: Path, project: str, *, wait_seconds: float = 600.0
) -> tuple[bool, str]:
    """Start one compose project (idempotent: an already-up project is fine)."""
    argv = compose_command()
    if not argv:
        return False, f"{external_bench.container_bin()} has no compose plugin"
    result = external_bench.run(
        [*argv, "-f", str(compose_file), "-p", project, "up", "-d", "--wait"],
        timeout=wait_seconds,
    )
    if not result.ok:
        # --wait is unsupported by some compose versions; retry without it.
        result = external_bench.run(
            [*argv, "-f", str(compose_file), "-p", project, "up", "-d"],
            timeout=wait_seconds,
        )
    return result.ok, result.output[-2000:]


def compose_down(compose_file: Path, project: str) -> None:
    """Tear one compose project down (best-effort, never raises)."""
    argv = compose_command()
    if not argv:
        return
    external_bench.run(
        [*argv, "-f", str(compose_file), "-p", project, "down", "--volumes"],
        timeout=600.0,
    )
