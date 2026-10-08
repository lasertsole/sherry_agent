"""Curator-layer restore: dual-record sync, lifecycle re-entry, collisions.

``restore_skill`` lives in ``context_engine/curator/usage.py`` (the agent side
forwards to it). These tests lock the two records in step — the curator record
under ``skills/auto/.usage/<name>.json`` and the agent telemetry record under
``skills/auto/.usage.json`` — and prove a restored skill re-enters
``apply_automatic_transitions`` instead of staying stuck as ``archived``
(where all four transition branches miss and the live skill is never managed
again). Same-second archive collisions must stay flat siblings; a single
non-probing timestamp suffix would let ``shutil.move`` nest the source inside
the existing entry.
"""

import pytest
from typing import Any
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from context_engine.curator import constants as curator_constants
from context_engine.curator import usage as curator_usage
from context_engine.curator.transitions import apply_automatic_transitions
from agent.tools.pub_base import skill_usage as agent_skill_usage


pytestmark = [pytest.mark.unit]


@dataclass(frozen=True)
class _Tree:
    root: Any
    auto: Any
    archive: Any
    usage_dir: Any


@pytest.fixture
def curator_tree(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    auto = skills_root / "auto"
    usage_dir = auto / ".usage"
    archive = skills_root / ".archive"
    usage_dir.mkdir(parents=True)

    monkeypatch.setattr(curator_constants, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr(curator_constants, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(curator_usage, "USAGE_DIR", usage_dir)
    monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", auto)
    return _Tree(root=skills_root, auto=auto, archive=archive, usage_dir=usage_dir)


def _make_skill(tree: _Tree, name: str, body: str = "Body") -> None:
    skill_dir = tree.auto / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body} of {name}.\n",
        encoding="utf-8",
    )


def _seed_record(name: str, **overrides: Any) -> None:
    rec = curator_usage._default_record(name)
    rec.update(overrides)
    rec["_persisted"] = True
    curator_usage.save_record(name, rec)


class _Clock:
    """Stand-in for the ``datetime`` class used by ``archive_skill``."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self, tz: Any = None) -> datetime:
        return self.value


def _archive_aged_skill(tree: _Tree, name: str, now: datetime) -> None:
    _make_skill(tree, name)
    _seed_record(
        name,
        use_count=3,
        created_at=(now - timedelta(days=120)).isoformat(),
        last_activity_at=(now - timedelta(days=100)).isoformat(),
    )
    counts = apply_automatic_transitions(now=now)
    assert counts["archived"] == 1


class TestRestoreDualRecordSync:
    def test_restore_activates_the_curator_record(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _archive_aged_skill(curator_tree, "docker", now)
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ARCHIVED

        ok, msg = curator_usage.restore_skill("docker")

        assert ok, msg
        record = curator_usage.load_record("docker")
        assert record["state"] == curator_constants.STATE_ACTIVE
        assert record["_persisted"] is True
        assert (curator_tree.auto / "docker" / "SKILL.md").is_file()
        assert not (curator_tree.archive / "docker").exists()

    def test_agent_forwarder_syncs_both_records_active(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _archive_aged_skill(curator_tree, "docker", now)

        ok, msg = agent_skill_usage.restore_skill("docker")

        assert ok, msg
        curator_record = curator_usage.load_record("docker")
        assert curator_record["state"] == curator_constants.STATE_ACTIVE
        assert curator_record["_persisted"] is True
        agent_record = agent_skill_usage.get_record("docker")
        assert agent_record["state"] == agent_skill_usage.STATE_ACTIVE
        assert agent_record["archived_at"] is None

    def test_restored_skill_reenters_lifecycle_and_is_archived_again(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _archive_aged_skill(curator_tree, "docker", now)

        ok, msg = agent_skill_usage.restore_skill("docker")
        assert ok, msg

        later = now + timedelta(days=90)
        counts = apply_automatic_transitions(now=later)

        assert counts["archived"] == 1
        assert (curator_tree.archive / "docker" / "SKILL.md").is_file()
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ARCHIVED

    def test_restore_activates_even_when_the_record_keeps_a_stale_pin(self, curator_tree):
        _make_skill(curator_tree, "docker")
        assert curator_usage.archive_skill("docker")[0] is True
        rec = curator_usage.load_record("docker")
        rec["pinned"] = True
        curator_usage.save_record("docker", rec)

        ok, msg = curator_usage.restore_skill("docker")

        assert ok, msg
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE
        assert (curator_tree.auto / "docker" / "SKILL.md").is_file()


class TestSameSecondArchiveCollisions:
    def _prepopulate_archive(self, tree: _Tree) -> None:
        (tree.archive / "docker").mkdir(parents=True)
        (tree.archive / "docker" / "SKILL.md").write_text("old", encoding="utf-8")

    def test_two_archives_in_one_second_stay_flat_siblings(self, curator_tree, monkeypatch):
        clock = _Clock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        monkeypatch.setattr(curator_usage, "datetime", clock)
        self._prepopulate_archive(curator_tree)

        _make_skill(curator_tree, "docker", body="first")
        assert curator_usage.archive_skill("docker")[0] is True
        _make_skill(curator_tree, "docker", body="second")
        assert curator_usage.archive_skill("docker")[0] is True

        names = sorted(p.name for p in curator_tree.archive.iterdir())
        assert names == ["docker", "docker-20260101120000", "docker-20260101120001"]
        assert not (curator_tree.archive / "docker-20260101120000" / "docker").exists()
        assert (
            (curator_tree.archive / "docker-20260101120000" / "SKILL.md")
            .read_text(encoding="utf-8")
            .endswith("first of docker.\n")
        )
        assert (
            (curator_tree.archive / "docker-20260101120001" / "SKILL.md")
            .read_text(encoding="utf-8")
            .endswith("second of docker.\n")
        )

    def test_three_archives_in_one_second_stay_flat_siblings(self, curator_tree, monkeypatch):
        clock = _Clock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        monkeypatch.setattr(curator_usage, "datetime", clock)
        self._prepopulate_archive(curator_tree)

        for body in ("first", "second", "third"):
            _make_skill(curator_tree, "docker", body=body)
            assert curator_usage.archive_skill("docker")[0] is True

        names = sorted(p.name for p in curator_tree.archive.iterdir())
        assert names == [
            "docker",
            "docker-20260101120000",
            "docker-20260101120001",
            "docker-20260101120002",
        ]
        for name in names[1:]:
            entry = curator_tree.archive / name
            assert (entry / "SKILL.md").is_file()
            assert [p.name for p in entry.iterdir()] == ["SKILL.md"]

    def test_restore_picks_the_newest_same_second_entry(self, curator_tree):
        for suffix, body in (
            ("20260101120000", "first"),
            ("20260101120001", "second"),
            ("20260101120002", "third"),
        ):
            entry = curator_tree.archive / f"docker-{suffix}"
            entry.mkdir(parents=True)
            (entry / "SKILL.md").write_text(
                f"---\nname: docker\n---\n\n{body} of docker.\n", encoding="utf-8"
            )

        ok, msg = curator_usage.restore_skill("docker")

        assert ok, msg
        restored = (curator_tree.auto / "docker" / "SKILL.md").read_text(encoding="utf-8")
        assert restored.endswith("third of docker.\n")
        assert not (curator_tree.archive / "docker-20260101120002").exists()
        assert (curator_tree.archive / "docker-20260101120000").is_dir()
        assert (curator_tree.archive / "docker-20260101120001").is_dir()


class TestListArchived:
    def test_missing_archive_root_yields_empty(self, curator_tree):
        assert curator_usage.list_archived() == []

    def test_lists_flat_directories_sorted_and_skips_files(self, curator_tree):
        curator_tree.archive.mkdir(parents=True)
        (curator_tree.archive / "docker").mkdir()
        (curator_tree.archive / "docker" / "SKILL.md").write_text("x", encoding="utf-8")
        (curator_tree.archive / "docker-20260101120000").mkdir()
        (curator_tree.archive / "notes.txt").write_text("x", encoding="utf-8")

        assert curator_usage.list_archived() == ["docker", "docker-20260101120000"]

    def test_timestamped_entries_are_restorable_by_their_listed_name(self, curator_tree):
        _make_skill(curator_tree, "docker")
        assert curator_usage.archive_skill("docker")[0] is True
        listed = curator_usage.list_archived()
        assert listed == ["docker"]

        ok, msg = curator_usage.restore_skill(listed[0])

        assert ok, msg
        assert (curator_tree.auto / "docker" / "SKILL.md").is_file()


class TestPublicApi:
    def test_package_exports_restore_and_list_archived(self):
        import context_engine.curator as curator

        assert curator.restore_skill is curator_usage.restore_skill
        assert curator.list_archived is curator_usage.list_archived
