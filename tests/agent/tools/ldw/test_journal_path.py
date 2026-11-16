"""The LDW journal must move with SHERRY_HOME, not the code directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.ldw import replay as replay_mod

pytestmark = [pytest.mark.unit]


def test_a_relative_journal_is_anchored_at_src_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(replay_mod, "SRC_DIR", tmp_path / "src")

    resolved = replay_mod.resolve_journal_path("data/workflow_runs.db")

    assert resolved == tmp_path / "src" / "data" / "workflow_runs.db"


def test_an_absolute_journal_is_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(replay_mod, "SRC_DIR", tmp_path / "src")
    custom = tmp_path / "custom" / "journal.db"

    assert replay_mod.resolve_journal_path(str(custom)) == custom


def test_two_tenant_homes_resolve_to_different_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(replay_mod, "SRC_DIR", tmp_path / "tenant-a" / "src")
    first = replay_mod.resolve_journal_path("data/workflow_runs.db")
    monkeypatch.setattr(replay_mod, "SRC_DIR", tmp_path / "tenant-b" / "src")
    second = replay_mod.resolve_journal_path("data/workflow_runs.db")

    assert first != second
    assert first.parent.parent.parent == tmp_path / "tenant-a"
    assert second.parent.parent.parent == tmp_path / "tenant-b"
