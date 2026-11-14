"""Persisted per-model structured-output degradation labels."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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


def _write_entry(
    model: str = "deepseek-flash",
    *,
    level: int = 1,
    age_hours: float = 0.0,
    reprobe_count: int = 0,
    escalated_at: str | None = "auto",
) -> None:
    """Persist one entry with a controllable ``escalated_at`` age."""
    if escalated_at == "auto":
        stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat(timespec="seconds")
    else:
        stamp = escalated_at
    path = model_caps._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                f"deepseek/{model}": {
                    "level": level,
                    "reason": "400 tool_choice",
                    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "escalated_at": stamp,
                    "reprobe_count": reprobe_count,
                }
            }
        ),
        encoding="utf-8",
    )
    model_caps.clear_cache()


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
    assert entry["escalated_at"]
    assert entry["reprobe_count"] == 0


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
                "zhipu/glm-4.6": {
                    "level": 2,
                    "escalated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
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
# Re-probe decay (2026-11-12 design-defect plan, defect 1)
# ---------------------------------------------------------------------------


def test_get_level_decays_after_the_first_reprobe_interval():
    _write_entry(level=1, age_hours=25)

    assert model_caps.get_level("deepseek", "deepseek-flash") == 0


def test_get_level_keeps_the_level_inside_the_interval():
    _write_entry(level=1, age_hours=1)

    assert model_caps.get_level("deepseek", "deepseek-flash") == 1


def test_an_old_persist_without_escalated_at_decays_immediately():
    _write_entry(level=2, escalated_at="")

    assert model_caps.get_level("deepseek", "deepseek-flash") == 1


def test_the_reprobe_backoff_grows_after_each_attempt():
    _write_entry(level=2, age_hours=25, reprobe_count=1)
    assert model_caps.get_level("deepseek", "deepseek-flash") == 2

    _write_entry(level=2, age_hours=49, reprobe_count=1)
    assert model_caps.get_level("deepseek", "deepseek-flash") == 1

    _write_entry(level=2, age_hours=100, reprobe_count=2)
    assert model_caps.get_level("deepseek", "deepseek-flash") == 2

    _write_entry(level=2, age_hours=169, reprobe_count=2)
    assert model_caps.get_level("deepseek", "deepseek-flash") == 1

    _write_entry(level=2, age_hours=169, reprobe_count=3)
    assert model_caps.get_level("deepseek", "deepseek-flash") == 1


def test_confirm_level_commits_a_decayed_level():
    _write_entry(level=2, age_hours=25)

    model_caps.confirm_level("deepseek", "deepseek-flash", 1)

    model_caps.clear_cache()
    assert model_caps.get_level("deepseek", "deepseek-flash") == 1
    entry = _read()["deepseek/deepseek-flash"]
    assert entry["level"] == 1
    assert entry["reprobe_count"] == 1


def test_confirm_level_at_zero_clears_the_decay_state():
    _write_entry(level=1, age_hours=25)

    model_caps.confirm_level("deepseek", "deepseek-flash", 0)

    model_caps.clear_cache()
    assert model_caps.get_level("deepseek", "deepseek-flash") == 0
    entry = _read()["deepseek/deepseek-flash"]
    assert entry["escalated_at"] == ""
    assert entry["reprobe_count"] == 0


def test_confirm_level_is_a_noop_when_the_spawn_used_the_persisted_level():
    _write_entry(level=2, age_hours=1, reprobe_count=0)

    model_caps.confirm_level("deepseek", "deepseek-flash", 2)

    before = _read()["deepseek/deepseek-flash"]
    assert before["level"] == 2
    assert before["reprobe_count"] == 0
    assert before["escalated_at"]


def test_reescalating_after_a_decay_refreshes_the_clock_and_backs_off():
    _write_entry(level=1, age_hours=25, reprobe_count=0)
    assert model_caps.get_level("deepseek", "deepseek-flash") == 0

    assert model_caps.escalate("deepseek", "deepseek-flash", 1, reason="still rejected") == 1

    entry = _read()["deepseek/deepseek-flash"]
    assert entry["level"] == 1
    assert entry["reprobe_count"] == 1
    # The refreshed clock keeps get_level at the degraded rung for another 48 h.
    model_caps.clear_cache()
    assert model_caps.get_level("deepseek", "deepseek-flash") == 1


def test_list_entries_reports_the_effective_level():
    _write_entry(level=2, age_hours=25, reprobe_count=0)

    entries = model_caps.list_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["provider"] == "deepseek"
    assert entry["model"] == "deepseek-flash"
    assert entry["level"] == 2
    assert entry["effective_level"] == 1
    assert entry["reason"] == "400 tool_choice"


def test_clear_entry_and_clear_all_forget_the_persisted_rows():
    model_caps.escalate("deepseek", "deepseek-flash", 1, reason="one")
    model_caps.escalate("zhipu", "glm-4.6", 2, reason="two")

    assert model_caps.clear_entry("deepseek", "deepseek-flash") is True
    assert model_caps.clear_entry("deepseek", "deepseek-flash") is False
    assert model_caps.get_level("deepseek", "deepseek-flash") == 0
    assert model_caps.get_level("zhipu", "glm-4.6") == 2

    assert model_caps.clear_all() == 1
    assert model_caps.list_entries() == []
    assert model_caps.clear_all() == 0


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
