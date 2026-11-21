"""The expert registry: persisted JSON definitions both new modes reference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.expert_registry import (
    ExpertConfig,
    ExpertRegistryError,
    list_experts,
    load_expert,
    save_expert,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture
def registry_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "experts"
    monkeypatch.setattr("agent.tools.expert_registry.EXPERTS_DIR", target)
    return target


def test_save_and_load_round_trip(registry_dir: Path):
    config = ExpertConfig(
        name="frontend-expert",
        description="Frontend specialist",
        persona_prompt="You are a frontend expert.",
        model="claude",
        thinking="medium",
        skills=["code_wiki"],
        goal_max_turns=1,
    )

    path = save_expert(config)

    assert path == registry_dir / "frontend-expert.json"
    loaded = load_expert("frontend-expert")
    assert loaded.persona_prompt == "You are a frontend expert."
    assert loaded.model == "claude"
    assert loaded.skills == ["code_wiki"]
    assert loaded.goal_max_turns == 1


def test_load_missing_expert_raises(registry_dir: Path):
    with pytest.raises(ExpertRegistryError, match="not registered"):
        load_expert("nobody")


def test_malformed_definition_raises(registry_dir: Path):
    (
        registry_dir.mkdir(exist_ok=True),
        (registry_dir / "bad.json").write_text("{oops", encoding="utf-8"),
    )
    with pytest.raises(ExpertRegistryError, match="malformed"):
        load_expert("bad")


def test_invalid_names_are_refused():
    for name in ("", "../escape", "a/b", "with space", "x" * 65):
        with pytest.raises(ExpertRegistryError):
            save_expert(ExpertConfig(name=name, persona_prompt="p"))


def test_empty_persona_refused(registry_dir: Path):
    with pytest.raises(ExpertRegistryError, match="persona_prompt"):
        save_expert(ExpertConfig(name="no-persona", persona_prompt="  "))


def test_list_skips_malformed_files(registry_dir: Path):
    save_expert(ExpertConfig(name="good", persona_prompt="ok"))
    registry_dir.joinpath("broken.json").write_text("not json", encoding="utf-8")

    names = [expert.name for expert in list_experts()]

    assert names == ["good"]


def test_unknown_keys_are_ignored_on_load(registry_dir: Path):
    registry_dir.mkdir(exist_ok=True)
    (registry_dir / "future.json").write_text(
        json.dumps({"name": "ignored-here", "persona_prompt": "p", "brand_new_field": 1}),
        encoding="utf-8",
    )

    loaded = load_expert("future")

    assert loaded.persona_prompt == "p"
