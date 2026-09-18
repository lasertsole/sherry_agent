"""Archive (not delete) semantics for the curator's 90-day lifecycle transition.

The 90-day transition used to rmtree the skill and its usage record, silently
destroying agent-authored skills. These tests lock the recoverable behavior:
the directory lands under ``skills/.archive/``, the usage record is kept with
``state="archived"``, and the agent-side ``restore_skill`` brings it back.
"""

import json
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
    """Isolated skills tree: ``<tmp>/skills/{auto,.archive}`` with usage records.

    ``constants.AUTO_SKILLS_DIR`` / ``constants.ARCHIVE_DIR`` are read lazily by
    ``helpers``/``usage``, so patching them there redirects skill lookup, the
    provenance gate, and the archive root in one place. ``USAGE_DIR`` is bound
    by value in ``usage.py`` (like ``CURATOR_STATE_FILE`` in ``state.py``), so
    it is patched on that module.
    """
    skills_root = tmp_path / "skills"
    auto = skills_root / "auto"
    usage_dir = auto / ".usage"
    archive = skills_root / ".archive"
    usage_dir.mkdir(parents=True)

    monkeypatch.setattr(curator_constants, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr(curator_constants, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(curator_usage, "USAGE_DIR", usage_dir)
    return _Tree(root=skills_root, auto=auto, archive=archive, usage_dir=usage_dir)


def _make_skill(tree: _Tree, name: str) -> None:
    skill_dir = tree.auto / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )


def _seed_record(name: str, **overrides: Any) -> None:
    rec = curator_usage._default_record(name)
    rec.update(overrides)
    rec["_persisted"] = True
    curator_usage.save_record(name, rec)


class TestNinetyDayTransitionArchives:
    def test_aged_skill_moves_to_archive_root_and_keeps_record(self, curator_tree):
        now = datetime.now(UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=4,
            created_at=(now - timedelta(days=120)).isoformat(),
            last_activity_at=(now - timedelta(days=100)).isoformat(),
        )

        counts = apply_automatic_transitions(now=now)

        assert counts["archived"] == 1
        assert (curator_tree.archive / "docker" / "SKILL.md").is_file()
        assert not (curator_tree.auto / "docker").exists()
        record = curator_usage.load_record("docker")
        assert record["state"] == curator_constants.STATE_ARCHIVED
        assert record["use_count"] == 4
        assert (curator_tree.usage_dir / "docker.json").is_file()

    def test_archived_record_is_not_treated_as_orphan_on_next_run(self, curator_tree):
        now = datetime.now(UTC)
        _make_skill(curator_tree, "docker")
        _seed_record(
            "docker",
            use_count=2,
            created_at=(now - timedelta(days=120)).isoformat(),
            last_activity_at=(now - timedelta(days=100)).isoformat(),
        )
        apply_automatic_transitions(now=now)

        counts = apply_automatic_transitions(now=now)

        assert counts["archived"] == 0
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ARCHIVED
        assert (curator_tree.usage_dir / "docker.json").is_file()


class TestRestoreEndToEnd:
    def test_archived_skill_restores_into_auto(self, curator_tree, monkeypatch):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        _make_skill(curator_tree, "docker")

        ok, msg = curator_usage.archive_skill("docker")
        assert ok, msg
        assert (curator_tree.archive / "docker" / "SKILL.md").is_file()

        ok, msg = agent_skill_usage.restore_skill("docker")
        assert ok, msg
        assert (curator_tree.auto / "docker" / "SKILL.md").is_file()
        assert not (curator_tree.archive / "docker").exists()


class TestArchiveGuards:
    def test_pinned_skill_is_refused(self, curator_tree):
        _make_skill(curator_tree, "docker")
        _seed_record("docker", pinned=True)

        ok, msg = curator_usage.archive_skill("docker")

        assert ok is False
        assert "pinned" in msg.lower()
        assert (curator_tree.auto / "docker").is_dir()
        assert not curator_tree.archive.exists()

    def test_bundled_skill_is_refused(self, curator_tree):
        _make_skill(curator_tree, "seed_skill")
        (curator_tree.auto / ".bundled_manifest").write_text(
            "seed_skill:deadbeef\n", encoding="utf-8"
        )

        ok, msg = curator_usage.archive_skill("seed_skill")

        assert ok is False
        assert "bundled" in msg
        assert (curator_tree.auto / "seed_skill").is_dir()

    def test_hub_installed_skill_is_refused(self, curator_tree):
        _make_skill(curator_tree, "hub_skill")
        hub_dir = curator_tree.auto / ".hub"
        hub_dir.mkdir()
        (hub_dir / "lock.json").write_text(
            json.dumps({"installed": {"hub_skill": {}}}), encoding="utf-8"
        )

        ok, msg = curator_usage.archive_skill("hub_skill")

        assert ok is False
        assert (curator_tree.auto / "hub_skill").is_dir()

    def test_missing_skill_is_refused(self, curator_tree):
        ok, msg = curator_usage.archive_skill("ghost")

        assert ok is False
        assert "not found" in msg.lower()


class TestArchiveCollision:
    def test_existing_archive_entry_gets_timestamp_suffix(self, curator_tree):
        _make_skill(curator_tree, "docker")
        previous = curator_tree.archive / "docker"
        previous.mkdir(parents=True)
        (previous / "SKILL.md").write_text("previous archive", encoding="utf-8")

        ok, msg = curator_usage.archive_skill("docker")

        assert ok, msg
        moved = sorted(p for p in curator_tree.archive.iterdir() if p.name.startswith("docker-"))
        assert len(moved) == 1
        assert (moved[0] / "SKILL.md").is_file()
        assert (previous / "SKILL.md").read_text(encoding="utf-8") == "previous archive"


class TestConstantsContract:
    def test_state_archived_matches_the_agent_literal(self):
        from agent.tools.pub_base.skill_usage import STATE_ARCHIVED as AGENT_STATE_ARCHIVED

        assert curator_constants.STATE_ARCHIVED == AGENT_STATE_ARCHIVED == "archived"

    def test_archive_roots_are_aligned_across_layers(self):
        assert curator_usage._archive_dir() == agent_skill_usage._archive_dir()

    def test_public_api_exports_archive_skill(self):
        import context_engine.curator as curator

        assert curator.archive_skill is curator_usage.archive_skill
        assert curator.STATE_ARCHIVED == curator_constants.STATE_ARCHIVED
