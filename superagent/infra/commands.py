"""CLI transport abstraction (prototype ``src/infra/commands.ts``).

Sources and setup shell out to ``gh``/``lark-cli``.  Routing every invocation
through a narrow port keeps the transport mockable in unit tests and keeps
credentials out of logged command lines.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol

SENSITIVE_ENV = ("TOKEN", "PASSWORD", "SECRET", "KEY")


@dataclass(frozen=True)
class CommandResult:
    """One completed CLI invocation."""

    code: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command exited successfully."""
        return self.code == 0

    def json(self) -> object:
        """Parse stdout as JSON, returning None when it is not JSON."""
        import json

        try:
            return json.loads(self.stdout)
        except (ValueError, TypeError):
            return None


class Commands(Protocol):
    """The CLI transport used by Sources and setup."""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run one command and return its captured output."""
        ...


class SubprocessCommands:
    """Run CLI commands through ``asyncio.create_subprocess_exec``."""

    def __init__(self, *, base_env: dict[str, str] | None = None) -> None:
        self.base_env = base_env

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Execute one command and capture its output."""
        if not argv:
            raise ValueError("argv must not be empty")
        merged = {**os.environ, **(self.base_env or {}), **(env or {})}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=merged,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return CommandResult(
            code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
        )


class FakeCommands:
    """Recorded CLI transport for tests.

    ``responses`` maps a substring of the joined argv to a ``CommandResult`` or
    an exception.  The first matching entry wins.
    """

    def __init__(self, responses: dict[str, CommandResult | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Return a queued response and record the call."""
        self.calls.append((list(argv), {"cwd": cwd, "env": env, "timeout": timeout}))
        joined = " ".join(argv)
        for needle, response in self.responses.items():
            if needle in joined:
                if isinstance(response, Exception):
                    raise response
                return response
        return CommandResult(code=0, stdout="", stderr="")

    @property
    def argv(self) -> list[list[str]]:
        """Return all recorded argv lists."""
        return [call[0] for call in self.calls]


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Remove credential-looking variables before logging an environment."""
    return {
        key: value
        for key, value in env.items()
        if not any(part in key.upper() for part in SENSITIVE_ENV)
    }


__all__ = [
    "CommandResult",
    "Commands",
    "FakeCommands",
    "SubprocessCommands",
    "redact_env",
]
