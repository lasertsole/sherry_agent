# pyright: basic
# pyright: reportUnknownParameterType=false
# pyright: reportUnusedParameter=false
"""Simulate whether ``maybe_run_curator`` actually triggers curator maintenance.

Answers the user's question: "到了时间是否真会触发curator技能维护？" (Does the
curator skill maintenance actually trigger when the scheduled time arrives?)

We verify the FULL trigger path (``maybe_run_curator``), not just ``should_run_now``:
- ``maybe_run_curator`` must call ``should_run_now()`` (time + enabled + paused gate)
- then check the idle gate (``idle_for_seconds >= min_idle_hours * 3600``)
- then invoke ``run_curator_review(on_summary=...)`` in-module.

Scenarios:
  1. Time elapsed + idle ≥ min  → run_curator_review IS called
  2. Within interval           → run_curator_review NOT called
  3. Insufficient idle         → run_curator_review NOT called
  4. paused=True               → run_curator_review NOT called
  5. enabled=False (config)    → run_curator_review NOT called

NOTE: ``CURATOR_STATE_FILE`` is resolved once at import time, so the patch MUST
target ``context_engine.curator.state.CURATOR_STATE_FILE`` (the module-level name
``state.py`` references), NOT ``constants.CURATOR_STATE_FILE``. See
``test_curator_interval.py`` header.

``maybe_run_curator`` lives in ``orchestrator.py`` and calls
``run_curator_review(on_summary=...)`` directly in its own module namespace, so we
patch ``context_engine.curator.orchestrator.run_curator_review`` to observe whether
maintenance actually fires without touching real ``skills/auto/`` or ``logs/curator/``.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import context_engine.curator.state as curator_state
import context_engine.curator.transitions as curator_transitions
from context_engine.curator import maybe_run_curator, get_min_idle_hours


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def isolated_state(tmp_path):
    """Point CURATOR_STATE_FILE at a temp file so tests stay hermetic."""
    state_file = tmp_path / ".curator_state"
    with patch.object(curator_state, "CURATOR_STATE_FILE", state_file):
        yield state_file


@pytest.fixture
def with_elapsed_interval(isolated_state):
    """Seed state so the time gate has elapsed (last run 8 days ago; default 168h).

    ``maybe_run_curator`` uses the real clock for ``should_run_now()`` (it cannot
    inject ``now``), so we back-date ``last_run_at`` below the default 7-day
    interval. Mimics ``test_should_run_over_interval_runs``.
    """
    now = datetime.now(timezone.utc)
    st = curator_state.load_state()
    st["last_run_at"] = (now - timedelta(days=8)).isoformat()
    curator_state.save_state(st)


@pytest.fixture
def mock_review():
    """Patch the actual maintenance orchestrator so we assert trigger/no-trigger."""
    with patch(
        "context_engine.curator.orchestrator.run_curator_review"
    ) as mock:
        mock.return_value = {"started_at": "mock", "auto_transitions": {}}
        yield mock


# --- scenario 1: time up + idle enough → triggers ----------------------------

def test_trigger_time_elapsed_and_idle_enough(with_elapsed_interval, mock_review):
    """Interval elapses AND agent idle ≥ min_idle_hours → maintenance fires."""
    min_idle_s = get_min_idle_hours() * 3600.0
    result = maybe_run_curator(idle_for_seconds=min_idle_s * 2)

    assert mock_review.called, "maintenance must run once interval + idle gates pass"
    assert result is not None


# --- scenario 2: within interval → no trigger --------------------------------

def test_no_trigger_within_interval(isolated_state, mock_review):
    """Last run is recent (within interval) → maintenance must NOT fire."""
    now = datetime.now(timezone.utc)
    st = curator_state.load_state()
    st["last_run_at"] = now.isoformat()  # just ran now
    curator_state.save_state(st)

    min_idle_s = get_min_idle_hours() * 3600.0
    result = maybe_run_curator(idle_for_seconds=min_idle_s * 2)

    assert mock_review.call_count == 0, "within interval must not trigger"
    assert result is None


# --- scenario 3: time up but idle insufficient → no trigger ------------------

def test_no_trigger_insufficient_idle(with_elapsed_interval, mock_review):
    """Interval elapsed but agent idle < min_idle_hours → maintenance must NOT fire."""
    min_idle_s = get_min_idle_hours() * 3600.0
    result = maybe_run_curator(idle_for_seconds=min_idle_s / 2.0)

    assert mock_review.call_count == 0, "insufficient idle must not trigger"
    assert result is None


# --- scenario 4: paused → no trigger -----------------------------------------

def test_no_trigger_paused(with_elapsed_interval, mock_review):
    """Interval + idle pass but paused=True → maintenance must NOT fire."""
    st = curator_state.load_state()
    st["paused"] = True
    curator_state.save_state(st)

    min_idle_s = get_min_idle_hours() * 3600.0
    result = maybe_run_curator(idle_for_seconds=min_idle_s * 2)

    assert mock_review.call_count == 0, "paused curator must not trigger"
    assert result is None


# --- scenario 5: disabled (config) → no trigger ------------------------------

def test_no_trigger_disabled(enabled_false, with_elapsed_interval, mock_review):
    """Interval + idle pass but curator disabled in config → maintenance must NOT fire.

    ``should_run_now`` calls ``is_enabled()`` which it imports *by value* from
    ``config``, so patch the name transitions actually references.
    """
    min_idle_s = get_min_idle_hours() * 3600.0
    result = maybe_run_curator(idle_for_seconds=min_idle_s * 2)

    assert mock_review.call_count == 0, "disabled curator must not trigger"
    assert result is None


@pytest.fixture
def enabled_false():
    """Force ``is_enabled()`` to False for the disabled scenario."""
    with patch.object(curator_transitions, "is_enabled", return_value=False):
        yield
