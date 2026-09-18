"""Consolidation and pruning removals must archive, never delete.

All three curator removals (90-day, consolidation, pruning) land in
``skills/.archive/`` and are recoverable via ``restore_skill``. A consolidated
source carries an ``ABSORBED_INTO`` marker naming its umbrella, and that marker
survives the restore. A source whose umbrella could not be created stays in
``skills/auto/`` untouched.
"""

import pytest
from typing import Any
from dataclasses import dataclass
from pathlib import Path

from context_engine.curator import constants as curator_constants
from context_engine.curator import usage as curator_usage
from context_engine.curator import orchestrator
from context_engine.curator.classify import _parse_structured_summary
from agent.tools.pub_base import skill_usage as agent_skill_usage
from runtime import data_provider


pytestmark = [pytest.mark.unit]

_LLM_CONSOLIDATE = (
    "```yaml\nconsolidations:\n  - from: alpha\n    into: umbrella\n    reason: overlap\n```"
)
_LLM_PRUNE = "```yaml\nprunings:\n  - name: beta\n    reason: stale\n```"
_LLM_MIXED = (
    "```yaml\n"
    "consolidations:\n"
    "  - from: alpha\n"
    "    into: umbrella\n"
    "    reason: overlap\n"
    "prunings:\n"
    "  - name: alpha\n"
    "    reason: duplicate of consolidation\n"
    "  - name: gamma\n"
    "    reason: stale\n"
    "```"
)


@dataclass(frozen=True)
class _Tree:
    root: Path
    auto: Path
    archive: Path
    usage_dir: Path


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


@pytest.fixture(autouse=True)
def _clean_skill_writer():
    data_provider.clear_skill_write_provider()
    orchestrator._provider_misses_logged.clear()
    yield
    data_provider.clear_skill_write_provider()
    orchestrator._provider_misses_logged.clear()


