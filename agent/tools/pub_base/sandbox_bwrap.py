"""Linux bwrap (bubblewrap) sandbox backend.

Construction logic only — never verified on a real Linux machine (the Windows
dev box has no bwrap binary; this module's tests all mock subprocess and never
execute a real bwrap).

argv order is load-bearing:
- ``--ro-bind / /`` first mounts the entire root read-only;
- only ROOT_DIR / TEMP_DIR are then writable-mounted via ``--bind``
  (writable surface = these two directories);
- ``--clearenv`` must precede every ``--setenv`` — combined, the two implement
  env allowlist semantics (upstream env_scrub already cleaned the env; only
  allowlisted variables are injected here);
- everything after ``--`` is the original wrapped command.

Reference: oh-my-openagent sandbox-bwrap-probe.ts / sandbox-platform.ts buildBwrapArgs
(probe semantics prototype: when bwrap exists but is blocked by AppArmor
(Ubuntu 24.04+ kernel.apparmor_restrict_unprivileged_userns=1), an existence
check is not enough — a smoke test is mandatory).
"""

from __future__ import annotations

import subprocess

from config.path import ROOT_DIR, TEMP_DIR

try:  # Prefer the output; fall back to the local ABC shape when hasn't landed (see notepad problems.md)
    from agent.tools.pub_base.sandbox import SandboxBackend
except ImportError:  # pragma: no cover
    from abc import ABC, abstractmethod

    class SandboxBackend(ABC):
        """Local ABC-shaped fallback mirroring agent.tools.pub_base.sandbox contract."""

        @abstractmethod
        def probe(self) -> bool: ...

        @abstractmethod
        def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]: ...


#: Probe smoke-test timeout (seconds)
_PROBE_TIMEOUT_SECONDS = 3

#: Smoke-test command: a minimal call that still triggers user-namespace/uid-map setup
_PROBE_ARGV = [
    "bwrap",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "true",
]


class BwrapBackend(SandboxBackend):
    """Linux bubblewrap backend. bwrap does not accept an env dict; env vars are injected via --setenv."""

    _probe_cache: bool | None = None  # Class-level cache: probe only once per process lifetime

    def probe(self) -> bool:
        """Smoke-test bwrap availability; exceptions / non-zero return codes / timeouts all yield False, result cached at class level."""
        if BwrapBackend._probe_cache is not None:
            return BwrapBackend._probe_cache
        try:
            result = subprocess.run(
                _PROBE_ARGV,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            ok = result.returncode == 0
        except Exception:  # FileNotFoundError / TimeoutExpired / OSError, etc.
            ok = False
        BwrapBackend._probe_cache = ok
        return ok

    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        """Wrap cmd into a bwrap argv; the env allowlist is injected via --setenv."""
        argv: list[str] = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
        ]
        for path in _writable_paths():
            argv += ["--bind", path, path]
        argv += [
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        for key, value in env.items():
            argv += ["--setenv", key, str(value)]
        argv += ["--", *cmd]
        return argv, env


def _writable_paths() -> list[str]:
    """Writable directory list (as str), deduplicated when paths coincide (e.g. TEMP_DIR == ROOT_DIR)."""
    root, temp = str(ROOT_DIR), str(TEMP_DIR)
    return [root] if root == temp else [root, temp]
