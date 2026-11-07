"""The writable app-data root (``APP_HOME``) resolver — one implementation.

Four modules compute ``APP_HOME`` at import time: ``config/path.py`` (every
runtime directory), ``config/sherry_settings.py`` (``sherry.jsonc``),
``config/models_settings.py`` (``.models``) and ``config/network_settings.py``
(``.network``). They cannot simply import ``config.path`` — it imports
``config.sherry_settings`` at the top of the module (to read a setting), so a
reverse import would be a cycle.

The rules live here instead, in a module with NO project imports, so all four
resolve the same directory by construction:

* a packaged shell passes ``--sherry-home <dir>`` on the command line and sets
  no environment variable;
* ``SHERRY_HOME`` stays as the transitional fallback for manual runs;
* a checkout sets neither, so the caller falls back to its own code root.

This module must stay import-time-safe: it is loaded before anything else in
``config`` and must not import from ``config`` or any application package.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def cli_arg(name: str) -> str | None:
    """The value of ``--name value`` / ``--name=value`` in ``sys.argv``, or None.

    A direct argv scan instead of argparse on purpose: these modules compute
    their constants at IMPORT time, before any entrypoint's parser would run,
    and the shell passes the flags to either entrypoint (``python -m server``
    and the frozen ``backend_entry``) without either one needing to know.
    """
    argv = sys.argv
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
    return None


def resolve_sherry_home_raw() -> str:
    """The writable-home request: ``--sherry-home`` first, then the env var.

    The environment variable is a TRANSITIONAL fallback: a packaged shell
    passes the CLI flag and no longer sets the env, so the env path only
    serves manual runs against an older shell or a plain
    ``SHERRY_HOME=x python -m server`` invocation. A checkout sets neither.
    """
    from_cli = cli_arg("--sherry-home")
    if from_cli and from_cli.strip():
        return from_cli.strip()
    return os.environ.get("SHERRY_HOME", "").strip()


def resolve_app_home(code_root: Path) -> Path:
    """``APP_HOME`` for a module whose code tree lives at ``code_root``.

    Unset (a checkout, a server, CI) answers ``code_root`` exactly, so nothing
    changes for existing installs; a packaged desktop build answers the
    OS app-data directory the shell passed.
    """
    raw = resolve_sherry_home_raw()
    return Path(raw).expanduser().resolve() if raw else code_root


def resolve_code_root(code_root: Path) -> Path:
    """The code root that owns SHIPPED data files (templates, not user data).

    Under PyInstaller, shipped data lives under ``sys._MEIPASS`` (onedir =
    ``_internal``), NOT under the synthetic ``__file__`` of a frozen module.
    A checkout (no ``_MEIPASS``) keeps the historical ``code_root`` exactly.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass).resolve() if meipass else code_root