class _DiskWriter:
    """SkillWriteProvider stub that mirrors skill_manage by writing to disk."""

    def __init__(self, auto_dir: Path, fail_create: bool = False) -> None:
        self._auto = auto_dir
        self._fail_create = fail_create

    def create_skill(self, name: str, content: str) -> dict[str, Any]:
        if self._fail_create:
            return {"success": False, "error": "generation failed"}
        skill_dir = self._auto / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return {"success": True}

    def write_file(self, name: str, file_path: str, file_content: str) -> dict[str, Any]:
        target = self._auto / name / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_content, encoding="utf-8")
        return {"success": True}

    def split_oversized_skill(
        self,
        main_content: str,
        target: int,
        supporting_files: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        return main_content, dict(supporting_files or {})

    def umbrella_skill_char_target(self) -> int:
        return 15000


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


@pytest.fixture
def apply_consolidation(monkeypatch):
    monkeypatch.setattr(orchestrator, "_schedule_system_prompt_refresh", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_generate_umbrella_skill",
        lambda umbrella, reasons, merged_content, file_inventory: (
            f"---\nname: {umbrella}\n---\nbody\n",
            {},
        ),
    )
    return orchestrator._apply_consolidation


class TestArchiveWithAbsorbedInto:
    def test_marker_is_written_inside_the_archive_entry(self, curator_tree):
        _make_skill(curator_tree, "docker")

        ok, msg = curator_usage.archive_skill("docker", absorbed_into="containers")

        assert ok, msg
        marker = curator_tree.archive / "docker" / curator_constants.ABSORBED_INTO_FILE
        assert marker.read_text(encoding="utf-8") == "containers\n"
        assert "absorbed into containers" in msg

    def test_no_marker_without_absorbed_into(self, curator_tree):
        _make_skill(curator_tree, "docker")

        ok, msg = curator_usage.archive_skill("docker")

        assert ok, msg
        marker = curator_tree.archive / "docker" / curator_constants.ABSORBED_INTO_FILE
        assert not marker.exists()


class TestConsolidationArchivesSources:
    def test_source_lands_in_archive_with_absorbed_into(
        self, curator_tree, apply_consolidation, monkeypatch
    ):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        _make_skill(curator_tree, "alpha")
        _seed_record("alpha", use_count=3)
        data_provider.set_skill_write_provider(_DiskWriter(curator_tree.auto))

        apply_consolidation(_LLM_CONSOLIDATE)

        assert (curator_tree.auto / "umbrella" / "SKILL.md").is_file()
        assert not (curator_tree.auto / "alpha").exists()
        assert (curator_tree.archive / "alpha" / "SKILL.md").is_file()
        marker = curator_tree.archive / "alpha" / curator_constants.ABSORBED_INTO_FILE
        assert marker.read_text(encoding="utf-8") == "umbrella\n"
        assert curator_usage.load_record("alpha")["state"] == curator_constants.STATE_ARCHIVED

    def test_restore_returns_the_source_and_keeps_provenance(
        self, curator_tree, apply_consolidation, monkeypatch
    ):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        _make_skill(curator_tree, "alpha")
        data_provider.set_skill_write_provider(_DiskWriter(curator_tree.auto))
        apply_consolidation(_LLM_CONSOLIDATE)

        ok, msg = agent_skill_usage.restore_skill("alpha")

        assert ok, msg
        assert (curator_tree.auto / "alpha" / "SKILL.md").is_file()
        restored = curator_tree.auto / "alpha" / curator_constants.ABSORBED_INTO_FILE
        assert restored.read_text(encoding="utf-8") == "umbrella\n"
        assert not (curator_tree.archive / "alpha").exists()

    def test_failed_umbrella_keeps_sources_in_auto(self, curator_tree, apply_consolidation):
        _make_skill(curator_tree, "alpha")
        data_provider.set_skill_write_provider(_DiskWriter(curator_tree.auto, fail_create=True))

        apply_consolidation(_LLM_CONSOLIDATE)

        assert (curator_tree.auto / "alpha" / "SKILL.md").is_file()
        assert not (curator_tree.auto / "umbrella").exists()
        assert not (curator_tree.archive / "alpha").exists()

    def test_missing_source_is_tolerated(self, curator_tree, apply_consolidation):
        data_provider.set_skill_write_provider(_DiskWriter(curator_tree.auto))

        apply_consolidation(_LLM_CONSOLIDATE)

        assert (curator_tree.auto / "umbrella" / "SKILL.md").is_file()
        assert not curator_tree.archive.exists()


class TestPruningArchivesSkills:
    def test_pruned_skill_is_archived_and_restorable(
        self, curator_tree, apply_consolidation, monkeypatch
    ):
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", curator_tree.auto)
        _make_skill(curator_tree, "beta")
        _seed_record("beta", use_count=1)
        data_provider.set_skill_write_provider(_DiskWriter(curator_tree.auto))

        apply_consolidation(_LLM_PRUNE)

        assert not (curator_tree.auto / "beta").exists()
        assert (curator_tree.archive / "beta" / "SKILL.md").is_file()
        marker = curator_tree.archive / "beta" / curator_constants.ABSORBED_INTO_FILE
        assert not marker.exists()
        assert curator_usage.load_record("beta")["state"] == curator_constants.STATE_ARCHIVED

        ok, msg = agent_skill_usage.restore_skill("beta")
        assert ok, msg
        assert (curator_tree.auto / "beta" / "SKILL.md").is_file()

    def test_entry_absorbed_in_consolidation_is_not_archived_twice(
        self, curator_tree, apply_consolidation
    ):
        _make_skill(curator_tree, "alpha")
        _make_skill(curator_tree, "gamma")
        data_provider.set_skill_write_provider(_DiskWriter(curator_tree.auto))

        apply_consolidation(_LLM_MIXED)

        alpha_entries = [p for p in curator_tree.archive.iterdir() if p.name.startswith("alpha")]
        assert [p.name for p in alpha_entries] == ["alpha"]
        marker = alpha_entries[0] / curator_constants.ABSORBED_INTO_FILE
        assert marker.read_text(encoding="utf-8") == "umbrella\n"
        assert (curator_tree.archive / "gamma" / "SKILL.md").is_file()


class TestStructuredSummaryContract:
    def test_parser_accepts_the_fixtures_used_above(self):
        parsed = _parse_structured_summary(_LLM_MIXED)

        assert parsed["consolidations"] == [
            {"from": "alpha", "into": "umbrella", "reason": "overlap"}
        ]
        assert parsed["prunings"] == [
            {"name": "alpha", "reason": "duplicate of consolidation"},
            {"name": "gamma", "reason": "stale"},
        ]
