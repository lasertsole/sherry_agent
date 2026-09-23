"""LSP server binary resolution — multi-tier discovery with per-process caching.

Probing order (mirrors oh-my-openagent ``server-installation.ts``, extended with
the ast-grep runtime/Homebrew tiers so the discovery surface stays consistent):

  1. Explicit path — the language's ``lsp_*_server`` / ``command[0]`` is absolute
  2. Repo-local bin dirs — **marker-gated**: ``pyproject.toml`` → ``.venv/bin``,
     ``package.json`` → ``node_modules/.bin``, ``Cargo.toml`` → ``target/debug``,
     ``go.mod`` → ``bin`` (walking up to the repo root)
  3. Sherry runtime — ``~/.sherry/runtime/lsp/<slug>`` (``SHERRY_LSP_RUNTIME_DIR``)
  4. ``PATH`` lookup (Windows ``PATHEXT`` aware)
  5. Homebrew / Linuxbrew prefixes
  → ``None`` (not_installed; the caller returns an install hint + fallback)

Results are cached per process keyed by ``(cwd, command, platform)``; a cached
path is reused only while the file still exists. :func:`_clear_cache_for_tests`
invalidates it after an install.
"""

from __future__ import annotations

import os
import platform as _platform
import sys
from pathlib import Path

from config.features import LSP

_resolution_cache: dict[str, str | None] = {}

__all__ = [
    "get_install_hint",
    "get_local_install_hint",
    "resolve_lsp_server",
    "runtime_dir",
]


def _runtime_slug() -> str:
    """Return the ``<platform>-<arch>`` runtime slug (arch override: ``SHERRY_LSP_ARCH``)."""
    plat_name = sys.platform
    plat = "win32" if plat_name == "win32" else ("darwin" if plat_name == "darwin" else "linux")
    raw_arch = os.environ.get("SHERRY_LSP_ARCH", "").strip().lower() or _platform.machine().lower()
    arch = "arm64" if raw_arch in ("arm64", "aarch64") else "x64"
    return f"{plat}-{arch}"


def runtime_dir() -> Path:
    """Return the user-placement runtime dir (config / env override, else default)."""
    configured = str(LSP["lsp_runtime_dir"]).strip()
    if configured:
        return Path(configured).expanduser()
    override = os.environ.get("SHERRY_LSP_RUNTIME_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".sherry" / "runtime" / "lsp" / _runtime_slug()


def _executable_suffixes() -> list[str]:
    """Return candidate suffixes for executable lookup (Windows PATHEXT aware)."""
    if sys.platform != "win32":
        return [""]
    pathext = os.environ.get("PATHEXT", "")
    suffixes = [entry.lower() for entry in pathext.split(";") if entry]
    return list(dict.fromkeys(["", *suffixes, ".exe", ".cmd", ".bat"]))


def _probe(directory: str, command: str, suffixes: list[str]) -> str | None:
    """Return the first existing regular file ``directory/command<suffix>``."""
    for suffix in suffixes:
        candidate = Path(directory) / (command + suffix)
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _explicit_override(language: str) -> str | None:
    """Return the per-language explicit server value from the base config fields."""
    if language == "python":
        value = LSP["lsp_python_server"]
    elif language == "typescript":
        value = LSP["lsp_typescript_server"]
    elif language == "rust":
        value = LSP["lsp_rust_server"]
    elif language == "go":
        value = LSP["lsp_go_server"]
    else:
        return None
    stripped = value.strip()
    return stripped or None


def _server_command(language: str) -> str | None:
    """Return the configured command name/path for *language*, or ``None``."""
    spec = LSP["lsp_supported_servers"].get(language)
    if not spec:
        return None
    override = _explicit_override(language)
    return override or spec["command"][0]


def _resolve_local(command: str, cwd: str, suffixes: list[str], language: str) -> str | None:
    """Walk up from *cwd*, checking marker-gated bin dirs, stopping at the repo root."""
    rules = LSP["lsp_repo_local_bin_rules"].get(language, {})
    markers = rules.get("markers", [])
    bin_dirs = rules.get("bin_dirs", [])

    current = Path(cwd).resolve()
    while True:
        if any((current / marker).exists() for marker in markers):
            for bin_dir in bin_dirs:
                found = _probe(str(current / bin_dir), command, suffixes)
                if found:
                    return found
        if (current / ".git").exists():
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _resolve_from_path(command: str, suffixes: list[str]) -> str | None:
    path_env = os.environ.get("PATH") or os.environ.get("Path") or ""
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        found = _probe(entry, command, suffixes)
        if found:
            return found
    return None


def _homebrew_dirs() -> list[str]:
    if sys.platform == "darwin":
        return ["/opt/homebrew/bin", "/usr/local/bin"]
    if sys.platform == "linux":
        return ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin"]
    return []


def _resolve_uncached(command: str, work_dir: str, language: str) -> str | None:
    suffixes = _executable_suffixes()

    # 1. Explicit absolute path (a missing explicit path falls through, fail-open)
    if os.path.isabs(command):
        return command if Path(command).is_file() else None

    # 2. Repo-local bin dirs (marker-gated)
    local = _resolve_local(command, work_dir, suffixes, language)
    if local:
        return local

    # 3. Sherry runtime placement
    runtime = _probe(str(runtime_dir()), command, suffixes)
    if runtime:
        return runtime

    # 4. PATH
    from_path = _resolve_from_path(command, suffixes)
    if from_path:
        return from_path

    # 5. Homebrew / Linuxbrew
    for directory in _homebrew_dirs():
        found = _probe(directory, command, suffixes)
        if found:
            return found
    return None


def resolve_lsp_server(language: str, cwd: str | None = None) -> str | None:
    """Resolve the LSP server binary for *language*, or ``None`` when not installed."""
    command = _server_command(language)
    if not command:
        return None
    work_dir = cwd or os.getcwd()

    if not LSP["lsp_resolve_cache_enabled"]:
        return _resolve_uncached(command, work_dir, language)

    cache_key = f"{work_dir}:{command}:{sys.platform}"
    if cache_key in _resolution_cache:
        cached = _resolution_cache[cache_key]
        if cached is None or Path(cached).is_file():
            return cached

    result = _resolve_uncached(command, work_dir, language)
    _resolution_cache[cache_key] = result
    return result


def get_install_hint(language: str) -> str:
    """Return a human-readable install hint for a language's LSP server."""
    return LSP["lsp_install_hints"].get(
        language, f"Install the LSP server for {language} and ensure it is on PATH."
    )


def get_local_install_hint(language: str) -> str | None:
    """Return the project-local install command, or ``None`` when unavailable."""
    server = LSP["lsp_supported_servers"].get(language)
    return server.get("local_install") if server else None


def _clear_cache_for_tests() -> None:
    """Drop the per-process resolution cache (test/provisioning hook)."""
    _resolution_cache.clear()


# Alias matching the 1A ast-grep resolver naming, so tests and installers can use
# the same call regardless of which subsystem they came from.
_reset_cache_for_tests = _clear_cache_for_tests
