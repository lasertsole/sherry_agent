"""Deployment configuration and machine facts (prototype ``src/deployment.ts``)."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Superagent process configuration."""

    state_dir: str = ".artifacts/superagent"
    sherry_backend_url: str = "http://127.0.0.1:3081"
    port: int = 3082
    shared_secret: str = ""
    mcp_transport: str = "stdio"

    @classmethod
    def from_env(cls) -> Config:
        """Read configuration from environment variables."""
        return cls(
            state_dir=os.getenv("DSH_SUPERAGENT_STATE_DIR", ".artifacts/superagent"),
            sherry_backend_url=os.getenv("DSH_SUPERAGENT_SHERRY_URL", "http://127.0.0.1:3081"),
            port=int(os.getenv("DSH_SUPERAGENT_PORT", "3082")),
            shared_secret=os.getenv("DSH_SUPERAGENT_SECRET", ""),
            mcp_transport=os.getenv("DSH_SUPERAGENT_MCP_TRANSPORT", "stdio"),
        )


@dataclass(frozen=True)
class Machine:
    """Facts about the host the service runs on."""

    system: str
    shell: str

    @property
    def posix(self) -> bool:
        """Whether a POSIX shell is available."""
        return self.system != "Windows"


def resolve_machine() -> Machine:
    """Detect the host platform and worker shell."""
    system = platform.system()
    return Machine(system=system, shell="bash" if system == "Windows" else "/bin/sh")


def resolve_deployment(config: Config | None = None) -> tuple[Config, Machine]:
    """Return the resolved config and machine facts."""
    return config or Config.from_env(), resolve_machine()


def execution_root(config: Config) -> Path:
    """Return the root directory for Ticket worktrees."""
    return Path(config.state_dir).expanduser() / "executions"


__all__ = ["Config", "Machine", "execution_root", "resolve_deployment", "resolve_machine"]
