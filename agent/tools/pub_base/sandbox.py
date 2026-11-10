"""Sandbox abstraction layer: policy parsing, backend contract, platform dispatch.

This module is the single sandbox entry point consumed by the terminal and
python_repl tools. It defines:

- ``SandboxPolicy``: the three-state ``SANDBOX_POLICY`` configuration enum.
- ``parse_policy`` / ``read_policy``: strict env parsing (no silent fallback).
- ``SandboxBackend``: the ABC that the platform backends (bwrap on Linux,
  seatbelt on macOS) must satisfy.
- ``get_backend``: platform dispatch + probe + degrade semantics.

Backends are imported *lazily* inside ``get_backend`` so this module works even
before the backend modules exist (and so a broken/missing backend module is
treated as "sandbox unavailable", never as a crash).

Precedence matrix (``SANDBOX_POLICY`` x tool-call ``sandbox`` flag)
-------------------------------------------------------------------
This is the authoritative behavior table, tested cell-by-cell in

========== ============================== ==============================
policy     sandbox=True                   sandbox=False
========== ============================== ==============================
required   sandboxed via backend;         DENIED outright (no approval
           RuntimeError if the backend    path exists for it)
           is unavailable on this system
auto       sandboxed via backend; if      HITL approval required (the
           unavailable: degrade to        card is raised by the
           unsandboxed + one loguru       humanInTheLoop middleware)
           warning (warning emitted by
           the tool layer)
off        NEVER sandboxed, NEVER         NEVER sandboxed, NEVER
           approved                       approved
========== ============================== ==============================

Notes:
- ``required`` + ``sandbox=True`` + probe failure raises
  ``RuntimeError("Required sandbox unavailable on {system}")`` from
  ``get_backend`` -- the tool layer surfaces it as a tool error.
- The ``auto`` degrade warning (one loguru line) belongs to the tool layer
  (the sandboxed execution tools), which know whether the call wanted sandboxing;
  ``get_backend`` itself stays silent to avoid double warnings.
- Env parsing is strict: unknown ``SANDBOX_POLICY`` values raise ``ValueError``
  listing the legal values (silent fallback to the default is explicitly NOT
  acceptable -- a typoed safety setting must fail loudly).
- ``read_policy`` reads the environment on EVERY call (no import-time caching)
  so runtime changes and test monkeypatching take effect immediately.
"""

import importlib
import os
import platform
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path

from config.path import TEMP_DIR

__all__ = [
    "SandboxPolicy",
    "SandboxBackend",
    "parse_policy",
    "read_policy",
    "register_sandbox_backend",
    "get_backend",
]

#: Default read-deny paths, masked by both OS backends: well-known credential
#: stores that no sandboxed command may read. Extend the list with the
#: ``SHERRY_DENY_READ_PATHS`` env var (``os.pathsep``-separated, ``~`` expanded).
DEFAULT_DENY_READ_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gh",
    "~/.docker",
)


def _sensitive_read_paths() -> list[Path]:
    """Return the sensitive read-deny paths: defaults + ``SHERRY_DENY_READ_PATHS``.

    ``SHERRY_DENY_READ_PATHS`` extends the defaults with ``os.pathsep``-separated
    entries (``:`` on Linux/macOS, e.g. ``~/.kube:~/.config/gh``); ``~`` is
    expanded, blank entries are skipped, order is preserved and duplicates are
    dropped. Paths are returned unresolved and may not exist — the bwrap backend
    skips missing mount points (nothing to read), while the seatbelt backend
    emits rules for them anyway (denying a missing path is harmless).
    """
    raw = os.getenv("SHERRY_DENY_READ_PATHS", "")
    entries: list[str] = [*DEFAULT_DENY_READ_PATHS, *raw.split(os.pathsep)]
    paths: list[Path] = []
    for entry in entries:
        cleaned = entry.strip()
        if not cleaned:
            continue
        expanded = Path(os.path.expanduser(cleaned))
        if expanded not in paths:
            paths.append(expanded)
    return paths


class SandboxPolicy(Enum):
    """Three-state sandbox configuration (``SANDBOX_POLICY`` env var)."""

    REQUIRED = "required"  # backend unavailable => reject the command
    AUTO = "auto"  # backend unavailable => degrade to unsandboxed (default)
    OFF = "off"  # sandboxing disabled entirely


def parse_policy(raw: str | None) -> SandboxPolicy:
    """Parse a raw ``SANDBOX_POLICY`` value into a :class:`SandboxPolicy`.

    ``None`` maps to the default (:attr:`SandboxPolicy.AUTO`); any other value
    is matched case-insensitively after stripping surrounding whitespace.
    Unknown values raise ``ValueError`` with the three legal values in the
    message -- a mistyped safety setting must fail loudly, never fall back
    silently.
    """
    if raw is None:
        return SandboxPolicy.AUTO
    normalized = raw.strip().lower()
    if normalized == "required":
        return SandboxPolicy.REQUIRED
    if normalized == "auto":
        return SandboxPolicy.AUTO
    if normalized == "off":
        return SandboxPolicy.OFF
    raise ValueError(
        f"Invalid SANDBOX_POLICY value {raw!r}: expected one of 'required', 'auto', 'off'"
    )


def read_policy() -> SandboxPolicy:
    """Read ``SANDBOX_POLICY`` from the environment.

    Reads on EVERY call (canonical ``os.getenv`` pattern, cf. logs/logger.py)
    -- no import-time caching -- so the policy can be switched at runtime and
    monkeypatched in tests.
    """
    return parse_policy(os.getenv("SANDBOX_POLICY", "auto"))


