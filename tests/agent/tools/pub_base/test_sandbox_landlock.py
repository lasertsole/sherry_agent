"""The landlock backend: kernel-level fallback when bwrap is absent.

Pinned here (the real syscall path can only run on a Linux 5.13+ kernel, so
everything else is probed/spec-shaped):

* ``probe()`` is honest about the platform: unsupported machine / not-Linux /
  a frozen build → False, and the answer is cached per process;
* ``wrap()`` refuses (RuntimeError) when the probe says unsupported;
* ``wrap()`` builds the ``-I -c`` launcher with the real command after ``--``,
  carries the rules in ONE env var, and the ruleset is an allow-list: the
  system read trees + every writable root + ``/dev/null``, with the handled
  rights grown by ABI (REFER at v2, TRUNCATE at v3);
* the loader source is fully substituted (no ``_LL_*`` holes left) and free of
  project imports — it runs under ``-I``;
* the composite Linux factory prefers a working bwrap and falls back to
  landlock; the written spec has a size ceiling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import agent.tools.pub_base.sandbox_landlock as landlock
from agent.tools.pub_base.sandbox import _linux_backend_factory

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _reset_abi_cache():
    landlock._ABI_CACHE = None
    yield
    landlock._ABI_CACHE = None


class TestProbe:
    def test_windows_and_other_machines_are_unsupported(self):
        if sys.platform == "linux":
            pytest.skip("only meaningful off Linux")
        assert landlock._probe_abi() == 0
        assert not landlock.LandlockBackend().probe()

    def test_a_frozen_build_is_unsupported_even_on_linux(self, monkeypatch):
        if sys.platform != "linux":
            pytest.skip("only meaningful on Linux")
        monkeypatch.setattr(sys, "_MEIPASS", "/fake/bundle", raising=False)
        assert landlock._probe_abi() == 0

    def test_the_abi_answer_is_cached(self):
        landlock._ABI_CACHE = 2
        assert landlock._probe_abi() == 2


class TestWrap:
    def _backend(self, abi: int, monkeypatch) -> landlock.LandlockBackend:
        monkeypatch.setattr(landlock, "_probe_abi", lambda: abi)
        return landlock.LandlockBackend()

    def _wrap(self, backend, roots, cmd=None):
        return backend.wrap(
            cmd or ["/bin/sh", "-c", "echo hi"],
            {},
            roots,
        )

    def test_unsupported_abi_refuses(self, monkeypatch):
        backend = self._backend(0, monkeypatch)
        with pytest.raises(RuntimeError, match="landlock unsupported"):
            self._wrap(backend, [Path("/proj")])

    def test_the_launcher_is_an_isolated_interpreter(self, monkeypatch, tmp_path):
        backend = self._backend(1, monkeypatch)
        argv, env = self._wrap(backend, [tmp_path])
        assert argv[0] == sys.executable
        assert "-I" in argv and "-c" in argv
        assert argv[argv.index("--") + 1 :] == ["/bin/sh", "-c", "echo hi"]

    def test_rules_are_an_allow_list_carrying_the_writable_roots(self, monkeypatch, tmp_path):
        backend = self._backend(1, monkeypatch)
        project = tmp_path / "proj"
        project.mkdir()
        argv, env = self._wrap(backend, [project])
        spec = json.loads(env["SHERRY_LANDLOCK_SPEC"])
        assert spec["abi"] == 1
        paths = {rule[0] for rule in spec["rules"]}
        assert str(project) in paths
        for system_path in ("/usr", "/etc", "/dev", "/proc"):
            assert system_path in paths
        assert "/dev/null" in paths
        write_rules = {rule[0]: rule[1] for rule in spec["rules"]}
        assert write_rules[str(project)] & landlock._WRITE_RIGHTS
        assert write_rules["/usr"] == landlock._READ_RIGHTS & landlock._handled_for_abi(1)
        assert "SHERRY_LANDLOCK_SPEC" not in argv

    def test_higher_abi_grows_the_handled_set(self, monkeypatch, tmp_path):
        backend = self._backend(3, monkeypatch)
        argv, env = self._wrap(backend, [tmp_path])
        spec = json.loads(env["SHERRY_LANDLOCK_SPEC"])
        base = 0x1FFF
        assert spec["abi"] == 3
        # handled rights must include REFER + TRUNCATE for a v3 kernel
        assert landlock._handled_for_abi(3) == base | (1 << 13) | (1 << 14)
        assert landlock._handled_for_abi(2) == base | (1 << 13)
        assert landlock._handled_for_abi(1) == base

    def test_the_loader_source_is_fully_substituted(self):
        assert "_LL_" not in landlock._LOADER_SRC
        for required in ("landlock", "execvp", "SHERRY_LANDLOCK_SPEC"):
            assert required in landlock._LOADER_SRC
        # -I means the loader imports stdlib only; nothing from this project.
        assert "agent." not in landlock._LOADER_SRC

    def test_an_empty_root_list_is_a_caller_bug(self, monkeypatch):
        backend = self._backend(1, monkeypatch)
        with pytest.raises(ValueError, match="writable root"):
            self._wrap(backend, [])

    def test_an_oversized_spec_refuses(self, monkeypatch, tmp_path):
        backend = self._backend(1, monkeypatch)
        huge = tmp_path / ("x" * 120_000)
        with pytest.raises(ValueError, match="too large"):
            self._wrap(backend, [huge])


class TestLinuxFactory:
    def test_a_working_bwrap_wins(self, monkeypatch):
        class FakeBwrap:
            def __init__(self):
                pass

            def probe(self):
                return True

        import agent.tools.pub_base.sandbox_bwrap as bwrap_module

        monkeypatch.setattr(bwrap_module, "BwrapBackend", FakeBwrap)
        assert isinstance(_linux_backend_factory(), FakeBwrap)

    def test_a_broken_or_missing_bwrap_falls_back_to_landlock(self, monkeypatch):
        class FakeBwrap:
            def __init__(self):
                pass

            def probe(self):
                return False

        import agent.tools.pub_base.sandbox_bwrap as bwrap_module

        monkeypatch.setattr(bwrap_module, "BwrapBackend", FakeBwrap)
        backend = _linux_backend_factory()
        assert isinstance(backend, landlock.LandlockBackend)

    def test_an_import_failure_still_falls_back(self, monkeypatch):
        import agent.tools.pub_base.sandbox_bwrap as bwrap_module

        def boom():
            raise ImportError("no module")

        monkeypatch.setattr(bwrap_module, "BwrapBackend", boom)
        assert isinstance(_linux_backend_factory(), landlock.LandlockBackend)
