"""Desktop shell capability checks that do not need a Tauri build.

The close-to-tray path calls the frontend ``getCurrentWindow().hide()`` IPC
command. Tauri 2 grants only read-only window introspection through
``core:default``; the mutating ``hide`` command needs its own permission or the
window stays where it is after the dialog acknowledges the close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = ROOT / "client" / "src-tauri" / "capabilities" / "default.json"


def test_close_to_tray_hide_is_granted():
    permissions = json.loads(CAPABILITIES.read_text(encoding="utf-8"))["permissions"]

    assert "core:window:allow-hide" in permissions
