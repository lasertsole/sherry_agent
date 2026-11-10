"""Persisted per-model structured-output degradation labels."""

from __future__ import annotations

import json

import pytest

from agent.tools.subagent import model_caps

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    """Point ``SRC_DIR`` at a temp dir and drop the process cache."""
    import config.path as path_mod

    monkeypatch.setattr(path_mod, "SRC_DIR", tmp_path)
    model_caps.clear_cache()
    yield
    model_caps.clear_cache()


def _read() -> dict:
    return json.loads((model_caps._path()).read_text(encoding="utf-8"))


def test_an_unknown_model_starts_at_level_zero():
    assert model_caps.get_level("deepseek", "deepseek-flash") == model_caps.DEFAULT_LEVEL


def test_escalate_persists_the_level_for_later_spawns():
    assert model_caps.escalate("deepseek", "deepseek-flash", 1, reason="400 tool_choice") == 1

    # A fresh process (cache dropped) still sees the learned level.
    model_caps.clear_cache()
    assert model_caps.get_level("deepseek", "deepseek-flash") == 1
    assert model_caps.get_level("deepseek", "deepseek-chat") == 0
    entry = _read()["deepseek/deepseek-flash"]
    assert entry["level"] == 1
    assert "tool_choice" in entry["reason"]
    assert entry["updated_at"]


def test_the_level_is_monotonic_per_model():
    model_caps.escalate("deepseek", "deepseek-flash", 2, reason="second")
    # A later lower rung (a concurrent run's stale attempt) cannot undo it.
    assert model_caps.escalate("deepseek", "deepseek-flash", 1) == 2
    assert model_caps.get_level("deepseek", "deepseek-flash") == 2


def test_a_corrupt_file_reads_as_level_zero(monkeypatch):
    path = model_caps._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    model_caps.clear_cache()

    assert model_caps.get_level("deepseek", "deepseek-flash") == 0


def test_a_bad_entry_is_skipped_not_fatal():
    path = model_caps._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "deepseek/deepseek-flash": {"level": "two"},
                "zhipu/glm-4.6": {"level": 2},
            }
        ),
        encoding="utf-8",
    )
    model_caps.clear_cache()

    assert model_caps.get_level("deepseek", "deepseek-flash") == 0
    assert model_caps.get_level("zhipu", "glm-4.6") == 2


def test_level_labels_are_stable_strings():
    assert model_caps.level_label(0) == "langchain-structured"
    assert model_caps.level_label(1) == "self-json-object"
    assert model_caps.level_label(2) == "self-json-object+thinking-off"


# ---------------------------------------------------------------------------
# H2 (2026-11-09 audit): the rung's behaviour is one strategy object
# ---------------------------------------------------------------------------


def test_strategies_are_the_single_source_of_rung_behaviour():
    level0 = model_caps.strategy_for(0)
    level1 = model_caps.strategy_for(1)
    level2 = model_caps.strategy_for(2)

    assert level0.sends_json_mode is False
    assert level0.thinking_off is False
    assert level0.local_validation_fatal is False

    assert level1.sends_json_mode is True
    assert level1.thinking_off is False
    assert level1.local_validation_fatal is True

    assert level2.sends_json_mode is True
    assert level2.thinking_off is True
    assert level2.local_validation_fatal is True


def test_strategy_levels_are_clamped_and_labels_stay_consistent():
    assert model_caps.strategy_for(-5).level == 0
    assert model_caps.strategy_for(99).level == model_caps.MAX_LEVEL
    for level in range(model_caps.MAX_LEVEL + 1):
        assert model_caps.strategy_for(level).label == model_caps.level_label(level)
