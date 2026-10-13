"""P2: the read whitelist (bwrap + seatbelt), shipped OFF pending real-machine smoke.

The write surface is the sandbox's hard guarantee (P1). The READ surface is the
other half: ``--ro-bind / /`` exposes the whole filesystem read-only, so a shell
command can read any user file without ever touching the external-path gate.
The whitelist replaces that with system trees + toolchain caches + the writable
roots, masking the rest with empty views.

It is default OFF on purpose: the plan's own gate for P2 is a real-machine smoke
(``scripts/sandbox_smoke.py``) on a host where the sandbox actually runs, which
this development host cannot do (no user namespaces). These tests pin the
mechanism and the switch, not the host behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base import sandbox_bwrap, sandbox_seatbelt

pytestmark = [pytest.mark.unit]

ROOTS = [Path("/tmp/sess-proj")]


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    monkeypatch.delenv("SANDBOX_READ_ALLOWLIST", raising=False)
    monkeypatch.delenv("SANDBOX_READ_EXTRA", raising=False)


class TestBwrapReadBase:
    def test_default_is_the_whole_root_read_only(self):
        assert sandbox_bwrap._read_base_args() == ["--ro-bind", "/", "/"]

    def test_whitelist_replaces_the_base_mount(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")

        args = sandbox_bwrap._read_base_args()

        assert ["--ro-bind", "/", "/"] != args[:3]
        assert "--tmpfs" in args  # user-data trees get empty views
        assert args.index("--ro-bind") == 0

    def test_user_data_trees_are_masked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")

        masks = [
            args[i + 1]
            for args in [sandbox_bwrap._read_base_args()]
            for i, token in enumerate(args)
            if token == "--tmpfs"
        ]

        assert all(path in masks or not Path(path).exists() for path in ("/home", "/root"))

    def test_extra_read_paths_come_from_the_env(self, monkeypatch, tmp_path):
        extra = tmp_path / "cache"
        extra.mkdir()
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")
        monkeypatch.setenv("SANDBOX_READ_EXTRA", str(extra))

        args = sandbox_bwrap._read_base_args()

        assert str(extra) in args

    def test_default_extras_include_the_toolchain_cache(self):
        assert any("cache" in str(path) for path in sandbox_bwrap.read_extra_paths())

    def test_non_existent_extras_are_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")
        monkeypatch.setenv("SANDBOX_READ_EXTRA", str(tmp_path / "nope"))

        assert str(tmp_path / "nope") not in sandbox_bwrap._read_base_args()

    def test_wrap_still_binds_the_writable_roots(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")

        cmd, _env = sandbox_bwrap.BwrapBackend().wrap(["echo"], {}, [tmp_path])

        binds = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--bind"]
        assert str(tmp_path.resolve()) in binds


class TestSeatbeltReadAllowlist:
    def test_off_by_default(self):
        profile = sandbox_seatbelt.SeatbeltBackend._build_profile(ROOTS)

        assert "(deny file-read*)" not in profile

    def test_enabling_denies_reads_and_reopens_the_system_trees(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")

        profile = sandbox_seatbelt.SeatbeltBackend._build_profile(ROOTS)

        assert "(deny file-read*)" in profile
        assert '(allow file-read* (subpath "/usr"))' in profile

    def test_the_writable_roots_stay_readable_when_enabled(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")

        profile = sandbox_seatbelt.SeatbeltBackend._build_profile(ROOTS)

        assert '"' + str(ROOTS[0]) + '"' in profile

    def test_the_whitelist_rules_precede_the_env_denies(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_READ_ALLOWLIST", "1")

        profile = sandbox_seatbelt.SeatbeltBackend._build_profile(ROOTS)

        # Order is load-bearing in sbpl: the broad deny must come before the
        # specific allows, and the .env denies must still follow them.
        assert profile.index("(deny file-read*)") < profile.index("(allow file-read*")
        assert profile.index("(allow file-read*") < profile.index("file-read*" and ".env")
