"""Entry point of the PACKAGED desktop backend.

PyInstaller needs a script; the real program is the ordinary server entry
(``python -m server``), so this runs exactly that module as ``__main__`` —
one behavior, no second boot path to keep in sync.

The Tauri shell passes ``--sherry-home <app-data-dir>`` (plus the bare
``--sherry-desktop`` flag) on the command line; ``config/path.py`` scans
``sys.argv`` for them (the environment variables remain a transitional
fallback for older shells and manual runs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# A STATIC import on purpose: PyInstaller analyses bytecode, and a
# ``runpy.run_module("server")`` would be invisible to it (the first build
# shipped without the ``server`` package at all). Importing the module also
# runs its module-level boot, exactly like ``python -m server`` does.
from server.__main__ import main

if __name__ == "__main__":
    # A frozen app has no reliable cwd: run from the executable's own directory
    # so anything path-relative stays near the bundle.
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    main()
