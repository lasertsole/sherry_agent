"""macOS Seatbelt (sandbox-exec) sandbox backend.

Construction logic only — never verified on a real macOS machine (Windows dev
box limitation; all tests are mocked and never execute a real sandbox-exec).
The sbpl profile template and the ``sandbox-exec -p ... --`` wrapping structure
follow oh-my-openagent ``sandbox-platform.ts`` ``buildDarwinProfile``.
"""

import json
from pathlib import Path
from collections.abc import Sequence
import os
import shutil

from agent.tools.pub_base.sandbox import SandboxBackend, _sensitive_read_paths
from .sandbox import normalize_writable_roots

#: ``.env`` / ``.env.*`` basenames, denied at any depth via an sbpl regex
#: (``(^|/)`` anchors the basename; the leading ``/`` of an absolute path also
#: satisfies it).
_ENV_READ_DENY_RULES = (
    r'(deny file-read* (regex #"(^|/)\.env$"))',
    r'(deny file-read* (regex #"(^|/)\.env\."))',
)


#: Read whitelist switch (shared with the bwrap backend): default OFF because
#: it is only safe to enable after scripts/sandbox_smoke.py has verified it on
#: the host. When on, reads are denied by default and re-opened for the system
#: trees, the toolchain caches and the writable roots.
_READ_ALLOWLIST_ENV = "SANDBOX_READ_ALLOWLIST"

_READ_ALLOW_SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/Library",
    "/System",
    "/private/etc",
    "/private/var",
    "/dev",
    "/tmp",
)


def _read_allowlist_enabled() -> bool:
    raw = os.environ.get(_READ_ALLOWLIST_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _read_allow_lines(writable_roots: Sequence[Path]) -> list[str]:
    """The whitelist rules: no rule when the switch is off (default)."""
    if not _read_allowlist_enabled():
        return []
    from .sandbox_bwrap import read_extra_paths

    paths = [*_READ_ALLOW_SYSTEM_PATHS, *read_extra_paths(), *(str(r) for r in writable_roots)]
    lines = ["(deny file-read*)"]
    lines += [
        f"(allow file-read* (subpath {json.dumps(os.path.expanduser(str(path)))}))"
        for path in paths
    ]
    return lines


def _read_deny_lines() -> list[str]:
    """sbpl deny-read rules for sensitive paths plus ``.env``-family files.

    Paths are expanded (``~``) and embedded through ``json.dumps`` so quotes or
    backslashes cannot break out into injected sbpl forms. Missing paths are
    still denied: a denial on a nonexistent path is harmless and keeps the
    profile independent of the host's current directory layout.
    """
    lines = [
        f"(deny file-read* (subpath {json.dumps(os.path.expanduser(str(path)))}))"
        for path in _sensitive_read_paths()
    ]
    lines += list(_ENV_READ_DENY_RULES)
    return lines


class SeatbeltBackend(SandboxBackend):
    """Wraps commands with ``sandbox-exec -p <profile> -- <cmd...>``.

    ``probe()`` only checks ``shutil.which("sandbox-exec")`` — the seatbelt
    binary provides no exit-code based smoke probe (unlike bwrap). The probe
    result is cached at class level (probed once per process lifetime).
    """

    _probe_result: bool | None = None

    def probe(self) -> bool:
        cls = type(self)
        if cls._probe_result is None:
            cls._probe_result = shutil.which("sandbox-exec") is not None
        return cls._probe_result

    @staticmethod
    def _build_profile(writable_roots: Sequence[Path]) -> str:
        """Build the sbpl profile string.

        Order is load-bearing: ``(deny file-write*)`` under ``(allow default)``
        turns the sandbox into "allow everything except file writes", then
        explicit allows re-open the writable paths. The deny-read rules for
        sensitive paths and ``.env`` files sit directly after
        ``(deny file-write*)``. Template order is the spec.
        Paths are embedded via ``json.dumps`` (sbpl strings are JSON-like) so
        quotes/backslashes in paths cannot break out into injected sbpl forms.
        """
        lines = [
            "(version 1)",
            "(allow default)",
            # Read whitelist (only when enabled; ordered before the write rules
            # so the write allows below can never re-expose a read).
            *_read_allow_lines(writable_roots),
            "(deny file-write*)",
            *_read_deny_lines(),
            *(
                f"(allow file-write* (subpath {json.dumps(str(root))}))"
                for root in normalize_writable_roots(writable_roots)
            ),
            '(allow file-write* (literal "/dev/null"))',
            '(allow file-write* (literal "/dev/tty"))',
        ]
        return "\n".join(lines)

    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: Sequence[Path],
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        """Wrap cmd into a sandbox-exec invocation over the caller's roots.

        Only the caller's roots (plus TEMP_DIR) are re-opened for writing; the
        whole profile is "allow everything except file writes", so the write
        surface IS the sandbox. ``allow_network`` is accepted for the shared
        backend contract; this profile never restricted the network.
        """
        profile = self._build_profile(writable_roots)
        return ["sandbox-exec", "-p", profile, "--", *cmd], env
