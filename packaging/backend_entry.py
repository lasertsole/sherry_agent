"""Entry point of the PACKAGED desktop backend.

PyInstaller needs a script; the real program is the ordinary server entry
(``python -m server``), so this runs exactly that module as ``__main__`` —
one behavior, no second boot path to keep in sync.

A frozen executable cannot run ``python -c``, so the local model daemons
re-execute THIS binary with ``--sherry-daemon <module>`` (see
``models/daemon/base.py``) and the flag dispatches to that module's ``main()``
before the server boot. The server import is therefore inside the normal branch:
a daemon child must not initialise logging, event-loop patching or LangSmith
tracing on its way to its own model loader.

The Tauri shell passes ``--sherry-home <app-data-dir>`` (plus the bare
``--sherry-desktop`` flag) on the command line; ``config/_home.py`` scans
``sys.argv`` for them at import time (the environment variables remain a
transitional fallback for older shells and manual runs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def daemon_module(argv: list[str] | None = None) -> str | None:
    """The module named by ``--sherry-daemon <module>``, or None."""
    args = sys.argv if argv is None else argv
    for index, arg in enumerate(args):
        if arg == "--sherry-daemon" and index + 1 < len(args):
            return args[index + 1].strip() or None
        if arg.startswith("--sherry-daemon="):
            return arg.split("=", 1)[1].strip() or None
    return None


def main() -> None:
    module = daemon_module()
    if module is not None:
        import importlib

        importlib.import_module(module).main()
        return

    # A STATIC import on purpose: PyInstaller analyses bytecode, and a
    # ``runpy.run_module("server")`` would be invisible to it (the first build
    # shipped without the ``server`` package at all). Importing the module also
    # runs its module-level boot, exactly like ``python -m server`` does.
    from server.__main__ import main as server_main

    server_main()


if __name__ == "__main__":
    # A frozen app has no reliable cwd: run from the executable's own directory
    # so anything path-relative stays near the bundle.
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    main()
