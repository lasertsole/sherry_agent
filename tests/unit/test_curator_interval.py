# pyright: basic
# NOTE: This file is checked by `basedpyright`, which does NOT honor the
# mypy-style `# type: ignore[arg-type]` comments used below to deliberately call
# `set_interval_override_days` with invalid types (the invalid-type-guard tests
# are intentional). Suppress those misuse reports at file level. The remaining
# `reportUnknownParameterType` / `reportUnusedParameter` warnings are benign for
# a pytest module (the shared `isolated_state` fixture is intentionally unused
# by some tests).
# pyright: reportArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportAny=false
# pyright: reportUnusedParameter=false
"""Unit tests for curator auto-maintenance interval override.

Covers:
- interval override get/set clamping (1..5 days) persisted in .curator_state
- effective interval hours (override takes precedence over curator.yaml)
- should_run_now trigger / no-trigger via simulated time
- manual run (run_curator_review) updates last_maintenance_at
- last maintenance timestamp persistence + round-trip

NOTE: ``CURATOR_STATE_FILE`` is resolved once at import time from the real
``SKILLS_DIR`` (see context_engine/curator/constants.py), so the shared
``unit_test_config`` fixture alone is NOT enough to redirect state reads.
``state.py`` imports ``CURATOR_STATE_FILE`` **by value** (``from ... import``),
binding it as a module-level global in ``state.py``'s own namespace, so the
patch MUST target ``context_engine.curator.state.CURATOR_STATE_FILE`` (the name
`state.py` actually references), NOT ``constants.CURATOR_STATE_FILE``. Every
curator config/transition/orchestrator function reads/writes state through
``load_state()``/``save_state()`` (which call ``read_json``/``write_json`` on
``state.CURATOR_STATE_FILE``), so this single patch fully isolates state I/O.
"""


import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import context_engine.curator.config as curator_config
import context_engine.curator.state as curator_state
from context_engine.curator import (
    get_effective_interval_hours,
    get_interval_override_days,
    get_last_maintenance_at,
    set_interval_override_days,
    set_last_maintenance_at,
    should_run_now,
    run_curator_review,
)


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def isolated_state(tmp_path):
    """Point CURATOR_STATE_FILE at a temp file so tests stay hermetic.

    Patches ``context_engine.curator.state.CURATOR_STATE_FILE`` (the module-level
    global `state.py` actually references). ``load_state``/``save_state`` route
    all I/O through this single name, isolating every read/write.
    """
    state_file = tmp_path / ".curator_state"
    with patch.object(curator_state, "CURATOR_STATE_FILE", state_file):
        yield state_file


# --- interval override get/set clamping -------------------------------------

def test_interval_override_default_none(isolated_state):
    """No override configured -> get returns None, effective falls back to yaml."""
    assert get_interval_override_days() is None
    assert get_effective_interval_hours() == curator_config.get_interval_hours()


def test_set_interval_override_min_clamp(isolated_state):
    """1 keep as-is (lower bound)."""
    assert set_interval_override_days(1) == 1
    assert get_interval_override_days() == 1
    assert json.loads(curator_state.CURATOR_STATE_FILE.read_text(encoding="utf-8")).get("auto_interval_days") == 1
    assert get_effective_interval_hours() == 24


def test_set_interval_override_max_clamp(isolated_state):
    """5 keep as-is (upper bound)."""
    assert set_interval_override_days(5) == 5
    assert get_interval_override_days() == 5
    assert get_effective_interval_hours() == 5 * 24


def test_set_interval_override_zero_rejected(isolated_state):
    """0 below min -> rejected/reset to None (use yaml default)."""
    assert set_interval_override_days(0) is None
    assert get_interval_override_days() is None
    assert get_effective_interval_hours() == curator_config.get_interval_hours()


def test_set_interval_override_negative_rejected(isolated_state):
    """Negative below min -> rejected/reset to None."""
    assert set_interval_override_days(-3) is None
    assert get_interval_override_days() is None


def test_set_interval_override_above_max_rejected(isolated_state):
    """6 above max -> rejected/reset to None."""
    assert set_interval_override_days(6) is None
    assert get_interval_override_days() is None


def test_set_interval_override_none_clears(isolated_state):
    """Passing None clears a prior override."""
    set_interval_override_days(5)
    assert get_interval_override_days() == 5
    assert set_interval_override_days(None) is None
    assert get_interval_override_days() is None
    assert get_effective_interval_hours() == curator_config.get_interval_hours()


def test_set_interval_override_persists_round_trip(isolated_state):
    """Setting persists to disk; a fresh load reads it back unchanged."""
    set_interval_override_days(3)
    raw = json.loads(curator_state.CURATOR_STATE_FILE.read_text(encoding="utf-8"))
    assert raw.get("auto_interval_days") == 3
    # New process-like read (same file) returns the same value.
    assert get_interval_override_days() == 3
    assert get_effective_interval_hours() == 72


def test_invalid_types_reset_to_none(isolated_state):
    """Non-integer / non-empty garbage yields None (not a crash)."""
    assert set_interval_override_days("abc") is None  # type: ignore[arg-type]
    assert set_interval_override_days("") is None  # type: ignore[arg-type]
    assert set_interval_override_days("2x") is None  # type: ignore[arg-type]
    assert get_interval_override_days() is None


