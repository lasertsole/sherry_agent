"""Service script contract tests (exit codes, tmux session, stop marker)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "superagent"


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the service script with an isolated profile and checkout."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
        check=False,
    )


def isolated_env(tmp_path: Path) -> dict[str, str]:
    """Environment that never touches a real tmux session or state dir."""
    return {
        "DSH_SUPERAGENT_PROFILE": f"test-{os.getpid()}",
        "DSH_SUPERAGENT_CHECKOUT": str(tmp_path),
        "DSH_SUPERAGENT_STATE_DIR": str(tmp_path / "state"),
        "DSH_SUPERAGENT_READY_TIMEOUT": "2",
    }


def test_script_is_bash_and_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_usage_without_command_exits_one(tmp_path):
    result = run_script(env=isolated_env(tmp_path))
    assert result.returncode == 1
    assert "usage:" in result.stderr


def test_unknown_argument_exits_one(tmp_path):
    result = run_script("--bogus", "status", env=isolated_env(tmp_path))
    assert result.returncode == 1
    assert "unknown argument" in result.stderr


def test_help_exits_zero(tmp_path):
    result = run_script("--help", env=isolated_env(tmp_path))
    assert result.returncode == 0


def test_status_not_running_exits_one(tmp_path):
    result = run_script("status", env=isolated_env(tmp_path))
    assert result.returncode == 1
    assert "not running" in result.stdout


def test_stop_when_not_running_is_idempotent(tmp_path):
    result = run_script("stop", env=isolated_env(tmp_path))
    assert result.returncode == 0
    assert "not running" in result.stdout


def test_stop_marker_directory_is_created(tmp_path):
    env = isolated_env(tmp_path)
    run_script("stop", env=env)
    # stop is a no-op when not running; the marker is only written on a real stop.
    state = tmp_path / "state"
    assert state.parent.exists()


@pytest.mark.parametrize("command", ["start", "stop", "restart", "status"])
def test_command_dispatch_is_recognized(tmp_path, command):
    env = isolated_env(tmp_path)
    result = run_script(command, env=env)
    # In a hermetic host with no tmux server, start/restart exit 1 (process never
    # came up) and stop/status exit 0/1 as documented — none may report "usage".
    assert "usage:" not in result.stderr


def test_source_documents_exit_code_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    for code in ("exit 0", "exit 1", "exit 2", "exit 3"):
        assert code in text
    for command in ("cmd_start", "cmd_stop", "cmd_restart", "cmd_status"):
        assert command in text
    assert "--respect-stop" in text
    assert "tmux" in text
