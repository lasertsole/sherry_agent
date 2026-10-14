"""``SHERRY_WORKSPACE_DIR`` — the isolated-workspace override.

``config.path.WORKSPACE_DIR`` is an import-time constant (persona files,
per-session directories, plan knowledge and the memory markdown tree all derive
from it), so the eval harness must be able to point it at a throwaway directory
BEFORE the module is first imported. This pins the override, its derived paths,
and the byte-for-byte historical default when it is unset.

The constant is computed once per interpreter, so the override is exercised in
a subprocess rather than by monkeypatching a reloaded module: a reload would
also have to reproduce every module that already bound the old value.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]


_PROBE = """
import sys
sys.path.insert(0, {root!r})
import config.path as path
print("|".join(str(p) for p in (path.WORKSPACE_DIR, path.SESSIONS_DIR, path.MEMORY_DIR,
                                path.KNOWLEDGE_DIR, path.PLAN_KNOWLEDGE_DIR,
                                path.HEARTBEAT_PATH, path.WORKSPACE_TEMPLATE_DIR)))
"""


def _probe(env_overrides: dict[str, str]) -> list[str]:
    """Import config.path in a clean interpreter and read the workspace paths."""
    import os

    env = {**os.environ, **env_overrides}
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(root=str(REPO_ROOT))],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip().splitlines()[-1].split("|")


def test_workspace_dir_defaults_to_the_app_home():
    values = _probe({"SHERRY_WORKSPACE_DIR": ""})
    workspace, sessions, memory, knowledge, plans, heartbeat, template = values

    assert workspace.endswith("/workspace") or workspace.endswith("\\workspace")
    assert sessions == f"{workspace}/sessions"
    assert memory == f"{workspace}/memory"
    assert knowledge == f"{workspace}/knowledge"
    assert plans == f"{knowledge}/plans"
    assert heartbeat == f"{workspace}/HEARTBEAT.md"
    assert template == f"{workspace}/template"


def test_the_override_moves_the_workspace_and_every_derived_path(tmp_path: Path):
    target = tmp_path / "instance-workspace"

    workspace, sessions, memory, knowledge, plans, heartbeat, template = _probe(
        {"SHERRY_WORKSPACE_DIR": str(target)}
    )

    assert workspace == str(target)
    assert sessions == f"{target}/sessions"
    assert memory == f"{target}/memory"
    assert knowledge == f"{target}/knowledge"
    assert plans == f"{knowledge}/plans"
    assert heartbeat == f"{target}/HEARTBEAT.md"
    assert template == f"{target}/template"


def test_blank_and_whitespace_values_fall_back_to_the_default():
    default = _probe({"SHERRY_WORKSPACE_DIR": ""})
    blank = _probe({"SHERRY_WORKSPACE_DIR": "   "})

    assert blank == default
