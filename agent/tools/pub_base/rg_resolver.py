"""ripgrep binary resolution — same five-tier discovery as the ast-grep resolver.

Probing order (mirrors ``agent/tools/code_intel/ast_grep/resolver.py``):

  1. Env override (``SHERRY_RG_PATH``)
  2. sherry runtime (``~/.sherry/runtime/ripgrep/<slug>/rg``)
  3. code-intel bin cache (``CODE_INTEL_DIR/ripgrep/bin/rg``)
  4. PATH lookup (``rg`` / ``rg.exe``, Windows ``PATHEXT`` aware)
  5. Homebrew / Linuxbrew prefixes

Every candidate must be a non-empty regular file AND pass a ``--version`` probe
whose combined output contains ``ripgrep``; a candidate that fails for any
reason is rejected and resolution continues to the next tier. The result is
cached per process, and a stale cache entry is dropped when the resolved binary
turns out not to be executable at spawn time (uninstalled, permissions changed,
``which``-vs-exec race).

Missing ripgrep is a normal state, not an error: the caller keeps its pure
Python implementation for exactly this case, so a host without ``rg`` (Windows,
a stripped container) searches correctly and only loses the speed-up.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from config.features import RIPGREP
from config.path import CODE_INTEL_DIR

__all__ = ["resolve_rg", "reset_cache", "spawn_failed"]

_resolution_cache: str | None = None
_cache_filled = False


def _binary_name() -> str:
    """Return the runtime binary name (``rg`` / ``rg.exe``)."""
    return "rg.exe" if sys.platform == "win32" else "rg"


def _runtime_slug() -> str:
    """Return the ``<platform>-<arch>`` runtime slug used by the release assets."""
    plat_name = sys.platform
    plat = "win32" if plat_name == "win32" else ("darwin" if plat_name == "darwin" else "linux")
    raw_arch = os.environ.get("SHERRY_RG_ARCH", "").strip().lower() or _platform.machine().lower()
    arch = "arm64" if raw_arch in ("arm64", "aarch64") else "x64"
    return f"{plat}-{arch}"


def _candidate_exists(path: str) -> bool:
    """Return True when *path* is a non-empty regular file."""
    p = Path(path)
    try:
        if not p.exists():
            return False
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _probe_version(binary_path: str) -> bool:
    """Run ``--version`` and return True when the output mentions ripgrep."""
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=RIPGREP["version_probe_timeout_ms"] / 1000,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "ripgrep" in (result.stdout + result.stderr).lower()


def _accepts(binary_path: str) -> bool:
    """Return True when *binary_path* is a non-empty file passing the probe."""
    return _candidate_exists(binary_path) and _probe_version(binary_path)


def runtime_dir() -> Path:
    """Return the provision target directory (config override or ``~/.sherry``)."""
    configured = str(RIPGREP["runtime_dir"]).strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".sherry" / "runtime" / "ripgrep" / _runtime_slug()


def _candidates() -> list[str]:
    """Every candidate path, in probing order (env → runtime → bin → PATH → brew)."""
    candidates: list[str] = []

    override = os.environ.get(str(RIPGREP["path_env_key"]), "").strip()
    if override:
        candidates.append(override)

    candidates.append(str(runtime_dir() / _binary_name()))
    candidates.append(str(CODE_INTEL_DIR / "ripgrep" / "bin" / _binary_name()))

    # ``shutil.which`` is PATHEXT-aware on Windows, so one lookup covers both.
    found = shutil.which("rg")
    if found:
        candidates.append(found)
    for prefix in ("/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin"):
        candidates.append(str(Path(prefix) / _binary_name()))

    return [c for c in candidates if c]


def resolve_rg() -> str | None:
    """Return the path of a usable ``rg``, or ``None`` (resolved once per process)."""
    global _resolution_cache, _cache_filled
    if _cache_filled:
        return _resolution_cache
    if not RIPGREP["enabled"]:
        _cache_filled = True
        return None

    for candidate in _candidates():
        if _accepts(candidate):
            _resolution_cache = candidate
            break
    _cache_filled = True

    if _resolution_cache is None:
        # INFO once per process, not once per call: operators need to know the
        # fast path is unavailable, and the caller's Python fallback is slower.
        logger.info(
            "ripgrep ('rg') not found or failed its version probe; "
            "searches use the Python fallback (install ripgrep for faster scans)"
        )
    return _resolution_cache


def spawn_failed() -> None:
    """Drop a cached resolution whose binary could not be executed.

    Called when the spawn itself fails (uninstalled, permission change, or a
    ``which``-vs-exec race) rather than when discovery failed — the next call
    re-probes, and in the meantime the Python fallback serves the request.
    """
    global _resolution_cache, _cache_filled
    _resolution_cache = None
    _cache_filled = False


def reset_cache() -> None:
    """Forget the cached resolution (tests that swap ``rg`` mid-process)."""
    spawn_failed()
