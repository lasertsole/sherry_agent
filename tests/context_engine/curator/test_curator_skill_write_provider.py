"""Curator degradation/forwarding tests for the runtime SkillWriteProvider seam.

The curator's consolidation write path (create umbrella / write supporting
files / migrate source files) must resolve
``runtime.data_provider.get_skill_write_provider()`` and must abort safely when
nothing is registered: no partial merge, and crucially no deletion of source
skills while their umbrella cannot be written. A registered writer's result
dicts flow through unchanged.
"""

from typing import Any

import pytest

from context_engine.curator import orchestrator
from runtime import data_provider

pytestmark = [pytest.mark.unit]

_LLM_FINAL = "```yaml\nconsolidations:\n  - from: a\n    into: u\n    reason: r\n```"


class _RecordingWriter:
    """SkillWriteProvider stub recording every forwarded call."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.written: list[tuple[str, str, str]] = []

    def create_skill(self, name: str, content: str) -> dict[str, Any]:
        self.created.append((name, content))
        return {"success": True}

    def write_file(self, name: str, file_path: str, file_content: str) -> dict[str, Any]:
        self.written.append((name, file_path, file_content))
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


@pytest.fixture(autouse=True)
def _clean_skill_writer():
    data_provider.clear_skill_write_provider()
    orchestrator._provider_misses_logged.clear()
    yield
    data_provider.clear_skill_write_provider()
    orchestrator._provider_misses_logged.clear()


def _record_phases(monkeypatch) -> list[str]:
    called: list[str] = []
    for name in (
        "_merge_umbrella_skills",
        "_delete_consolidated_sources",
        "_delete_pruned_skills",
        "_schedule_system_prompt_refresh",
    ):
        monkeypatch.setattr(
            orchestrator,
            name,
            lambda *a, _n=name, **k: called.append(_n),  # noqa: ARG005
        )
    return called


class TestUnregisteredWriterDegradation:
    def test_write_supporting_files_is_noop(self):
        orchestrator._write_supporting_files("umbrella", {"references/a.md": "A"})

        assert data_provider.get_skill_write_provider() is None

    def test_migrate_source_files_is_noop(self):
        orchestrator._migrate_source_files("umbrella", [{"from": "a"}], set())

        assert data_provider.get_skill_write_provider() is None

    def test_merge_umbrella_skills_aborts_before_llm(self, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("LLM generation must not run without a writer")

        monkeypatch.setattr(orchestrator, "_generate_umbrella_skill", _explode)

        orchestrator._merge_umbrella_skills([{"from": "a", "into": "u", "reason": "r"}])

    def test_apply_consolidation_skips_every_phase(self, monkeypatch):
        called = _record_phases(monkeypatch)

        orchestrator._apply_consolidation(_LLM_FINAL)

        assert called == []


class TestRegisteredWriterForwarding:
    def test_write_supporting_files_forwards_each_entry(self):
        writer = _RecordingWriter()
        data_provider.set_skill_write_provider(writer)

        orchestrator._write_supporting_files("umbrella", {"references/a.md": "A"})

        assert writer.written == [("umbrella", "references/a.md", "A")]

    def test_apply_consolidation_runs_phases_in_order(self, monkeypatch):
        called = _record_phases(monkeypatch)
        data_provider.set_skill_write_provider(_RecordingWriter())

        orchestrator._apply_consolidation(_LLM_FINAL)

        assert called == [
            "_merge_umbrella_skills",
            "_delete_consolidated_sources",
            "_delete_pruned_skills",
            "_schedule_system_prompt_refresh",
        ]

    def test_generate_umbrella_skill_splits_through_writer(self, monkeypatch):
        import models

        writer = _RecordingWriter()
        split_calls: list[tuple[str, int]] = []

        def fake_split(
            main_content: str,
            target: int,
            supporting_files: dict[str, str] | None = None,
        ) -> tuple[str, dict[str, str]]:
            split_calls.append((main_content, target))
            return "SLIM", {"references/part01.md": "P"}

        monkeypatch.setattr(writer, "split_oversized_skill", fake_split)
        data_provider.set_skill_write_provider(writer)

        class _Response:
            content = "<<<SKILL.md>>>\n---\nname: u\n---\nbody"

        class _LLM:
            def invoke(self, messages):
                return _Response()

        monkeypatch.setattr(models, "build_main_llm", lambda **kwargs: _LLM())

        main_content, supporting_files = orchestrator._generate_umbrella_skill(
            "u", ["- a: r"], "source"
        )

        assert main_content == "SLIM"
        assert supporting_files == {"references/part01.md": "P"}
        assert split_calls == [("---\nname: u\n---\nbody", 15000)]
