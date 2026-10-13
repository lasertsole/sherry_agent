"""Curator lifecycle boundary conditions.

Covers the edges the happy-path suite leaves open: exact stale/archive cutoffs,
the `.pinned` marker vs the record flag, stale -> reactivate -> stale round
trips, the never-used seed path, repeated archive collisions, and the restore
guards. The `.archive/` root is created lazily; a collision gets a timestamp
suffix that the agent-side restore finds again via its prefix search.
"""

import pytest
from typing import Any
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from context_engine.curator import constants as curator_constants
from context_engine.curator import usage as curator_usage
from context_engine.curator.config import get_archive_after_days, get_stale_after_days
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


def _anchor(days: int, now: datetime) -> str:
    return (now - timedelta(days=days)).isoformat()


class _Clock:
    """Minimal stand-in for the ``datetime`` class used by ``archive_skill``."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self, tz: Any = None) -> datetime:
        return self.value


class TestCutoffBoundaries:
    def test_anchor_exactly_at_archive_cutoff_is_archived(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=3,
            last_activity_at=_anchor(get_archive_after_days(), now),
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["archived"] == 1
        assert (curator_tree.archive / "docker" / "SKILL.md").is_file()
        assert not (curator_tree.auto / "docker").exists()

    def test_anchor_one_day_before_archive_cutoff_marks_stale_only(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=3,
            last_activity_at=_anchor(get_archive_after_days() - 1, now),
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["archived"] == 0
        assert counts["marked_stale"] == 1
        assert (curator_tree.auto / "docker").is_dir()
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_STALE

    def test_anchor_exactly_at_stale_cutoff_marks_stale(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record("docker", use_count=2, last_activity_at=_anchor(get_stale_after_days(), now))

        counts = apply_automatic_transitions(now=now)

        assert counts["marked_stale"] == 1
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_STALE

    def test_anchor_just_inside_stale_window_is_untouched(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=2,
            last_activity_at=_anchor(get_stale_after_days() - 1, now),
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["marked_stale"] == 0
        assert counts["archived"] == 0
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE

    def test_stale_skill_one_day_before_archive_stays_stale(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=2,
            state=curator_constants.STATE_STALE,
            last_activity_at=_anchor(get_archive_after_days() - 1, now),
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["archived"] == 0
        assert counts["reactivated"] == 0
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_STALE


class TestPinningBoundaries:
    def test_pinned_marker_blocks_the_ninety_day_archive(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record("docker", use_count=5, last_activity_at=_anchor(120, now))
        (curator_tree.auto / "docker" / curator_constants.PINNED_FILE).write_text(
            "", encoding="utf-8"
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["checked"] == 1
        assert counts["archived"] == 0
        assert (curator_tree.auto / "docker").is_dir()
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE

    def test_pinned_record_flag_blocks_archive_skill(self, curator_tree):
        _make_skill(curator_tree, "docker")
        _seed_record("docker", pinned=True)

        ok, msg = curator_usage.archive_skill("docker")

        assert ok is False
        assert "pinned" in msg.lower()
        assert (curator_tree.auto / "docker").is_dir()

    def test_missing_marker_and_record_are_not_pinned(self, curator_tree):
        _make_skill(curator_tree, "docker")

        assert curator_usage.is_pinned("docker") is False

    def test_garbage_marker_content_still_pins(self, curator_tree):
        _make_skill(curator_tree, "docker")
        (curator_tree.auto / "docker" / curator_constants.PINNED_FILE).write_text(
            "not-a-valid-marker", encoding="utf-8"
        )

        assert curator_usage.is_pinned("docker") is True

    def test_corrupt_usage_record_falls_back_to_unpinned(self, curator_tree):
        _make_skill(curator_tree, "docker")
        (curator_tree.usage_dir / "docker.json").write_text("{broken json", encoding="utf-8")

        assert curator_usage.is_pinned("docker") is False

    def test_pin_then_unpin_round_trip(self, curator_tree):
        _make_skill(curator_tree, "docker")

        ok, _msg = curator_usage.pin_skill("docker")
        assert ok is True
        assert curator_usage.is_pinned("docker") is True

        ok, _msg = curator_usage.unpin_skill("docker")
        assert ok is True
        assert curator_usage.is_pinned("docker") is False

    def test_pin_refuses_a_missing_skill_directory(self, curator_tree):
        ok, msg = curator_usage.pin_skill("ghost")

        assert ok is False
        assert "not found" in msg.lower()


class TestStaleReactivateRoundTrip:
    def test_stale_activity_reactivates_then_goes_stale_again(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record("docker", use_count=1, last_activity_at=_anchor(40, now))

        assert apply_automatic_transitions(now=now)["marked_stale"] == 1
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_STALE

        _seed_record(
            "docker",
            use_count=1,
            state=curator_constants.STATE_STALE,
            last_activity_at=now.isoformat(),
        )
        assert apply_automatic_transitions(now=now)["reactivated"] == 1
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE

        _seed_record(
            "docker",
            use_count=1,
            state=curator_constants.STATE_ACTIVE,
            last_activity_at=_anchor(35, now),
        )
        assert apply_automatic_transitions(now=now)["marked_stale"] == 1
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_STALE

    def test_never_used_skill_inside_stale_window_is_never_marked(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record("docker", use_count=0, last_activity_at=_anchor(10, now))

        counts = apply_automatic_transitions(now=now)

        assert counts["marked_stale"] == 0
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE

    def test_never_used_skill_past_stale_window_is_marked(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record("docker", use_count=0, last_activity_at=_anchor(40, now))

        counts = apply_automatic_transitions(now=now)

        assert counts["marked_stale"] == 1
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_STALE

    def test_never_used_stale_skill_inside_window_is_reactivated(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=0,
            state=curator_constants.STATE_STALE,
            last_activity_at=_anchor(5, now),
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["reactivated"] == 1
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE

    def test_unpersisted_record_is_seeded_and_skipped_for_one_pass(self, curator_tree):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        _make_skill(curator_tree, "docker")
        rec = curator_usage._default_record("docker")
        rec.update({"use_count": 4, "last_activity_at": _anchor(120, now)})
        curator_usage.save_record("docker", rec)

        first = apply_automatic_transitions(now=now)

        assert first["seeded"] == 1
        assert first["archived"] == 0
        assert curator_usage.load_record("docker")["_persisted"] is True
        assert (curator_tree.auto / "docker").is_dir()

        second = apply_automatic_transitions(now=now)

        assert second["archived"] == 1


class TestArchiveCollisions:
    def test_archive_root_is_created_on_demand(self, curator_tree):
        _make_skill(curator_tree, "docker")
        assert not curator_tree.archive.exists()

        ok, _msg = curator_usage.archive_skill("docker")

        assert ok is True
        assert (curator_tree.archive / "docker" / "SKILL.md").is_file()

    def test_two_consecutive_collisions_get_distinct_timestamp_suffixes(
        self, curator_tree, monkeypatch
    ):
        clock = _Clock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        monkeypatch.setattr(curator_usage, "datetime", clock)
        (curator_tree.archive / "docker").mkdir(parents=True)
        (curator_tree.archive / "docker" / "SKILL.md").write_text("old", encoding="utf-8")

        _make_skill(curator_tree, "docker", body="first")
        assert curator_usage.archive_skill("docker")[0] is True

        _make_skill(curator_tree, "docker", body="second")
        clock.value = clock.value + timedelta(seconds=1)
        assert curator_usage.archive_skill("docker")[0] is True

        names = sorted(p.name for p in curator_tree.archive.iterdir())
        assert names == ["docker", "docker-20260101120000", "docker-20260101120001"]
        assert not (curator_tree.auto / "docker").exists()

    def test_collision_does_not_disturb_the_existing_archive_entry(self, curator_tree):
        _make_skill(curator_tree, "docker")
        previous = curator_tree.archive / "docker"
        previous.mkdir(parents=True)
        (previous / "SKILL.md").write_text("previous", encoding="utf-8")

        assert curator_usage.archive_skill("docker")[0] is True

        assert (previous / "SKILL.md").read_text(encoding="utf-8") == "previous"
        moved = [p for p in curator_tree.archive.iterdir() if p.name.startswith("docker-")]
        assert len(moved) == 1
        assert (moved[0] / "SKILL.md").read_text(encoding="utf-8").endswith("docker.\n")


class TestRestoreBoundaries:
    def _archived_skill(self, curator_tree: _Tree, name: str, body: str = "Body") -> None:
        _make_skill(curator_tree, name, body=body)
        assert curator_usage.archive_skill(name)[0] is True

    def test_restore_refused_when_destination_name_is_occupied(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        self._archived_skill(curator_tree, "docker")
        _make_skill(curator_tree, "docker", body="new occupant")

        ok, msg = agent_skill_usage.restore_skill("docker")

        assert ok is False
        assert "destination already exists" in msg
        assert (curator_tree.archive / "docker").is_dir()

    def test_restore_refused_when_name_is_now_bundled(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        self._archived_skill(curator_tree, "docker")
        (curator_tree.auto / ".bundled_manifest").write_text("docker:deadbeef\n", encoding="utf-8")

        ok, msg = agent_skill_usage.restore_skill("docker")

        assert ok is False
        assert "bundled or hub-installed" in msg
        assert (curator_tree.archive / "docker").is_dir()

    def test_restore_sets_the_agent_usage_record_active(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        self._archived_skill(curator_tree, "docker")

        ok, _msg = agent_skill_usage.restore_skill("docker")

        assert ok is True
        assert (curator_tree.auto / "docker" / "SKILL.md").is_file()
        record = agent_skill_usage.get_record("docker")
        assert record["state"] == agent_skill_usage.STATE_ACTIVE
        assert record["archived_at"] is None

    def test_restore_picks_the_newest_timestamped_entry(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        older = curator_tree.archive / "docker-20240101000000"
        newer = curator_tree.archive / "docker-20240102000000"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        (older / "SKILL.md").write_text("older copy", encoding="utf-8")
        (newer / "SKILL.md").write_text("newer copy", encoding="utf-8")

        ok, _msg = agent_skill_usage.restore_skill("docker")

        assert ok is True
        restored = curator_tree.auto / "docker" / "SKILL.md"
        assert restored.read_text(encoding="utf-8") == "newer copy"
        assert older.is_dir()

    def test_restore_refused_without_an_archive_root(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)

        ok, msg = agent_skill_usage.restore_skill("docker")

        assert ok is False
        assert "no archive directory" in msg

    def test_restore_refused_for_an_unknown_skill(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        curator_tree.archive.mkdir(parents=True)

        ok, msg = agent_skill_usage.restore_skill("ghost")

        assert ok is False
        assert "not found in archive" in msg

    def test_absorbed_into_marker_survives_restore(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        _make_skill(curator_tree, "docker")
        assert curator_usage.archive_skill("docker", absorbed_into="containers")[0] is True

        ok, _msg = agent_skill_usage.restore_skill("docker")

        assert ok is True
        marker = curator_tree.auto / "docker" / curator_constants.ABSORBED_INTO_FILE
        assert marker.read_text(encoding="utf-8") == "containers\n"
