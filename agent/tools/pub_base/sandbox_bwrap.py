"""Linux bwrap (bubblewrap) sandbox backend.

Construction logic only — never verified on a real Linux machine (the Windows
dev box has no bwrap binary; this module's tests all mock subprocess and never
execute a real bwrap).

argv order is load-bearing:
- ``--ro-bind / /`` first mounts the entire root read-only;
- only the CALLER's roots (the session's project directory) plus TEMP_DIR are
  then writable-mounted via ``--bind``; everything else stays read-only
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

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from config.features import TOOLS_TIMEOUTS

try:  # Prefer the shared contract; fall back to a local ABC shape if it is unavailable
    from agent.tools.pub_base.sandbox import SandboxBackend, normalize_writable_roots
except ImportError:  # pragma: no cover
    from abc import ABC, abstractmethod

    class SandboxBackend(ABC):
        """Local ABC-shaped fallback mirroring agent.tools.pub_base.sandbox contract."""

        @abstractmethod
        def probe(self) -> bool: ...

        @abstractmethod
        def wrap(
            self, cmd: list[str], env: dict, writable_roots, *, allow_network: bool = False
        ) -> tuple[list[str], dict]: ...

    def normalize_writable_roots(writable_roots):  # type: ignore[misc]
        raise RuntimeError("the shared sandbox contract is unavailable")


from agent.tools.pub_base.sandbox import _sensitive_read_paths  # noqa: E402

# Probe smoke-test timeout (seconds), bound to the feature registry.
_PROBE_TIMEOUT_SECONDS = TOOLS_TIMEOUTS["sandbox_bwrap_probe_timeout_seconds"]

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

#: Source directory for the read-shield bind. Some minimal distros do not ship
#: ``/var/empty``; directory masks then fall back to ``--tmpfs`` (same empty
#: view, no source directory required).
_EMPTY_MASK_DIR = Path("/var/empty")

#: Source device for masking a sensitive *file* (a directory cannot be bound
#: over a file); reading it yields EOF.
_NULL_DEVICE = "/dev/null"


#: System trees a sandboxed command legitimately needs to READ (binaries,
#: libraries, DNS/TLS config, package metadata). Missing entries are skipped —
#: bwrap cannot mount what the host does not have.
_READ_ALLOW_SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/var",
    "/dev",
    "/proc",
    "/sys",
)

#: Extra read paths from the environment (``os.pathsep``-separated). Default:
#: the toolchain caches a project build needs (npm/pip/uv), which live under
#: ``~/.cache`` — without them ``uv run``/``pip install`` fail inside the
#: whitelist. ``~`` is expanded.
_READ_EXTRA_ENV = "SANDBOX_READ_EXTRA"

#: Enable the read whitelist (default OFF: see scripts/sandbox_smoke.py — the
#: whitelist is only safe to switch on after it has been verified on the host).
_READ_ALLOWLIST_ENV = "SANDBOX_READ_ALLOWLIST"

_DEFAULT_READ_EXTRA = ("~/.cache",)


def read_allowlist_enabled() -> bool:
    """Whether the read whitelist replaces ``--ro-bind / /`` for this run."""
    raw = os.environ.get(_READ_ALLOWLIST_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def read_extra_paths() -> list[Path]:
    """The extra read paths for the whitelist (env, else the toolchain caches)."""
    raw = os.environ.get(_READ_EXTRA_ENV, "")
    entries = [item for item in raw.split(os.pathsep) if item.strip()]
    if not entries:
        entries = list(_DEFAULT_READ_EXTRA)
    resolved: list[Path] = []
    for entry in entries:
        try:
            path = Path(os.path.expanduser(entry.strip()))
        except (OSError, RuntimeError):
            continue
        if path not in resolved:
            resolved.append(path)
    return resolved


def _read_allow_args() -> list[str]:
    """The mount base for the read whitelist: system trees + extras, read-only.

    Everything the host does not have is skipped; the caller masks the REST of
    the tree with ``--tmpfs`` (see :func:`_read_mask_args`), so the resulting
    read surface is exactly this list plus the writable roots.
    """
    args: list[str] = []
    for path in (*_READ_ALLOW_SYSTEM_PATHS, *(str(p) for p in read_extra_paths())):
        if not Path(path).exists():
            continue
        args += ["--ro-bind", path, path]
    return args


def _read_mask_args() -> list[str]:
    """Empty views over the trees the whitelist does not cover.

    ``/home`` / ``/root`` / ``/mnt`` / ``/media`` / ``/srv`` / ``/run`` carry
    user data; a ``--tmpfs`` over each means a command sees an empty directory
    instead of the operator's files. Missing mount points are skipped (nothing
    to hide).
    """
    args: list[str] = []
    for path in ("/home", "/root", "/mnt", "/media", "/srv", "/run"):
        if not Path(path).exists():
            continue
        args += ["--tmpfs", path]
    return args


def _read_base_args() -> list[str]:
    """The read base: the whole root read-only, or the whitelist when enabled."""
    if not read_allowlist_enabled():
        return ["--ro-bind", "/", "/"]
    return [*_read_allow_args(), *_read_mask_args()]


def _read_shield_args() -> list[str]:
    """bwrap argv fragment that masks sensitive paths from reads.

    Placed after the writable binds so a writable mount can never re-expose a
    masked path. Mount points must already exist on the host — bwrap cannot
    create one under the read-only root bind — so a missing sensitive path is
    skipped (there is nothing to read there anyway). Directories are covered
    with an empty directory (``--ro-bind /var/empty <path>``), falling back to
    ``--tmpfs <path>`` when ``/var/empty`` is absent; files are covered with
    ``--ro-bind /dev/null <path>``.
    """
    args: list[str] = []
    for path in _sensitive_read_paths():
        try:
            if not path.exists():
                continue
            if not path.is_dir():
                args += ["--ro-bind", _NULL_DEVICE, str(path)]
            elif _EMPTY_MASK_DIR.is_dir():
                args += ["--ro-bind", str(_EMPTY_MASK_DIR), str(path)]
            else:
                args += ["--tmpfs", str(path)]
        except OSError:
            continue
    return args


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

    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: Sequence[Path],
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        """Wrap cmd into a bwrap argv; the env allowlist is injected via --setenv.

        The ONLY writable mounts are the caller's roots (plus TEMP_DIR, added by
        ``normalize_writable_roots``): everything else stays read-only under the
        ``--ro-bind / /`` base mount, so a command cannot write outside the
        session's project directory.

        ``allow_network=True`` adds ``--share-net`` beside ``--unshare-all``:
        PTC's child must reach the parent's loopback RPC listener, which a
        private network namespace makes unreachable. The write/read sandbox is
        unchanged.
        """
        argv: list[str] = [
            "bwrap",
            *_read_base_args(),
        ]
        for path in _writable_paths(writable_roots):
            argv += ["--bind", path, path]
        argv += _read_shield_args()
        argv += [
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-all",
            *(["--share-net"] if allow_network else []),
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        for key, value in env.items():
            argv += ["--setenv", key, str(value)]
        argv += ["--", *cmd]
        return argv, env


def _writable_paths(writable_roots: Sequence[Path]) -> list[str]:
    """The ``--bind`` list: the caller's roots + TEMP_DIR, deduplicated."""
    roots = normalize_writable_roots(writable_roots)
    return [str(root) for root in roots]