class SandboxBackend(ABC):
    """Contract for OS-native sandbox backends (bwrap on Linux, seatbelt on macOS)."""

    @abstractmethod
    def probe(self) -> bool:
        """Return True if the sandbox tool is present AND actually works.

        Must never raise (backends catch their own probe exceptions and return
        False); backends may cache the probe result for the process lifetime.
        """
        ...

    @abstractmethod
    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: Sequence[Path],
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        """Wrap a command argv + environment for sandboxed execution.

        ``writable_roots`` is the only write surface the wrapped command gets:
        the calling session's project directory (plus the process temp dir,
        which backends append themselves because the sandbox needs a writable
        ``/tmp``). The parameter is REQUIRED — a default would silently restore
        the old "whole ROOT_DIR is writable" behaviour at any call site that
        forgot to pass it, which is exactly the write bypass this contract
        exists to close. An empty list is a caller bug and raises ``ValueError``
        (fail-closed).

        ``allow_network`` keeps the host network namespace for callers that
        must reach a parent-owned loopback socket (PTC's RPC bridge). It is
        opt-in and false by default; the write/read sandbox still applies.

        Returns the (wrapped argv, scrubbed env) tuple to be exec'd directly
        (list form, no shell).
        """
        ...


def normalize_writable_roots(writable_roots: Sequence[Path]) -> list[Path]:
    """Validate + normalize the write surface for a sandboxed run.

    Fail-closed contract shared by the backends: an empty list is a caller bug
    (``ValueError``), every entry is resolved, duplicates collapse, and
    ``TEMP_DIR`` is always present because the sandbox itself needs a writable
    temp directory.
    """
    roots = [Path(root) for root in writable_roots if str(root).strip()]
    if not roots:
        raise ValueError(
            "wrap() requires at least one writable root: refusing to sandbox "
            "with an empty write surface (fail-closed)"
        )
    resolved: list[Path] = []
    for root in roots:
        try:
            candidate = root.resolve()
        except (OSError, RuntimeError):
            candidate = root
        if candidate not in resolved:
            resolved.append(candidate)
    temp = Path(TEMP_DIR).resolve() if str(TEMP_DIR).strip() else None
    if temp is not None:
        # bwrap needs a bind-mount SOURCE that exists; a fresh checkout / CI
        # runner has no gitignored ``temp/`` yet, which made every sandboxed
        # command fail with "bwrap: Can't find source path .../temp". The
        # contract already promises TEMP_DIR is always writable, so create it
        # here (fail-open: a read-only home keeps the old error path).
        try:
            temp.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if temp not in resolved:
            resolved.append(temp)
    return resolved


#: Factory for one platform's backend. Registered through
#: :func:`register_sandbox_backend`; the built-ins are lazy import wrappers so
#: this module still imports (and answers ``OFF``) on a checkout where the
#: backend modules are missing.
SandboxBackendFactory = Callable[[], SandboxBackend]

_BACKEND_FACTORIES: dict[str, SandboxBackendFactory] = {}


def _lazy_backend(module: str, class_name: str) -> SandboxBackendFactory:
    """Build a factory that imports the backend only when the platform needs it."""

    def factory() -> SandboxBackend:
        backend_module = importlib.import_module(module)
        return getattr(backend_module, class_name)()

    return factory


def register_sandbox_backend(
    system: str, factory: SandboxBackendFactory, *, replace: bool = False
) -> None:
    """Register (or replace) the backend factory for ``platform.system()``.

    A duplicate registration is refused unless ``replace=True``: two modules
    claiming the same platform is a real conflict, not something to resolve by
    import order. Tests replace a platform to inject a fake backend.
    """
    name = str(system).strip()
    if not name:
        raise ValueError("system must be a non-empty platform name")
    if name in _BACKEND_FACTORIES and not replace:
        raise ValueError(
            f"a sandbox backend is already registered for {name!r}; pass replace=True to override"
        )
    _BACKEND_FACTORIES[name] = factory


register_sandbox_backend(
    "Linux", _lazy_backend("agent.tools.pub_base.sandbox_bwrap", "BwrapBackend")
)
register_sandbox_backend(
    "Darwin", _lazy_backend("agent.tools.pub_base.sandbox_seatbelt", "SeatbeltBackend")
)


def get_backend(policy: SandboxPolicy) -> SandboxBackend | None:
    """Platform dispatch + probe. Return a usable backend, or ``None``.

    Semantics:
    - ``OFF``: skip probe entirely (no import, no subprocess) -> ``None``.
    - The registered factory for ``platform.system()`` builds the backend
      (Linux -> bwrap, Darwin -> seatbelt, anything else -> no factory).
    - Backend imports are lazy and failures degrade: a missing/broken backend
      module is treated as "sandbox unavailable".
    - If the backend exists but ``probe()`` fails, or there is no backend for
      this platform:
      - ``REQUIRED``: raise ``RuntimeError("Required sandbox unavailable on "
        "{system}")``.
      - ``AUTO``/``OFF``: return ``None`` (degrade silently; the tool layer
        owns the one-line loguru degrade warning).
    """
    system = platform.system()

    if policy is SandboxPolicy.OFF:
        # OFF must never touch the filesystem/subprocess: skip probe entirely.
        return None

    backend: SandboxBackend | None = None
    factory = _BACKEND_FACTORIES.get(system)
    if factory is not None:
        try:
            backend = factory()
        except ImportError:
            # Lazy import failure degrades to "sandbox unavailable".
            backend = None

    if backend is not None and backend.probe():
        return backend

    if policy is SandboxPolicy.REQUIRED:
        raise RuntimeError(f"Required sandbox unavailable on {system}")
    return None
