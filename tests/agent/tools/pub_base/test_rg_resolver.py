"""ripgrep discovery: five tiers, a version probe, and a cache that can be dropped.

The resolver is the difference between "the agent happens to find rg on PATH"
and a search path that works on a host where nobody installed it: a missing
binary is a normal outcome that the caller answers with its Python walk, so the
tests pin both directions — a good candidate wins, and every bad candidate is
rejected for its own reason (missing, empty, not executable, wrong program).
"""

from __future__ import annotations

import os
import stat

import pytest

from agent.tools.pub_base import rg_resolver

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Each case starts with an empty resolution cache."""
    rg_resolver.reset_cache()
    yield
    rg_resolver.reset_cache()


def _fake_binary(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _rg_like(tmp_path, name: str = "shim-rg") -> str:
    return _fake_binary(tmp_path, name, '#!/bin/sh\necho "ripgrep 14.1.1"\n')


def test_env_override_wins_and_is_cached(tmp_path, monkeypatch):
    binary = _rg_like(tmp_path)
    monkeypatch.setenv("SHERRY_RG_PATH", binary)

    assert rg_resolver.resolve_rg() == binary
    # Cached: deleting the file does not change the answer until the cache drops.
    os.remove(binary)
    assert rg_resolver.resolve_rg() == binary
    rg_resolver.reset_cache()
    assert rg_resolver.resolve_rg() != binary


def test_a_candidate_that_is_not_ripgrep_is_rejected(tmp_path, monkeypatch):
    impostor = _fake_binary(tmp_path, "not-rg", '#!/bin/sh\necho "gnu grep 3.11"\n')
    monkeypatch.setenv("SHERRY_RG_PATH", impostor)

    assert rg_resolver.resolve_rg() != impostor


def test_a_missing_candidate_falls_through_to_the_next_tier(tmp_path, monkeypatch):
    """Tier 1 pointing nowhere must not stop the search at the other tiers."""
    good = _rg_like(tmp_path)
    monkeypatch.setenv("SHERRY_RG_PATH", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(rg_resolver, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(rg_resolver, "CODE_INTEL_DIR", tmp_path)
    monkeypatch.setattr(rg_resolver.shutil, "which", lambda name: good)

    assert rg_resolver.resolve_rg() == good


def test_an_empty_file_is_not_a_candidate(tmp_path, monkeypatch):
    empty = tmp_path / "empty-rg"
    empty.write_text("", encoding="utf-8")
    empty.chmod(0o755)
    monkeypatch.setenv("SHERRY_RG_PATH", str(empty))
    monkeypatch.setattr(rg_resolver, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(rg_resolver, "CODE_INTEL_DIR", tmp_path / "bin")
    monkeypatch.setattr(rg_resolver.shutil, "which", lambda name: None)

    assert rg_resolver.resolve_rg() is None


def test_nothing_anywhere_resolves_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERRY_RG_PATH", raising=False)
    monkeypatch.setattr(rg_resolver, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(rg_resolver, "CODE_INTEL_DIR", tmp_path / "bin")
    monkeypatch.setattr(rg_resolver.shutil, "which", lambda name: None)

    assert rg_resolver.resolve_rg() is None
    # A second call must not re-probe (the INFO is emitted once per process).
    assert rg_resolver.resolve_rg() is None


def test_the_config_switch_disables_resolution(tmp_path, monkeypatch):
    """The rollback switch: 'never use rg' must not even look for it."""
    binary = _rg_like(tmp_path)
    monkeypatch.setenv("SHERRY_RG_PATH", binary)
    monkeypatch.setitem(rg_resolver.RIPGREP, "enabled", False)

    assert rg_resolver.resolve_rg() is None


def test_spawn_failed_drops_the_cache(tmp_path, monkeypatch):
    """A binary that resolves but cannot be executed is re-probed next call."""
    binary = _rg_like(tmp_path)
    monkeypatch.setenv("SHERRY_RG_PATH", binary)
    assert rg_resolver.resolve_rg() == binary

    rg_resolver.spawn_failed()
    os.remove(binary)
    monkeypatch.setattr(rg_resolver.shutil, "which", lambda name: None)
    monkeypatch.setattr(rg_resolver, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(rg_resolver, "CODE_INTEL_DIR", tmp_path / "bin")
    assert rg_resolver.resolve_rg() is None