# --- effective interval hours ------------------------------------------------

def test_effective_hours_uses_override(isolated_state):
    """A valid override (2d) overrides the default 5d/120h."""
    set_interval_override_days(2)
    assert get_effective_interval_hours() == 48


def test_effective_hours_cleared_uses_yaml(isolated_state):
    """After clearing, effective hours fall back to curator.yaml interval."""
    set_interval_override_days(4)  # 96h
    set_interval_override_days(None)
    assert get_effective_interval_hours() == curator_config.get_interval_hours()


# --- should_run_now trigger / no-trigger ------------------------------------

def test_should_run_no_state_file_runs(isolated_state):
    """No state file yet -> eligible (last_run_at missing)."""
    assert should_run_now() is True


def test_should_run_missing_last_run_runs(isolated_state):
    """State exists but last_run_at missing -> eligible."""
    curator_state.save_state({"last_run_at": None})
    assert should_run_now() is True


def test_should_run_within_interval_no_run(isolated_state):
    """Now is less than the effective interval since last run -> no trigger."""
    now = datetime.now(timezone.utc)
    curator_state.save_state({"last_run_at": (now - timedelta(hours=24)).isoformat()})  # default 120h
    assert should_run_now(now=now) is False


def test_should_run_over_interval_runs(isolated_state):
    """Now exceeds the effective (default) interval -> trigger."""
    now = datetime.now(timezone.utc)
    curator_state.save_state({"last_run_at": (now - timedelta(days=8)).isoformat()})  # > 5 days
    assert should_run_now(now=now) is True


def test_should_run_override_shorter_window_no_run(isolated_state):
    """With a 1-day override, 2 days since last run -> already eligible."""
    set_interval_override_days(1)
    now = datetime.now(timezone.utc)
    st = curator_state.load_state()
    st["last_run_at"] = (now - timedelta(days=2)).isoformat()
    curator_state.save_state(st)
    assert should_run_now(now=now) is True


def test_should_run_override_shorter_window_not_yet(isolated_state):
    """With a 2-day override, 1 day since last run -> not yet eligible."""
    set_interval_override_days(2)
    now = datetime.now(timezone.utc)
    curator_state.save_state({"last_run_at": (now - timedelta(days=1)).isoformat()})
    assert should_run_now(now=now) is False


def test_should_run_override_longer_window(isolated_state):
    """With a 3-day override, 2 days since last run -> not yet eligible."""
    set_interval_override_days(3)
    now = datetime.now(timezone.utc)
    curator_state.save_state({"last_run_at": (now - timedelta(days=2)).isoformat()})
    assert should_run_now(now=now) is False


# --- manual run updates last_maintenance_at ---------------------------------

def test_run_review_updates_last_maintenance_at(isolated_state):
    """Non-dry-run run_curator_review writes last_maintenance_at."""
    result = run_curator_review(dry_run=False, consolidate=False)
    state = curator_state.load_state()
    assert state.get("last_maintenance_at") == result["started_at"]
    assert state.get("last_run_at") == result["started_at"]
    assert state.get("run_count") == 1


def test_run_review_dry_run_does_not_update_maintenance(isolated_state):
    """Dry-run leaves last_maintenance_at untouched."""
    run_curator_review(dry_run=True, consolidate=False)
    state = curator_state.load_state()
    assert state.get("last_maintenance_at") is None
    assert state.get("last_run_at") is None
    assert state.get("run_count", 0) == 0


def test_run_review_increments_run_count(isolated_state):
    """Manual runs increment run_count each time."""
    run_curator_review(dry_run=False, consolidate=False)
    run_curator_review(dry_run=False, consolidate=False)
    state = curator_state.load_state()
    assert state.get("run_count") == 2


# --- last maintenance timestamp persistence + round-trip --------------------

def test_last_maintenance_set_and_read(isolated_state):
    """set_last_maintenance_at persists to state file."""
    stamp = datetime.now(timezone.utc).isoformat()
    set_last_maintenance_at(stamp)
    assert get_last_maintenance_at() == stamp
    raw = json.loads(curator_state.CURATOR_STATE_FILE.read_text(encoding="utf-8"))
    assert raw.get("last_maintenance_at") == stamp


def test_last_maintenance_none(isolated_state):
    """Null maintenance timestamp is readable (default)."""
    assert get_last_maintenance_at() is None


def test_last_maintenance_clear(isolated_state):
    """Clearing via None removes the persisted stamp."""
    set_last_maintenance_at("2026-08-19T00:00:00+00:00")
    assert get_last_maintenance_at() is not None
    set_last_maintenance_at(None)
    assert get_last_maintenance_at() is None


def test_last_maintenance_round_trip_across_state_file(isolated_state):
    """A manual run's timestamp survives a reload from disk."""
    run_curator_review(dry_run=False, consolidate=False)
    loaded = curator_state.load_state()
    assert isinstance(loaded.get("last_maintenance_at"), str)
    # Re-instantiate state (fresh read from the same patched file).
    assert get_last_maintenance_at() == loaded["last_maintenance_at"]
