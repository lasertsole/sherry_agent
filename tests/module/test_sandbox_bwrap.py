"""TDD tests for the Linux bwrap sandbox backend (Task 3).

仅验证构造逻辑，未在 Linux 实机验证（Windows 开发机无 bwrap 二进制）。
All subprocess interaction is mocked — no real bwrap is ever executed here.

argv order is load-bearing:
- ``--clearenv`` must precede every ``--setenv`` (env allowlist semantics);
- ``--ro-bind / /`` keeps the whole root read-only;
- writable locations are ROOT_DIR and TEMP_DIR only.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

import agent.tools.pub_base.sandbox_bwrap as sandbox_bwrap
from agent.tools.pub_base.sandbox_bwrap import BwrapBackend

PROBE_ARGV = [
    "bwrap",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "true",
]


@pytest.fixture(autouse=True)
def reset_probe_cache():
    """Class-level probe cache must not leak between tests."""
    BwrapBackend._probe_cache = None
    yield
    BwrapBackend._probe_cache = None


# --------------------------------------------------------------------------
# wrap()
# --------------------------------------------------------------------------


def test_wrap_argv_starts_with_bwrap_ro_bind():
    cmd, _env = BwrapBackend().wrap(["echo", "hi"], {})
    assert cmd[0:3] == ["bwrap", "--ro-bind", "/"]


def test_wrap_contains_unshare_all_and_hardening_flags():
    cmd, _env = BwrapBackend().wrap(["echo", "hi"], {})
    for flag in (
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--tmpfs",
        "--dev",
        "--proc",
    ):
        assert flag in cmd, f"missing {flag}"


def test_wrap_setenv_count_matches_env_entries():
    env = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "FOO": "bar"}
    cmd, returned_env = BwrapBackend().wrap(["echo", "hi"], env)
    assert cmd.count("--setenv") == len(env)
    # env is passed through unchanged (scrub happened upstream)
    assert returned_env == env


def test_wrap_clearenv_precedes_all_setenv():
    cmd, _env = BwrapBackend().wrap(["echo", "hi"], {"PATH": "/usr/bin", "FOO": "bar"})
    first_setenv = cmd.index("--setenv")
    assert "--clearenv" in cmd[:first_setenv]
    assert cmd[first_setenv : first_setenv + 3] == ["--setenv", "PATH", "/usr/bin"]


def test_wrap_binds_root_and_temp_dirs_writable():
    cmd, _env = BwrapBackend().wrap(["echo", "hi"], {})
    root, temp = str(sandbox_bwrap.ROOT_DIR), str(sandbox_bwrap.TEMP_DIR)
    # exactly two writable binds (the --ro-bind token is not counted)
    assert cmd.count("--bind") == 2
    assert ["--bind", root, root] == cmd[cmd.index("--bind") : cmd.index("--bind") + 3]
    temp_pos = cmd.index(["--bind", temp, temp][1])
    assert cmd[temp_pos - 1 : temp_pos + 2] == ["--bind", temp, temp]


def test_wrap_trailing_separator_and_original_command():
    cmd, _env = BwrapBackend().wrap(["echo", "hi"], {"PATH": "/usr/bin"})
    sep = cmd.index("--")
    assert cmd[sep + 1 :] == ["echo", "hi"]


def test_wrap_dedupes_writable_paths_when_equal(monkeypatch):
    monkeypatch.setattr(sandbox_bwrap, "ROOT_DIR", "/same/dir")
    monkeypatch.setattr(sandbox_bwrap, "TEMP_DIR", "/same/dir")
    cmd, _env = BwrapBackend().wrap(["echo"], {})
    assert cmd.count("--bind") == 1
    assert cmd.count("/same/dir") == 2  # --bind src dst


def test_wrap_return_type_is_tuple_of_list_and_dict():
    result = BwrapBackend().wrap(["echo"], {"A": "1"})
    assert isinstance(result, tuple)
    cmd, env = result
    assert isinstance(cmd, list)
    assert isinstance(env, dict)


# --------------------------------------------------------------------------
# probe()
# --------------------------------------------------------------------------


def test_probe_success_runs_smoke_command_and_returns_true():
    completed = subprocess.CompletedProcess(args=PROBE_ARGV, returncode=0)
    with mock.patch.object(sandbox_bwrap.subprocess, "run", return_value=completed) as run:
        assert BwrapBackend().probe() is True
    run.assert_called_once()
    assert run.call_args.args[0] == PROBE_ARGV
    assert run.call_args.kwargs.get("timeout") == 3


def test_probe_file_not_found_returns_false():
    with mock.patch.object(sandbox_bwrap.subprocess, "run", side_effect=FileNotFoundError):
        assert BwrapBackend().probe() is False


def test_probe_timeout_returns_false():
    with mock.patch.object(
        sandbox_bwrap.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="bwrap", timeout=3),
    ):
        assert BwrapBackend().probe() is False


def test_probe_nonzero_returncode_returns_false():
    completed = subprocess.CompletedProcess(args=PROBE_ARGV, returncode=1)
    with mock.patch.object(sandbox_bwrap.subprocess, "run", return_value=completed):
        assert BwrapBackend().probe() is False


def test_probe_cache_effective_second_call_does_not_reinvoke_subprocess():
    completed = subprocess.CompletedProcess(args=PROBE_ARGV, returncode=0)
    with mock.patch.object(sandbox_bwrap.subprocess, "run", return_value=completed) as run:
        backend = BwrapBackend()
        assert backend.probe() is True
        assert backend.probe() is True
    assert run.call_count == 1


def test_probe_cache_is_class_level_across_instances():
    completed = subprocess.CompletedProcess(args=PROBE_ARGV, returncode=0)
    with mock.patch.object(sandbox_bwrap.subprocess, "run", return_value=completed) as run:
        assert BwrapBackend().probe() is True
    assert run.call_count == 1
    with mock.patch.object(sandbox_bwrap.subprocess, "run") as second_run:
        # a fresh instance must reuse the class-level verdict
        assert BwrapBackend().probe() is True
    second_run.assert_not_called()


def test_probe_failure_result_is_also_cached():
    with mock.patch.object(sandbox_bwrap.subprocess, "run", side_effect=FileNotFoundError) as run:
        backend = BwrapBackend()
        assert backend.probe() is False
        assert backend.probe() is False
    assert run.call_count == 1
