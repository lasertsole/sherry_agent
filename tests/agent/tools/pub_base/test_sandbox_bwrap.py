"""TDD tests for the Linux bwrap sandbox backend (Task 3).

Construction logic only — never verified on a real Linux machine (the Windows
dev box has no bwrap binary).
All subprocess interaction is mocked — no real bwrap is ever executed here.

argv order is load-bearing:
- ``--clearenv`` must precede every ``--setenv`` (env allowlist semantics);
- ``--ro-bind / /`` keeps the whole root read-only;
- writable locations are ROOT_DIR and TEMP_DIR only.
"""

from __future__ import annotations

import os
import subprocess
from unittest import mock

import pytest

import agent.tools.pub_base.sandbox_bwrap as sandbox_bwrap
from agent.tools.pub_base.sandbox import _sensitive_read_paths
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


pytestmark = [pytest.mark.module]


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
# Read shield (P0-1): sensitive paths masked from reads
# --------------------------------------------------------------------------


def _sublist_index(cmd: list[str], sub: list[str]) -> int:
    for i in range(len(cmd) - len(sub) + 1):
        if cmd[i : i + len(sub)] == sub:
            return i
    return -1


class TestReadShield:
    def test_existing_dir_masked_with_empty_dir_after_writable_binds(self, tmp_path, monkeypatch):
        secret = tmp_path / "ssh"
        secret.mkdir()
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(sandbox_bwrap, "_sensitive_read_paths", lambda: [secret])
        monkeypatch.setattr(sandbox_bwrap, "_EMPTY_MASK_DIR", empty)

        cmd, _env = BwrapBackend().wrap(["echo"], {})

        mask_index = _sublist_index(cmd, ["--ro-bind", str(empty), str(secret)])
        assert mask_index != -1, f"missing read-shield mount in {cmd!r}"
        last_writable_bind = max(i for i, token in enumerate(cmd) if token == "--bind")
        assert mask_index > last_writable_bind, "read-shield must come after the writable binds"

    def test_missing_empty_dir_falls_back_to_tmpfs(self, tmp_path, monkeypatch):
        secret = tmp_path / "ssh"
        secret.mkdir()
        monkeypatch.setattr(sandbox_bwrap, "_sensitive_read_paths", lambda: [secret])
        monkeypatch.setattr(sandbox_bwrap, "_EMPTY_MASK_DIR", tmp_path / "absent")

        cmd, _env = BwrapBackend().wrap(["echo"], {})

        assert _sublist_index(cmd, ["--tmpfs", str(secret)]) != -1

    def test_existing_file_masked_with_dev_null(self, tmp_path, monkeypatch):
        secret = tmp_path / "credentials"
        secret.write_text("token", encoding="utf-8")
        monkeypatch.setattr(sandbox_bwrap, "_sensitive_read_paths", lambda: [secret])

        cmd, _env = BwrapBackend().wrap(["echo"], {})

        assert _sublist_index(cmd, ["--ro-bind", "/dev/null", str(secret)]) != -1

    def test_nonexistent_path_is_skipped_without_error(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(sandbox_bwrap, "_sensitive_read_paths", lambda: [missing])

        cmd, _env = BwrapBackend().wrap(["echo"], {})

        assert str(missing) not in cmd

    def test_env_extension_and_defaults_reach_argv(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        custom = tmp_path / "custom-secret"
        custom.mkdir()
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("SHERRY_DENY_READ_PATHS", f"{custom}{os.pathsep}")
        monkeypatch.setattr(sandbox_bwrap, "_EMPTY_MASK_DIR", empty)

        cmd, _env = BwrapBackend().wrap(["echo"], {})

        assert _sublist_index(cmd, ["--ro-bind", str(empty), str(custom)]) != -1
        assert _sublist_index(cmd, ["--ro-bind", str(empty), str(home / ".ssh")]) != -1


class TestSensitiveReadPaths:
    def test_defaults_plus_env_are_deduped_and_expanded(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv(
            "SHERRY_DENY_READ_PATHS",
            f"~/custom-secret{os.pathsep}{home / '.ssh'}",
        )

        paths = [str(path) for path in _sensitive_read_paths()]

        assert str(home / "custom-secret") in paths
        assert str(home / ".ssh") in paths
        assert paths.count(str(home / ".ssh")) == 1, "default + env duplicate must collapse"

    def test_blank_env_entries_are_skipped(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("SHERRY_DENY_READ_PATHS", f"{os.pathsep}  {os.pathsep}")

        paths = _sensitive_read_paths()

        assert all(str(path).strip() for path in paths)


#: Real-bwrap smoke runs only where the probe passes (user namespaces allowed);
#: construction tests above stay hermetic and mock nothing here.
_BWRAP_USABLE = BwrapBackend().probe()


@pytest.mark.skipif(not _BWRAP_USABLE, reason="bwrap probe failed on this host")
def test_real_bwrap_read_shield_smoke(tmp_path, monkeypatch):
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    secret_file = secret_dir / "id_rsa"
    secret_file.write_text("PRIVATE-KEY", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sandbox_bwrap, "_sensitive_read_paths", lambda: [secret_dir])
    monkeypatch.setattr(sandbox_bwrap, "_EMPTY_MASK_DIR", empty)

    cmd, _env = BwrapBackend().wrap(["/bin/sh", "-c", f"cat {secret_file}"], {})
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    assert result.returncode != 0, "masked secret must not be readable"
    assert "PRIVATE-KEY" not in result.stdout
    assert secret_file.read_text(encoding="utf-8") == "PRIVATE-KEY", "host file untouched"


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
