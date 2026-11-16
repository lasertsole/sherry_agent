"""Host-level owner for the shared local-model daemons.

Cells run with ``SHERRY_DAEMON_CLIENT_ONLY=1`` and a shared
``SHERRY_DAEMON_HOME``; they connect to these processes and never spawn or
atexit-kill one themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from config import APP_HOME
from config.features import LOCAL_DAEMONS
from models.daemon import base as daemon_base
from models.daemon.base import DaemonSpec

from .config import IngressConfig


@dataclass(frozen=True)
class _DaemonFamily:
    """Static mapping from a family name to its port/import target/pid file."""

    name: str
    port_key: str
    import_fn: str
    pid_name: str
    ready_key: str


_FAMILIES: dict[str, _DaemonFamily] = {
    "embed": _DaemonFamily(
        "embedding",
        "embed_port",
        "models.embed_model.text.daemon",
        ".embed_daemon.pid",
        "embed_ready_wait_s",
    ),
    "reranker": _DaemonFamily(
        "reranker",
        "reranker_port",
        "models.reranker_model.daemon",
        ".reranker_daemon.pid",
        "reranker_ready_wait_s",
    ),
    "auxiliary": _DaemonFamily(
        "local chat",
        "auxiliary_port",
        "models.LLMs.local_daemon.server",
        ".llm_daemon.pid",
        "auxiliary_ready_wait_s",
    ),
    "vision": _DaemonFamily(
        "local vision",
        "vision_port",
        "models.LLMs.local_daemon.server",
        ".vision_daemon.pid",
        "vision_ready_wait_s",
    ),
}


class FleetDaemons:
    """Start/stop/status for the host's single copy of each daemon."""

    def __init__(self, config: IngressConfig) -> None:
        self.config = config
        self.root = config.fleet_root / "daemons"
        self.host = str(LOCAL_DAEMONS["host"])

    def family(self, name: str) -> DaemonSpec:
        """Build the spec for one family (pid path under the shared root)."""
        family = _FAMILIES.get(name)
        if family is None:
            raise ValueError(f"unknown daemon family {name!r}; expected {sorted(_FAMILIES)}")
        self.root.mkdir(parents=True, exist_ok=True)
        return DaemonSpec(
            name=family.name,
            host=self.host,
            port=int(LOCAL_DAEMONS[family.port_key]),
            pid_path=self.root / family.pid_name,
            import_fn=family.import_fn,
            ready_wait_s=float(LOCAL_DAEMONS[family.ready_key]),
            liveness_timeout_s=float(LOCAL_DAEMONS["liveness_timeout_s"]),
        )

    def start(self, families: list[str] | None = None) -> list[dict[str, Any]]:
        """Start each requested family (idempotent when already ready)."""
        return [self._start_one(name) for name in (families or sorted(_FAMILIES))]

    def _start_one(self, name: str) -> dict[str, Any]:
        spec = self.family(name)
        if daemon_base.is_ready(spec):
            return self._status(name, spec, ready=True)
        extra_env = {
            "SHERRY_HOME": str(APP_HOME),
            "SHERRY_DAEMON_HOME": str(self.root),
        }
        if name in {"auxiliary", "vision"}:
            extra_env["SHERRY_LOCAL_LLM_DAEMON_PORT"] = str(spec.port)
            model_env = f"SHERRY_FLEET_{name.upper()}_MODEL_PATH"
            model_path = os.environ.get(model_env, "").strip()
            if not model_path:
                return self._missing(name, spec, model_env)
            extra_env["SHERRY_LOCAL_LLM_MODEL_PATH"] = model_path
            if name == "vision":
                mmproj_env = "SHERRY_FLEET_VISION_MMPROJ_PATH"
                mmproj_path = os.environ.get(mmproj_env, "").strip()
                if not mmproj_path:
                    return self._missing(name, spec, mmproj_env)
                extra_env["SHERRY_LOCAL_LLM_MMPROJ_PATH"] = mmproj_path
        started = daemon_base.spawn(spec, extra_env=extra_env, log_path=self.root / f"{name}.log")
        if started:
            daemon_base.wait_until_ready(spec)
        return self._status(name, spec, ready=daemon_base.is_ready(spec))

    def _missing(self, name: str, spec: DaemonSpec, variable: str) -> dict[str, Any]:
        """Report a local-chat daemon that cannot load without its weights."""
        status = self._status(name, spec, ready=False)
        status["error"] = f"{variable} is not set"
        return status

    def stop(self, families: list[str] | None = None) -> None:
        """Stop each requested family (pid identity is checked by base.kill)."""
        for name in families or sorted(_FAMILIES):
            daemon_base.kill(self.family(name))

    def status(self) -> list[dict[str, Any]]:
        """Report readiness/pid for every family."""
        return [
            self._status(name, self.family(name), ready=daemon_base.is_ready(self.family(name)))
            for name in sorted(_FAMILIES)
        ]

    def _status(self, name: str, spec: DaemonSpec, *, ready: bool) -> dict[str, Any]:
        pid = daemon_base.recorded_pid(spec)
        return {
            "family": name,
            "name": spec.name,
            "host": spec.host,
            "port": spec.port,
            "pid": pid,
            "ready": ready,
            "pid_file": str(spec.pid_path),
        }
