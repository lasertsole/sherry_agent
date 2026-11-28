"""HealthBoard claim and failure-debounce tests."""

from __future__ import annotations

import pytest

from superagent.infra.health import ClaimError, HealthBoard


def test_claim_rejects_collisions_and_reserved_names() -> None:
    board = HealthBoard()
    board.claim("reply", "reply")
    with pytest.raises(ClaimError):
        board.claim("reply", "other")
    with pytest.raises(ClaimError):
        board.claim("runtime", "someone-else")
    board.claim("runtime", "runtime")
    board.claim("sync:github", "github")


def test_errors_appear_after_three_failures_and_clear_on_success() -> None:
    board = HealthBoard()
    board.claim("launch", "launch")
    for _ in range(2):
        board.report("launch", {"state": "error", "detail": "boom"})
        assert "launch" not in board.all()
    board.report("launch", {"state": "error", "detail": "boom"})
    entry = board.all()["launch"]
    assert entry["state"] == "error" and entry["failures"] == 3
    board.report("launch", {"state": "ready"})
    assert "launch" not in board.all()
    assert board.failures("launch") == 0


def test_worker_error_shows_on_first_failure() -> None:
    board = HealthBoard()
    board.claim("worker", "worker")
    board.report("worker", {"state": "error", "detail": "first"})
    assert board.all()["worker"]["failures"] == 1
