"""ast-grep binary resolution — 5-tier discovery with a ``--version`` probe.

Probing order (mirrors oh-my-openagent's sg-resolver.ts + sg-candidates.ts):

  1. Env override (``SHERRY_SG_PATH``)
  2. sherry runtime (``~/.sherry/runtime/ast-grep/<slug>/sg``)
  3. code-intel bin cache (``CODE_INTEL_DIR/ast-grep/bin/sg``)
  4. PATH lookup (``ast-grep`` / ``sg``, with Windows ``PATHEXT``)
  5. Homebrew / Linuxbrew prefixes

Every candidate must be a non-empty regular file AND pass a short ``--version``
probe whose combined output contains ``ast-grep``; a candidate that fails for
any reason is rejected and resolution continues to the next tier. Results are
cached per process (the cache is intentionally invalidatable for tests).
"""

from __future__ import annotations

import os
import platform as _platform
import subprocess
import sys
from pathlib import Path

from config.features import AST_GREP
from config.path import CODE_INTEL_DIR

_resolution_cache: str | None = None


def _binary_name(platform: str | None = None) -> str:
    """Return the runtime binary name (``sg`` / ``sg.exe``)."""
    return "sg.exe" if (platform or sys.platform) == "win32" else "sg"


def _ast_grep_binary_name(platform: str | None = None) -> str:
    """Return the long binary name (``ast-grep`` / ``ast-grep.exe``)."""
    return "ast-grep.exe" if (platform or sys.platform) == "win32" else "ast-grep"


def _runtime_slug() -> str:
    """Return the ``<platform>-<arch>`` runtime slug used by the release assets.

    ``SHERRY_SG_ARCH`` may override the detected architecture (used by drills and
    tests); accepted values are ``arm64`` / ``aarch64`` / ``x64`` / ``x86_64``.
    """
    plat_name = sys.platform
    plat = "win32" if plat_name == "win32" else ("darwin" if plat_name == "darwin" else "linux")
    raw_arch = os.environ.get("SHERRY_SG_ARCH", "").strip().lower() or _platform.machine().lower()
    arch = "arm64" if raw_arch in ("arm64", "aarch64") else "x64"
    return f"{plat}-{arch}"


def _executable_suffixes() -> list[str]:
    """Return candidate suffixes for executable lookup (Windows PATHEXT aware)."""
    if sys.platform != "win32":
        return [""]
    pathext = os.environ.get("PATHEXT", "")
    suffixes = [entry.lower() for entry in pathext.split(";") if entry]
    return list(dict.fromkeys(["", *suffixes, ".exe", ".cmd", ".bat"]))


def _probe_version(binary_path: str) -> bool:
    """Run ``--version`` and return True when the output contains ``ast-grep``."""
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=AST_GREP["ast_grep_version_probe_timeout_ms"] / 1000,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "ast-grep" in (result.stdout + result.stderr).lower()


def _candidate_exists(path: str) -> bool:
    """Return True when *path* is a non-empty regular file."""
    p = Path(path)
    try:
        if not p.exists():
            return False
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _accepts(binary_path: str) -> bool:
    """Return True when *binary_path* is a non-empty file passing the probe."""
    return _candidate_exists(binary_path) and _probe_version(binary_path)


def _env_override_candidates() -> list[str]:
    """Tier 1: the ``SHERRY_SG_PATH`` env override."""
    key = AST_GREP["ast_grep_path_env_key"]
    value = os.environ.get(key, "").strip()
    return [value] if value else []


def runtime_dir() -> Path:
    """Return the provision target directory (config override or ``~/.sherry``)."""
    configured = str(AST_GREP["ast_grep_runtime_dir"]).strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".sherry" / "runtime" / "ast-grep" / _runtime_slug()


def _runtime_candidates() -> list[str]:
    """Tier 2: the provisioned runtime binary."""
    return [str(runtime_dir() / _binary_name())]


def _code_intel_bin_candidates() -> list[str]:
    """Tier 3: the in-repo code-intel bin cache (gitignored)."""
    bin_dir = CODE_INTEL_DIR / "ast-grep" / "bin"
    return [
        str(bin_dir / _ast_grep_binary_name()),
        str(bin_dir / _binary_name()),
    ]


def _which(command: str) -> str | None:
    """Cross-platform ``which(1)`` honoring ``PATHEXT`` on Windows."""
    suffixes = _executable_suffixes()
    path_env = os.environ.get("PATH") or os.environ.get("Path") or ""
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        for suffix in suffixes:
            candidate = Path(entry) / (command + suffix)
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def _path_candidates() -> list[str]:
    """Tier 4: ``ast-grep`` / ``sg`` found on PATH."""
    found: list[str] = []
    for name in (_ast_grep_binary_name(), _binary_name()):
        path = _which(name)
        if path:
            found.append(path)
    return found


def _homebrew_candidates() -> list[str]:
    """Tier 5: Homebrew / Linuxbrew install prefixes."""
    if sys.platform == "darwin":
        prefixes = ["/opt/homebrew/bin", "/usr/local/bin"]
    elif sys.platform == "linux":
        prefixes = ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin"]
    else:
        return []
    names = (_ast_grep_binary_name(), _binary_name())
    return [str(Path(prefix) / name) for prefix in prefixes for name in names]


def tier_candidates() -> list[tuple[str, list[str]]]:
    """Return the ordered ``(tier_name, candidates)`` list for discovery."""
    return [
        ("env_override", _env_override_candidates()),
        ("runtime", _runtime_candidates()),
        ("code_intel_bin", _code_intel_bin_candidates()),
        ("path", _path_candidates()),
        ("homebrew", _homebrew_candidates()),
    ]


def _first_accepted(candidates: list[str]) -> str | None:
    """Return the first candidate that exists and passes the probe."""
    for candidate in candidates:
        if _accepts(candidate):
            return candidate
    return None


def resolve_sg_binary() -> str | None:
    """Resolve the ast-grep binary across the 5 tiers, or ``None`` if not found.

    Results are cached per process; a cached path is reused while the file still
    exists. Call :func:`_clear_cache_for_tests` after installing a binary so the
    next resolution observes it.
    """
    global _resolution_cache
    if _resolution_cache is not None and _candidate_exists(_resolution_cache):
        return _resolution_cache

    all_candidates: list[str] = []
    for _tier, candidates in tier_candidates():
        all_candidates.extend(candidates)

    result = _first_accepted(all_candidates)
    if result:
        _resolution_cache = result
    return result


def _clear_cache_for_tests() -> None:
    """Drop the per-process resolution cache (test/provisioning hook)."""
    global _resolution_cache
    _resolution_cache = None
