"""Sandbox abstraction layer: policy parsing, backend contract, platform dispatch.

This module is the single sandbox entry point consumed by the terminal and
python_repl tools (Tasks 6/7). It defines:

- ``SandboxPolicy``: the three-state ``SANDBOX_POLICY`` configuration enum.
- ``parse_policy`` / ``read_policy``: strict env parsing (no silent fallback).
- ``SandboxBackend``: the ABC that the platform backends (bwrap on Linux,
  seatbelt on macOS, implemented in Tasks 3/4) must satisfy.
- ``get_backend``: platform dispatch + probe + degrade semantics.

Backends are imported *lazily* inside ``get_backend`` so this module works even
before the backend modules exist (and so a broken/missing backend module is
treated as "sandbox unavailable", never as a crash).

Precedence matrix (``SANDBOX_POLICY`` x tool-call ``sandbox`` flag)
-------------------------------------------------------------------
This is the authoritative behavior table, tested cell-by-cell in Task 9:

========== ============================== ==============================
policy     sandbox=True                   sandbox=False
========== ============================== ==============================
required   sandboxed via backend;         DENIED outright (tool layer,
           RuntimeError if the backend    Tasks 6/7 -- no approval path)
           is unavailable on this system
auto       sandboxed via backend; if      HITL approval required
           unavailable: degrade to        (Task 8, humanInTheLoop)
           unsandboxed + one loguru
           warning (warning emitted by
           the tool layer, Tasks 6/7)
off        NEVER sandboxed, NEVER         NEVER sandboxed, NEVER
           approved                       approved
========== ============================== ==============================

Notes:
- ``required`` + ``sandbox=True`` + probe failure raises
  ``RuntimeError("Required sandbox unavailable on {system}")`` from
  ``get_backend`` -- the tool layer surfaces it as a tool error.
- The ``auto`` degrade warning (one loguru line) belongs to the tool layer
  (Tasks 6/7), which knows whether the current call actually wanted sandboxing;
  ``get_backend`` itself stays silent to avoid double warnings.
- Env parsing is strict: unknown ``SANDBOX_POLICY`` values raise ``ValueError``
  listing the legal values (silent fallback to the default is explicitly NOT
  acceptable -- a typoed safety setting must fail loudly).
- ``read_policy`` reads the environment on EVERY call (no import-time caching)
  so runtime changes and test monkeypatching take effect immediately.
"""

import os
import platform
from abc import ABC, abstractmethod
from enum import Enum

__all__ = [
    "SandboxPolicy",
    "SandboxBackend",
    "parse_policy",
    "read_policy",
    "get_backend",
]


class SandboxPolicy(Enum):
    """Three-state sandbox configuration (``SANDBOX_POLICY`` env var)."""

    REQUIRED = "required"  # backend unavailable => reject the command
    AUTO = "auto"          # backend unavailable => degrade to unsandboxed (default)
    OFF = "off"            # sandboxing disabled entirely


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
    """Contract for OS-native sandbox backends (bwrap / seatbelt, Tasks 3/4)."""

    @abstractmethod
    def probe(self) -> bool:
        """Return True if the sandbox tool is present AND actually works.

        Must never raise (backends catch their own probe exceptions and return
        False); backends may cache the probe result for the process lifetime.
        """
        ...

    @abstractmethod
    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        """Wrap a command argv + environment for sandboxed execution.

        Returns the (wrapped argv, scrubbed env) tuple to be exec'd directly
        (list form, no shell).
        """
        ...


def get_backend(policy: SandboxPolicy) -> SandboxBackend | None:
    """Platform dispatch + probe. Return a usable backend, or ``None``.

    Semantics (mirrors SANDBOX_PLAN.md section 2.1):
    - ``OFF``: skip probe entirely (no import, no subprocess) -> ``None``.
    - Linux -> ``BwrapBackend``, Darwin -> ``SeatbeltBackend``, anything else
      (e.g. Windows) -> no native backend -> ``None``.
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
    try:
        if system == "Linux":
            # Lazy import: sandbox_bwrap.py is created by Task 3 (parallel
            # wave); ImportError == unavailable, never a crash.
            from agent.tools.pub_base.sandbox_bwrap import BwrapBackend

            backend = BwrapBackend()
        elif system == "Darwin":
            # Lazy import: sandbox_seatbelt.py is created by Task 4.
            from agent.tools.pub_base.sandbox_seatbelt import SeatbeltBackend

            backend = SeatbeltBackend()
    except ImportError:
        backend = None

    if backend is not None and backend.probe():
        return backend

    if policy is SandboxPolicy.REQUIRED:
        raise RuntimeError(f"Required sandbox unavailable on {system}")
    return None
