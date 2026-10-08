"""End-to-end: a poisoned parent environment does not reach the child.

The unit tests prove the filter; these prove the boundary. A real ``/bin/sh`` and a
real ``python`` child are spawned with ``env=scrub_env(poisoned)`` and asked what
they actually received — the shell prints its own environment, and a startup file
that would have run is proved not to have run by the marker it would have left.

The strict tier is exercised the same way: with ``SHERRY_STRICT_ENV_HIJACK`` on,
the loader variables disappear from the child's environment too.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from agent.tools.pub_base import env_scrub
from agent.tools.pub_base.env_scrub import scrub_env

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    poisoned = {
        "PATH": "/usr/bin:/bin",
        "BASH_ENV": str(extra.pop("bash_env", "/tmp/should-not-exist")),
        "PYTHONSTARTUP": str(extra.pop("pythonstartup", "/tmp/should-not-exist")),
        "PYTHONPATH": "/tmp/attacker-modules",
        "LD_PRELOAD": "/tmp/attacker.so",
        "HARMLESS_FLAG": "kept",
        **extra,
    }
    return scrub_env(poisoned)


def test_a_real_shell_child_never_sees_the_hijack_variables(tmp_path: pathlib.Path):
    marker = tmp_path / "bash_env_ran"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch {marker}\n", encoding="utf-8")

    env = _child_env({"bash_env": str(startup)})
    result = subprocess.run(
        ["/bin/sh", "-c", "env"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    child_env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert "BASH_ENV" not in child_env
    assert "PYTHONPATH" not in child_env
    assert "PYTHONSTARTUP" not in child_env
    assert child_env.get("HARMLESS_FLAG") == "kept", "ordinary variables still pass through"
    assert not marker.exists(), "the startup file ran in the child"


def test_a_real_python_child_starts_clean(tmp_path: pathlib.Path):
    startup = tmp_path / "pythonstartup.py"
    startup.write_text("raise SystemExit('PYTHONSTARTUP ran in the child')\n", encoding="utf-8")

    env = _child_env({"pythonstartup": str(startup)})
    result = subprocess.run(
        [sys.executable, "-c", "import os; print('|'.join(sorted(os.environ)))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert result.returncode == 0, result.stderr
    child_names = set(result.stdout.strip().split("|"))
    assert "PYTHONSTARTUP" not in child_names
    assert "PYTHONPATH" not in child_names
    assert "HARMLESS_FLAG" in child_names


def test_strict_mode_also_strips_the_loader_variables(monkeypatch: pytest.MonkeyPatch):
    """With the strict switch on, the loader vars join the block in a real child."""
    monkeypatch.setattr(env_scrub, "_STRICT_ENV_HIJACK", True)

    env = _child_env({})
    assert "LD_PRELOAD" not in env

    result = subprocess.run(
        ["/bin/sh", "-c", "env"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "LD_PRELOAD" not in result.stdout
    assert "LD_LIBRARY_PATH" not in result.stdout


def test_the_ptc_runner_still_gets_its_stub_directory():
    """The one child that needs PYTHONPATH sets it after scrubbing — pinned here.

    Blocking ``PYTHONPATH`` is only safe while that stays true; this asserts the
    ordering rather than the value, so the guard survives any stub-dir change.
    """
    from agent.tools.ptc.runner import build_child_env

    env = build_child_env("/tmp/ptc-stub", "token")

    assert env["PYTHONPATH"] == "/tmp/ptc-stub"
    assert env["SHERRY_PTC_RPC_TOKEN"] == "token"
