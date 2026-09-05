"""Unit tests for runtime.crash_loop_breaker (CrashLoopBreaker boot lifecycle guard).

Every test redirects the module-level STATE_PATH into tmp_path via
monkeypatch.setattr with a dotted string target (self-restoring: tests/unit
runs in ONE pytest process, patches must never leak).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from loguru import logger

from runtime.crash_loop_breaker import (
    RETENTION_S,
    WINDOW_S,
    clear,
    is_tripped,
    mark_clean_exit,
    record_boot,
    was_last_exit_clean,
)


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch the breaker's STATE_PATH to a tmp_path file; monkeypatch restores it."""
    p = tmp_path / "boot_lifecycle.json"
    monkeypatch.setattr("runtime.crash_loop_breaker.STATE_PATH", p)
    return p


def test_no_file_is_empty_state_and_not_tripped(state_path: Path):
    # First-boot semantics: missing state file → zero records, never tripped.
    assert is_tripped() is False
    assert not state_path.exists()


def test_two_unclean_below_threshold_not_tripped(state_path: Path):
    record_boot(clean=False, reason="crash-1")
    record_boot(clean=False, reason="crash-2")
    assert is_tripped() is False


def test_three_unclean_in_window_trip(state_path: Path):
    assert record_boot(clean=False, reason="crash-1") is False
    assert record_boot(clean=False, reason="crash-2") is False
    assert record_boot(clean=False, reason="crash-3") is True
    assert is_tripped() is True


def test_clean_records_do_not_count(state_path: Path):
    for i in range(4):
        record_boot(clean=True, reason=f"ok-{i}")
    assert is_tripped() is False


def test_out_of_window_records_pruned_and_not_counted(state_path: Path):
    now = time.time()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "boots": [
                    # Unclean but outside the WINDOW_S → not counted.
                    {"ts": now - WINDOW_S - 1, "clean": False, "reason": "old-in-retention"},
                    # Older than RETENTION_S → must be pruned on next record_boot.
                    {"ts": now - RETENTION_S - 10, "clean": False, "reason": "ancient"},
                ],
                "last_exit_clean": False,
            }
        ),
        encoding="utf-8",
    )
    # Only the fresh unclean boot counts (1 < 3) → not tripped.
    assert record_boot(clean=False, reason="fresh") is False

    data = json.loads(state_path.read_text(encoding="utf-8"))
    reasons = [b["reason"] for b in data["boots"]]
    assert "ancient" not in reasons  # pruned: older than RETENTION_S
    assert "old-in-retention" in reasons  # kept: within RETENTION_S
    assert reasons[-1] == "fresh"


def test_clear_deletes_state_file_and_resets(state_path: Path):
    record_boot(clean=False, reason="crash-1")
    record_boot(clean=False, reason="crash-2")
    record_boot(clean=False, reason="crash-3")
    assert is_tripped() is True

    clear()

    assert not state_path.exists()  # clear() DELETES the state file
    assert is_tripped() is False


def test_corrupted_json_empty_state_semantics(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not-json{{", encoding="utf-8")

    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        # No exception: corrupted state is treated as empty (first boot).
        assert is_tripped() is False
        assert was_last_exit_clean() is False
        assert record_boot(clean=False, reason="after-corrupt") is False
    finally:
        logger.remove(handler_id)

    # Warning was logged about the unreadable state file.
    assert any("corrupt" in m.lower() for m in messages)

    # Functionality continues on the fresh state: 3 more unclean boots trip.
    record_boot(clean=False, reason="n2")
    assert record_boot(clean=False, reason="n3") is True


def test_clean_exit_marker_roundtrip(state_path: Path):
    # Default: unclean unless proven clean.
    assert was_last_exit_clean() is False

    mark_clean_exit()
    assert was_last_exit_clean() is True

    # One-shot: record_boot consumes the marker so a later hard crash after a
    # clean exit is not mistaken for another clean boot.
    record_boot(clean=True, reason="reboot")
    assert was_last_exit_clean() is False


def test_record_boot_persists_record_shape(state_path: Path):
    record_boot(clean=False, reason="startup")
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(data["boots"], list) and len(data["boots"]) == 1
    rec = data["boots"][0]
    assert set(rec) == {"ts", "clean", "reason"}
    assert rec["clean"] is False
    assert rec["reason"] == "startup"
