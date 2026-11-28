"""Prompts, versions and settings history tests."""

from __future__ import annotations

import pytest

from superagent.app.prompts import PromptConfig, Prompts
from superagent.app.settings import SettingsStore
from superagent.app.settings_history import SettingsHistory
from superagent.core.errors import UserError


def test_versions_and_settings_revisions() -> None:
    settings = SettingsStore()
    assert settings.get("policy").revision == 0
    saved = settings.save("policy", {"accept_mode": "auto"}, 0)
    assert saved.revision == 1
    with pytest.raises(UserError):
        settings.save("policy", {}, 0)
    assert settings.history.for_block("policy")[0].revision == 1


def test_settings_history_is_bounded() -> None:
    history = SettingsHistory(limit=2)
    for i in range(4):
        history.append("executor", i, {}, "t")
    assert [entry.revision for entry in history.for_block("executor")] == [2, 3]


def test_prompts_assembly_and_default_diff() -> None:
    prompts = Prompts()
    current = prompts.current()
    assert current["revision"] == 0
    assert set(current["defaults"]) == {"admission", "delivery", "worker"}
    assert current["changed"] == []
    config = PromptConfig(
        team_sections=["Team rules"],
        builtin_parts={"admission": "custom"},
        skills=["worker"],
    )
    assert "Team rules" in config.assemble()
    assert "skill:worker" in config.assemble()
    revision = prompts.save(config.__dict__, 0)
    assert revision == 1
    assert prompts.current()["changed"] == ["admission"]
    with pytest.raises(UserError):
        prompts.save({}, 0, base_revision=99)
