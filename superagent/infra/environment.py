"""Worker environment, CLI shims and git config (prototype ``src/infra/environment.ts``).

The worker shims and gitconfig are process-level side effects of the
superagent service, never of the Sherry backend.  Setup is their only writer;
a foreign file in either directory must stop startup (fail closed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from superagent.core.model import Ticket

if TYPE_CHECKING:
    from superagent.infra.paths import StatePaths

PREFIX = "DSH_SUPERAGENT_"

CREDENTIAL_ENV = ("GH_TOKEN", "GITHUB_TOKEN")


@dataclass(frozen=True)
class ExecutionEnvironment:
    """Per-Ticket variables injected into a worker run."""

    values: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str]:
        """Return the environment as a plain dictionary."""
        return dict(self.values)


def execution_environment(
    ticket: Ticket,
    *,
    extra: dict[str, str] | None = None,
    variables: dict[str, str] | None = None,
) -> ExecutionEnvironment:
    """Build the ``DSH_SUPERAGENT_*`` environment for one Ticket.

    ``variables`` are Source-provided values (repository, issue number, ...);
    they win over the generic Ticket values.  No credential is copied here.
    """
    values: dict[str, str] = {
        f"{PREFIX}DIR": str(ticket.execution.get("directory", "")),
        f"{PREFIX}NAME": str(ticket.execution.get("name", ticket.id)),
        f"{PREFIX}TICKET": ticket.id,
        f"{PREFIX}BLOCKED_COMMANDS": ",".join(
            str(item) for item in ticket.execution.get("blocked_commands", [])
        ),
    }
    for key, value in (variables or {}).items():
        name = key if key.startswith(PREFIX) else f"{PREFIX}{key}"
        values[name] = str(value)
    for key, value in (extra or {}).items():
        values[key] = str(value)
    for key in CREDENTIAL_ENV:
        values.pop(key, None)
    return ExecutionEnvironment(values=values)


def worker_env(
    ticket: Ticket,
    *,
    paths: StatePaths | None = None,
    variables: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the full worker process environment (PATH prefixed, no tokens)."""
    from superagent.infra.paths import StatePaths, state_paths

    env = dict(os.environ)
    for key in CREDENTIAL_ENV:
        env.pop(key, None)
    env.update(execution_environment(ticket, variables=variables).as_dict())
    if paths is not None:
        layout = paths if isinstance(paths, StatePaths) else state_paths(paths)
        env["PATH"] = f"{layout.worker_bin}{os.pathsep}{env.get('PATH', '')}"
        env["GIT_CONFIG_GLOBAL"] = str(layout.worker_gitconfig / "config")
        env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


_SHIM = """#!/usr/bin/env bash
set -euo pipefail
exec {target} "$@"
"""

_BLOCKED = """#!/usr/bin/env bash
echo "blocked command: $(basename "$0")" >&2
exit 1
"""

_GITCONFIG = """[credential "https://github.com"]
    helper =
    helper = !{gh} auth git-credential
[url "git@github.com:"]
    insteadOf = https://github.com/
[filter "lfs"]
    clean = git-lfs clean -- %f
    smudge = git-lfs smudge -- %f
    process = git-lfs filter-process
    required = true
[core]
    askpass =
"""


def write_worker_environment(
    paths: StatePaths,
    *,
    gh_cli: str,
    lark_cli: str | None = None,
    blocked_commands: tuple[str, ...] = (),
) -> list[str]:
    """Write shims + gitconfig; return the managed relative file names."""
    paths.worker_bin.mkdir(parents=True, exist_ok=True)
    paths.worker_gitconfig.mkdir(parents=True, exist_ok=True)

    managed: list[str] = []
    shims = {"gh": gh_cli}
    if lark_cli:
        shims["lark-cli"] = lark_cli
    for name, target in shims.items():
        path = paths.worker_bin / name
        path.write_text(_SHIM.format(target=target), encoding="utf-8")
        path.chmod(0o755)
        managed.append(f"worker-bin/{name}")
    for name in blocked_commands:
        path = paths.worker_bin / name
        if "/" in name or name in shims or name == "." or name == "..":
            raise ValueError(f"invalid blocked command name: {name!r}")
        path.write_text(_BLOCKED, encoding="utf-8")
        path.chmod(0o755)
        managed.append(f"worker-bin/{name}")
    config = paths.worker_gitconfig / "config"
    config.write_text(_GITCONFIG.format(gh=gh_cli), encoding="utf-8")
    managed.append("worker-gitconfig/config")
    return managed


def unmanaged_files(paths: StatePaths, managed: list[str]) -> list[str]:
    """Return files in managed directories that setup did not write."""
    found: list[str] = []
    for directory, prefix in (
        (paths.worker_bin, "worker-bin/"),
        (paths.worker_gitconfig, "worker-gitconfig/"),
    ):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            name = f"{prefix}{path.name}"
            if path.is_file() and name not in managed:
                found.append(name)
    return found


__all__ = [
    "CREDENTIAL_ENV",
    "PREFIX",
    "ExecutionEnvironment",
    "execution_environment",
    "unmanaged_files",
    "worker_env",
    "write_worker_environment",
]
