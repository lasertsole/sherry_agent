"""Direction 3 guard: heavy optional dependencies must not enter the boot set.

The 2026-11 memory optimization measured what the target imports actually cost
(``torch`` ≈ +490 MB / 5.9 s, ``transformers`` ≈ +41 MB / 9.9 s, and the listed
``cv2`` / ``mineru_vl_utils`` / ``leidenalg`` / ``snkv`` / ``tree_sitter`` /
``gguf`` / ``llama_cpp`` are not installed in the base environment). None of
them may be pulled in by the boot import chain: moving an import that never
runs would be a no-op, so the optimization's contract is this absent set.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.perf, pytest.mark.regression, pytest.mark.timeout(180)]

_ROOT = Path(__file__).resolve().parents[2]
_HEAVY = (
    "torch",
    "transformers",
    "cv2",
    "mineru_vl_utils",
    "leidenalg",
    "snkv",
    "tree_sitter",
    "gguf",
    "llama_cpp",
)

# Mirrors the imports server/__main__.main() performs before serving. Nothing
# here calls an init() (no threads, no DB writes) — it is an import profile.
_BOOT_IMPORTS = """
import json
import sys

import server.__main__  # module-level logging/env boot
from models.providers.registry import register_provider_registry  # noqa: F401
import runtime.process.crash_loop_breaker  # noqa: F401
from agent.core import init as init_agent_core  # noqa: F401
from agent.tools import (  # noqa: F401
    build_python_repl_tool,
    build_read_file_tool,
    build_write_file_tool,
)
from server.service.skill_scanner import scan_skill  # noqa: F401
from context_engine.curator import init as init_curator  # noqa: F401
from skills.builtin.core.cron.scripts import init as init_cron  # noqa: F401
from server.trigger import app, init as init_trigger  # noqa: F401
from server.service import auto_turn as auto_turn_service  # noqa: F401
from server.trigger.ws import messages as ws_messages  # noqa: F401
from server.service.browser_manager import get_browser_manager  # noqa: F401
from server.service.checkpoint_maintenance import clean_checkpoints_at_boot  # noqa: F401
from server.service.boot_recovery import reconcile_at_boot  # noqa: F401
from server.service.effects.boot import run_restart_recovery  # noqa: F401
from server.service.lane_lifecycle import install_lane_lifecycle  # noqa: F401
from server.DAO import auth_store  # noqa: F401
from server.service import auth_service  # noqa: F401
from runtime.session.git_head import prime_git_head_from_store  # noqa: F401
from runtime.session.project_dir import prime_mem_from_store  # noqa: F401

heavy = HEAVY
print("IMPORTED:" + json.dumps([module for module in heavy if module in sys.modules]))
"""


def test_boot_import_set_excludes_the_heavy_optional_dependencies():
    script = f"HEAVY = {_HEAVY!r}\n" + _BOOT_IMPORTS
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:]
    marker = [line for line in result.stdout.splitlines() if line.startswith("IMPORTED:")]
    assert marker, f"the boot profile printed no result:\n{result.stdout[-1000:]}"
    loaded = json.loads(marker[-1].removeprefix("IMPORTED:"))
    assert loaded == [], (
        "the boot import chain pulled in a heavy optional dependency; move that "
        f"import into its feature function: {loaded}"
    )
