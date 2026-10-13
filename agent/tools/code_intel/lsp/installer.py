"""LSP server auto-install — allow-listed subprocess execution with timeout.

Mirrors oh-my-openagent ``server-definitions.ts`` + ``install-decision.ts``.
Auto-install is **off by default** (``lsp_auto_install=False``): the caller gets
the install hint instead of a silent package-manager run. When enabled, only the
command for the requested language from ``lsp_auto_install_commands`` is ever
executed — arbitrary command strings are never accepted.
"""

from __future__ import annotations

import os
import subprocess

from loguru import logger

from config.features import LSP

from .resolver import _clear_cache_for_tests, get_install_hint, resolve_lsp_server

__all__ = ["install_lsp_server"]


def _failure(message: str) -> dict:
    return {"ok": False, "binary_path": None, "message": message}


def install_lsp_server(language: str, cwd: str | None = None) -> dict:
    """Install the LSP server for *language*.

    Returns ``{"ok": bool, "binary_path": str | None, "message": str}``; never
    raises (a missing interpreter or a hung installer degrades to a message).
    """
    if not LSP["lsp_auto_install"]:
        return _failure(f"Auto-install disabled. Install manually: {get_install_hint(language)}")

    command = LSP["lsp_auto_install_commands"].get(language)
    if not command:
        return _failure(
            f"No auto-install command for {language}. Install manually: {get_install_hint(language)}"
        )

    # Lazy: importing pub_base pulls the runtime/robyn chain at import time.
    from agent.tools.pub_base.env_scrub import scrub_env

    timeout = LSP["lsp_install_timeout_s"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or os.getcwd(),
            env=scrub_env(os.environ.copy()),
        )
    except subprocess.TimeoutExpired:
        return _failure(f"Install timed out after {timeout}s: {' '.join(command)}")
    except FileNotFoundError:
        return _failure(f"Install tool not found: {command[0]}. Is it on PATH?")
    except OSError as exc:
        return _failure(f"Install could not start: {exc}")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return _failure(f"Install failed (exit {proc.returncode}): {stderr[:500]}")

    logger.info("LSP server installed for {}: {}", language, " ".join(command))

    # Invalidate the cache so re-resolution observes the freshly installed binary.
    _clear_cache_for_tests()
    binary_path = resolve_lsp_server(language, cwd)
    if binary_path:
        return {
            "ok": True,
            "binary_path": binary_path,
            "message": f"Installed {language} LSP server",
        }
    return _failure("Install ran but the binary was not found on PATH")
