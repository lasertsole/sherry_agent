"""Setup status codes and probing (prototype ``src/setup.ts``).

``SetupResult`` is the stable value the service script reads for its 0/2/3
exit codes.  ``probe`` fills it from real machine facts without mutating
 anything: worker-bin / worker-gitconfig writing stays in
:mod:`superagent.infra.environment` and is the caller's decision.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from superagent.deployment import Config, Machine
from superagent.infra.paths import StatePaths

SETUP_CODES = ("ok", "missingRepo", "missingCli", "noPush", "badCredentials", "unmanagedFiles")


@dataclass
class SetupIssue:
    """One blocking setup problem."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        """Reject unknown codes."""
        if self.code not in SETUP_CODES:
            raise ValueError(f"unknown setup code: {self.code}")


@dataclass
class SetupResult:
    """Aggregated setup state for the panel and service script."""

    issues: list[SetupIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether setup is complete."""
        return not self.issues

    @property
    def exit_code(self) -> int:
        """Service-script exit code: 0 ready / 3 waiting for setup."""
        return 0 if self.ok else 3

    def add(self, code: str, detail: str = "") -> None:
        """Record an issue."""
        self.issues.append(SetupIssue(code, detail))

    def as_dict(self) -> dict[str, object]:
        """Return a panel-friendly snapshot."""
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "issues": [{"code": item.code, "detail": item.detail} for item in self.issues],
        }


@dataclass(frozen=True)
class SetupProbe:
    """External facts ``probe`` needs; injectable for tests."""

    repo_exists: bool = True
    cli_paths: dict[str, str] = field(default_factory=dict)
    credentials: tuple[str, ...] = ()
    unmanaged: tuple[str, ...] = ()


def probe(
    config: Config,
    machine: Machine,
    paths: StatePaths,
    *,
    facts: SetupProbe | None = None,
    cli: tuple[str, ...] = ("gh",),
) -> SetupResult:
    """Probe machine/worker state and return the aggregated setup result.

    ``facts`` overrides real filesystem lookups so tests can drive every code
    without a configured CLI or credentials on the host.  Credentials are
    checked by *presence only* — their values are never read or logged.
    """
    result = SetupResult()
    facts = facts or _real_probe(paths, cli)

    if not facts.repo_exists:
        result.add("missingRepo", str(paths.root))
    for name in cli:
        if not facts.cli_paths.get(name):
            result.add("missingCli", name)
    if not facts.credentials:
        result.add("badCredentials", "GH_TOKEN / GITHUB_TOKEN")
    if facts.unmanaged:
        result.add("unmanagedFiles", ", ".join(facts.unmanaged))
    del config, machine
    return result


def _real_probe(paths: StatePaths, cli: tuple[str, ...]) -> SetupProbe:
    """Collect setup facts from the host without side effects."""
    from superagent.infra.environment import CREDENTIAL_ENV, unmanaged_files

    managed = [f"worker-bin/{name}" for name in cli if (paths.worker_bin / name).exists()]
    managed.append("worker-gitconfig/config")
    credentials = tuple(name for name in CREDENTIAL_ENV if os.getenv(name))
    return SetupProbe(
        repo_exists=paths.root.exists(),
        cli_paths={name: shutil.which(name) or "" for name in cli},
        credentials=credentials,
        unmanaged=tuple(unmanaged_files(paths, managed)),
    )


def cli_path(name: str) -> str:
    """Return the resolved path of a worker CLI, or an empty string."""
    return shutil.which(name) or ""


def worker_bin_script(paths: StatePaths, name: str) -> Path:
    """Return the path of a managed worker-bin shim."""
    return paths.worker_bin / name


__all__ = [
    "SETUP_CODES",
    "SetupIssue",
    "SetupProbe",
    "SetupResult",
    "cli_path",
    "probe",
    "worker_bin_script",
]
