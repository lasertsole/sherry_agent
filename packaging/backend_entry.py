"""Entry point of the PACKAGED desktop backend.

PyInstaller needs a script; the real program is the ordinary server entry
(``python -m server``), so this runs exactly that module as ``__main__`` —
one behavior, no second boot path to keep in sync.

``SHERRY_HOME`` decides where the backend writes (set by the Tauri shell to the
OS app-data directory); see ``config/path.py``.
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
