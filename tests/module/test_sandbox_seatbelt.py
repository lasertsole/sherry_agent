"""Tests for the macOS Seatbelt (sandbox-exec) sandbox backend.

仅验证构造逻辑，未在 macOS 实机验证（Windows 开发机限制）。
All tests mock ``shutil.which`` — the real ``sandbox-exec`` binary is never
executed (probe is which-only by design; seatbelt offers no exit-code probe).
"""

import json
import shutil

import pytest

from agent.tools.pub_base.sandbox import SandboxBackend
from agent.tools.pub_base import sandbox_seatbelt as seatbelt_mod
from agent.tools.pub_base.sandbox_seatbelt import SeatbeltBackend
from config.path import ROOT_DIR, TEMP_DIR


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """Class-level probe cache must not leak between tests."""
    SeatbeltBackend._probe_result = None
    yield
    SeatbeltBackend._probe_result = None


def test_seatbelt_probe_false_when_which_missing(monkeypatch):
    """shutil.which -> None means sandbox-exec is unavailable: probe() False."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert SeatbeltBackend().probe() is False


def test_seatbelt_probe_true_when_which_present(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    assert SeatbeltBackend().probe() is True


def test_seatbelt_probe_which_called_with_sandbox_exec(monkeypatch):
    calls = []

    def fake_which(name):
        calls.append(name)
        return "/usr/bin/sandbox-exec"

    monkeypatch.setattr(shutil, "which", fake_which)
    SeatbeltBackend().probe()
    assert calls == ["sandbox-exec"]


def test_seatbelt_probe_result_cached_at_class_level(monkeypatch):
    """Class-level cache: repeated probes (incl. new instances) hit which once."""
    calls = []

    def fake_which(name):
        calls.append(name)
        return "/usr/bin/sandbox-exec"

    monkeypatch.setattr(shutil, "which", fake_which)
    first = SeatbeltBackend()
    second = SeatbeltBackend()
    assert first.probe() is True
    assert second.probe() is True
    assert first.probe() is True
    assert len(calls) == 1


def test_seatbelt_is_sandbox_backend_subclass():
    assert issubclass(SeatbeltBackend, SandboxBackend)


def test_seatbelt_wrap_command_structure():
    """wrap -> ["sandbox-exec", "-p", profile, "--", *cmd]; env passthrough."""
    env = {"PATH": "/usr/bin"}
    wrapped, out_env = SeatbeltBackend().wrap(["echo", "hi"], env)
    assert wrapped[0] == "sandbox-exec"
    assert wrapped[1] == "-p"
    assert isinstance(wrapped[2], str) and wrapped[2]  # profile text
    assert wrapped[3] == "--"
    assert wrapped[4:] == ["echo", "hi"]
    assert out_env is env  # env passed through untouched


def test_seatbelt_profile_contains_deny_file_write():
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    assert "deny file-write*" in profile


def test_seatbelt_profile_deny_after_allow_default():
    """Profile ORDER is load-bearing: (deny file-write*) after (allow default)."""
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    assert profile.index("(allow default)") < profile.index("(deny file-write*)")


def test_seatbelt_profile_allows_root_dir_subpath():
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    expected = f'(allow file-write* (subpath {json.dumps(str(ROOT_DIR))}))'
    assert expected in profile


def test_seatbelt_profile_allows_temp_dir_subpath():
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    expected = f'(allow file-write* (subpath {json.dumps(str(TEMP_DIR))}))'
    assert expected in profile


def test_seatbelt_profile_allows_dev_null_literal():
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    assert '(allow file-write* (literal "/dev/null"))' in profile


def test_seatbelt_profile_allows_dev_tty_literal():
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    assert '(allow file-write* (literal "/dev/tty"))' in profile


def test_seatbelt_json_escaping_for_quote_containing_path(monkeypatch):
    """Path embedding must go through json.dumps (sbpl injection guard)."""
    weird = '/tmp/we"ird; (deny file-write*)'
    monkeypatch.setattr(seatbelt_mod, "TEMP_DIR", weird)
    profile = SeatbeltBackend().wrap(["echo"], {})[0][2]
    expected = f'(allow file-write* (subpath {json.dumps(weird)}))'
    assert expected in profile
    # Raw unescaped form (quote not preceded by backslash) must NOT appear.
    assert 'we"ird;' not in profile
