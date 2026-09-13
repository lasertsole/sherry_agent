"""Unit tests for the E3 stagnation tracker (pure, deterministic).

The tracker keeps per-session module state and a monotonic clock seam (``_now``)
so every test drives time explicitly — no real sleeps, no flakiness.

Coverage: stagnation increments on an unchanged snapshot, changed-snapshot
within-window count retention, out-of-window reset, exponential backoff growth
and cap, recovery attempt cap, abort-marker detection, and full reset.
"""

from __future__ import annotations

import pytest

from agent.tools.todolist import stagnation_tracker as st

pytestmark = [pytest.mark.unit]

_SID = "sess-stag"


def _todo(status: str, content: str = "task one") -> dict:
    return {
        "content": content,
        "status": status,
        "priority": "high",
        "flow_id": None,
        "step_id": None,
    }


@pytest.fixture(autouse=True)
def _clean() -> None:
    st.reset(_SID)
    yield
    st.reset(_SID)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Deterministic monotonic clock injected into the tracker."""
    now = {"t": 1000.0}
    monkeypatch.setattr(st, "_now", lambda: now["t"])
    return now


# ============================================================================
# Stagnation detection
# ============================================================================


def test_stagnation_increments_on_unchanged_snapshot(clock: dict[str, float]) -> None:
    todos = _todo("pending")

    # Given: baseline snapshot recorded (count stays 0, no prior snapshot).
    assert st.check_stagnation(_SID, [todos]) is False
    # When: the same snapshot arrives repeatedly.
    assert st.check_stagnation(_SID, [todos]) is False  # count 1
    assert st.check_stagnation(_SID, [todos]) is False  # count 2
    # Then: the threshold fires on the third unchanged repeat.
    assert st.check_stagnation(_SID, [todos]) is True  # count 3
    assert st._stagnation_count[_SID] == st._MAX_STAGNATION


def test_changed_snapshot_keeps_count_within_window(clock: dict[str, float]) -> None:
    st.check_stagnation(_SID, [_todo("pending")])  # baseline
    st.check_stagnation(_SID, [_todo("pending")])  # count 1
    clock["t"] += 10.0  # well inside _FAILURE_RESET_WINDOW_S

    assert st.check_stagnation(_SID, [_todo("in_progress")]) is False
    # Then: the stalled streak is not reset while inside the window.
    assert st._stagnation_count[_SID] == 1


def test_changed_snapshot_resets_count_after_window(clock: dict[str, float]) -> None:
    st.check_stagnation(_SID, [_todo("pending")])  # baseline
    st.check_stagnation(_SID, [_todo("pending")])  # count 1, records failure time
    clock["t"] += st._FAILURE_RESET_WINDOW_S + 1

    assert st.check_stagnation(_SID, [_todo("completed")]) is False
    # Then: a change after the reset window clears the stalled streak.
    assert st._stagnation_count[_SID] == 0


# ============================================================================
# Cooldown (exponential backoff with cap)
# ============================================================================


def test_cooldown_grows_and_caps(clock: dict[str, float]) -> None:
    # Given: zero failures -> base cooldown of 2s.
    st.mark_injected(_SID)
    assert st.is_in_cooldown(_SID) is True
    clock["t"] += st._BASE_COOLDOWN_S
    assert st.is_in_cooldown(_SID) is False

    # When: failures reach the exponent cap -> 2 * 2^5 = 64s, clamped to 60s.
    st._stagnation_count[_SID] = 5
    st.mark_injected(_SID)
    clock["t"] += st._MAX_COOLDOWN_S - 1.0
    assert st.is_in_cooldown(_SID) is True
    clock["t"] += 2.0
    assert st.is_in_cooldown(_SID) is False


def test_cooldown_false_before_any_injection(clock: dict[str, float]) -> None:
    assert st.is_in_cooldown(_SID) is False


# ============================================================================
# Recovery mode
# ============================================================================


def test_recovery_attempts_capped(clock: dict[str, float]) -> None:
    st._stagnation_count[_SID] = st._MAX_STAGNATION

    assert st.should_enter_recovery(_SID) is True
    st.enter_recovery(_SID)
    assert st._stagnation_count[_SID] == 0
    assert st._recovery_attempts[_SID] == 1

    st._stagnation_count[_SID] = st._MAX_STAGNATION
    assert st.should_enter_recovery(_SID) is True
    st.enter_recovery(_SID)
    assert st._recovery_attempts[_SID] == st._MAX_RECOVERY_ATTEMPTS

    st._stagnation_count[_SID] = st._MAX_STAGNATION
    assert st.should_enter_recovery(_SID) is False


def test_recovery_not_offered_below_stagnation_threshold(clock: dict[str, float]) -> None:
    st._stagnation_count[_SID] = st._MAX_STAGNATION - 1
    assert st.should_enter_recovery(_SID) is False


# ============================================================================
# Abort detection
# ============================================================================


@pytest.mark.parametrize(
    "message",
    [
        "abort",
        "Operation cancelled",
        "interrupted by user",
        "operation cancelled",
        "request timeout",
    ],
)
def test_is_abort_error_detects_markers(message: str) -> None:
    assert st.is_abort_error(RuntimeError(message)) is True


def test_is_abort_error_false_for_regular_failure() -> None:
    assert st.is_abort_error(RuntimeError("model produced invalid JSON")) is False


# ============================================================================
# Reset
# ============================================================================


def test_reset_clears_all_session_state(clock: dict[str, float]) -> None:
    st.check_stagnation(_SID, [_todo("pending")])
    st.check_stagnation(_SID, [_todo("pending")])
    st.mark_injected(_SID)
    st.enter_recovery(_SID)

    st.reset(_SID)

    assert _SID not in st._stagnation_count
    assert _SID not in st._last_snapshot
    assert _SID not in st._last_inject_time
    assert _SID not in st._last_failure_time
    assert _SID not in st._recovery_attempts
